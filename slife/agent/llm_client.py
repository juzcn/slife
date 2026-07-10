"""LLM client wrapper for OpenAI-compatible APIs (DeepSeek & others).

Supports both batch (chat) and real-time streaming (chat_stream) modes.
"""

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from openai import AsyncOpenAI

from slife.config import ModelConfig

logger = logging.getLogger(__name__)


@dataclass
class TokenUsage:
    """Token usage from a single API response, supports accumulation."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def __add__(self, other: "TokenUsage") -> "TokenUsage":
        return TokenUsage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
            total_tokens=self.total_tokens + other.total_tokens,
        )

    def __repr__(self) -> str:
        return (
            f"TokenUsage(prompt={self.prompt_tokens}, "
            f"completion={self.completion_tokens}, "
            f"total={self.total_tokens})"
        )


@dataclass
class StreamChunk:
    """A single chunk from a streaming LLM response.

    Fields are mutually exclusive in practice — a given chunk
    carries either thinking, content, tool deltas, or usage.
    """

    thinking: str | None = None
    content: str | None = None
    tool_deltas: list[dict] | None = None
    usage: TokenUsage | None = None


class LLMClient:
    """Wrapper around AsyncOpenAI, configured from a ModelConfig.

    Supports any OpenAI-compatible provider (DeepSeek, OpenAI, etc.).
    Handles thinking mode via extra_body for DeepSeek V4 models.
    """

    def __init__(self, model: ModelConfig):
        self.model_config = model
        self.client = AsyncOpenAI(
            api_key=model.api_key,
            base_url=model.base_url,
        )
        logger.debug(
            "LLM client: %s/%s @ %s (thinking=%s max_tokens=%d)",
            model.provider,
            model.api_model,
            model.base_url,
            model.thinking_enabled,
            model.max_tokens,
        )

    def _is_deepseek(self) -> bool:
        """Check if the configured provider is DeepSeek.

        Only DeepSeek supports the 'thinking' extra_body parameter.
        Sending it to other providers (OpenAI, etc.) would be rejected.
        """
        provider = self.model_config.provider.lower()
        base_url = self.model_config.base_url.lower()
        return "deepseek" in provider or "deepseek" in base_url

    # ── Shared kwargs ──────────────────────────────────────────────

    def _build_kwargs(
        self, messages: list[dict], tools: list[dict] | None
    ) -> dict:
        """Build shared kwargs for both batch and streaming requests."""
        kwargs: dict = {
            "model": self.model_config.api_model,
            "messages": messages,
            "max_tokens": self.model_config.max_tokens,
            "temperature": self.model_config.temperature,
            "top_p": self.model_config.top_p,
        }

        if tools:
            kwargs["tools"] = tools

        # DeepSeek-specific thinking mode control
        if self._is_deepseek():
            extra_body: dict = {
                "thinking": {
                    "type": (
                        "enabled"
                        if self.model_config.thinking_enabled
                        else "disabled"
                    )
                }
            }
            if (
                self.model_config.thinking_enabled
                and self.model_config.reasoning_effort
            ):
                extra_body["reasoning_effort"] = (
                    self.model_config.reasoning_effort
                )
            kwargs["extra_body"] = extra_body

        return kwargs

    # ── Usage extraction ──────────────────────────────────────────

    @staticmethod
    def _usage_from_response(usage_obj) -> TokenUsage:
        """Extract TokenUsage from an API usage object (may be None)."""
        if usage_obj:
            return TokenUsage(
                prompt_tokens=usage_obj.prompt_tokens or 0,
                completion_tokens=usage_obj.completion_tokens or 0,
                total_tokens=usage_obj.total_tokens or 0,
            )
        return TokenUsage()

    # ── Batch (non-streaming) ─────────────────────────────────────

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> tuple:
        """Send a chat completion request (batch mode).

        Args:
            messages: OpenAI-format message list.
            tools: Optional function definitions.

        Returns:
            Tuple of (response, TokenUsage).
        """
        kwargs = self._build_kwargs(messages, tools)
        response = await self.client.chat.completions.create(**kwargs)

        usage = self._usage_from_response(
            response.usage if hasattr(response, "usage") else None
        )

        return response, usage

    # ── Streaming ─────────────────────────────────────────────────

    async def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion, yielding chunks as they arrive.

        Yields StreamChunk objects in real-time:
          - thinking: reasoning/thinking tokens (DeepSeek V4 Pro etc.)
          - content: regular text tokens
          - tool_deltas: raw tool-call deltas from the API
          - usage: TokenUsage (in the final chunk)

        Usage:
            async for chunk in client.chat_stream(messages, tools):
                if chunk.thinking:
                    await emit_thinking(chunk.thinking)
                if chunk.content:
                    await emit_text(chunk.content)
                if chunk.usage:
                    total_usage += chunk.usage
        """
        kwargs = self._build_kwargs(messages, tools)
        kwargs["stream"] = True
        kwargs["stream_options"] = {"include_usage": True}

        logger.debug(
            "Streaming: model=%s messages=%d tools=%d",
            self.model_config.api_model,
            len(messages),
            len(tools) if tools else 0,
        )

        stream = await self.client.chat.completions.create(**kwargs)

        async for event in stream:
            if not event.choices:
                continue

            delta = event.choices[0].delta

            # DeepSeek reasoning/thinking content (streaming delta)
            reasoning = getattr(delta, "reasoning_content", None) or ""
            if reasoning:
                yield StreamChunk(thinking=reasoning)

            # Regular text content
            if delta.content:
                yield StreamChunk(content=delta.content)

            # Tool call deltas (may be partial)
            if delta.tool_calls:
                raw_deltas = []
                for tc in delta.tool_calls:
                    raw_deltas.append({
                        "index": tc.index,
                        "id": tc.id,
                        "function": {
                            "name": tc.function.name if tc.function else None,
                            "arguments": (
                                tc.function.arguments
                                if tc.function
                                else ""
                            ),
                        },
                    })
                yield StreamChunk(tool_deltas=raw_deltas)

            # Usage (final chunk with stream_options.include_usage)
            if hasattr(event, "usage") and event.usage:
                usage = self._usage_from_response(event.usage)
                logger.debug("Stream done: %s", usage)
                yield StreamChunk(usage=usage)
