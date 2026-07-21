"""Function-calling agent loop with real-time streaming and thinking support."""

import asyncio
import json
import logging
import time as _time
from dataclasses import dataclass
from typing import Protocol

from slife.agent.llm_client import LLMClient, TokenUsage
from slife.logfmt import sanitize_secrets
from slife.agent.conversation import Conversation
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
        self, tool_call: ToolCallInfo, iteration: int = 0, max_iterations: int = 10
    ) -> None:
        """Called before a tool is executed.

        iteration: 1-based current iteration number.
        max_iterations: configured maximum iterations.
        """
        ...

    async def on_tool_result(
        self, tool_call_id: str, result: str, is_error: bool
    ) -> None:
        """Called after a tool finishes executing."""
        ...

    async def on_token_usage(self, usage: TokenUsage) -> None:
        """Called with cumulative token usage after each LLM call."""
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
        max_iterations: int = 10,
        max_tool_result_chars: int = 0,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self._cancel_event = asyncio.Event()

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

    # ── Stream processing ──────────────────────────────────────────

    async def _process_stream(
        self,
        conversation: Conversation,
        handler: AgentEventHandler | None,
    ) -> _StreamResult:
        """Consume a single streaming LLM response.

        Emits thinking and text chunks to the handler in real-time.
        Accumulates tool call deltas, content, and usage.

        When cancelled, stops emitting to the handler but continues
        consuming the stream to avoid resource leaks.

        Returns a _StreamResult with the complete response data.
        """
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_accum: dict[int, dict] = {}
        stream_usage = TokenUsage()

        async for chunk in self.llm_client.chat_stream(
            messages=conversation.to_openai_messages(),
            tools=self.tool_registry.to_openai_functions(),
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
        """
        for tc in tool_calls:
            if handler:
                await handler.on_tool_call(
                    tc,
                    iteration=iteration,
                    max_iterations=self.max_iterations,
                )

            result = await self.tool_registry.execute(
                tc.name, **tc.arguments
            )
            # Sanitize secrets BEFORE anything else — prevents API keys
            # from reaching the LLM context or TUI display.
            result = sanitize_secrets(result)
            # Truncate oversized tool results so a single large file
            # read doesn't blow up the context window.
            max_chars = self.max_tool_result_chars
            if max_chars > 0 and len(result) > max_chars:
                result = result[:max_chars] + f"\n…（已截断，原文 {len(result)} 字符）"
            is_error = result.startswith("Error")

            if handler:
                await handler.on_tool_result(tc.id, result, is_error)

            conversation.add_tool_result(tc.id, result)

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
        conversation.add_user_message(user_input, images=images)
        total_usage = TokenUsage()
        t_request = _time.monotonic()

        n_imgs = len(images) if images else 0
        logger.info("req_start msg=%.100s imgs=%d", user_input, n_imgs)

        with request_scope(user_input[:50]):
            for i in range(self.max_iterations):
                # Check for cancellation before each iteration
                if self._cancel_event.is_set():
                    logger.info("agent_cancelled iter=%d", i + 1)
                    raise AgentCancelled()

                with elapsed("iter", logger, iter=i + 1):
                    result = await self._process_stream(conversation, handler)

                    # Check for cancellation after stream (may have been
                    # cancelled mid-stream — stop emitting was handled in
                    # _process_stream, now break out of the loop)
                    if self._cancel_event.is_set():
                        logger.info("agent_cancelled after_stream iter=%d", i + 1)
                        raise AgentCancelled()

                    total_usage = total_usage + result.usage
                    if handler:
                        await handler.on_token_usage(total_usage)

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

        raise MaxIterationsExceeded(self.max_iterations)
