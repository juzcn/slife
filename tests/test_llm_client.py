"""Tests for LLM client (slife.agent.llm_client)."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.agent.llm_client import LLMClient, TokenUsage, StreamChunk
from slife.config import ModelConfig


# ══════════════════════════════════════════════════════════════════════
# TokenUsage
# ══════════════════════════════════════════════════════════════════════


class TestTokenUsage:
    """Tests for TokenUsage dataclass."""

    def test_default_values(self):
        """Default values are zero."""
        usage = TokenUsage()
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0
        assert usage.total_tokens == 0

    def test_explicit_values(self):
        """Values set via constructor."""
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        assert usage.prompt_tokens == 100
        assert usage.completion_tokens == 50
        assert usage.total_tokens == 150

    def test_add_two_usages(self, small_usage, large_usage):
        """Adding two TokenUsage objects sums all fields."""
        result = small_usage + large_usage
        assert result.prompt_tokens == 1100
        assert result.completion_tokens == 550
        assert result.total_tokens == 1650

    def test_add_with_zero(self, small_usage, zero_usage):
        """Adding zero usage returns same values."""
        result = small_usage + zero_usage
        assert result.prompt_tokens == small_usage.prompt_tokens
        assert result.completion_tokens == small_usage.completion_tokens
        assert result.total_tokens == small_usage.total_tokens

    def test_add_chain(self):
        """Chained addition works."""
        a = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        b = TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30)
        c = TokenUsage(prompt_tokens=30, completion_tokens=15, total_tokens=45)
        result = a + b + c
        assert result.prompt_tokens == 60
        assert result.completion_tokens == 30
        assert result.total_tokens == 90

    def test_repr(self):
        """String representation includes all fields."""
        usage = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3)
        r = repr(usage)
        assert "prompt=1" in r
        assert "completion=2" in r
        assert "total=3" in r


# ══════════════════════════════════════════════════════════════════════
# StreamChunk
# ══════════════════════════════════════════════════════════════════════


class TestStreamChunk:
    """Tests for StreamChunk dataclass."""

    def test_empty_chunk(self):
        """Default chunk has all None fields."""
        chunk = StreamChunk()
        assert chunk.thinking is None
        assert chunk.content is None
        assert chunk.tool_deltas is None
        assert chunk.usage is None

    def test_thinking_chunk(self):
        """Thinking field is set."""
        chunk = StreamChunk(thinking="Hmm, let me think...")
        assert chunk.thinking == "Hmm, let me think..."
        assert chunk.content is None

    def test_content_chunk(self):
        """Content field is set."""
        chunk = StreamChunk(content="Hello!")
        assert chunk.content == "Hello!"
        assert chunk.thinking is None

    def test_tool_deltas_chunk(self):
        """Tool deltas field is set."""
        deltas = [{"index": 0, "id": "abc"}]
        chunk = StreamChunk(tool_deltas=deltas)
        assert chunk.tool_deltas == deltas

    def test_usage_chunk(self):
        """Usage field is set."""
        usage = TokenUsage(prompt_tokens=5, completion_tokens=3, total_tokens=8)
        chunk = StreamChunk(usage=usage)
        assert chunk.usage == usage


# ══════════════════════════════════════════════════════════════════════
# LLMClient
# ══════════════════════════════════════════════════════════════════════


def _make_model_config(**overrides) -> ModelConfig:
    """Helper to create a ModelConfig for testing."""
    defaults = {
        "ref": "test/test-model",
        "provider": "test",
        "api_model": "test-model",
        "display_name": "Test Model",
        "api_key": "sk-test",
        "base_url": "https://api.test.com",
        "api": "openai-completions",
        "supports_vision": False,
        "max_tokens": 4096,
        "context_window": 131072,
        "temperature": 0.7,
        "top_p": 1.0,
        "thinking_enabled": False,
        "reasoning_effort": None,
    }
    defaults.update(overrides)
    return ModelConfig(**defaults)


class TestLLMClientInit:
    """Tests for LLMClient.__init__()."""

    def test_creates_async_openai_client(self):
        """LLMClient creates an AsyncOpenAI instance."""
        model = _make_model_config()
        client = LLMClient(model)
        assert client.model_config == model
        # Client is created (just check it exists)
        assert client.client is not None

    def test_stores_model_config(self):
        """Model config is accessible."""
        model = _make_model_config(api_model="gpt-5")
        client = LLMClient(model)
        assert client.model_config.api_model == "gpt-5"


class TestIsDeepseek:
    """Tests for LLMClient._is_deepseek()."""

    def test_deepseek_provider_returns_true(self):
        """Provider containing 'deepseek' returns True."""
        model = _make_model_config(provider="DeepSeek")
        client = LLMClient(model)
        assert client._is_deepseek() is True

    def test_deepseek_in_base_url_returns_true(self):
        """Base URL containing 'deepseek' returns True."""
        model = _make_model_config(
            provider="other",
            base_url="https://api.deepseek.com/v1",
        )
        client = LLMClient(model)
        assert client._is_deepseek() is True

    def test_non_deepseek_returns_false(self):
        """OpenAI provider returns False."""
        model = _make_model_config(
            provider="openai",
            base_url="https://api.openai.com/v1",
        )
        client = LLMClient(model)
        assert client._is_deepseek() is False

    def test_case_insensitive(self):
        """Check is case-insensitive."""
        model = _make_model_config(provider="DEEPSEEK")
        client = LLMClient(model)
        assert client._is_deepseek() is True


class TestBuildKwargs:
    """Tests for LLMClient._build_kwargs()."""

    def test_basic_kwargs(self):
        """Basic kwargs include model, messages, and generation params."""
        model = _make_model_config()
        client = LLMClient(model)
        kwargs = client._build_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )
        assert kwargs["model"] == "test-model"
        assert kwargs["messages"] == [{"role": "user", "content": "hi"}]
        assert kwargs["max_tokens"] == 4096
        assert kwargs["temperature"] == 0.7
        assert kwargs["top_p"] == 1.0
        assert "tools" not in kwargs

    def test_with_tools(self):
        """When tools are provided, they are included."""
        model = _make_model_config()
        client = LLMClient(model)
        tools_def = [{"type": "function", "function": {"name": "test"}}]
        kwargs = client._build_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            tools=tools_def,
        )
        assert kwargs["tools"] == tools_def

    def test_empty_tools_list_not_included(self):
        """Empty tools list is not added to kwargs."""
        model = _make_model_config()
        client = LLMClient(model)
        kwargs = client._build_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        )
        # Empty list is falsy, so tools key not added
        assert "tools" not in kwargs

    def test_deepseek_thinking_enabled(self):
        """DeepSeek with thinking enabled adds extra_body."""
        model = _make_model_config(
            provider="deepseek",
            thinking_enabled=True,
        )
        client = LLMClient(model)
        kwargs = client._build_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )
        assert "extra_body" in kwargs
        assert kwargs["extra_body"]["thinking"]["type"] == "enabled"
        assert "reasoning_effort" not in kwargs["extra_body"]

    def test_deepseek_thinking_disabled(self):
        """DeepSeek with thinking disabled still adds extra_body with disabled."""
        model = _make_model_config(
            provider="deepseek",
            thinking_enabled=False,
        )
        client = LLMClient(model)
        kwargs = client._build_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )
        assert "extra_body" in kwargs
        assert kwargs["extra_body"]["thinking"]["type"] == "disabled"

    def test_deepseek_with_reasoning_effort(self):
        """DeepSeek with thinking and reasoning_effort set."""
        model = _make_model_config(
            provider="deepseek",
            thinking_enabled=True,
            reasoning_effort="high",
        )
        client = LLMClient(model)
        kwargs = client._build_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )
        assert kwargs["extra_body"]["reasoning_effort"] == "high"

    def test_deepseek_reasoning_effort_ignored_if_thinking_off(self):
        """reasoning_effort is only added when thinking_enabled."""
        model = _make_model_config(
            provider="deepseek",
            thinking_enabled=False,
            reasoning_effort="high",
        )
        client = LLMClient(model)
        kwargs = client._build_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )
        assert "reasoning_effort" not in kwargs["extra_body"]

    def test_non_deepseek_no_extra_body(self):
        """Non-DeepSeek providers don't get extra_body."""
        model = _make_model_config(
            provider="openai",
            thinking_enabled=True,
        )
        client = LLMClient(model)
        kwargs = client._build_kwargs(
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
        )
        assert "extra_body" not in kwargs


class TestChat:
    """Tests for LLMClient.chat()."""

    @pytest.mark.asyncio
    async def test_chat_returns_response_and_usage(self):
        """chat() returns (response, TokenUsage) tuple."""
        model = _make_model_config(provider="openai")

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            # Set up mock response
            mock_response = MagicMock()
            mock_response.usage = MagicMock()
            mock_response.usage.prompt_tokens = 10
            mock_response.usage.completion_tokens = 5
            mock_response.usage.total_tokens = 15
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            client = LLMClient(model)
            response, usage = await client.chat(
                messages=[{"role": "user", "content": "hi"}],
            )

            assert response == mock_response
            assert usage.prompt_tokens == 10
            assert usage.completion_tokens == 5
            assert usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_chat_no_usage(self):
        """chat() handles responses without usage info."""
        model = _make_model_config(provider="openai")

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            mock_response = MagicMock(spec=[])  # no 'usage' attr
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            client = LLMClient(model)
            response, usage = await client.chat(
                messages=[{"role": "user", "content": "hi"}],
            )

            assert usage.prompt_tokens == 0
            assert usage.completion_tokens == 0
            assert usage.total_tokens == 0

    @pytest.mark.asyncio
    async def test_chat_passes_tools(self):
        """chat() passes tools to the API."""
        model = _make_model_config(provider="openai")

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            mock_response = MagicMock()
            mock_response.usage = None
            mock_client.chat.completions.create = AsyncMock(return_value=mock_response)

            tools = [{"type": "function", "function": {"name": "test_tool"}}]
            client = LLMClient(model)
            await client.chat(
                messages=[{"role": "user", "content": "hi"}],
                tools=tools,
            )

            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            assert "tools" in call_kwargs
            assert call_kwargs["tools"] == tools


class TestChatStream:
    """Tests for LLMClient.chat_stream()."""

    def _make_mock_event(self, content=None, reasoning=None, tool_calls=None, usage=None):
        """Helper to create a mock streaming event."""
        event = MagicMock()
        event.choices = [MagicMock()]
        delta = MagicMock()
        delta.content = content
        delta.reasoning_content = reasoning
        delta.tool_calls = tool_calls

        if usage:
            event.usage = MagicMock()
            event.usage.prompt_tokens = usage.get("prompt_tokens", 0)
            event.usage.completion_tokens = usage.get("completion_tokens", 0)
            event.usage.total_tokens = usage.get("total_tokens", 0)
        else:
            del event.usage

        event.choices[0].delta = delta
        return event

    @pytest.mark.asyncio
    async def test_chat_stream_yields_content(self):
        """Stream yields content chunks."""
        model = _make_model_config(provider="openai")

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            async def mock_stream():
                yield self._make_mock_event(content="Hello")
                yield self._make_mock_event(content=" world")
                yield self._make_mock_event(
                    usage={"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
                )

            mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

            client = LLMClient(model)
            chunks = []
            async for chunk in client.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
            ):
                chunks.append(chunk)

            assert len(chunks) == 3
            assert chunks[0].content == "Hello"
            assert chunks[1].content == " world"
            assert chunks[2].usage is not None
            assert chunks[2].usage.total_tokens == 12

    @pytest.mark.asyncio
    async def test_chat_stream_yields_thinking(self):
        """Stream yields thinking chunks for DeepSeek reasoning."""
        model = _make_model_config(provider="deepseek", thinking_enabled=True)

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            async def mock_stream():
                yield self._make_mock_event(reasoning="Let me think...")
                yield self._make_mock_event(content="Answer")
                yield self._make_mock_event(
                    usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
                )

            mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

            client = LLMClient(model)
            chunks = []
            async for chunk in client.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
            ):
                chunks.append(chunk)

            assert chunks[0].thinking == "Let me think..."
            assert chunks[1].content == "Answer"

    @pytest.mark.asyncio
    async def test_chat_stream_yields_tool_deltas(self):
        """Stream yields tool call deltas."""
        model = _make_model_config(provider="openai")

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            # Build mock tool calls
            tc = MagicMock()
            tc.index = 0
            tc.id = "call_123"
            tc.function = MagicMock()
            tc.function.name = "search"
            tc.function.arguments = '{"query":"cats"}'

            async def mock_stream():
                yield self._make_mock_event(tool_calls=[tc])
                yield self._make_mock_event(
                    usage={"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}
                )

            mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

            client = LLMClient(model)
            chunks = []
            async for chunk in client.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
            ):
                chunks.append(chunk)

            assert len(chunks) == 2
            assert chunks[0].tool_deltas is not None
            assert len(chunks[0].tool_deltas) == 1
            assert chunks[0].tool_deltas[0]["index"] == 0
            assert chunks[0].tool_deltas[0]["id"] == "call_123"
            assert chunks[0].tool_deltas[0]["function"]["name"] == "search"

    @pytest.mark.asyncio
    async def test_chat_stream_no_choices_skipped(self):
        """Events without choices are skipped."""
        model = _make_model_config(provider="openai")

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            async def mock_stream():
                # Empty event (no choices)
                event = MagicMock()
                event.choices = []
                yield event

                yield self._make_mock_event(content="valid")
                yield self._make_mock_event(
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                )

            mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

            client = LLMClient(model)
            chunks = []
            async for chunk in client.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
            ):
                chunks.append(chunk)

            # The empty event is skipped
            assert len(chunks) == 2
            assert chunks[0].content == "valid"

    @pytest.mark.asyncio
    async def test_chat_stream_includes_stream_options(self):
        """Stream request includes stream_options for usage."""
        model = _make_model_config(provider="openai")

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            async def mock_stream():
                yield self._make_mock_event(content="ok")
                yield self._make_mock_event(
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                )

            mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

            client = LLMClient(model)
            async for chunk in client.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
            ):
                pass

            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            assert call_kwargs["stream"] is True
            assert call_kwargs["stream_options"] == {"include_usage": True}

    @pytest.mark.asyncio
    async def test_chat_stream_tool_delta_null_function(self):
        """Tool call delta with null function is handled."""
        model = _make_model_config(provider="openai")

        with patch("slife.agent.llm_client.AsyncOpenAI") as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            tc = MagicMock()
            tc.index = 0
            tc.id = "call_abc"
            tc.function = None  # null function

            async def mock_stream():
                yield self._make_mock_event(tool_calls=[tc])
                yield self._make_mock_event(
                    usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
                )

            mock_client.chat.completions.create = AsyncMock(return_value=mock_stream())

            client = LLMClient(model)
            chunks = []
            async for chunk in client.chat_stream(
                messages=[{"role": "user", "content": "hi"}],
            ):
                chunks.append(chunk)

            assert chunks[0].tool_deltas[0]["function"]["name"] is None
            assert chunks[0].tool_deltas[0]["function"]["arguments"] == ""
