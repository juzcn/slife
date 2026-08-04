"""Anthropic Messages API backend.

Takes OpenAI-format messages (slife's internal format) and internally
adapts them for the Anthropic Messages API.  No public conversion layer —
the adaptation is a private implementation detail of chat/chat_stream.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time as _time
from collections.abc import AsyncIterator

from anthropic import AsyncAnthropic

from slife.agent.llm_client import StreamChunk, TokenUsage
from slife.config import ModelConfig

logger = logging.getLogger(__name__)


class AnthropicBackend:
    """Anthropic Messages API backend."""

    def __init__(self, model: ModelConfig):
        self.model_config = model
        self.client = AsyncAnthropic(
            api_key=model.api_key,
            base_url=model.base_url or "https://api.anthropic.com",
        )
        logger.debug(
            "anthropic_init model=%s provider=%s thinking=%s",
            model.api_model, model.provider, model.thinking_enabled,
        )

    # ── Internal adaptation (private) ─────────────────────────────────

    @staticmethod
    def _oa_msgs_to_anthropic(
        messages: list[dict],
    ) -> tuple[str | None, list[dict]]:
        """Adapt OpenAI-format messages → Anthropic Messages format.

        Returns (system_prompt, anthropic_messages).
        """
        system: str | None = None
        converted: list[dict] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "system":
                s = str(content)
                system = (system + "\n\n" + s) if system else s
            elif role == "user":
                if isinstance(content, list):
                    blocks = []
                    for part in content:
                        if part.get("type") == "image_url":
                            url = part.get("image_url", {}).get("url", "")
                            header, b64 = url.split(",", 1)
                            mime = header.split(":")[1].split(";")[0]
                            blocks.append({
                                "type": "image",
                                "source": {"type": "base64", "media_type": mime, "data": b64},
                            })
                        else:
                            blocks.append(part)
                    converted.append({"role": "user", "content": blocks})
                else:
                    converted.append({"role": "user", "content": str(content)})
            elif role == "assistant":
                blocks: list[dict] = []
                if content and str(content).strip():
                    blocks.append({"type": "text", "text": str(content)})
                for tc in (msg.get("tool_calls") or []):
                    fn = tc.get("function", {})
                    try:
                        inp = json.loads(fn.get("arguments", "{}"))
                    except (json.JSONDecodeError, TypeError):
                        inp = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc["id"],
                        "name": fn.get("name", ""),
                        "input": inp,
                    })
                converted.append({"role": "assistant", "content": blocks})
            elif role == "tool":
                converted.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": str(content),
                    }],
                })
        return system, converted

    @staticmethod
    def _oa_tools_to_anthropic(tools: list[dict]) -> list[dict]:
        """Adapt OpenAI function defs → Anthropic tool format."""
        result = []
        for t in tools:
            fn = t.get("function", {})
            schema = dict(fn.get("parameters", {}))
            schema.setdefault("type", "object")
            props = schema.get("properties", {})
            props.pop("_timeout", None)
            props.pop("_async", None)
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": schema,
            })
        return result

    # ── Build kwargs ──────────────────────────────────────────────────

    def _build_kwargs(
        self, messages: list[dict], tools: list[dict] | None
    ) -> dict:
        system, msgs = self._oa_msgs_to_anthropic(messages)
        kwargs: dict = {
            "model": self.model_config.api_model,
            "messages": msgs,
            "max_tokens": self.model_config.max_tokens,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = self._oa_tools_to_anthropic(tools)
        if self.model_config.temperature:
            kwargs["temperature"] = self.model_config.temperature
        if self.model_config.top_p:
            kwargs["top_p"] = self.model_config.top_p
        if self.model_config.thinking_enabled:
            compat = self.model_config.compat or {}
            # Bailian / qwen models with thinkingFormat "openai" always
            # think — no explicit Anthropic thinking param is needed (and
            # sending one may cause errors).
            if compat.get("thinkingFormat") != "openai":
                kwargs["thinking"] = {
                    "type": "enabled",
                    "budget_tokens": max(self.model_config.max_tokens // 2, 1024),
                }
        return kwargs

    # ── Chat ──────────────────────────────────────────────────────────

    async def chat(
        self, messages: list[dict], tools: list[dict] | None = None,
    ) -> tuple:
        kwargs = self._build_kwargs(messages, tools)
        response = await self.client.messages.create(**kwargs)
        text = "".join(
            b.text for b in response.content
            if hasattr(b, "type") and b.type == "text"
        )
        usage = TokenUsage(
            prompt_tokens=response.usage.input_tokens or 0,
            completion_tokens=response.usage.output_tokens or 0,
            total_tokens=(response.usage.input_tokens or 0) + (response.usage.output_tokens or 0),
        )
        # Minimal compat shape for callers
        C = type("Choice", (), {"message": type("Msg", (), {"content": text})()})()
        R = type("Response", (), {"choices": [C], "usage": response.usage})()
        return R, usage

    # ── Streaming ─────────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        cancel_event: asyncio.Event | None = None,
    ) -> AsyncIterator[StreamChunk]:
        kwargs = self._build_kwargs(messages, tools)
        t0 = _time.monotonic()
        tool_accum: dict[int, dict] = {}
        usage = TokenUsage()

        logger.debug(
            "anthropic_stream_start model=%s msgs=%d tools=%d",
            self.model_config.api_model, len(messages),
            len(tools) if tools else 0,
        )

        async with self.client.messages.stream(**kwargs) as stream:
            async for event in stream:
                if cancel_event is not None and cancel_event.is_set():
                    logger.debug("anthropic_stream_cancelled")
                    break
                etype = getattr(event, "type", None)

                if etype == "message_start":
                    msg = getattr(event, "message", None)
                    if msg and msg.usage:
                        usage.prompt_tokens = msg.usage.input_tokens or 0

                elif etype == "content_block_start":
                    block = getattr(event, "content_block", None)
                    if block and getattr(block, "type", None) == "tool_use":
                        idx = getattr(event, "index", 0)
                        tool_accum[idx] = {
                            "id": getattr(block, "id", ""),
                            "name": getattr(block, "name", ""),
                            "arguments": "",
                        }

                elif etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta is None:
                        continue
                    idx = getattr(event, "index", 0)
                    dtype = getattr(delta, "type", None)

                    if dtype == "text_delta":
                        yield StreamChunk(content=delta.text)
                    elif dtype == "thinking_delta":
                        yield StreamChunk(thinking=delta.thinking)
                    elif dtype == "input_json_delta":
                        if idx in tool_accum:
                            tool_accum[idx]["arguments"] += delta.partial_json
                        acc = tool_accum.get(idx, {})
                        yield StreamChunk(tool_deltas=[{
                            "index": idx,
                            "id": acc.get("id", ""),
                            "function": {
                                "name": acc.get("name", ""),
                                "arguments": delta.partial_json,
                            },
                        }])

                elif etype == "message_delta":
                    u = getattr(event, "usage", None)
                    if u:
                        usage.completion_tokens = u.output_tokens or 0
                        usage.total_tokens = usage.prompt_tokens + usage.completion_tokens
                        elapsed = (_time.monotonic() - t0) * 1000
                        logger.debug(
                            "anthropic_stream_done prompt=%d completion=%d total=%d took_ms=%.0f",
                            usage.prompt_tokens, usage.completion_tokens,
                            usage.total_tokens, elapsed,
                        )
                        yield StreamChunk(usage=usage)
