"""Tests for Slife.agent.llm_client — LLM client, token usage, and streaming."""

import pytest
from unittest.mock import AsyncMock, MagicMock

from slife.agent.llm_client import (
    LLMClient,
    TokenUsage,
    StreamChunk,
)
from slife.config import ModelConfig
from tests.conftest import (
    _MockStreamEvent,
    _MockDelta,
    _MockUsage,
    _MockToolCallDelta,
    _MockFunctionDelta,
    make_async_iter,
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
    """Tests for LLMClient."""

    def test_construction(self, sample_model_config):
        client = LLMClient(sample_model_config)
        assert client.model_config == sample_model_config
        assert client.client is not None

    def test_is_deepseek_true(self, sample_model_config):
        client = LLMClient(sample_model_config)
        assert client._is_deepseek() is True

    def test_is_deepseek_openai(self, openai_model_config):
        client = LLMClient(openai_model_config)
        assert client._is_deepseek() is False

    def test_is_deepseek_case_insensitive(self):
        mc = ModelConfig(
            ref="DeepSeek/v3",
            provider="DeepSeek",
            api_model="v3",
            display_name="DS",
            api_key="key",
        )
        client = LLMClient(mc)
        assert client._is_deepseek() is True

    def test_is_deepseek_in_base_url(self):
        mc = ModelConfig(
            ref="custom/v1",
            provider="custom",
            api_model="v1",
            display_name="C",
            api_key="key",
            base_url="https://api.DeepSeek.com/v1",
        )
        client = LLMClient(mc)
        assert client._is_deepseek() is True


# ── _build_kwargs ─────────────────────────────────────────────────────


class TestBuildKwargs:
    """Tests for LLMClient._build_kwargs."""

    def test_basic_kwargs(self, sample_model_config):
        client = LLMClient(sample_model_config)
        kw = client._build_kwargs([{"role": "user", "content": "hi"}], None)
        assert kw["model"] == "deepseek-v4-flash"
        assert kw["max_tokens"] == 4096
        assert kw["temperature"] == 0.7
        assert kw["top_p"] == 1.0
        assert "tools" not in kw

    def test_with_tools(self, sample_model_config):
        client = LLMClient(sample_model_config)
        tools = [{"type": "function", "function": {"name": "test"}}]
        kw = client._build_kwargs([], tools)
        assert kw["tools"] == tools

    def test_thinking_enabled_deepseek(self, thinking_model_config):
        client = LLMClient(thinking_model_config)
        kw = client._build_kwargs([], None)
        assert "extra_body" in kw
        assert kw["extra_body"]["thinking"]["type"] == "enabled"
        assert kw["extra_body"]["reasoning_effort"] == "high"

    def test_thinking_disabled_deepseek(self, sample_model_config):
        client = LLMClient(sample_model_config)
        kw = client._build_kwargs([], None)
        assert kw["extra_body"]["thinking"]["type"] == "disabled"
        assert "reasoning_effort" not in kw["extra_body"]

    def test_no_extra_body_for_openai(self, openai_model_config):
        client = LLMClient(openai_model_config)
        kw = client._build_kwargs([], None)
        assert "extra_body" not in kw

    def test_thinking_without_reasoning_effort(self):
        mc = ModelConfig(
            ref="deepseek/v4",
            provider="deepseek",
            api_model="v4",
            display_name="V4",
            api_key="key",
            thinking_enabled=True,
            reasoning_effort=None,
        )
        client = LLMClient(mc)
        kw = client._build_kwargs([], None)
        assert kw["extra_body"]["thinking"]["type"] == "enabled"
        assert "reasoning_effort" not in kw["extra_body"]


# ── chat (non-streaming) ─────────────────────────────────────────────


class TestChat:
    """Tests for LLMClient.chat."""

    @pytest.mark.asyncio
    async def test_chat_returns_response_and_usage(self, sample_model_config):
        client = LLMClient(sample_model_config)
        mock_create = AsyncMock()
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 50
        mock_usage.completion_tokens = 25
        mock_usage.total_tokens = 75
        mock_create.return_value = MagicMock(usage=mock_usage)
        client.client.chat.completions.create = mock_create

        response, usage = await client.chat([{"role": "user", "content": "hi"}])

        assert usage.prompt_tokens == 50
        assert usage.completion_tokens == 25
        assert usage.total_tokens == 75

    @pytest.mark.asyncio
    async def test_chat_no_usage(self, sample_model_config):
        client = LLMClient(sample_model_config)
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(usage=None)
        client.client.chat.completions.create = mock_create

        _, usage = await client.chat([{"role": "user", "content": "hi"}])
        assert usage.prompt_tokens == 0
        assert usage.completion_tokens == 0

    @pytest.mark.asyncio
    async def test_chat_no_usage_attribute(self, sample_model_config):
        client = LLMClient(sample_model_config)
        mock_create = AsyncMock()
        delattr(mock_create.return_value, "usage")
        client.client.chat.completions.create = mock_create

        _, usage = await client.chat([{"role": "user", "content": "hi"}])
        assert usage.total_tokens == 0

    @pytest.mark.asyncio
    async def test_chat_with_tools(self, sample_model_config):
        client = LLMClient(sample_model_config)
        mock_create = AsyncMock()
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        mock_usage.total_tokens = 15
        mock_create.return_value = MagicMock(usage=mock_usage)
        client.client.chat.completions.create = mock_create

        tools = [{"type": "function", "function": {"name": "echo"}}]
        _, usage = await client.chat([], tools)
        assert usage.total_tokens == 15


# ── chat_stream ───────────────────────────────────────────────────────


class TestChatStream:
    """Tests for LLMClient.chat_stream."""

    @pytest.mark.asyncio
    async def test_stream_content_chunks(self, sample_model_config):
        """Stream content chunks are yielded correctly."""
        client = LLMClient(sample_model_config)

        events = make_async_iter([
            _MockStreamEvent(delta=_MockDelta(content="Hello")),
            _MockStreamEvent(delta=_MockDelta(content=" world")),
            _MockStreamEvent(delta=_MockDelta(content="!")),
            _MockStreamEvent(delta=_MockDelta(content=""), usage=_MockUsage(10, 5, 15)),
        ])

        mock_create = AsyncMock(return_value=events)
        client.client.chat.completions.create = mock_create

        chunks = []
        async for chunk in client.chat_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        # Content chunks
        contents = [c.content for c in chunks if c.content]
        assert "".join(contents) == "Hello world!"

        # Usage chunk
        usages = [c.usage for c in chunks if c.usage]
        assert len(usages) == 1
        assert usages[0].total_tokens == 15

    @pytest.mark.asyncio
    async def test_stream_thinking_chunks(self, thinking_model_config):
        """Stream with reasoning/thinking content."""
        client = LLMClient(thinking_model_config)

        events = make_async_iter([
            _MockStreamEvent(delta=_MockDelta(reasoning_content="Let me think...")),
            _MockStreamEvent(delta=_MockDelta(reasoning_content=" about this.")),
            _MockStreamEvent(delta=_MockDelta(content="The answer is 42.")),
            _MockStreamEvent(usage=_MockUsage(20, 10, 30)),
        ])

        client.client.chat.completions.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in client.chat_stream([]):
            chunks.append(chunk)

        thinkings = [c.thinking for c in chunks if c.thinking]
        assert "".join(thinkings) == "Let me think... about this."

        contents = [c.content for c in chunks if c.content]
        assert "".join(contents) == "The answer is 42."

    @pytest.mark.asyncio
    async def test_stream_tool_call_deltas(self, sample_model_config):
        """Stream with tool call deltas."""
        client = LLMClient(sample_model_config)

        events = make_async_iter([
            _MockStreamEvent(delta=_MockDelta(tool_calls=[
                _MockToolCallDelta(
                    index=0,
                    id="call_abc",
                    function=_MockFunctionDelta(name="web_search"),
                )
            ])),
            _MockStreamEvent(delta=_MockDelta(tool_calls=[
                _MockToolCallDelta(
                    index=0,
                    function=_MockFunctionDelta(arguments='{"query"'),
                )
            ])),
            _MockStreamEvent(delta=_MockDelta(tool_calls=[
                _MockToolCallDelta(
                    index=0,
                    function=_MockFunctionDelta(arguments=': "cats"}'),
                )
            ])),
            _MockStreamEvent(usage=_MockUsage(30, 15, 45)),
        ])

        client.client.chat.completions.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in client.chat_stream([]):
            chunks.append(chunk)

        tool_chunks = [c for c in chunks if c.tool_deltas]
        assert len(tool_chunks) == 3

        # Verify first tool delta
        first = tool_chunks[0].tool_deltas[0]
        assert first["id"] == "call_abc"
        assert first["function"]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_stream_empty_choices(self, sample_model_config):
        """Events with no choices are skipped."""
        client = LLMClient(sample_model_config)

        events = make_async_iter([
            _MockStreamEvent(),  # No choices
            _MockStreamEvent(delta=_MockDelta(content="valid")),
            _MockStreamEvent(usage=_MockUsage(1, 1, 2)),
        ])

        client.client.chat.completions.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in client.chat_stream([]):
            chunks.append(chunk)

        contents = [c.content for c in chunks if c.content]
        assert contents == ["valid"]

    @pytest.mark.asyncio
    async def test_stream_reasoning_empty_string(self, thinking_model_config):
        """Empty reasoning_content string not yielded (falsy)."""
        client = LLMClient(thinking_model_config)

        events = make_async_iter([
            _MockStreamEvent(delta=_MockDelta(reasoning_content="")),
            _MockStreamEvent(delta=_MockDelta(content="OK")),
            _MockStreamEvent(usage=_MockUsage(0, 1, 1)),
        ])

        client.client.chat.completions.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in client.chat_stream([]):
            chunks.append(chunk)

        thinkings = [c for c in chunks if c.thinking]
        assert len(thinkings) == 0

    @pytest.mark.asyncio
    async def test_stream_passes_kwargs_correctly(self, sample_model_config):
        """Verify stream=True and stream_options are set."""
        client = LLMClient(sample_model_config)

        events = make_async_iter([
            _MockStreamEvent(usage=_MockUsage(0, 0, 0)),
        ])

        mock_create = AsyncMock(return_value=events)
        client.client.chat.completions.create = mock_create

        chunks = [c async for c in client.chat_stream([], None)]

        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["stream"] is True
        assert call_kwargs["stream_options"] == {"include_usage": True}
