"""LLM client — thin router dispatching to one of three API backends.

Each API is a first-class citizen with its own backend class::

    openai-completions → OpenAIBackend   (OpenAI, DeepSeek, Ollama, …)
    anthropic-messages → AnthropicBackend (Claude, Bailian/Qwen, …)
    openai-responses   → OpenAIResponsesBackend (newer OpenAI Responses API)
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

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
    """A single chunk from a streaming LLM response — backend-agnostic.

    Fields are mutually exclusive in practice — a given chunk
    carries either thinking, content, tool deltas, or usage.
    """

    thinking: str | None = None
    content: str | None = None
    tool_deltas: list[dict] | None = None
    usage: TokenUsage | None = None


class LLMClient:
    """Thin router — delegates to the backend matching ``ModelConfig.api``.

    Three backends, equal citizens::

        "openai-completions" → OpenAIBackend
        "anthropic-messages" → AnthropicBackend
        "openai-responses"   → OpenAIResponsesBackend
    """

    def __init__(self, model: ModelConfig):
        self.model_config = model
        api = model.api

        if api == "anthropic-messages":
            from slife.agent.llm_backends.anthropic import AnthropicBackend
            self._backend = AnthropicBackend(model)
        elif api == "openai-responses":
            from slife.agent.llm_backends.openai_responses import OpenAIResponsesBackend
            self._backend = OpenAIResponsesBackend(model)
        else:
            from slife.agent.llm_backends.openai import OpenAIBackend
            self._backend = OpenAIBackend(model)

        logger.debug(
            "llm_client_init model=%s api=%s backend=%s",
            model.api_model,
            api,
            type(self._backend).__name__,
        )

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> tuple:
        """Send a chat request (batch mode)."""
        return await self._backend.chat(messages, tools)

    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        cancel_event: "asyncio.Event | None" = None,
    ) -> AsyncIterator[StreamChunk]:
        """Stream a chat completion, yielding ``StreamChunk`` objects."""
        return self._backend.chat_stream(messages, tools, cancel_event)
