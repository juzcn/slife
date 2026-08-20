"""Tests for Slife.agent.loop — agent loop, streaming, and tool execution."""

import pytest; pytestmark = pytest.mark.unit


import pytest
from unittest.mock import AsyncMock, patch

from slife.agent.loop import (
    AgentLoop,
    ToolCallInfo,
    AgentResult,
    MaxIterationsExceeded,
    AgentEventHandler,
)
from slife.agent.llm_client import LLMClient, TokenUsage, StreamChunk


# ── ToolCallInfo ──────────────────────────────────────────────────────


class TestToolCallInfo:
    """Tests for ToolCallInfo dataclass."""

    def test_creation(self):
        tci = ToolCallInfo(id="call_1", name="web_search", arguments={"query": "cats"})
        assert tci.id == "call_1"
        assert tci.name == "web_search"
        assert tci.arguments == {"query": "cats"}

    def test_empty_arguments(self):
        tci = ToolCallInfo(id="call_2", name="echo", arguments={})
        assert tci.arguments == {}


# ── AgentResult ───────────────────────────────────────────────────────


class TestAgentResult:
    """Tests for AgentResult dataclass."""

    def test_creation(self):
        usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        result = AgentResult(text="Hello", usage=usage)
        assert result.text == "Hello"
        assert result.usage.total_tokens == 15


# ── MaxIterationsExceeded ─────────────────────────────────────────────


class TestMaxIterationsExceeded:
    """Tests for MaxIterationsExceeded exception."""

    def test_creation(self):
        exc = MaxIterationsExceeded(5)
        assert exc.iterations == 5
        assert "5" in str(exc)

    def test_can_catch(self):
        with pytest.raises(MaxIterationsExceeded):
            raise MaxIterationsExceeded(3)


# ── _serialize_tool_calls ─────────────────────────────────────────────


class TestSerializeToolCalls:
    """Tests for AgentLoop._serialize_tool_calls static method."""

    def test_single_tool_call(self):
        tcs = [ToolCallInfo(id="c1", name="echo", arguments={"msg": "hi"})]
        result = AgentLoop._serialize_tool_calls(tcs)
        assert len(result) == 1
        assert result[0]["id"] == "c1"
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "echo"
        assert result[0]["function"]["arguments"] == '{"msg": "hi"}'

    def test_multiple_tool_calls(self):
        tcs = [
            ToolCallInfo(id="c1", name="t1", arguments={"a": 1}),
            ToolCallInfo(id="c2", name="t2", arguments={"b": 2}),
        ]
        result = AgentLoop._serialize_tool_calls(tcs)
        assert len(result) == 2
        assert result[0]["id"] == "c1"
        assert result[1]["id"] == "c2"

    def test_unicode_arguments(self):
        """Arguments with unicode are serialized correctly."""
        tcs = [ToolCallInfo(id="c1", name="search", arguments={"query": "café"})]
        result = AgentLoop._serialize_tool_calls(tcs)
        assert "café" in result[0]["function"]["arguments"]


# ── _truncate_args ────────────────────────────────────────────────────


class TestTruncateArgs:
    """Tests for AgentLoop._truncate_args static method."""

    def test_short_args_unchanged(self):
        """Values under max_len are returned unchanged."""
        result = AgentLoop._truncate_args({"key": "short value"})
        assert result["key"] == "short value"

    def test_long_args_truncated(self):
        """Values over max_len are truncated with ellipsis."""
        long_value = "x" * 100
        result = AgentLoop._truncate_args({"key": long_value})
        assert result["key"] == "x" * 80 + "…"

    def test_exact_max_len_unchanged(self):
        """Values exactly at max_len are not truncated."""
        exact = "y" * 80
        result = AgentLoop._truncate_args({"key": exact})
        assert result["key"] == exact

    def test_custom_max_len(self):
        """Custom max_len is respected."""
        result = AgentLoop._truncate_args({"a": "1234567890"}, max_len=5)
        assert result["a"] == "12345…"

    def test_multiple_keys_mixed(self):
        """Mixed short/long keys in one call."""
        long_val = "a" * 100
        result = AgentLoop._truncate_args({"short": "hi", "long": long_val})
        assert result["short"] == "hi"
        assert result["long"] == "a" * 80 + "…"

    def test_non_string_values(self):
        """Non-string values are stringified before length check."""
        result = AgentLoop._truncate_args({"num": 42})
        assert result["num"] == "42"


# ── _build_tool_calls_from_deltas ─────────────────────────────────────


class TestBuildToolCallsFromDeltas:
    """Tests for AgentLoop._build_tool_calls_from_deltas."""

    def test_single_complete_tool_call(self):
        accum = {
            0: {"id": "call_abc", "name": "web_search", "arguments": '{"query": "cats"}'}
        }
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert len(result) == 1
        assert result[0].id == "call_abc"
        assert result[0].name == "web_search"
        assert result[0].arguments == {"query": "cats"}

    def test_multiple_tool_calls_sorted_by_index(self):
        accum = {
            1: {"id": "c2", "name": "t2", "arguments": '{}'},
            0: {"id": "c1", "name": "t1", "arguments": '{}'},
        }
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert [tc.id for tc in result] == ["c1", "c2"]

    def test_empty_arguments(self):
        accum = {0: {"id": "c1", "name": "echo", "arguments": ""}}
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert result[0].arguments == {}

    def test_whitespace_only_arguments(self):
        accum = {0: {"id": "c1", "name": "echo", "arguments": "   "}}
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert result[0].arguments == {}

    def test_invalid_json_arguments(self):
        """Malformed JSON defaults to empty dict."""
        accum = {0: {"id": "c1", "name": "echo", "arguments": "not valid json {"}}
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert result[0].arguments == {}


# ── AgentLoop construction ────────────────────────────────────────────


class TestAgentLoopConstruction:
    """Tests for AgentLoop.__init__."""

    def test_construction(self, sample_model_config, tool_registry):
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry, max_iterations=5)
        assert loop.llm_client == llm
        assert loop.tool_registry == tool_registry
        assert loop.max_iterations == 5

    def test_default_max_iterations(self, sample_model_config, empty_registry):
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)
        assert loop.max_iterations == 30


# ── set_max_iterations ────────────────────────────────────────────────


class TestSetMaxIterations:
    """set_max_iterations — runtime cap change (0 = unlimited)."""

    def test_accepts_zero_unlimited(self, sample_model_config, empty_registry):
        loop = AgentLoop(LLMClient(sample_model_config), empty_registry)
        msg = loop.set_max_iterations(0)
        assert "unlimited" in msg
        assert loop.max_iterations == 0

    def test_accepts_positive(self, sample_model_config, empty_registry):
        loop = AgentLoop(LLMClient(sample_model_config), empty_registry)
        msg = loop.set_max_iterations(5)
        assert msg.startswith("Max iterations set to 5")
        assert loop.max_iterations == 5

    def test_rejects_negative(self, sample_model_config, empty_registry):
        loop = AgentLoop(LLMClient(sample_model_config), empty_registry)
        assert "Error" in loop.set_max_iterations(-1)
        assert loop.max_iterations == 30  # unchanged

    def test_rejects_non_int(self, sample_model_config, empty_registry):
        loop = AgentLoop(LLMClient(sample_model_config), empty_registry)
        assert "Error" in loop.set_max_iterations("5")
        assert "Error" in loop.set_max_iterations(True)
        assert loop.max_iterations == 30  # unchanged


# ── _process_stream ───────────────────────────────────────────────────


class TestProcessStream:
    """Tests for AgentLoop._process_stream."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self, sample_model_config, empty_registry, conversation):
        """Stream returns a simple text response."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        # Mock chat_stream to return text chunks
        async def mock_stream(messages, tools, **kwargs):
            yield StreamChunk(content="Hello")
            yield StreamChunk(content=" world!")
            yield StreamChunk(usage=TokenUsage(5, 3, 8))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop._process_stream(conversation, None)

        assert result.content == "Hello world!"
        assert result.usage.total_tokens == 8

    @pytest.mark.asyncio
    async def test_with_thinking(self, sample_model_config, empty_registry, conversation):
        """Stream returns thinking + content."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        async def mock_stream(messages, tools, **kwargs):
            yield StreamChunk(thinking="Let me think...")
            yield StreamChunk(content="OK")
            yield StreamChunk(usage=TokenUsage(3, 1, 4))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop._process_stream(conversation, None)

        assert result.thinking == "Let me think..."
        assert result.content == "OK"

    @pytest.mark.asyncio
    async def test_with_handler_callbacks(self, sample_model_config, empty_registry, conversation):
        """Handler receives callbacks during streaming."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        handler = AsyncMock(spec=AgentEventHandler)

        async def mock_stream(messages, tools, **kwargs):
            yield StreamChunk(thinking="Hmm")
            yield StreamChunk(content="Answer")
            yield StreamChunk(usage=TokenUsage(2, 1, 3))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop._process_stream(conversation, handler)

        handler.on_thinking_chunk.assert_awaited_with("Hmm")
        handler.on_text_chunk.assert_awaited_with("Answer")

    @pytest.mark.asyncio
    async def test_with_tool_deltas(self, sample_model_config, empty_registry, conversation):
        """Stream accumulates tool call deltas."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        async def mock_stream(messages, tools, **kwargs):
            # First chunk: tool call with id and name
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "call_x", "function": {"name": "echo", "arguments": ""}}
            ])
            # Second chunk: more arguments
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "", "function": {"name": "", "arguments": '{"msg"'}}
            ])
            # Third chunk: arguments continued
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "", "function": {"name": "", "arguments": ': "hi"}'}}
            ])
            yield StreamChunk(usage=TokenUsage(10, 5, 15))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop._process_stream(conversation, None)

        assert 0 in result.tool_accum
        acc = result.tool_accum[0]
        assert acc["id"] == "call_x"
        assert acc["name"] == "echo"
        assert acc["arguments"] == '{"msg": "hi"}'

    @pytest.mark.asyncio
    async def test_handler_is_none(self, sample_model_config, empty_registry, conversation):
        """Handler=None should not cause errors."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        async def mock_stream(messages, tools, **kwargs):
            yield StreamChunk(content="test")
            yield StreamChunk(usage=TokenUsage(1, 1, 2))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop._process_stream(conversation, None)

        assert result.content == "test"

    @pytest.mark.asyncio
    async def test_retries_transient_error(
        self, sample_model_config, empty_registry, conversation,
    ):
        """A transient httpx transport failure is retried transparently."""
        import httpx

        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)
        calls: list[int] = []

        async def flaky_stream(messages, tools, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.RemoteProtocolError(
                    "peer closed connection without sending complete message "
                    "body (incomplete chunked read)"
                )
            yield StreamChunk(content="Hello")
            yield StreamChunk(usage=TokenUsage(2, 1, 3))

        with (
            patch.object(llm, "chat_stream", side_effect=flaky_stream),
            patch("slife.agent.loop._LLM_STREAM_RETRY_BASE_DELAY", 0),
        ):
            result = await loop._process_stream(conversation, None)

        assert result.content == "Hello"
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_non_transient(
        self, sample_model_config, empty_registry, conversation,
    ):
        """Non-transient errors are not retried."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)
        calls: list[int] = []

        async def failing_stream(messages, tools, **kwargs):
            calls.append(1)
            raise ValueError("bad request")
            yield  # pragma: no cover — marks this as an async generator

        with (
            patch.object(llm, "chat_stream", side_effect=failing_stream),
            patch("slife.agent.loop._LLM_STREAM_RETRY_BASE_DELAY", 0),
        ):
            with pytest.raises(ValueError):
                await loop._process_stream(conversation, None)

        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_resets_handler_before_retry(
        self, sample_model_config, empty_registry, conversation,
    ):
        """Partial streamed output is cleared via on_stream_retry before retry."""
        import httpx

        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)
        handler = AsyncMock(spec=AgentEventHandler)
        calls: list[int] = []

        async def flaky_stream(messages, tools, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                yield StreamChunk(thinking="partial thinking...")
                raise httpx.RemoteProtocolError("connection dropped mid-stream")
            yield StreamChunk(content="clean answer")
            yield StreamChunk(usage=TokenUsage(2, 1, 3))

        with (
            patch.object(llm, "chat_stream", side_effect=flaky_stream),
            patch("slife.agent.loop._LLM_STREAM_RETRY_BASE_DELAY", 0),
        ):
            result = await loop._process_stream(conversation, handler)

        handler.on_stream_retry.assert_awaited_once()
        assert result.content == "clean answer"
        assert result.thinking == ""
        assert len(calls) == 2

    @pytest.mark.asyncio
    async def test_gives_up_after_retries(
        self, sample_model_config, empty_registry, conversation,
    ):
        """Retryable failures stop after the max retries and wrap the error."""
        import httpx

        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)
        calls: list[int] = []

        async def always_fails(messages, tools, **kwargs):
            calls.append(1)
            raise httpx.RemoteProtocolError("peer closed connection")
            yield  # pragma: no cover — marks this as an async generator

        with (
            patch.object(llm, "chat_stream", side_effect=always_fails),
            patch("slife.agent.loop._LLM_STREAM_RETRY_BASE_DELAY", 0),
        ):
            with pytest.raises(RuntimeError, match="LLM stream failed after 3 attempts"):
                await loop._process_stream(conversation, None)

        assert len(calls) == 3


# ── _execute_tools ────────────────────────────────────────────────────


class TestExecuteTools:
    """Tests for AgentLoop._execute_tools."""

    @pytest.mark.asyncio
    async def test_single_tool_execution(self, sample_model_config, tool_registry, conversation):
        """Single tool executed and added to conversation."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry)

        tcs = [ToolCallInfo(id="c1", name="echo", arguments={"message": "hi"})]
        handler = AsyncMock(spec=AgentEventHandler)

        await loop._execute_tools(tcs, conversation, handler)

        # Handler should be called
        handler.on_tool_call.assert_awaited_once()
        handler.on_tool_result.assert_awaited_once()
        call_args = handler.on_tool_result.call_args
        assert call_args[0][0] == "c1"  # tool_call_id
        assert "Echo: hi" in call_args[0][1]  # result

        # Conversation should have tool result
        msgs = conversation.to_openai_messages()
        tool_msgs = [m for m in msgs if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert tool_msgs[0]["content"] == "Echo: hi"

    @pytest.mark.asyncio
    async def test_tool_error(self, sample_model_config, tool_registry, conversation):
        """Failing tool returns error result."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry)

        tcs = [ToolCallInfo(id="c2", name="failer", arguments={"reason": "test"})]
        handler = AsyncMock(spec=AgentEventHandler)

        await loop._execute_tools(tcs, conversation, handler)

        result_call = handler.on_tool_result.call_args
        assert result_call[0][2] is True  # is_error

        msgs = conversation.to_openai_messages()
        tool_msg = [m for m in msgs if m["role"] == "tool"][0]
        assert "Intentional failure" in tool_msg["content"]

    @pytest.mark.asyncio
    async def test_no_handler(self, sample_model_config, tool_registry, conversation):
        """Handler=None doesn't break execution."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry)

        tcs = [ToolCallInfo(id="c1", name="echo", arguments={"message": "x"})]
        await loop._execute_tools(tcs, conversation, None)

        msgs = conversation.to_openai_messages()
        assert any(m["role"] == "tool" for m in msgs)

    @pytest.mark.asyncio
    async def test_error_detection_by_prefix(self, sample_model_config, tool_registry, conversation):
        """Results starting with 'Error' are flagged as errors."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry)

        tcs = [ToolCallInfo(id="c1", name="echo", arguments={"message": "Error: something"})]
        handler = AsyncMock(spec=AgentEventHandler)

        await loop._execute_tools(tcs, conversation, handler)

        # "Echo: Error: something" starts with "Echo", not "Error"
        # So this should NOT be flagged as an error
        call_args = handler.on_tool_result.call_args
        assert call_args[0][2] is False  # Not an error prefix


# ── AgentLoop.run ─────────────────────────────────────────────────────


class TestAgentLoopRun:
    """Integration tests for AgentLoop.run."""

    @pytest.mark.asyncio
    async def test_simple_text_run(self, sample_model_config, empty_registry, conversation):
        """Full run with a simple text response."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        async def mock_stream(messages, tools, **kwargs):
            yield StreamChunk(content="Hello!")
            yield StreamChunk(usage=TokenUsage(5, 3, 8))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop.run("Hi", conversation)

        assert result.text == "Hello!"
        assert result.usage.total_tokens == 8

    @pytest.mark.asyncio
    async def test_run_adds_user_message(self, sample_model_config, empty_registry, empty_conversation):
        """Run adds the user message to conversation."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        async def mock_stream(messages, tools, **kwargs):
            yield StreamChunk(content="OK")
            yield StreamChunk(usage=TokenUsage(1, 1, 2))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            await loop.run("User input here", empty_conversation)

        msgs = empty_conversation.to_openai_messages()
        assert msgs[0]["role"] == "user"
        assert msgs[0]["content"] == "User input here"

    @pytest.mark.asyncio
    async def test_context_tokens_isolated_per_conversation(
        self, sample_model_config, empty_registry, empty_conversation
    ):
        """A heartbeat/one-shot conversation's small usage must NOT overwrite
        the human conversation's context reading (status bar / _sys_note).

        Regression: the heartbeat runs in a fresh, small conversation every
        beat; a global _last_usage let it drag the human conversation's
        context measurement down (9.6% while the real context is 26.5%)."""
        from slife.agent.conversation import Conversation

        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        async def mock_human(messages, tools, **kwargs):
            yield StreamChunk(content="ok")
            yield StreamChunk(usage=TokenUsage(10_000, 5, 10_005))

        with patch.object(llm, 'chat_stream', side_effect=mock_human):
            await loop.run("hi", empty_conversation)
        assert loop.context_tokens_for(empty_conversation) == 10_000

        # A heartbeat-like fresh conversation runs a much smaller call.
        heartbeat_conv = Conversation(system_prompt="sys")

        async def mock_heartbeat(messages, tools, **kwargs):
            yield StreamChunk(content=".")
            yield StreamChunk(usage=TokenUsage(1_000, 2, 1_002))

        with patch.object(llm, 'chat_stream', side_effect=mock_heartbeat):
            await loop.run("[Heartbeat]", heartbeat_conv)

        # The human conversation's reading is unaffected by the heartbeat.
        assert loop.context_tokens_for(empty_conversation) == 10_000
        assert loop.context_tokens_for(heartbeat_conv) == 1_000

    @pytest.mark.asyncio
    async def test_run_with_tool_calls(self, sample_model_config, tool_registry, conversation):
        """Agent correctly handles tool calls and loops back."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry)

        call_count = [0]

        async def mock_stream(messages, tools, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: LLM requests tool
                yield StreamChunk(content="Let me echo that.")
                yield StreamChunk(tool_deltas=[
                    {"index": 0, "id": "c1", "function": {"name": "echo", "arguments": ""}}
                ])
                yield StreamChunk(tool_deltas=[
                    {"index": 0, "id": "", "function": {"name": "", "arguments": '{"message": "hello"}'}}
                ])
                yield StreamChunk(usage=TokenUsage(10, 5, 15))
            else:
                # Second call: final response
                yield StreamChunk(content="Done!")
                yield StreamChunk(usage=TokenUsage(5, 3, 8))

        handler = AsyncMock(spec=AgentEventHandler)

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop.run("echo hello", conversation, handler=handler)

        assert result.text == "Done!"
        # Total usage should be accumulated across both calls
        assert result.usage.prompt_tokens == 15
        assert result.usage.completion_tokens == 8
        assert result.usage.total_tokens == 23

        # Handler should have been called for both tool execution
        handler.on_tool_call.assert_awaited_once()
        handler.on_token_usage.assert_awaited()

    @pytest.mark.asyncio
    async def test_run_max_iterations(self, sample_model_config, tool_registry, conversation):
        """Agent returns cancelled result when max iterations exceeded."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry, max_iterations=2)

        async def always_tool_call(messages, tools, **kwargs):
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "c1", "function": {"name": "echo", "arguments": '{"message":"x"}'}}
            ])
            yield StreamChunk(usage=TokenUsage(2, 1, 3))

        handler = AsyncMock(spec=AgentEventHandler)

        with patch.object(llm, 'chat_stream', side_effect=always_tool_call):
            result = await loop.run("test", conversation, handler=handler)
            assert result.cancelled is True
            assert result.usage.total_tokens > 0
            # The limit is surfaced via the handler, not left silent.
            handler.on_max_iterations.assert_awaited_once_with(2)

    @pytest.mark.asyncio
    async def test_max_iterations_zero_is_unlimited(
        self, sample_model_config, tool_registry, conversation,
    ):
        """max_iterations=0 means no cap — the loop runs past any fixed
        iteration count until a final response arrives (never raises
        MaxIterationsExceeded)."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry, max_iterations=0)

        calls = 0

        async def tool_then_done(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 6:  # 5 tool-calling iterations, then a final answer
                yield StreamChunk(tool_deltas=[
                    {"index": 0, "id": "c1", "function": {"name": "echo", "arguments": '{"message":"x"}'}}
                ])
                yield StreamChunk(usage=TokenUsage(2, 1, 3))
            else:
                yield StreamChunk(content="Done!")
                yield StreamChunk(usage=TokenUsage(1, 1, 2))

        with patch.object(llm, 'chat_stream', side_effect=tool_then_done):
            result = await loop.run("test", conversation)

        assert result.text == "Done!"
        assert result.cancelled is False
        assert calls == 6  # no cap at 0 — all 5 tool iterations + final ran

    @pytest.mark.asyncio
    async def test_set_max_iterations_applies_next_turn(
        self, sample_model_config, tool_registry, conversation,
    ):
        """A runtime cap change does not affect the running turn; the next
        run reads the new value."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry, max_iterations=1)

        async def always_tool_call(messages, tools, **kwargs):
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "c1", "function": {"name": "echo", "arguments": '{"message":"x"}'}}
            ])
            yield StreamChunk(usage=TokenUsage(2, 1, 3))

        # Turn 1: capped at 1 → cancelled at the cap.
        with patch.object(llm, 'chat_stream', side_effect=always_tool_call):
            r1 = await loop.run("test", conversation)
            assert r1.cancelled is True

        # Raise to unlimited mid-session → the next turn runs freely.
        assert loop.set_max_iterations(0).startswith("Max iterations set to 0")

        calls = 0

        async def tool_then_done(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls < 4:  # 3 tool iterations, then a final answer
                yield StreamChunk(tool_deltas=[
                    {"index": 0, "id": "c1", "function": {"name": "echo", "arguments": '{"message":"x"}'}}
                ])
                yield StreamChunk(usage=TokenUsage(2, 1, 3))
            else:
                yield StreamChunk(content="Done!")
                yield StreamChunk(usage=TokenUsage(1, 1, 2))

        with patch.object(llm, 'chat_stream', side_effect=tool_then_done):
            r2 = await loop.run("test", conversation)
            assert r2.text == "Done!"
            assert calls == 4  # cap 0 took effect — not stopped at 1

    @pytest.mark.asyncio
    async def test_set_max_iterations_applies_mid_turn(
        self, sample_model_config, tool_registry, conversation,
    ):
        """Tightening the cap mid-turn stops the running turn immediately —
        the cap is checked live, not fixed at run() start."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, tool_registry, max_iterations=5)

        calls = 0

        async def shrink_cap(messages, tools, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                loop.set_max_iterations(1)  # tighten the cap mid-turn
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "c1", "function": {"name": "echo", "arguments": '{"message":"x"}'}}
            ])
            yield StreamChunk(usage=TokenUsage(2, 1, 3))

        handler = AsyncMock(spec=AgentEventHandler)

        with patch.object(llm, 'chat_stream', side_effect=shrink_cap):
            result = await loop.run("test", conversation, handler=handler)

        assert result.cancelled is True
        # Stopped at the tightened cap (iteration 2's live check), not the
        # original 5 — only 2 LLM calls happened.
        assert calls == 2
        handler.on_max_iterations.assert_awaited_once_with(1)

    @pytest.mark.asyncio
    async def test_run_with_images(self, sample_model_config, empty_registry, conversation, tmp_path):
        """@path / programmatic attachments auto-invoke include_image: the
        user message stays verbatim text, the image block is injected into
        it, and the harness synthesizes the assistant(tool_use) + tool
        result pair — no LLM iteration spent deciding to attach."""
        # Create a real temp image file
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake png")

        from slife.tools.vision import IncludeImageTool
        from slife.tools.context import ToolContext
        from slife.tools.registry import ToolRegistry
        registry = ToolRegistry()
        tool = IncludeImageTool()
        tool._ctx = ToolContext(conversation=conversation)
        registry.register(tool)

        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, registry, supports_vision=True)

        async def mock_stream(messages, tools, **kwargs):
            yield StreamChunk(content="I see an image!")
            yield StreamChunk(usage=TokenUsage(5, 3, 8))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop.run("Describe this", conversation, images=[str(img)])

        assert result.text == "I see an image!"
        # Find the just-added user message, then the harness pair after it.
        ui = next(
            i for i, m in enumerate(conversation.messages) if m["role"] == "user"
        )
        user = conversation.messages[ui]
        assert user["content"][0] == {"type": "text", "text": "Describe this"}
        assert user["content"][1]["type"] == "image_url"
        assert user["content"][1]["image_url"]["url"].startswith("data:image/")
        # Harness-synthesized include_image pair follows the user message
        helper = conversation.messages[ui + 1]
        assert helper["role"] == "assistant"
        tc = helper["tool_calls"][0]
        assert tc["function"]["name"] == "include_image"
        assert tc["id"].startswith("_harness_include_image_")
        res = conversation.messages[ui + 2]
        assert res["role"] == "tool"
        assert res["content"].startswith("Image included:")

    @pytest.mark.asyncio
    async def test_run_content_accumulation(self, sample_model_config, empty_registry, conversation):
        """Content from multiple chunks is accumulated correctly."""
        llm = LLMClient(sample_model_config)
        loop = AgentLoop(llm, empty_registry)

        parts = ["The ", "quick ", "brown ", "fox"]
        async def mock_stream(messages, tools, **kwargs):
            for p in parts:
                yield StreamChunk(content=p)
            yield StreamChunk(usage=TokenUsage(4, 4, 8))

        with patch.object(llm, 'chat_stream', side_effect=mock_stream):
            result = await loop.run("test", conversation)

        assert result.text == "The quick brown fox"
