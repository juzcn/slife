"""Tests for LLMClient thin router, TokenUsage, and StreamChunk."""

import pytest; pytestmark = pytest.mark.unit

from slife.agent.llm_client import (
    LLMClient,
    TokenUsage,
    StreamChunk,
)


# ── TokenUsage ────────────────────────────────────────────────────────


class TestTokenUsage:
    """Tests for TokenUsage dataclass."""

    def test_default_values(self):
        tu = TokenUsage()
        assert tu.prompt_tokens == 0
        assert tu.completion_tokens == 0
        assert tu.total_tokens == 0

    def test_addition(self):
        tu1 = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        tu2 = TokenUsage(prompt_tokens=200, completion_tokens=30, total_tokens=230)
        result = tu1 + tu2
        assert result.prompt_tokens == 300
        assert result.completion_tokens == 80
        assert result.total_tokens == 380

    def test_add_zero_usage(self):
        tu = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        zero = TokenUsage()
        result = tu + zero
        assert result.prompt_tokens == 100
        assert result.completion_tokens == 50
        assert result.total_tokens == 150

    def test_repr(self):
        tu = TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
        r = repr(tu)
        assert "prompt=10" in r
        assert "completion=20" in r
        assert "total=30" in r


# ── StreamChunk ───────────────────────────────────────────────────────


class TestStreamChunk:
    """Tests for StreamChunk dataclass."""

    def test_default_all_none(self):
        sc = StreamChunk()
        assert sc.thinking is None
        assert sc.content is None
        assert sc.tool_deltas is None
        assert sc.usage is None

    def test_thinking_chunk(self):
        sc = StreamChunk(thinking="Hmm, let me think...")
        assert sc.thinking == "Hmm, let me think..."
        assert sc.content is None

    def test_content_chunk(self):
        sc = StreamChunk(content="Hello!")
        assert sc.content == "Hello!"

    def test_tool_deltas_chunk(self):
        deltas = [{"index": 0, "id": "call_1"}]
        sc = StreamChunk(tool_deltas=deltas)
        assert sc.tool_deltas == deltas

    def test_usage_chunk(self):
        usage = TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8)
        sc = StreamChunk(usage=usage)
        assert sc.usage == usage


# ── LLMClient ─────────────────────────────────────────────────────────


class TestLLMClient:
    """Tests for LLMClient — thin router dispatching to backends."""

    def test_construction_openai_completions(self, sample_model_config):
        client = LLMClient(sample_model_config)
        assert client.model_config == sample_model_config
        from slife.agent.llm_backends.openai import OpenAIBackend
        assert isinstance(client._backend, OpenAIBackend)

    def test_construction_anthropic_messages(self):
        from slife.config import ModelConfig
        mc = ModelConfig(
            ref="anthropic/claude-sonnet",
            provider="anthropic",
            api_model="claude-sonnet-4-20250514",
            display_name="Claude",
            api_key="sk-test",
            base_url="https://api.anthropic.com",
            api="anthropic-messages",
        )
        client = LLMClient(mc)
        from slife.agent.llm_backends.anthropic import AnthropicBackend
        assert isinstance(client._backend, AnthropicBackend)

    def test_construction_openai_responses(self):
        from slife.config import ModelConfig
        mc = ModelConfig(
            ref="openai/gpt-4o",
            provider="openai",
            api_model="gpt-4o",
            display_name="GPT-4o",
            api_key="sk-test",
            api="openai-responses",
        )
        client = LLMClient(mc)
        from slife.agent.llm_backends.openai_responses import OpenAIResponsesBackend
        assert isinstance(client._backend, OpenAIResponsesBackend)

    def test_default_api_is_openai_completions(self):
        from slife.config import ModelConfig
        mc = ModelConfig(
            ref="custom/v1",
            provider="custom",
            api_model="v1",
            display_name="C",
            api_key="key",
        )
        client = LLMClient(mc)
        from slife.agent.llm_backends.openai import OpenAIBackend
        assert isinstance(client._backend, OpenAIBackend)

    def test_delegates_chat(self, sample_model_config):
        client = LLMClient(sample_model_config)
        assert callable(client.chat)
        assert callable(client.chat_stream)


