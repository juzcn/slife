"""OpenAI Chat Completions backend.

Takes OpenAI-format messages and tools directly — the internal format
of slife matches the OpenAI Chat Completions wire format natively.
"""

from __future__ import annotations

import asyncio
import logging
import time as _time
from collections.abc import AsyncIterator

from openai import AsyncOpenAI

from slife.config import ModelConfig
from slife.agent.llm_client import TokenUsage, StreamChunk

logger = logging.getLogger(__name__)


class OpenAIBackend:
    """OpenAI Chat Completions backend — native format, no conversion."""

    def __init__(self, model: ModelConfig):
        self.model_config = model
        self.client = AsyncOpenAI(
            api_key=model.api_key,
            base_url=model.base_url,
        )
        logger.debug(
            "openai_init model=%s provider=%s thinking=%s",
            model.api_model,
            model.provider,
            model.thinking_enabled,
        )

    # ── DeepSeek thinking ─────────────────────────────────────────────

    def _is_deepseek(self) -> bool:
        p = self.model_config.provider.lower()
        u = self.model_config.base_url.lower()
        return "deepseek" in p or "deepseek" in u

    # ── Build kwargs ──────────────────────────────────────────────────

    def _build_kwargs(
        self, messages: list[dict], tools: list[dict] | None
    ) -> dict:
        kwargs: dict = {
            "model": self.model_config.api_model,
            "messages": messages,
            "max_tokens": self.model_config.max_tokens,
            "temperature": self.model_config.temperature,
            "top_p": self.model_config.top_p,
        }
        if tools:
            kwargs["tools"] = tools
        if self.model_config.thinking_enabled:
            extra_body: dict = {"thinking": {"type": "enabled"}}
            if self.model_config.reasoning_effort:
                extra_body["reasoning_effort"] = self.model_config.reasoning_effort
            kwargs["extra_body"] = extra_body
        elif self._is_deepseek():
            # DeepSeek requires an explicit "disabled" when thinking is
            # off; most other providers simply omit the thinking field.
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        return kwargs

    # ── Usage ─────────────────────────────────────────────────────────

    @staticmethod
    def _usage_from_response(usage_obj) -> TokenUsage:
        if usage_obj:
            return TokenUsage(
                prompt_tokens=usage_obj.prompt_tokens or 0,
                completion_tokens=usage_obj.completion_tokens or 0,
                total_tokens=usage_obj.total_tokens or 0,
            )
        return TokenUsage()

    # ── Chat ──────────────────────────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> tuple:
        kwargs = self._build_kwargs(messages, tools)
        response = await self.client.chat.completions.create(**kwargs)
        usage = self._usage_from_response(
            response.usage if hasattr(response, "usage") else None
        )
        return response, usage

    # ── Streaming ─────────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        cancel_event: "asyncio.Event | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        kwargs = self._build_kwargs(messages, tools)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        t0 = _time.monotonic()
        logger.debug(
            "openai_stream_start model=%s msgs=%d tools=%d",
            self.model_config.api_model,
            len(messages),
            len(tools) if tools else 0,
        )

        stream = await self.client.chat.completions.create(**kwargs)
        try:
            async for event in stream:
                if cancel_event is not None and cancel_event.is_set():
                    logger.debug("openai_stream_cancelled")
                    await stream.close()
                    return
                # The include_usage final chunk has `choices=[]`, so the usage
                # block must come BEFORE the choices guard — otherwise the
                # OpenAI/DeepSeek/Ollama backend never emits a usage chunk and
                # context accounting collapses to the chars/3 estimate with
                # token_count=0 persisted (REVIEW NEW-H3).
                if hasattr(event, "usage") and event.usage:
                    usage = self._usage_from_response(event.usage)
                    elapsed = (_time.monotonic() - t0) * 1000
                    logger.debug(
                        "openai_stream_done prompt=%d completion=%d total=%d took_ms=%.0f",
                        usage.prompt_tokens, usage.completion_tokens,
                        usage.total_tokens, elapsed,
                    )
                    yield StreamChunk(usage=usage)
                if not event.choices:
                    continue
                delta = event.choices[0].delta

                reasoning = getattr(delta, "reasoning_content", None) or ""
                if reasoning:
                    yield StreamChunk(thinking=reasoning)
                if delta.content:
                    yield StreamChunk(content=delta.content)
                if delta.tool_calls:
                    yield StreamChunk(tool_deltas=[
                        {"index": tc.index, "id": tc.id,
                         "function": {
                             "name": tc.function.name if tc.function else None,
                             "arguments": tc.function.arguments if tc.function else "",
                         }}
                        for tc in delta.tool_calls
                    ])
        finally:
            from slife.agent.llm_client import _safe_close_stream
            await _safe_close_stream(stream)
