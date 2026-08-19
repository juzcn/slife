"""Function-calling agent loop with real-time streaming and thinking support."""

import asyncio
import copy
import itertools
import json
import logging
import os
import re
import time as _time
from datetime import datetime
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from slife.agent.llm_client import LLMClient, TokenUsage
from slife.logfmt import sanitize_secrets
from slife.agent.conversation import Conversation
from slife.agent.system_prompt import _current_shell
from slife.tools.registry import ToolRegistry
from slife.logfmt import request_scope, elapsed

logger = logging.getLogger(__name__)


class AgentCancelled(Exception):
    """Raised when the agent loop is cancelled by user request."""
    pass


# ── LLM stream retry ───────────────────────────────────────────────

#: Bounded retry for transient LLM transport failures (e.g. DeepSeek closing
#: the streaming connection mid-body → httpx RemoteProtocolError). The SDK's
#: built-in max_retries only covers request-establishment errors, not body-read
#: failures during stream iteration — so we retry here, at the contract layer,
#: for every turn source (main agent, subagents, heartbeat, WeChat, A2A).
_LLM_STREAM_MAX_RETRIES = 2  # total attempts = 3

#: Caps on the per-session caches.  Heartbeat / A2A one-shot conversations add
#: a usage entry keyed by ``id(conversation)`` every turn and the context-date
#: list grows per turn until a trim consumes it — without these bounds a long
#: session (or a huge context window that never trims) grows them forever.
_MAX_USAGE_CACHE = 1000
_MAX_CONTEXT_DATES = 5000
_LLM_STREAM_RETRY_BASE_DELAY = 0.5  # seconds, linear backoff: 0.5 * attempt


def _is_retryable_stream_error(exc: BaseException) -> bool:
    """True if *exc* is a transient LLM transport failure worth retrying.

    ``httpx.TransportError`` covers ``RemoteProtocolError`` (peer closed the
    connection before the chunked body completed), ``ReadError``,
    ``ConnectError`` and the timeout classes. The SDKs' ``*APIConnectionError`` /
    ``*APITimeoutError`` wrap the same httpx failures at request time and are
    also retried. Bad-request, content-filter and auth errors are NOT retried
    here — they are the SDK's / inbox's concern, and 429/5xx are already
    retried by the SDK internally.
    """
    try:
        import httpx
    except ImportError:
        httpx = None
    if httpx is not None and isinstance(exc, httpx.TransportError):
        return True
    try:
        import openai
    except ImportError:
        openai = None
    if openai is not None and isinstance(
        exc, (openai.APIConnectionError, openai.APITimeoutError),
    ):
        return True
    try:
        import anthropic
    except ImportError:
        anthropic = None
    if anthropic is not None and isinstance(
        exc, (anthropic.APIConnectionError, anthropic.APITimeoutError),
    ):
        return True
    return False


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

    async def on_stream_retry(self) -> None:
        """Discard any partial text/thinking shown for a retried LLM request.

        The agent loop retries transient transport failures (connection drop
        mid-stream). When partial output was already streamed, the handler
        resets it so the retried request starts visually clean — otherwise
        the user sees the partial output duplicated. Default is a no-op.
        """
        ...

    async def on_max_iterations(self, iterations: int) -> None:
        """Called when the agent loop hits the configured iteration limit.

        The turn still completes as a cancelled turn (persistence-wise),
        but the handler can surface the limit to the user. Default is a
        no-op.
        """
        ...

    async def on_memory_save_warning(self, message: str) -> None:
        """Called when a turn's diary save could not be confirmed.

        The MCP channel returned something the save path could not parse,
        so the turn may or may not have been persisted.  The handler can
        surface this to the user.  Default is a no-op.
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
        context_ceiling: float = 0.8,
        context_floor: float = 0.2,
        memdb_enabled: bool = True,
        supports_vision: bool = False,
        model_name: str = "",
        input_modalities: str = "",
        presence_provider: Callable[[], list[tuple[float, str]]] | None = None,
        advance_context_start: Callable[[int], Awaitable[bool]] | None = None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self.tool_timeout = tool_timeout
        self.context_window = context_window
        self.context_ceiling = context_ceiling
        self.context_floor = context_floor
        self.memdb_enabled = memdb_enabled
        #: Persist the live-context start boundary after a trim evicted
        #: *count* oldest turns, so a restart rebuilds the exit-time context
        #: from exactly where the live one now stands.  Best-effort: an
        #: unreachable memdb only leaves the boundary stale (restore becomes
        #: a superset, never a loss).  Wired by AgentService (bound method).
        self.advance_context_start = advance_context_start
        self.supports_vision = supports_vision
        self.model_name = model_name
        self.input_modalities = input_modalities
        #: Read-and-clear provider for pending A2A peer presence events.
        #: Injected by AgentService so the context footer can show what
        #: changed since the last turn.  Returns ``(epoch_seconds, text)``.
        self._presence_provider = presence_provider
        self._cancel_event = asyncio.Event()
        # Last API usage, tracked PER CONVERSATION (keyed by id()).  The
        # heartbeat / A2A / wechat turns run in their own small conversations,
        # so a global _last_usage would be overwritten by e.g. a heartbeat
        # (9.6%) and drag the human conversation's status bar / _sys_note
        # down to the wrong value.  _last_usage is kept only as the
        # restore-time estimate slot (primed by restore_session).
        self._usage_by_conv: dict[int, TokenUsage] = {}
        self._last_usage = TokenUsage()
        # Track stable fields — only emit in context footer when they change.
        self._last_cwd: str = ""
        self._last_shell: str = ""
        self._last_model_name: str = ""
        self._last_input_modalities: str = ""
        self._context_time_start: str = ""  # earliest turn date in context; set by restore, advanced by trim
        self._last_context_time_start: str = ""  # for change-detection in the footer
        self._context_turn_dates: list[str] = []  # dates of restored turns, oldest-first; consumed by trim
        #: ``id(conversation)`` whose restore must not be immediately
        #: shredded by the ceiling trim.  Restore primes the conversation
        #: up to the ceiling; the first replacement turn would otherwise
        #: compact it straight back to the floor before the user got to
        #: use it.  Consumed on that turn (see :meth:`run`).
        self._just_restored_conv: int | None = None

    def set_max_iterations(self, max_iterations: int) -> str:
        """Change the per-turn iteration cap at runtime (0 = unlimited).

        Takes effect from the next turn: the running turn's iteration
        budget was fixed when ``run()`` started, so this only affects
        subsequent ``run()`` calls.  Returns a human-readable confirmation
        or an error string.
        """
        if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
            return f"Error: max_iterations must be an integer, got {max_iterations!r}."
        if max_iterations < 0:
            return f"Error: max_iterations must be >= 0 (0 = unlimited), got {max_iterations}."
        self.max_iterations = max_iterations
        if max_iterations == 0:
            return "Max iterations set to 0 — unlimited (no cap). Applies next turn."
        return f"Max iterations set to {max_iterations}. Applies next turn."

    def cancel(self) -> None:
        """Signal the agent loop to stop at the next safe point."""
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        """Clear the cancel signal for the next run."""
        self._cancel_event.clear()

    def reset_context_time(self) -> None:
        """Clear the tracked context time range (after ``clear_context``).

        The next turn re-seeds ``_context_time_start`` from its own start,
        so "Context covers" reflects the fresh context instead of the
        pre-clear range.
        """
        self._context_time_start = ""
        self._context_turn_dates = []
        self._last_context_time_start = ""

    # ── Tool call helpers ──────────────────────────────────────────

    @staticmethod
    def _truncate_args(args: dict, max_len: int = 80) -> dict:
        """Truncate (and mask) long argument values for readable log output.

        Tool-call arguments can carry secrets the LLM passes through — mask
        before the values reach the session log.
        """
        result = {}
        for k, v in args.items():
            s = sanitize_secrets(str(v))
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
                # Truncated by max_tokens / provider mid-argument — surface it
                # instead of silently running the tool with no arguments (a
                # destructive command would otherwise become a confusing no-op).
                logger.warning(
                    "tool_args_malformed id=%s name=%s raw=%.120s",
                    acc["id"], acc["name"], acc["arguments"],
                )
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
          - ``_approve`` (boolean) — ask the user to confirm this call
            (approval dialog) before it runs.

        All are stripped before dispatch.
        """
        for func in functions:
            schema = func.get("function", {}).get("parameters", {})
            # Deep-copy so the injected params don't mutate the tool's own
            # shared parameters dict (to_openai_function() returns it by
            # reference — mutating here would poison every later call).
            schema = copy.deepcopy(schema)
            func["function"]["parameters"] = schema
            props = schema.setdefault("properties", {})
            if "_timeout" not in props:
                props["_timeout"] = {
                    "type": "number",
                    "description": (
                        "Optional. Override the timeout (seconds) for this call, "
                        "replacing the global default. Use for network requests, "
                        "large-file operations, or other long-running work."
                    ),
                }
            if "async" not in props and "_async" not in props:
                props["_async"] = {
                    "type": "boolean",
                    "description": (
                        "Set true to run in the background and return a task_id "
                        "immediately. Use for long-running operations — poll with "
                        "check_async, cancel with cancel_async."
                    ),
                }
            if "_approve" not in props:
                props["_approve"] = {
                    "type": "boolean",
                    "description": (
                        "Optional. Set true to show a confirmation dialog to the "
                        "user before executing. Default false (no dialog)."
                    ),
                }
        return functions

    # ── Context trimming ────────────────────────────────────────────

    def context_tokens_for(self, conversation: Conversation) -> int:
        """Context tokens the next API call would send.

        Single source for ``_sys_note``, the trim decision
        (``_trim_after_save``), and the TUI status bar — one value, no
        recompute.  Resolution order:

        1. After an API call — that conversation's last call's actual
           ``prompt_tokens`` (tracked per conversation, so a heartbeat's
           small context never pollutes the human conversation's reading).
        2. First round after a restore — the restore-time estimate primed
           on ``_last_usage`` (computed once when the UI rebuilds the
           session to decide how many turns to restore; we have no real
           API usage yet).
        3. Genuinely fresh session — a live :meth:`Conversation.count_tokens`
           estimate.
        """
        usage = self._usage_by_conv.get(id(conversation))
        if usage is None:
            usage = self._last_usage  # restore-time estimate
        if usage.prompt_tokens:
            return usage.prompt_tokens
        if usage.total_tokens:
            return usage.total_tokens
        return conversation.count_tokens()

    async def _trim_after_save(
        self, conversation: Conversation, handler: object | None = None,
    ) -> None:
        """Trim the oldest turns after a turn is saved to memory.

        Called by ``save_to_memory`` once the just-completed turn is
        persisted.  By then the last API call's real ``prompt_tokens`` are
        known (``context_tokens_for`` reads ``_usage_by_conv``), so the
        ceiling check uses the true context occupancy — not the estimate
        the loop had at the turn's start.

        *handler* (optional) receives ``on_trim(count)`` so the live TUI
        can show the ``[TrimContext: N]`` note on the turn's last
        assistant message — mirroring the LLM-side marker.

        When occupancy is at/over the ceiling, compacts the conversation
        down to the floor (oldest complete turns removed) and appends a
        ``[TrimContext: N]`` marker to the last assistant message so the
        LLM knows how many turns were cut from its context.  The marker is
        runtime-only — never persisted, discarded on restore (a restored
        session is already the trimmed state; "a past session was
        truncated" is meaningless to the model, only the *current* cut is).

        A freshly-restored conversation is a legitimate pre-exit state,
        not growth — the ``_just_restored_conv`` marker (consumed in
        :meth:`run`) still guards the first replacement turn.
        """
        # A freshly-restored conversation is a legitimate pre-exit state,
        # not growth — never shred it on the very first replacement turn.
        # Consume the marker so from the second turn on the live rules
        # apply.  (Restore primes the context up to the ceiling; the first
        # turn's save would otherwise immediately compact it to the floor.)
        just_restored = self._just_restored_conv == id(conversation)
        if just_restored:
            self._just_restored_conv = None
            return

        # Only the just-finished turn exists / nothing to trim — the loop
        # also needs a boundary to not trim a conversation whose context
        # usage is unmeasurable (no API call yet → estimate fallback).
        current = self.context_tokens_for(conversation)
        if current < int(self.context_window * self.context_ceiling):
            return

        # Compress to the floor; the current (just-saved) turn is kept by
        # extract_oldest_turns — it only ever removes complete older turns.
        target = int(self.context_window * self.context_floor)
        turns, tokens_freed = conversation.extract_oldest_turns(target)
        if not turns:
            return

        # Advance the persisted live-context boundary past the removed
        # turns (best-effort — see advance_context_start).
        if self.advance_context_start is not None:
            try:
                await self.advance_context_start(len(turns))
            except Exception:
                logger.exception(
                    "context_start_advance_failed count=%d", len(turns),
                )

        # Advance the tracked "Context covers" time range by the same
        # number of removed turns (each complete turn has one user msg).
        removed = len(turns)
        for _ in range(removed):
            if self._context_turn_dates:
                self._context_time_start = self._context_turn_dates.pop(0)
        if removed and not self._context_turn_dates:
            # The trim removed every tracked turn (list exhausted — e.g. a
            # fresh session whose only tracked turn was in
            # _context_time_start, or all later dates got popped).  The
            # context now starts at the current turn (the one this save
            # just finished, which extract_oldest_turns always keeps);
            # don't leave a stale "covers since …" that points at a turn
            # that is no longer in context.
            now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
            self._context_time_start = now
        logger.info(
            "context_trimmed_after_save turns=%d tokens_freed=%d time_start=%s",
            removed, tokens_freed, self._context_time_start,
        )

        # Tell the LLM how much of its context was just cut.  Runtime-only
        # marker appended to the last assistant message (guaranteed present
        # and last by _ensure_turn_consistent in save_to_memory).
        conversation.append_trim_marker(removed)
        # Mirror it in the live TUI — same [TrimContext: N] note on the
        # turn's last assistant message.
        if handler is not None:
            on_trim = getattr(handler, "on_trim", None)
            if on_trim is not None:
                try:
                    on_trim(removed)
                except Exception:
                    logger.exception("trim_marker_ui_failed")

    # ── Harness tool invocation ────────────────────────────────────

    def _footer_kwargs(self, conversation: Conversation, current: int) -> dict:
        """Build the render kwargs for the ``_sys_note`` status tool.

        Time + token always shown; model/CWD/shell only when they
        changed since the last turn.  *current* is the context token
        count — computed once in :meth:`run` for the note (the trim
        decision later uses its own reading in ``_trim_after_save``).
        """
        cwd_now = os.getcwd()
        shell_now = _current_shell()
        kwargs: dict = {
            "context_window": self.context_window,
            "last_context_tokens": current,
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
        # Context start is reported on the first turn and then only when it
        # changes (restore sets it, trim advances it) — same change-detection
        # as model/CWD/shell.
        if self._context_time_start != self._last_context_time_start:
            kwargs["context_time_start"] = self._context_time_start
            self._last_context_time_start = self._context_time_start
        # presence_events are NOT drained here — _auto_invoke reads them only
        # when the note is actually recorded, so a cancelled turn doesn't lose
        # them.
        return kwargs

    async def _auto_invoke(
        self,
        name: str,
        args: dict,
        conversation: Conversation,
    ) -> None:
        """Invoke a declared harness tool on the loop's behalf.

        Records a normal ``assistant(tool_calls)`` + ``tool`` result pair,
        executing the tool **directly** — not through :meth:`_execute_tools`
        (no approval / timeout / async wrapping, and no cancel early-return
        race that could orphan the pair).  The tool is still a real
        schema-declared tool; only *who* invokes it differs from an
        LLM-requested call.
        """
        if self._cancel_event.is_set():
            return
        if name == "_sys_note" and self._presence_provider is not None:
            # Drain pending peer-presence events only now that the note will
            # actually be recorded — draining them in the _footer_kwargs args
            # expression while cancelled would lose them.
            args = dict(args)
            args["presence_events"] = self._presence_provider()
        tool = self.tool_registry.get(name)
        if tool is None:
            logger.warning("auto_invoke_tool_missing name=%s", name)
            return
        tc = ToolCallInfo(
            id=f"_harness_{name[1:]}_{_time.time_ns():x}",
            name=name,
            arguments=args,
        )
        conversation.add_assistant_message(
            content=None, tool_calls=self._serialize_tool_calls([tc]),
        )
        try:
            # Run the tool against the conversation the loop is currently
            # processing.  `_ctx.conversation` is set once at startup to the
            # human conversation, but harness tools are invoked per-source
            # (WeChat / remote-agent turns have their own Conversation) — a
            # trim must target the active one, not always the human diary.
            # Swap for the duration of the call and restore afterwards.
            ctx = getattr(tool, "_ctx", None)
            prev_conversation = None
            if ctx is not None:
                prev_conversation = ctx.conversation
                ctx.conversation = conversation
            try:
                result = await tool.execute(**args)
            finally:
                if ctx is not None:
                    ctx.conversation = prev_conversation
        except Exception as e:
            result = f"Error: Tool '{name}' failed: {type(e).__name__}: {e}."
            logger.warning("auto_invoke_error name=%s err=%s", name, e)
        conversation.add_tool_result(
            tc.id, sanitize_secrets(result),
            is_error=result.startswith("Error"),
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

        # Transient transport failures (e.g. the peer closing the connection
        # mid-chunked-read) are retried with linear backoff.  The conversation
        # is untouched while streaming — the assistant message is only added
        # after this method returns — so a retry sends identical messages.
        attempts = 0
        while True:
            attempts += 1
            emitted_any = False
            try:
                async for chunk in self.llm_client.chat_stream(
                    messages=conversation.to_openai_messages(
                        thinking_enabled=self.llm_client.model_config.thinking_enabled,
                    ),
                    tools=self._inject_meta_params(
                        self.tool_registry.to_openai_functions()
                    ),
                    cancel_event=self._cancel_event,
                ):
                    emitted_any = True

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
                break  # stream completed cleanly
            except asyncio.CancelledError:
                # Cancellation is a control-flow signal — never retry it.
                raise
            except Exception as e:
                if not _is_retryable_stream_error(e):
                    raise
                if attempts > _LLM_STREAM_MAX_RETRIES:
                    # Wrap the exhausted-retries error so the surfaced message
                    # is actionable.  RuntimeError is not a BadRequestError, so
                    # the inbox keeps the conversation intact — correct for a
                    # transient failure.
                    raise RuntimeError(
                        f"LLM stream failed after {attempts} attempts: {e}"
                    ) from e
                # Reset partial state + TUI display before retrying, so the
                # retried request starts visually clean.
                if emitted_any and handler is not None:
                    on_retry = getattr(handler, "on_stream_retry", None)
                    if on_retry is not None:
                        try:
                            await on_retry()
                        except Exception:
                            pass
                content_parts.clear()
                thinking_parts.clear()
                tool_accum.clear()
                stream_usage = TokenUsage()
                logger.warning(
                    "llm_stream_retry attempt=%d max=%d err=%s",
                    attempts, _LLM_STREAM_MAX_RETRIES, e,
                )
                if self._cancel_event.is_set():
                    raise AgentCancelled()
                await asyncio.sleep(_LLM_STREAM_RETRY_BASE_DELAY * attempts)

        # Remember last API usage per conversation — heartbeat/A2A/wechat
        # turns run in their own conversations and must not overwrite the
        # human conversation's context measurement.
        if stream_usage.total_tokens > 0:
            self._usage_by_conv[id(conversation)] = stream_usage
            if len(self._usage_by_conv) > _MAX_USAGE_CACHE:
                # Drop the oldest entry (dict preserves insertion order) — a
                # heartbeat fires every 60s and each A2A remote turn uses a
                # fresh one-shot conversation, so the cache would otherwise
                # grow without bound.  Evicting also makes an id()-reused
                # conversation miss (fresh estimate) instead of reading a
                # stale unrelated usage.
                self._usage_by_conv.pop(next(iter(self._usage_by_conv)))

        return _StreamResult(
            content="".join(content_parts),
            thinking="".join(thinking_parts),
            usage=stream_usage,
            tool_accum=tool_accum,
        )

    # ── Tool execution ─────────────────────────────────────────────

    async def _await_approval(self, handler, tc) -> bool:
        """Wait for the user's approval decision OR the turn's cancellation.

        Esc-cancel (``_cancel_event``) must not leave the loop blocked forever
        on an approval prompt that may have lost focus (e.g. the model picker
        stole it) — every later message would queue behind it.  On cancel the
        prompt is denied and the turn's cancellation proceeds.
        """
        cancel_wait = asyncio.create_task(self._cancel_event.wait())
        approve_wait = asyncio.create_task(handler.on_tool_approval(tc))
        pending: set = set()
        try:
            done, pending = await asyncio.wait(
                {cancel_wait, approve_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if cancel_wait in pending:
                cancel_wait.cancel()
        if approve_wait in done:
            return approve_wait.result()
        # Cancelled while the prompt was open — deny it.  on_tool_approval
        # resolves its own prompt future on cancellation so nothing dangles.
        approve_wait.cancel()
        return False

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

        # Any tool that reads ctx.conversation (include_image, clear_context)
        # must see the conversation this loop is processing — not the startup
        # human conversation — while a WeChat/remote-agent turn is running.
        # All native tools share one ToolContext, so a single swap covers the
        # concurrent batch; it is restored in the finally below.
        _ctx = None
        for _tc in tool_calls:
            _t = self.tool_registry.get(_tc.name)
            if _t is None:
                continue
            _ctx = getattr(_t, "_ctx", None)
            if _ctx is not None:
                break
        _prev_conv = None
        if _ctx is not None:
            _prev_conv = _ctx.conversation
            _ctx.conversation = conversation

        # Serialize approval prompts — concurrent prompts would overlap
        _approval_lock = asyncio.Lock()

        async def _run_one(tc: ToolCallInfo) -> None:
            """Execute a single tool call with timeout, sanitization, and
            handler notifications.  Safe to run concurrently."""
            logger.debug("tool_start name=%s", tc.name)

            # ── Dynamic per-call timeout / async / approval ───────
            # LLM can pass _async (boolean), _timeout (number), or
            # _approve (boolean) on ANY tool call.  _async takes
            # priority — if true, the tool is scheduled in background
            # and we return immediately.
            actual_args = dict(tc.arguments)
            is_async = actual_args.pop("_async", None)
            inline_timeout = actual_args.pop("_timeout", None)
            approve_requested = bool(actual_args.pop("_approve", False))

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

            # ── Approval gate — pure model judgment ────────────────
            # The LLM decides per-call whether the operation needs user
            # confirmation by passing `_approve: true` (injected on every
            # tool schema, like _timeout/_async).  The harness no longer
            # hardcodes approval on any tool.  Serialised via lock so
            # concurrent approval dialogs never overlap.
            if approve_requested:
                async with _approval_lock:
                    if handler:
                        approved = await self._await_approval(handler, tc)
                    else:
                        approved = True
                if not approved:
                    result = "Error: Tool execution was denied by user."
                    result = sanitize_secrets(result)
                    if handler:
                        await handler.on_tool_result(tc.id, result, is_error=True)
                    conversation.add_tool_result(tc.id, result, is_error=True)
                    return

            # ── Handler notification — after the approval gate ────
            # The tool call is only surfaced once approved (or when no
            # approval was requested): a denied call returns above and
            # never mounts a tool widget — the approval prompt itself
            # carries the "denied" state.
            if handler:
                await handler.on_tool_call(
                    tc,
                    iteration=iteration,
                    max_iterations=self.max_iterations,
                )

            if is_async:
                # ── Async: schedule background task ─────────────
                from slife.tools.meta import schedule as schedule_async

                if _ctx is not None:
                    # The background task runs AFTER _execute_tools' finally
                    # restores ctx.conversation — a tool that reads it (e.g.
                    # include_image) would otherwise target the wrong
                    # conversation.  Pin it to the one this turn is processing.
                    _run_conv = conversation
                    _run_ctx = _ctx

                    async def _run_with_conv():
                        _saved = _run_ctx.conversation
                        _run_ctx.conversation = _run_conv
                        try:
                            return await self.tool_registry.execute(
                                tc.name, **actual_args,
                            )
                        finally:
                            _run_ctx.conversation = _saved

                    coro = _run_with_conv()
                else:
                    coro = self.tool_registry.execute(tc.name, **actual_args)
                task_id = schedule_async(coro)
                result = (
                    f"✓ Async task started.\n"
                    f"  task_id: {task_id}\n"
                    f"  tool: {tc.name}\n"
                    f"  Poll with check_async(task_id=\"{task_id}\").\n"
                    f"  Cancel with cancel_async(task_id=\"{task_id}\")."
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
                    logger.warning(
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
                    logger.warning(
                        "tool_timeout name=%s timeout=%ds args=%s",
                        tc.name, effective_timeout,
                        self._truncate_args(tc.arguments),
                    )
                except Exception as e:
                    result = (
                        f"Error: Tool '{tc.name}' failed: {type(e).__name__}: {e}."
                    )
                    logger.warning(
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
                result = result[:max_chars] + (
                    f"\n… (truncated: original {original_len} chars — "
                    f"re-run the tool to see the full output)"
                )
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

            conversation.add_tool_result(tc.id, result, is_error=is_error)

        try:
            await asyncio.gather(*(_run_one(tc) for tc in tool_calls))
        finally:
            if _ctx is not None:
                _ctx.conversation = _prev_conv

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
            AgentResult with final text and cumulative token usage.  On
            cancellation or hitting max_iterations, the result carries
            ``cancelled=True`` and is surfaced to the handler via
            ``on_max_iterations`` (it is not raised).
        """
        n_imgs = len(images) if images else 0
        if n_imgs > 0 and not self.supports_vision:
            msg = (
                f"⚠ Current model does not support image input (supports_vision=false), "
                f"but {n_imgs} image(s) were received. "
                f"Use a vision-capable model, or remove the @path attachment."
            )
            logger.warning("vision_unsupported imgs=%d model_vision=%s", n_imgs, self.supports_vision)
            # Add text-only — don't encode images the model can't handle.
            # The warning is recorded as the assistant reply so the
            # conversation doesn't end on a dangling user message.
            conversation.add_user_message(user_input, images=None)
            conversation.add_assistant_message(content=msg)
            return AgentResult(text=msg, usage=TokenUsage())

        conversation.add_user_message(user_input, images=images)
        total_usage = TokenUsage()
        t_request = _time.monotonic()

        logger.info("req_start msg=%.100s imgs=%d", sanitize_secrets(user_input), n_imgs)

        with request_scope(user_input[:50]):
            try:
                # Track the context time range.  "Context covers" is shown on
                # the first turn, then only when restore or a trim advances it.
                # Invariant: _context_time_start holds the OLDEST turn's date;
                # _context_turn_dates holds the rest (restore seeds dates[1:]).
                # Same 'YYYY-MM-DD HH:MM:SS' wall-clock format restore seeds
                # (_context_time_start), so "Context covers" never flips format.
                turn_start = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
                if not self._context_time_start:
                    self._context_time_start = turn_start
                else:
                    self._context_turn_dates.append(turn_start)
                    if len(self._context_turn_dates) > _MAX_CONTEXT_DATES:
                        # Keep the OLDEST dates (what a trim consumes) — a
                        # huge window that never trims must not grow the list
                        # without bound.
                        del self._context_turn_dates[_MAX_CONTEXT_DATES:]

                # Context usage is computed ONCE and shared: _sys_note
                # reports it as the usage %, and the TUI status bar.
                current = self.context_tokens_for(conversation)
                await self._auto_invoke(
                    "_sys_note", self._footer_kwargs(conversation, current), conversation,
                )
                # Context trimming no longer happens here — it moved to
                # _trim_after_save (after each turn is persisted), where the
                # real API usage is known.  The _sys_note percentage and the
                # trim decision now come from the same context_tokens_for
                # reading at their respective times.
                # max_iterations = 0 means no cap.  The cap is checked live
                # each iteration, so a mid-turn set_max_iterations applies
                # immediately (and to the next turn too).
                for i in itertools.count():
                    if self.max_iterations > 0 and i >= self.max_iterations:
                        raise MaxIterationsExceeded(self.max_iterations)
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
                        return AgentResult(text=result.content, usage=total_usage)
            except AgentCancelled:
                # Turn consistency is enforced at the single save point
                # (save_to_memory runs unconditionally after every turn) —
                # the conversation is repaired there, not here.
                return AgentResult(text="", usage=total_usage, cancelled=True)
            except MaxIterationsExceeded:
                logger.warning("max_iterations_exceeded max=%d", self.max_iterations)
                # Surface the limit to the handler (e.g. the TUI) before
                # returning the cancelled result — the turn is saved as a
                # normal cancelled turn, but the user should see why it
                # stopped.
                if handler is not None:
                    on_max = getattr(handler, "on_max_iterations", None)
                    if on_max is not None:
                        try:
                            await on_max(self.max_iterations)
                        except Exception:
                            pass
                return AgentResult(text="", usage=total_usage, cancelled=True)
            except Exception:
                # Re-raise so the caller (inbox) handles the error as before;
                # the conversation is repaired at the save point.
                raise
        # Unreachable: the loop above only exits via return or raise, but
        # Pylance can't see that itertools.count() is infinite.
        raise RuntimeError("agent loop exited without a result")
