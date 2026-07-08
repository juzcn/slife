"""LLM client wrapper for OpenAI-compatible APIs (DeepSeek & others)."""

from dataclasses import dataclass

from openai import AsyncOpenAI

from slife.config import ModelConfig


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

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        stream: bool = False,
    ) -> tuple:
        """Send a chat completion request.

        Args:
            messages: OpenAI-format message list.
            tools: Optional function definitions.
            stream: If True, return a streaming response.

        Returns:
            Tuple of (response, TokenUsage).
            response is the ChatCompletion or AsyncStream object.
        """
        kwargs: dict = {
            "model": self.model_config.api_model,
            "messages": messages,
            "max_tokens": self.model_config.max_tokens,
            "temperature": self.model_config.temperature,
            "top_p": self.model_config.top_p,
        }

        if tools:
            kwargs["tools"] = tools

        if stream:
            kwargs["stream"] = True
            kwargs["stream_options"] = {"include_usage": True}

        # DeepSeek V4 thinking mode control
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

        response = await self.client.chat.completions.create(**kwargs)

        # Extract token usage
        usage = TokenUsage()
        if not stream and hasattr(response, "usage") and response.usage:
            usage = TokenUsage(
                prompt_tokens=response.usage.prompt_tokens or 0,
                completion_tokens=response.usage.completion_tokens or 0,
                total_tokens=response.usage.total_tokens or 0,
            )

        return response, usage
