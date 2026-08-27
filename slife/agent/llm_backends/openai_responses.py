"""OpenAI Responses API backend.

Takes OpenAI-format messages (slife's internal format) and internally
adapts them for the OpenAI Responses API.  No public conversion layer.
"""

from __future__ import annotations

import logging
import time as _time
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

from openai import AsyncOpenAI

from slife.config import ModelConfig
from slife.agent.llm_client import (
    TokenUsage,
    StreamChunk,
    _chat_response,
    _safe_close_stream,
)

if TYPE_CHECKING:
    import asyncio

logger = logging.getLogger(__name__)


class OpenAIResponsesBackend:
    """OpenAI Responses API backend."""

    def __init__(self, model: ModelConfig):
        self.model_config = model
        self.client = AsyncOpenAI(
            api_key=model.api_key,
            base_url=model.base_url,
        )
        logger.debug(
            "responses_init model=%s provider=%s",
            model.api_model, model.provider,
        )

    # ── Internal adaptation (private) ─────────────────────────────────

    @staticmethod
    def _oa_msgs_to_responses(
        messages: list[dict],
    ) -> tuple[str | None, list[dict]]:
        """Adapt OpenAI-format messages → Responses API ``input`` format.

        Returns (instructions, input_items).

        The Responses API ``input`` is a flat list of items; a tool turn
        uses the API's native item shapes , NOT the
        Chat-Completions ``role:"tool"`` / ``tool_calls``-on-assistant
        shape:
          - assistant text → ``{"role": "assistant", "content": ...}``
          - tool calls     → standalone ``{"type": "function_call", ...}``
          - tool results   → ``{"type": "function_call_output", ...}``
        """
        instructions: str | None = None
        converted: list[dict] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                s = str(content)
                instructions = (instructions + "\n\n" + s) if instructions else s
            elif role == "user":
                if isinstance(content, list):
                    blocks: list[dict] = []
                    for part in content:
                        if part.get("type") == "image_url":
                            blocks.append({
                                "type": "input_image",
                                "image_url": part.get("image_url", {}).get("url", ""),
                            })
                        elif part.get("type") == "text":
                            blocks.append({"type": "input_text", "text": part.get("text", "")})
                        else:
                            blocks.append(part)
                    converted.append({"role": "user", "content": blocks})
                else:
                    converted.append({"role": "user", "content": str(content)})
            elif role == "assistant":
                if content and str(content).strip():
                    converted.append({"role": "assistant", "content": content})
                # Each tool call becomes its own top-level function_call item —
                # assistant items in the Responses API carry no ``tool_calls``.
                for tc in msg.get("tool_calls", []):
                    converted.append({
                        "type": "function_call",
                        "call_id": tc.get("id", ""),
                        "name": tc.get("function", {}).get("name", ""),
                        "arguments": tc.get("function", {}).get("arguments", "{}"),
                    })
            elif role == "tool":
                converted.append({
                    "type": "function_call_output",
                    "call_id": msg.get("tool_call_id", ""),
                    "output": str(content),
                })
        return instructions, converted

    @staticmethod
    def _oa_tools_to_responses(tools: list[dict]) -> list[dict]:
        """Adapt OpenAI function defs → Responses API tool format."""
        result = []
        for t in tools:
            fn = t.get("function", {})
            schema = dict(fn.get("parameters", {}))
            schema.setdefault("type", "object")
            schema.setdefault("properties", {})
            schema.setdefault("required", [])
            # Keep harness meta-params (_timeout/_async/_approve): the three
            # backends expose the same tool schemas to the model, so the LLM
            # can drive timeouts/async/approval uniformly on every API.
            result.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": schema,
            })
        return result

    # ── Build kwargs ──────────────────────────────────────────────────

    def _build_kwargs(
        self, messages: list[dict], tools: list[dict] | None
    ) -> dict:
        instructions, input_items = self._oa_msgs_to_responses(messages)
        kwargs: dict = {
            "model": self.model_config.api_model,
            "input": input_items,
            "max_output_tokens": self.model_config.max_tokens,
            "temperature": self.model_config.temperature,
            "top_p": self.model_config.top_p,
        }
        if instructions:
            kwargs["instructions"] = instructions
        if tools:
            kwargs["tools"] = self._oa_tools_to_responses(tools)
        if self.model_config.thinking_enabled:
            kwargs["reasoning"] = {
                "effort": self.model_config.reasoning_effort or "medium",
            }
        return kwargs

    # ── Chat ──────────────────────────────────────────────────────────

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None,
    ) -> tuple:
        kwargs = self._build_kwargs(messages, tools)
        response = await self.client.responses.create(**kwargs)

        text = ""
        output = getattr(response, "output", [])
        for item in output:
            if hasattr(item, "type") and item.type == "message":
                for block in getattr(item, "content", []):
                    if hasattr(block, "type") and block.type == "output_text":
                        text += getattr(block, "text", "")

        usage = TokenUsage()
        if hasattr(response, "usage") and response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.input_tokens or 0,
                completion_tokens=response.usage.output_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )

        return _chat_response(text, usage)

    # ── Streaming ─────────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        cancel_event: "asyncio.Event | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        kwargs = self._build_kwargs(messages, tools)
        kwargs["stream"] = True
        t0 = _time.monotonic()
        usage = TokenUsage()

        logger.debug(
            "responses_stream_start model=%s msgs=%d tools=%d",
            self.model_config.api_model, len(messages),
            len(tools) if tools else 0,
        )

        stream = await self.client.responses.create(**kwargs)
        # Function-call metadata keyed by item_id (fc_…): the name and the
        # call_id only arrive on response.output_item.{added,done} — the
        # function_call_arguments.delta event carries neither.
        _tool_meta: dict[str, dict] = {}
        _tool_index: dict[str, int] = {}
        _next_index = 0
        try:
            async for event in stream:
                if cancel_event is not None and cancel_event.is_set():
                    logger.debug("responses_stream_cancelled")
                    await stream.close()
                    return

                etype = getattr(event, "type", None)

                if etype == "response.output_text.delta":
                    yield StreamChunk(content=getattr(event, "delta", ""))
                elif etype in (
                    "response.reasoning_text.delta",
                    "response.reasoning_summary_text.delta",
                ):
                    yield StreamChunk(thinking=getattr(event, "delta", ""))
                elif etype in (
                    "response.output_item.added",
                    "response.output_item.done",
                ):
                    item = getattr(event, "item", None)
                    if (
                        item is not None
                        and getattr(item, "type", None) == "function_call"
                    ):
                        # Record name + call_id; argument deltas for this
                        # item reference it by item_id.
                        item_id = getattr(item, "id", "")
                        meta = _tool_meta.get(item_id, {})
                        meta.setdefault("name", getattr(item, "name", ""))
                        meta.setdefault("call_id", getattr(item, "call_id", ""))
                        _tool_meta[item_id] = meta
                        # A function call that never emits an arguments.delta
                        # (empty/auto-filled args) would otherwise be dropped —
                        # emit it with empty arguments when its item completes
                        # without having been indexed.
                        if (
                            etype == "response.output_item.done"
                            and item_id not in _tool_index
                        ):
                            _tool_index[item_id] = _next_index
                            _next_index += 1
                            yield StreamChunk(tool_deltas=[{
                                "index": _tool_index[item_id],
                                "id": meta.get("call_id") or item_id,
                                "function": {
                                    "name": meta.get("name", ""),
                                    "arguments": "",
                                },
                            }])
                elif etype == "response.function_call_arguments.delta":
                    item_id = getattr(event, "item_id", "")
                    if item_id not in _tool_index:
                        # Deterministic per-batch index (not a hash) so two
                        # parallel tool calls can never collide.
                        _tool_index[item_id] = _next_index
                        _next_index += 1
                    meta = _tool_meta.get(item_id, {})
                    yield StreamChunk(tool_deltas=[{
                        "index": _tool_index[item_id],
                        # Responses correlates tool results by call_id, not
                        # by the item id.
                        "id": meta.get("call_id") or item_id,
                        "function": {
                            "name": meta.get("name", ""),
                            "arguments": getattr(event, "delta", ""),
                        },
                    }])
                elif etype in ("response.completed", "response.done"):
                    resp = getattr(event, "response", None)
                    if resp and hasattr(resp, "usage") and resp.usage:
                        usage = TokenUsage(
                            prompt_tokens=resp.usage.input_tokens or 0,
                            completion_tokens=resp.usage.output_tokens or 0,
                            total_tokens=resp.usage.total_tokens or 0,
                        )
                        elapsed = (_time.monotonic() - t0) * 1000
                        logger.debug(
                            "responses_stream_done prompt=%d completion=%d total=%d took_ms=%.0f",
                            usage.prompt_tokens, usage.completion_tokens,
                            usage.total_tokens, elapsed,
                        )
                        yield StreamChunk(usage=usage)
        finally:
            await _safe_close_stream(stream)
