"""Tests for OpenAIBackend — extracted from test_llm_client.py."""

import pytest; pytestmark = pytest.mark.unit

import pytest
from unittest.mock import AsyncMock, MagicMock

from slife.config import ModelConfig
from slife.agent.llm_backends.openai import OpenAIBackend
from tests.conftest import (
    _MockStreamEvent,
    _MockDelta,
    _MockUsage,
    _MockToolCallDelta,
    _MockFunctionDelta,
    make_async_iter,
)


# ── Construction ────────────────────────────────────────────────────────


class TestOpenAIBackend:
    """Tests for OpenAIBackend construction and detection."""

    def test_construction(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        assert backend.model_config == sample_model_config
        assert backend.client is not None

    def test_is_deepseek_true(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        assert backend._is_deepseek() is True

    def test_is_deepseek_openai(self, openai_model_config):
        backend = OpenAIBackend(openai_model_config)
        assert backend._is_deepseek() is False

    def test_is_deepseek_case_insensitive(self):
        mc = ModelConfig(
            ref="DeepSeek/v3", provider="DeepSeek", api_model="v3",
            display_name="DS", api_key="key",
        )
        backend = OpenAIBackend(mc)
        assert backend._is_deepseek() is True

    def test_is_deepseek_in_base_url(self):
        mc = ModelConfig(
            ref="custom/v1", provider="custom", api_model="v1",
            display_name="C", api_key="key",
            base_url="https://api.DeepSeek.com/v1",
        )
        backend = OpenAIBackend(mc)
        assert backend._is_deepseek() is True


# ── _build_kwargs ───────────────────────────────────────────────────────


class TestBuildKwargs:
    """Tests for OpenAIBackend._build_kwargs."""

    def test_basic_kwargs(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        kw = backend._build_kwargs([{"role": "user", "content": "hi"}], None)
        assert kw["model"] == "deepseek-v4-flash"
        assert kw["max_tokens"] == 4096
        assert kw["temperature"] == 0.7
        assert kw["top_p"] == 1.0
        assert "tools" not in kw

    def test_with_tools(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        tools = [{"type": "function", "function": {"name": "test"}}]
        kw = backend._build_kwargs([], tools)
        assert kw["tools"] == tools

    def test_thinking_enabled_deepseek(self, thinking_model_config):
        backend = OpenAIBackend(thinking_model_config)
        kw = backend._build_kwargs([], None)
        assert "extra_body" in kw
        assert kw["extra_body"]["thinking"]["type"] == "enabled"
        assert kw["extra_body"]["reasoning_effort"] == "high"

    def test_thinking_disabled_deepseek(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        kw = backend._build_kwargs([], None)
        assert kw["extra_body"]["thinking"]["type"] == "disabled"
        assert "reasoning_effort" not in kw["extra_body"]

    def test_no_extra_body_for_openai(self, openai_model_config):
        backend = OpenAIBackend(openai_model_config)
        kw = backend._build_kwargs([], None)
        assert "extra_body" not in kw

    def test_thinking_without_reasoning_effort(self):
        mc = ModelConfig(
            ref="deepseek/v4", provider="deepseek", api_model="v4",
            display_name="V4", api_key="key",
            thinking_enabled=True, reasoning_effort=None,
        )
        backend = OpenAIBackend(mc)
        kw = backend._build_kwargs([], None)
        assert kw["extra_body"]["thinking"]["type"] == "enabled"
        assert "reasoning_effort" not in kw["extra_body"]

    def test_thinking_enabled_non_deepseek(self):
        """Non-DeepSeek provider with thinking_enabled also gets extra_body."""
        mc = ModelConfig(
            ref="ollama/qwen", provider="ollama", api_model="qwen",
            display_name="Qwen", api_key="key",
            thinking_enabled=True, reasoning_effort=None,
        )
        backend = OpenAIBackend(mc)
        kw = backend._build_kwargs([], None)
        assert "extra_body" in kw
        assert kw["extra_body"]["thinking"]["type"] == "enabled"
        assert "reasoning_effort" not in kw["extra_body"]


# ── chat (non-streaming) ────────────────────────────────────────────────


class TestChat:
    """Tests for OpenAIBackend.chat."""

    @pytest.mark.asyncio
    async def test_chat_returns_response_and_usage(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        mock_create = AsyncMock()
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 50
        mock_usage.completion_tokens = 25
        mock_usage.total_tokens = 75
        mock_create.return_value = MagicMock(usage=mock_usage)
        backend.client.chat.completions.create = mock_create

        response, usage = await backend.chat([{"role": "user", "content": "hi"}])
        assert usage.prompt_tokens == 50
        assert usage.completion_tokens == 25
        assert usage.total_tokens == 75

    @pytest.mark.asyncio
    async def test_chat_no_usage(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        mock_create = AsyncMock()
        mock_create.return_value = MagicMock(usage=None)
        backend.client.chat.completions.create = mock_create

        _, usage = await backend.chat([{"role": "user", "content": "hi"}])
        assert usage.prompt_tokens == 0

    @pytest.mark.asyncio
    async def test_chat_no_usage_attribute(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        mock_create = AsyncMock()
        delattr(mock_create.return_value, "usage")
        backend.client.chat.completions.create = mock_create

        _, usage = await backend.chat([{"role": "user", "content": "hi"}])
        assert usage.total_tokens == 0

    @pytest.mark.asyncio
    async def test_chat_with_tools(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        mock_create = AsyncMock()
        mock_usage = MagicMock()
        mock_usage.prompt_tokens = 10
        mock_usage.completion_tokens = 5
        mock_usage.total_tokens = 15
        mock_create.return_value = MagicMock(usage=mock_usage)
        backend.client.chat.completions.create = mock_create

        tools = [{"type": "function", "function": {"name": "echo"}}]
        _, usage = await backend.chat([], tools)
        assert usage.total_tokens == 15


# ── chat_stream ─────────────────────────────────────────────────────────


class TestChatStream:
    """Tests for OpenAIBackend.chat_stream."""

    @pytest.mark.asyncio
    async def test_stream_content_chunks(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        events = make_async_iter([
            _MockStreamEvent(delta=_MockDelta(content="Hello")),
            _MockStreamEvent(delta=_MockDelta(content=" world")),
            _MockStreamEvent(delta=_MockDelta(content="!")),
            _MockStreamEvent(delta=_MockDelta(content=""), usage=_MockUsage(10, 5, 15)),
        ])
        mock_create = AsyncMock(return_value=events)
        backend.client.chat.completions.create = mock_create

        chunks = []
        async for chunk in backend.chat_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        contents = [c.content for c in chunks if c.content]
        assert "".join(contents) == "Hello world!"
        usages = [c.usage for c in chunks if c.usage]
        assert len(usages) == 1
        assert usages[0].total_tokens == 15

    @pytest.mark.asyncio
    async def test_stream_thinking_chunks(self, thinking_model_config):
        backend = OpenAIBackend(thinking_model_config)
        events = make_async_iter([
            _MockStreamEvent(delta=_MockDelta(reasoning_content="Let me think...")),
            _MockStreamEvent(delta=_MockDelta(reasoning_content=" about this.")),
            _MockStreamEvent(delta=_MockDelta(content="The answer is 42.")),
            _MockStreamEvent(usage=_MockUsage(20, 10, 30)),
        ])
        backend.client.chat.completions.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in backend.chat_stream([]):
            chunks.append(chunk)

        thinkings = [c.thinking for c in chunks if c.thinking]
        assert "".join(thinkings) == "Let me think... about this."
        contents = [c.content for c in chunks if c.content]
        assert "".join(contents) == "The answer is 42."

    @pytest.mark.asyncio
    async def test_stream_tool_call_deltas(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        events = make_async_iter([
            _MockStreamEvent(delta=_MockDelta(tool_calls=[
                _MockToolCallDelta(index=0, id="call_abc",
                    function=_MockFunctionDelta(name="web_search"))
            ])),
            _MockStreamEvent(delta=_MockDelta(tool_calls=[
                _MockToolCallDelta(index=0,
                    function=_MockFunctionDelta(arguments='{"query"'))
            ])),
            _MockStreamEvent(delta=_MockDelta(tool_calls=[
                _MockToolCallDelta(index=0,
                    function=_MockFunctionDelta(arguments=': "cats"}'))
            ])),
            _MockStreamEvent(usage=_MockUsage(30, 15, 45)),
        ])
        backend.client.chat.completions.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in backend.chat_stream([]):
            chunks.append(chunk)

        tool_chunks = [c for c in chunks if c.tool_deltas]
        assert len(tool_chunks) == 3
        first = tool_chunks[0].tool_deltas[0]
        assert first["id"] == "call_abc"
        assert first["function"]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_stream_empty_choices(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        events = make_async_iter([
            _MockStreamEvent(),
            _MockStreamEvent(delta=_MockDelta(content="valid")),
            _MockStreamEvent(usage=_MockUsage(1, 1, 2)),
        ])
        backend.client.chat.completions.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in backend.chat_stream([]):
            chunks.append(chunk)
        contents = [c.content for c in chunks if c.content]
        assert contents == ["valid"]

    @pytest.mark.asyncio
    async def test_stream_reasoning_empty_string(self, thinking_model_config):
        backend = OpenAIBackend(thinking_model_config)
        events = make_async_iter([
            _MockStreamEvent(delta=_MockDelta(reasoning_content="")),
            _MockStreamEvent(delta=_MockDelta(content="OK")),
            _MockStreamEvent(usage=_MockUsage(0, 1, 1)),
        ])
        backend.client.chat.completions.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in backend.chat_stream([]):
            chunks.append(chunk)
        thinkings = [c for c in chunks if c.thinking]
        assert len(thinkings) == 0

    @pytest.mark.asyncio
    async def test_stream_passes_kwargs_correctly(self, sample_model_config):
        backend = OpenAIBackend(sample_model_config)
        events = make_async_iter([_MockStreamEvent(usage=_MockUsage(0, 0, 0))])
        mock_create = AsyncMock(return_value=events)
        backend.client.chat.completions.create = mock_create

        [c async for c in backend.chat_stream([], None)]
        call_kwargs = mock_create.call_args[1]
        assert call_kwargs["stream"] is True
        assert call_kwargs["stream_options"] == {"include_usage": True}


# ── OpenAIResponsesBackend (Responses API) ──────────────────────────────


class TestOpenAIResponsesBackend:
    """Tests for OpenAIResponsesBackend — Responses API streaming.

    The Responses API streams function calls as ``response.*`` events:
    the name/call_id arrive on ``output_item.added``/``done`` while
    ``function_call_arguments.delta`` carries only argument chunks.
    Regression for REVIEW H4: previously the name was never captured, so
    every tool call became "Unknown tool ''".
    """

    def _responses_cfg(self):
        return ModelConfig(
            ref="openai/gpt-5",
            provider="openai",
            api_model="gpt-5",
            display_name="GPT-5",
            api_key="sk-test",
            base_url="https://api.openai.com/v1",
            api="openai-responses",
        )

    @staticmethod
    def _evt(etype, **kw):
        from types import SimpleNamespace
        return SimpleNamespace(type=etype, **kw)

    @staticmethod
    def _fn_item(item_id, name, call_id):
        from types import SimpleNamespace
        return SimpleNamespace(
            type="function_call", id=item_id, name=name, call_id=call_id, arguments="",
        )

    @pytest.mark.asyncio
    async def test_stream_captures_function_name_from_output_item(self):
        """Tool deltas carry the name/call_id captured on output_item.added."""
        from slife.agent.llm_backends.openai_responses import OpenAIResponsesBackend

        events = make_async_iter([
            self._evt("response.output_item.added", item=self._fn_item("fc_1", "web_search", "call_abc"), output_index=0, sequence_number=0),
            self._evt("response.function_call_arguments.delta", item_id="fc_1", output_index=0, sequence_number=1, delta='{"query"'),
            self._evt("response.function_call_arguments.delta", item_id="fc_1", output_index=0, sequence_number=2, delta=': "cats"}'),
            self._evt("response.output_item.done", item=self._fn_item("fc_1", "web_search", "call_abc"), output_index=0, sequence_number=3),
            self._evt("response.completed", response=MagicMock(usage=MagicMock(input_tokens=10, output_tokens=5, total_tokens=15))),
        ])

        backend = OpenAIResponsesBackend(self._responses_cfg())
        backend.client.responses.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in backend.chat_stream([{"role": "user", "content": "hi"}]):
            chunks.append(chunk)

        tool_deltas = [c.tool_deltas[0] for c in chunks if c.tool_deltas]
        assert len(tool_deltas) == 2
        for td in tool_deltas:
            assert td["function"]["name"] == "web_search"
            assert td["id"] == "call_abc"  # call_id, not item_id
        # Stable, collision-free index across the deltas of one item.
        assert tool_deltas[0]["index"] == tool_deltas[1]["index"] == 0
        assert "".join(td["function"]["arguments"] for td in tool_deltas) == '{"query": "cats"}'

    @pytest.mark.asyncio
    async def test_stream_parallel_tool_calls_distinct_indices(self):
        """Two tool calls in one batch get distinct deterministic indices."""
        from slife.agent.llm_backends.openai_responses import OpenAIResponsesBackend

        events = make_async_iter([
            self._evt("response.output_item.added", item=self._fn_item("fc_1", "tool_a", "call_a"), output_index=0, sequence_number=0),
            self._evt("response.output_item.added", item=self._fn_item("fc_2", "tool_b", "call_b"), output_index=1, sequence_number=1),
            self._evt("response.function_call_arguments.delta", item_id="fc_1", output_index=0, sequence_number=2, delta='{}'),
            self._evt("response.function_call_arguments.delta", item_id="fc_2", output_index=1, sequence_number=3, delta='{}'),
        ])

        backend = OpenAIResponsesBackend(self._responses_cfg())
        backend.client.responses.create = AsyncMock(return_value=events)

        chunks = []
        async for chunk in backend.chat_stream([]):
            chunks.append(chunk)

        tool_deltas = [c.tool_deltas[0] for c in chunks if c.tool_deltas]
        assert [td["index"] for td in tool_deltas] == [0, 1]
        assert [td["function"]["name"] for td in tool_deltas] == ["tool_a", "tool_b"]
        assert [td["id"] for td in tool_deltas] == ["call_a", "call_b"]
