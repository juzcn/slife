"""Function-calling agent loop with real-time streaming and thinking support."""

import asyncio
import json
import logging
import os
import re
import time as _time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from slife.agent.llm_client import LLMClient, TokenUsage
from slife.logfmt import sanitize_secrets
from slife.agent.conversation import Conversation
from slife.agent.system_prompt import build_context_status, _current_shell
from slife.tools.registry import ToolRegistry
from slife.logfmt import request_scope, elapsed

logger = logging.getLogger(__name__)


class AgentCancelled(Exception):
    """Raised when the agent loop is cancelled by user request."""
    pass


# ── Types ──────────────────────────────────────────────────────────


@dataclass
class ToolCallInfo:
    """Information about a single tool call from the LLM."""

    id: str
    name: str
    arguments: dict


@dataclass
class AgentResult:
    """Result of running the agent loop."""

    text: str
    usage: TokenUsage
    cancelled: bool = False


class MaxIterationsExceeded(Exception):
    """Raised when the agent loop exceeds the configured iteration limit."""

    def __init__(self, iterations: int):
        self.iterations = iterations
        super().__init__(f"Agent exceeded maximum of {iterations} iterations")


class AgentEventHandler(Protocol):
    """Protocol for handling agent events during streaming.

    Implementations (e.g. a TUI) receive real-time callbacks
    as thinking, text, tool calls, and token usage are produced.
    """

    async def on_thinking_chunk(self, chunk: str) -> None:
        """Called with each reasoning/thinking token as it arrives."""
        ...

    async def on_text_chunk(self, chunk: str) -> None:
        """Called with each text token as it arrives from the LLM."""
        ...

    async def on_tool_call(
        self, tool_call: ToolCallInfo, iteration: int = 0, max_iterations: int = 30
    ) -> None:
        """Called before a tool is executed.

        iteration: 1-based current iteration number.
        max_iterations: configured maximum iterations.
        """
        ...

    async def on_tool_approval(self, tool_call: ToolCallInfo) -> bool:
        """Called before executing a tool that requires user approval.

        Return True to proceed with execution, False to deny.
        Default implementation approves everything — handlers that
        don't implement this method will auto-approve.
        """
        return True

    async def on_tool_result(
        self, tool_call_id: str, result: str, is_error: bool
    ) -> None:
        """Called after a tool finishes executing."""
        ...

    async def on_token_usage(self, usage: TokenUsage) -> None:
        """Called with cumulative token usage after each LLM call."""
        ...

    async def on_image(self, source: str) -> None:
        """Called when an image is produced (e.g. in tool results).

        *source* is a local file path or base64 data URI.
        Default is a no-op — handlers opt in by implementing this method.
        """
        ...

    def finalize_current(self) -> None:
        """Mark the current (last incomplete) assistant message as complete.

        Called on error/cancel to ensure the TUI spinner stops and the
        chat view does not stay in a permanent loading state.
        """
        ...


# ── Stream accumulator ─────────────────────────────────────────────


@dataclass
class _StreamResult:
    """Accumulated result from processing a single streaming response."""

    content: str
    thinking: str
    usage: TokenUsage
    tool_accum: dict[int, dict]  # index → partial tool call info


# ── Agent loop ─────────────────────────────────────────────────────


# ── Image detection in tool results ────────────────────────────────

# Matches [image: /path/to/file.png] markers from include_image tool and MCP client.
_IMAGE_MARKER_RE = re.compile(r"\[image:\s*(.+?)\]")


def extract_image_markers(text: str) -> list[str]:
    """Extract ``[image: <path>]`` markers from text — no existence check.

    Pure marker extraction + dedup.  Callers decide whether file
    existence matters: the live agent loop filters down to files that
    exist on disk (:func:`_scan_for_images`), while session restore
    resolves markers against the filesystem (file exists → render,
    file gone → ``⚠`` placeholder) — see ``slife.ui.restore``.

    Returns deduplicated marker paths in order of appearance.
    """
    found: list[str] = []
    seen: set[str] = set()

    for match in _IMAGE_MARKER_RE.finditer(text):
        path_str = match.group(1).strip()
        if path_str and path_str not in seen:
            found.append(path_str)
            seen.add(path_str)

    return found


def _scan_for_images(text: str) -> list[str]:
    """Scan tool result text for ``[image: <path>]`` markers pointing at real files.

    Only detects the explicit marker — no heuristic path matching.
    Tools (``include_image``, MCP binary data handler) are responsible
    for producing the marker when they have a real image to display.

    Returns deduplicated list of absolute paths that exist on disk.
    """
    found: list[str] = []
    seen: set[str] = set()

    for path_str in extract_image_markers(text):
        p = Path(path_str)
        if p.exists() and p.is_file():
            resolved = str(p.resolve())
            if resolved not in seen:
                found.append(resolved)
                seen.add(resolved)

    return found


class AgentLoop:
    """Core function-calling agent loop with real-time streaming.

    The loop:
      1. Sends conversation + tools to the LLM via streaming API
      2. Emits thinking and text chunks via callbacks in real-time
      3. Accumulates tool call deltas; if the model requests tools,
         executes them and loops back
      4. If the LLM returns text (no tool calls), returns the final text

    Tracks cumulative token usage across all API calls in the loop.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        max_iterations: int = 30,
        max_tool_result_chars: int = 0,
        tool_timeout: float = 60.0,
        context_window: int = 0,
        context_floor: float = 0.2,
        context_ceiling: float = 0.8,
        memdb_enabled: bool = True,
        supports_vision: bool = False,
        model_name: str = "",
        input_modalities: str = "",
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self.tool_timeout = tool_timeout
        self.context_window = context_window
        self.context_floor = context_floor
        self.context_ceiling = context_ceiling
        self.memdb_enabled = memdb_enabled
        self.supports_vision = supports_vision
        self.model_name = model_name
        self.input_modalities = input_modalities
        self._cancel_event = asyncio.Event()
        self._last_context_tokens: int = 0
        self._last_usage = TokenUsage()
        # Track stable fields — only emit in context footer when they change.
        self._last_cwd: str = ""
        self._last_shell: str = ""
        self._last_model_name: str = ""
        self._last_input_modalities: str = ""
        self._context_time_start: str = ""  # earliest turn date in context; set by restore, advanced by trim
        self._context_turn_dates: list[str] = []  # dates of restored turns, oldest-first; consumed by trim

    def cancel(self) -> None:
        """Signal the agent loop to stop at the next safe point."""
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        """Clear the cancel signal for the next run."""
        self._cancel_event.clear()

    # ── Tool call helpers ──────────────────────────────────────────

    @staticmethod
    def _truncate_args(args: dict, max_len: int = 80) -> dict:
        """Truncate long argument values for readable log output."""
        result = {}
        for k, v in args.items():
            s = str(v)
            if len(s) > max_len:
                s = s[:max_len] + "…"
            result[k] = s
        return result

    @staticmethod
    def _serialize_tool_calls(tool_calls: list[ToolCallInfo]) -> list[dict]:
        """Serialize ToolCallInfo list back to OpenAI API format."""
        return [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(
                        tc.arguments, ensure_ascii=False
                    ),
                },
            }
            for tc in tool_calls
        ]

    @staticmethod
    def _build_tool_calls_from_deltas(
        accum: dict[int, dict],
    ) -> list[ToolCallInfo]:
        """Build ToolCallInfo list from accumulated streaming deltas."""
        result = []
        for idx in sorted(accum.keys()):
            acc = accum[idx]
            try:
                args = (
                    json.loads(acc["arguments"])
                    if acc["arguments"].strip()
                    else {}
                )
            except json.JSONDecodeError:
                args = {}
            result.append(
                ToolCallInfo(
                    id=acc["id"],
                    name=acc["name"],
                    arguments=args,
                )
            )
        return result

    # ── Universal meta-param injection ──────────────────────────────

    @staticmethod
    def _inject_meta_params(functions: list[dict]) -> list[dict]:
        """Add ``_timeout`` and ``_async`` as optional parameters to every
        function definition.

        The LLM can pass these on ANY tool call:
          - ``_timeout`` (number) — override global ``tool_timeout``.
            For tools with a native ``timeout`` parameter (e.g.
            ``execute_shell``), ``_timeout`` is mapped to ``timeout``
            and the tool's internal timeout logic takes over.
          - ``_async`` (boolean) — run in background, return task_id
            immediately.

        Both are stripped before dispatch.
        """
        for func in functions:
            schema = func.get("function", {}).get("parameters", {})
            props = schema.setdefault("properties", {})
            if "_timeout" not in props:
                props["_timeout"] = {
                    "type": "number",
                    "description": (
                        "可选。本次调用的超时秒数，覆盖全局默认值。"
                        "用于网络请求、大文件操作等需要更长时间的场景。"
                    ),
                }
            if "async" not in props and "_async" not in props:
                props["_async"] = {
                    "type": "boolean",
                    "description": (
                        "设为 true 时后台异步执行，立即返回 task_id。"
                        "用于耗时很长的操作——用 check_async 查询结果，"
                        "用 cancel_async 取消。"
                    ),
                }
        return functions

    # ── Context trimming ────────────────────────────────────────────

    async def _maybe_trim_context(self, conversation: Conversation) -> None:
        """Check context size and trim oldest turns when over ceiling.

        When the conversation exceeds ``ceiling * context_window`` tokens,
        the oldest complete turns are removed and a synthetic
        ``_sys_trim`` tool-call + result pair is inserted so the LLM
        sees a visible notification.  Trimmed turns were already persisted
        by :meth:`AgentService.save_to_memory` when each turn is completed,
        so the LLM can retrieve them via ``memory_search``.
        """
        if self._cancel_event.is_set():
            return
        if not conversation.messages or self.context_window <= 0:
            return

        ceiling_tokens = int(self.context_window * self.context_ceiling)

        # Use the accurate context size from the last turn's final API
        # call when available; fall back to the chars/3 estimate for the
        # first turn of a session.
        current = self._last_context_tokens or conversation.count_tokens()
        if current <= ceiling_tokens:
            return

        target = int(self.context_window * self.context_floor)
        turns, tokens_freed = conversation.extract_oldest_turns(target)
        # After trimming, update the baseline so the next check is accurate.
        self._last_context_tokens = conversation.count_tokens()
        if not turns:
            return

        # ── Build human-readable summary ──────────────────────────
        summary_parts = []
        for idx, turn in enumerate(turns, 1):
            user_msg = turn.get("user_message", "(无文本)")
            est = turn.get("estimated_tokens", 0)
            if len(user_msg) > 80:
                user_msg = user_msg[:80] + "..."
            summary_parts.append(
                f'- 轮次{idx}: "{user_msg}" (约{est} tokens)'
            )

        turns_summary = "\n".join(summary_parts)
        if len(turns_summary) > 2000:
            turns_summary = turns_summary[:2000] + "\n...（摘要过长已截断）"

        tool_call_id = f"_trim_{uuid.uuid4().hex[:8]}"
        conversation.insert_trim_notification(
            tool_call_id=tool_call_id,
            turns_removed=len(turns),
            tokens_freed=tokens_freed,
            turns_summary=turns_summary,
            memory_saved=self.memdb_enabled,
        )

        # Advance the context time range so _sys_note reflects
        # the new earliest turn still in context.
        for _ in turns:
            if self._context_turn_dates:
                self._context_time_start = self._context_turn_dates.pop(0)
        logger.info(
            "context_trimmed turns=%d tokens_freed=%d tool_call_id=%s time_start=%s",
            len(turns), tokens_freed, tool_call_id, self._context_time_start,
        )

    # ── Stream processing ──────────────────────────────────────────

    async def _process_stream(
        self,
        conversation: Conversation,
        handler: AgentEventHandler | None,
    ) -> _StreamResult:
        """Consume a single streaming LLM response.

        Emits thinking and text chunks to the handler in real-time.
        Accumulates tool call deltas, content, and usage.

        When cancelled, the stream is closed immediately via
        ``chat_stream(cancel_event=...)`` — the underlying HTTP
        connection is released and no more chunks are consumed.

        Returns a _StreamResult with the response data accumulated
        up to the point of cancellation.
        """
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_accum: dict[int, dict] = {}
        stream_usage = TokenUsage()

        # Trim oldest turns when context exceeds ceiling — the LLM
        # sees a synthetic _trim_context tool call + result so it
        # knows what was removed and can retrieve it via memory_search.
        await self._maybe_trim_context(conversation)

        # Inject dynamic context footer.  Time + token always shown;
        # model, CWD, shell only when they changed since last turn.
        lu = self._last_usage
        cwd_now = os.getcwd()
        shell_now = _current_shell()
        kwargs: dict = {
            "context_window": self.context_window,
            "last_total_tokens": lu.total_tokens,
        }
        if self.model_name != self._last_model_name:
            kwargs["model_name"] = self.model_name
            kwargs["input_modalities"] = self.input_modalities
            self._last_model_name = self.model_name
        if cwd_now != self._last_cwd:
            kwargs["cwd"] = cwd_now
            self._last_cwd = cwd_now
        if shell_now != self._last_shell:
            kwargs["shell"] = shell_now
            self._last_shell = shell_now
        if self._context_time_start:
            kwargs["context_time_start"] = self._context_time_start
        conversation.insert_context_status(build_context_status(**kwargs))

        async for chunk in self.llm_client.chat_stream(
            messages=conversation.to_openai_messages(
                thinking_enabled=self.llm_client.model_config.thinking_enabled,
            ),
            tools=self._inject_meta_params(
                self.tool_registry.to_openai_functions()
            ),
            cancel_event=self._cancel_event,
        ):
            if chunk.thinking:
                thinking_parts.append(chunk.thinking)
                if handler and not self._cancel_event.is_set():
                    await handler.on_thinking_chunk(chunk.thinking)

            if chunk.content:
                content_parts.append(chunk.content)
                if handler and not self._cancel_event.is_set():
                    await handler.on_text_chunk(chunk.content)

            if chunk.tool_deltas:
                for td in chunk.tool_deltas:
                    idx = td["index"]
                    if idx not in tool_accum:
                        tool_accum[idx] = {
                            "id": "",
                            "name": "",
                            "arguments": "",
                        }
                    acc = tool_accum[idx]
                    if td["id"]:
                        acc["id"] = td["id"]
                    if td["function"]["name"]:
                        acc["name"] = td["function"]["name"]
                    if td["function"]["arguments"]:
                        acc["arguments"] += td["function"]["arguments"]

            if chunk.usage:
                stream_usage = chunk.usage

        # Remember last API usage for the next turn's context footer.
        if stream_usage.total_tokens > 0:
            self._last_usage = stream_usage

        return _StreamResult(
            content="".join(content_parts),
            thinking="".join(thinking_parts),
            usage=stream_usage,
            tool_accum=tool_accum,
        )

    # ── Tool execution ─────────────────────────────────────────────

    async def _execute_tools(
        self,
        tool_calls: list[ToolCallInfo],
        conversation: Conversation,
        handler: AgentEventHandler | None,
        iteration: int = 0,
    ) -> None:
        """Execute a batch of tool calls and record results.

        Emits on_tool_call/on_tool_result via the handler.
        Adds tool result messages to the conversation.

        Tools are executed **concurrently** — when the LLM issues
        multiple independent tool calls (e.g. two subscribe operations),
        they run in parallel via :func:`asyncio.gather`.  Each tool is
        still individually guarded by :attr:`tool_timeout`.

        Tools requiring user approval serialize behind an
        :class:`asyncio.Lock` so only one modal dialog appears at a time.
        """
        # Check cancellation before starting the batch
        if self._cancel_event.is_set():
            logger.info("agent_cancelled phase=before_batch iter=%d", iteration)
            return

        # Serialize approval dialogs — concurrent modals would overlap
        _approval_lock = asyncio.Lock()

        async def _run_one(tc: ToolCallInfo) -> None:
            """Execute a single tool call with timeout, sanitization, and
            handler notifications.  Safe to run concurrently."""
            logger.debug("tool_start name=%s", tc.name)
            if handler:
                await handler.on_tool_call(
                    tc,
                    iteration=iteration,
                    max_iterations=self.max_iterations,
                )

            # ── Dynamic per-call timeout / async ─────────────────
            # LLM can pass _async (boolean) or _timeout (number) on
            # ANY tool call.  _async takes priority — if true, the
            # tool is scheduled in background and we return immediately.
            actual_args = dict(tc.arguments)
            is_async = actual_args.pop("_async", None)
            inline_timeout = actual_args.pop("_timeout", None)

            # ── Native timeout mapping ───────────────────────────
            # Tools with a native ``timeout`` parameter (e.g.
            # execute_shell) handle their own timeout internally.
            # Map _timeout → timeout and let the tool drive — no
            # asyncio.wait_for wrapper.
            tool = self.tool_registry.get(tc.name)
            has_native_timeout = (
                "timeout" in getattr(tool, 'parameters', {}).get("properties", {})
            )
            if has_native_timeout and inline_timeout is not None:
                timeout_val = int(float(inline_timeout))
                if timeout_val > 0:
                    actual_args["timeout"] = timeout_val

            # ── Approval gate (serialised via lock) ─────────────────
            if getattr(tool, 'requires_approval', False):
                async with _approval_lock:
                    approved = await handler.on_tool_approval(tc) if handler else True
                if not approved:
                    result = "Error: Tool execution was denied by user."
                    result = sanitize_secrets(result)
                    if handler:
                        await handler.on_tool_result(tc.id, result, is_error=True)
                    conversation.add_tool_result(tc.id, result)
                    return

            if is_async:
                # ── Async: schedule background task ─────────────
                from slife.tools.meta import schedule as schedule_async

                coro = self.tool_registry.execute(tc.name, **actual_args)
                task_id = schedule_async(coro)
                result = (
                    f"✓ 异步任务已启动。\n"
                    f"  task_id: {task_id}\n"
                    f"  tool: {tc.name}\n"
                    f"  使用 check_async(task_id=\"{task_id}\") 查询结果。\n"
                    f"  使用 cancel_async(task_id=\"{task_id}\") 取消任务。"
                )
                result = sanitize_secrets(result)
                if handler:
                    await handler.on_tool_result(tc.id, result, is_error=False)
                conversation.add_tool_result(tc.id, result)
                return

            if has_native_timeout:
                # ── Native timeout: tool handles its own deadline ──
                # _timeout was already mapped to the timeout arg above.
                # The tool is responsible for enforcing its own timeout.
                try:
                    coro = self.tool_registry.execute(tc.name, **actual_args)
                    result = await coro
                except Exception as e:
                    result = (
                        f"Error: Tool '{tc.name}' failed: {type(e).__name__}: {e}."
                    )
                    logger.info(
                        "tool_error name=%s err=%s", tc.name, e,
                    )
            else:
                # ── Agent Loop timeout: wrap with asyncio.wait_for ──
                if inline_timeout is not None:
                    effective_timeout = float(inline_timeout) if float(inline_timeout) > 0 else 0.0
                else:
                    effective_timeout = self.tool_timeout

                try:
                    coro = self.tool_registry.execute(tc.name, **actual_args)
                    if effective_timeout > 0:
                        result = await asyncio.wait_for(coro, timeout=effective_timeout)
                    else:
                        result = await coro
                except asyncio.TimeoutError:
                    result = (
                        f"Error: Tool '{tc.name}' timed out "
                        f"({effective_timeout}s)."
                    )
                    logger.info(
                        "tool_timeout name=%s timeout=%ds args=%s",
                        tc.name, effective_timeout,
                        self._truncate_args(tc.arguments),
                    )
                except Exception as e:
                    result = (
                        f"Error: Tool '{tc.name}' failed: {type(e).__name__}: {e}."
                    )
                    logger.info(
                        "tool_error name=%s err=%s", tc.name, e,
                    )

            # Sanitize secrets BEFORE anything else — prevents API keys
            # from reaching the LLM context or TUI display.
            result = sanitize_secrets(result)
            # Truncate oversized tool results so a single large file
            # read doesn't blow up the context window.
            max_chars = self.max_tool_result_chars
            if max_chars > 0 and len(result) > max_chars:
                original_len = len(result)
                result = result[:max_chars] + f"\n…（已截断，原文 {original_len} 字符）"
                logger.debug("tool_result_truncated name=%s original=%d truncated=%d", tc.name, original_len, max_chars)
            is_error = result.startswith("Error")

            # ── Scan for images in tool output ──────────────────
            # Detect [image: path] markers from MCP binary data
            # and file paths that exist on disk.
            if handler:
                imgs = _scan_for_images(result)
                if imgs:
                    logger.info("tool_images_found tool=%s count=%d paths=%s", tc.name, len(imgs), imgs)
                for img_path in imgs:
                    await handler.on_image(img_path)

            if handler:
                await handler.on_tool_result(tc.id, result, is_error)

            conversation.add_tool_result(tc.id, result)

        await asyncio.gather(*(_run_one(tc) for tc in tool_calls))

    # ── Main loop ──────────────────────────────────────────────────

    async def run(
        self,
        user_input: str,
        conversation: Conversation,
        images: list[str] | None = None,
        handler: AgentEventHandler | None = None,
    ) -> AgentResult:
        """Run the agent loop for a single user input.

        Uses streaming API so thinking and text appear in real-time.

        Args:
            user_input: The user's message text.
            conversation: The conversation history (mutated in place).
            images: Optional list of image file paths to attach.
            handler: Optional event handler for real-time callbacks.

        Returns:
            AgentResult with final text and cumulative token usage.

        Raises:
            MaxIterationsExceeded: If the loop exceeds max_iterations.
            AgentCancelled: If cancel() was called during execution.
        """
        n_imgs = len(images) if images else 0
        if n_imgs > 0 and not self.supports_vision:
            msg = (
                f"⚠ 当前模型不支持图片输入（supports_vision=false），"
                f"但收到了 {n_imgs} 张图片。"
                f"请使用支持视觉的模型，或移除 @path 附件。"
            )
            logger.warning("vision_unsupported imgs=%d model_vision=%s", n_imgs, self.supports_vision)
            # Add text-only — don't encode images the model can't handle
            conversation.add_user_message(user_input, images=None)
            return AgentResult(text=msg, usage=TokenUsage())

        conversation.add_user_message(user_input, images=images)
        total_usage = TokenUsage()
        t_request = _time.monotonic()

        logger.info("req_start msg=%.100s imgs=%d", user_input, n_imgs)

        with request_scope(user_input[:50]):
            try:
                for i in range(self.max_iterations):
                    # Check for cancellation before each iteration
                    if self._cancel_event.is_set():
                        logger.info("agent_cancelled iter=%d", i + 1)
                        raise AgentCancelled()

                    with elapsed("iter", logger, iter=i + 1):
                        result = await self._process_stream(conversation, handler)

                        # Capture usage even when cancelled — the API call
                        # already consumed tokens regardless of outcome.
                        total_usage = total_usage + result.usage
                        if handler:
                            await handler.on_token_usage(total_usage)

                        # Check for cancellation after stream
                        if self._cancel_event.is_set():
                            logger.info("agent_cancelled phase=after_stream iter=%d", i + 1)
                            raise AgentCancelled()

                        # Tool calls?
                        if result.tool_accum:
                            tool_calls = self._build_tool_calls_from_deltas(
                                result.tool_accum
                            )
                            logger.debug(
                                "tool_calls=%d names=%s",
                                len(tool_calls),
                                [tc.name for tc in tool_calls],
                            )
                            conversation.add_assistant_message(
                                content=result.content or None,
                                tool_calls=self._serialize_tool_calls(tool_calls),
                                thinking=result.thinking or None,
                            )
                            await self._execute_tools(
                                tool_calls, conversation, handler, iteration=i + 1
                            )
                            continue

                        # No tool calls — final response
                        conversation.add_assistant_message(
                            content=result.content or "",
                            thinking=result.thinking or None,
                        )
                        t_total = (_time.monotonic() - t_request) * 1000
                        logger.info(
                            "response tok_p=%d tok_c=%d tok_t=%d took_ms=%.0f text=%.200s",
                            total_usage.prompt_tokens,
                            total_usage.completion_tokens,
                            total_usage.total_tokens,
                            t_total,
                            result.content,
                        )
                        self._last_context_tokens = self._last_usage.prompt_tokens
                        return AgentResult(text=result.content, usage=total_usage)

                raise MaxIterationsExceeded(self.max_iterations)
            except AgentCancelled:
                self._last_context_tokens = self._last_usage.prompt_tokens
                return AgentResult(text="", usage=total_usage, cancelled=True)
            except MaxIterationsExceeded:
                logger.warning("max_iterations_exceeded max=%d", self.max_iterations)
                self._last_context_tokens = self._last_usage.prompt_tokens
                return AgentResult(text="", usage=total_usage, cancelled=True)
