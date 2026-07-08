"""Tests for the agent loop (slife.agent.loop)."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.agent.loop import (
    AgentLoop,
    AgentResult,
    ToolCallInfo,
    MaxIterationsExceeded,
    _StreamResult,
)
from slife.agent.llm_client import StreamChunk, TokenUsage
from slife.agent.conversation import Conversation
from slife.tools.registry import ToolRegistry


# ══════════════════════════════════════════════════════════════════════
# ToolCallInfo
# ══════════════════════════════════════════════════════════════════════


class TestToolCallInfo:
    """Tests for ToolCallInfo dataclass."""

    def test_create(self):
        """Basic creation."""
        tc = ToolCallInfo(id="call_1", name="search", arguments={"query": "cats"})
        assert tc.id == "call_1"
        assert tc.name == "search"
        assert tc.arguments == {"query": "cats"}

    def test_empty_arguments(self):
        """Tool call with no arguments."""
        tc = ToolCallInfo(id="call_2", name="noop", arguments={})
        assert tc.arguments == {}


# ══════════════════════════════════════════════════════════════════════
# AgentResult
# ══════════════════════════════════════════════════════════════════════


class TestAgentResult:
    """Tests for AgentResult dataclass."""

    def test_create(self):
        """Basic creation."""
        usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        result = AgentResult(text="Hello!", usage=usage)
        assert result.text == "Hello!"
        assert result.usage.total_tokens == 15


# ══════════════════════════════════════════════════════════════════════
# MaxIterationsExceeded
# ══════════════════════════════════════════════════════════════════════


class TestMaxIterationsExceeded:
    """Tests for MaxIterationsExceeded exception."""

    def test_create(self):
        """Exception stores iteration count."""
        exc = MaxIterationsExceeded(5)
        assert exc.iterations == 5
        assert "5" in str(exc)

    def test_is_exception(self):
        """It is a proper Exception subclass."""
        exc = MaxIterationsExceeded(10)
        assert isinstance(exc, Exception)


# ══════════════════════════════════════════════════════════════════════
# AgentLoop._serialize_tool_calls
# ══════════════════════════════════════════════════════════════════════


class TestSerializeToolCalls:
    """Tests for AgentLoop._serialize_tool_calls()."""

    def test_single_tool_call(self):
        """Single tool call serialized correctly."""
        tc = ToolCallInfo(id="c1", name="search", arguments={"q": "cats"})
        result = AgentLoop._serialize_tool_calls([tc])
        assert len(result) == 1
        assert result[0]["id"] == "c1"
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "search"
        assert json.loads(result[0]["function"]["arguments"]) == {"q": "cats"}

    def test_multiple_tool_calls(self):
        """Multiple tool calls all serialized."""
        tcs = [
            ToolCallInfo(id="c1", name="t1", arguments={}),
            ToolCallInfo(id="c2", name="t2", arguments={"x": 1}),
        ]
        result = AgentLoop._serialize_tool_calls(tcs)
        assert len(result) == 2
        assert result[0]["id"] == "c1"
        assert result[1]["id"] == "c2"

    def test_empty_list(self):
        """Empty list returns empty list."""
        result = AgentLoop._serialize_tool_calls([])
        assert result == []

    def test_unicode_in_arguments(self):
        """Non-ASCII characters in arguments are preserved."""
        tc = ToolCallInfo(id="c1", name="search", arguments={"query": "café résumé"})
        result = AgentLoop._serialize_tool_calls([tc])
        decoded = json.loads(result[0]["function"]["arguments"])
        assert decoded["query"] == "café résumé"


# ══════════════════════════════════════════════════════════════════════
# AgentLoop._build_tool_calls_from_deltas
# ══════════════════════════════════════════════════════════════════════


class TestBuildToolCallsFromDeltas:
    """Tests for AgentLoop._build_tool_calls_from_deltas()."""

    def test_single_tool(self):
        """Build from a single accumulated delta."""
        accum = {
            0: {"id": "call_1", "name": "search", "arguments": '{"q":"cats"}'}
        }
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert len(result) == 1
        assert result[0].id == "call_1"
        assert result[0].name == "search"
        assert result[0].arguments == {"q": "cats"}

    def test_multiple_tools_sorted_by_index(self):
        """Tools are returned sorted by index key."""
        accum = {
            1: {"id": "c2", "name": "t2", "arguments": "{}"},
            0: {"id": "c1", "name": "t1", "arguments": "{}"},
        }
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert result[0].id == "c1"
        assert result[1].id == "c2"

    def test_empty_arguments(self):
        """Empty arguments string becomes empty dict."""
        accum = {0: {"id": "c1", "name": "t", "arguments": ""}}
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert result[0].arguments == {}

    def test_whitespace_only_arguments(self):
        """Whitespace-only arguments becomes empty dict."""
        accum = {0: {"id": "c1", "name": "t", "arguments": "   "}}
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert result[0].arguments == {}

    def test_invalid_json_arguments(self):
        """Invalid JSON in arguments becomes empty dict."""
        accum = {0: {"id": "c1", "name": "t", "arguments": "not valid json!!!"}}
        result = AgentLoop._build_tool_calls_from_deltas(accum)
        assert result[0].arguments == {}

    def test_empty_accum(self):
        """Empty accumulator returns empty list."""
        result = AgentLoop._build_tool_calls_from_deltas({})
        assert result == []


# ══════════════════════════════════════════════════════════════════════
# AgentLoop._process_stream
# ══════════════════════════════════════════════════════════════════════


class TestProcessStream:
    """Tests for AgentLoop._process_stream()."""

    @pytest.mark.asyncio
    async def test_basic_text_stream(self):
        """Stream with only text content."""
        llm_client = MagicMock()
        llm_client.chat_stream = AsyncMock()

        async def mock_stream(*args, **kwargs):
            yield StreamChunk(content="Hello")
            yield StreamChunk(content=" world")
            usage = TokenUsage(prompt_tokens=5, completion_tokens=2, total_tokens=7)
            yield StreamChunk(usage=usage)

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = []

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()
        handler = MagicMock()
        handler.on_thinking_chunk = AsyncMock()
        handler.on_text_chunk = AsyncMock()

        result = await loop._process_stream(conversation, handler)

        assert result.content == "Hello world"
        assert result.thinking == ""
        assert result.usage.total_tokens == 7
        assert result.tool_accum == {}
        assert handler.on_text_chunk.call_count == 2
        assert handler.on_thinking_chunk.call_count == 0

    @pytest.mark.asyncio
    async def test_stream_with_thinking(self):
        """Stream with thinking/reasoning content."""
        llm_client = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield StreamChunk(thinking="Let me analyze...")
            yield StreamChunk(thinking=" the problem.")
            yield StreamChunk(content="Based on my analysis...")
            yield StreamChunk(usage=TokenUsage(total_tokens=10))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = []

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()
        handler = MagicMock()
        handler.on_thinking_chunk = AsyncMock()
        handler.on_text_chunk = AsyncMock()
        handler.on_token_usage = AsyncMock()

        result = await loop._process_stream(conversation, handler)

        assert "Let me analyze" in result.thinking
        assert "Based on my analysis" in result.content
        assert handler.on_thinking_chunk.call_count == 2
        assert handler.on_text_chunk.call_count == 1

    @pytest.mark.asyncio
    async def test_stream_with_tool_deltas(self):
        """Stream with tool call deltas."""
        llm_client = MagicMock()

        async def mock_stream(*args, **kwargs):
            # Simulate multi-part tool call delta accumulation
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "call_abc", "function": {"name": "search", "arguments": '{"q":'}}
            ])
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "", "function": {"name": "", "arguments": '"cats"}'}}
            ])
            yield StreamChunk(usage=TokenUsage(total_tokens=8))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = []

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()
        handler = MagicMock()
        handler.on_text_chunk = AsyncMock()

        result = await loop._process_stream(conversation, handler)

        assert len(result.tool_accum) == 1
        assert result.tool_accum[0]["id"] == "call_abc"
        assert result.tool_accum[0]["name"] == "search"
        assert '{"q":"cats"}' in result.tool_accum[0]["arguments"]

    @pytest.mark.asyncio
    async def test_stream_with_none_handler(self):
        """Handler can be None (no callbacks)."""
        llm_client = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield StreamChunk(content="test")
            yield StreamChunk(usage=TokenUsage(total_tokens=2))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = []

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()

        # Should not raise
        result = await loop._process_stream(conversation, None)
        assert result.content == "test"

    @pytest.mark.asyncio
    async def test_stream_passes_messages_and_tools(self):
        """_process_stream passes messages and tool defs to LLM."""
        llm_client = MagicMock()

        async def mock_stream(messages, tools):
            assert isinstance(messages, list)
            assert isinstance(tools, list)
            yield StreamChunk(content="ok")
            yield StreamChunk(usage=TokenUsage(total_tokens=1))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = [{"type": "function"}]

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation(system_prompt="You are helpful.")

        result = await loop._process_stream(conversation, None)
        assert result.content == "ok"


# ══════════════════════════════════════════════════════════════════════
# AgentLoop._execute_tools
# ══════════════════════════════════════════════════════════════════════


class TestExecuteTools:
    """Tests for AgentLoop._execute_tools()."""

    @pytest.mark.asyncio
    async def test_executes_and_adds_results(self):
        """Tools are executed and results added to conversation."""
        registry = MagicMock()
        registry.execute = AsyncMock(return_value="Tool result here")

        loop = AgentLoop(llm_client=MagicMock(), tool_registry=registry)
        conversation = Conversation()
        handler = MagicMock()
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()

        tool_calls = [
            ToolCallInfo(id="c1", name="test_tool", arguments={"key": "val"})
        ]

        await loop._execute_tools(tool_calls, conversation, handler)

        # Tool was executed
        registry.execute.assert_called_once_with("test_tool", key="val")

        # Callbacks were called
        handler.on_tool_call.assert_called_once()
        handler.on_tool_result.assert_called_once_with("c1", "Tool result here", False)

        # Result added to conversation
        msgs = conversation.to_openai_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "tool"
        assert msgs[0]["tool_call_id"] == "c1"
        assert msgs[0]["content"] == "Tool result here"

    @pytest.mark.asyncio
    async def test_error_result_detected(self):
        """Results starting with 'Error' are flagged as errors."""
        registry = MagicMock()
        registry.execute = AsyncMock(return_value="Error: Something went wrong")

        loop = AgentLoop(llm_client=MagicMock(), tool_registry=registry)
        conversation = Conversation()
        handler = MagicMock()
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()

        tool_calls = [ToolCallInfo(id="c1", name="bad_tool", arguments={})]

        await loop._execute_tools(tool_calls, conversation, handler)

        handler.on_tool_result.assert_called_once_with("c1", "Error: Something went wrong", True)

    @pytest.mark.asyncio
    async def test_multiple_tools_executed_in_order(self):
        """Multiple tools are executed sequentially."""
        registry = MagicMock()
        registry.execute = AsyncMock(side_effect=["result1", "result2"])

        loop = AgentLoop(llm_client=MagicMock(), tool_registry=registry)
        conversation = Conversation()
        handler = MagicMock()
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()

        tool_calls = [
            ToolCallInfo(id="c1", name="t1", arguments={}),
            ToolCallInfo(id="c2", name="t2", arguments={"x": 1}),
        ]

        await loop._execute_tools(tool_calls, conversation, handler)

        assert registry.execute.call_count == 2
        assert handler.on_tool_call.call_count == 2
        assert handler.on_tool_result.call_count == 2

        # Results are in conversation
        msgs = conversation.to_openai_messages()
        assert len(msgs) == 2
        assert msgs[0]["content"] == "result1"
        assert msgs[1]["content"] == "result2"

    @pytest.mark.asyncio
    async def test_none_handler(self):
        """None handler works (no callbacks)."""
        registry = MagicMock()
        registry.execute = AsyncMock(return_value="result")

        loop = AgentLoop(llm_client=MagicMock(), tool_registry=registry)
        conversation = Conversation()
        tool_calls = [ToolCallInfo(id="c1", name="t1", arguments={})]

        await loop._execute_tools(tool_calls, conversation, None)

        # Still executes
        registry.execute.assert_called_once()
        assert len(conversation.to_openai_messages()) == 1


# ══════════════════════════════════════════════════════════════════════
# AgentLoop.run (main loop)
# ══════════════════════════════════════════════════════════════════════


class TestAgentLoopRun:
    """Tests for AgentLoop.run()."""

    @pytest.mark.asyncio
    async def test_simple_text_response(self):
        """Single iteration: LLM returns text, no tool calls."""
        llm_client = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield StreamChunk(content="Hello! How can I help?")
            yield StreamChunk(usage=TokenUsage(prompt_tokens=5, completion_tokens=4, total_tokens=9))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = []

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation(system_prompt="System prompt")
        handler = MagicMock()
        handler.on_text_chunk = AsyncMock()
        handler.on_token_usage = AsyncMock()

        result = await loop.run("Hello", conversation, handler=handler)

        assert result.text == "Hello! How can I help?"
        assert result.usage.total_tokens == 9
        # User message + assistant message added to conversation
        msgs = conversation.to_openai_messages()
        assert len(msgs) == 3  # system, user, assistant
        assert msgs[1]["role"] == "user"
        assert msgs[2]["role"] == "assistant"

    @pytest.mark.asyncio
    async def test_tool_call_loop(self):
        """LLM makes a tool call, result feeds back, then text response."""
        llm_client = MagicMock()

        call_count = [0]

        async def mock_stream(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: tool call deltas
                yield StreamChunk(tool_deltas=[
                    {"index": 0, "id": "call_1", "function": {"name": "search", "arguments": '{"q":"cats"}'}}
                ])
                yield StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            else:
                # Second call: final text
                yield StreamChunk(content="I found information about cats.")
                yield StreamChunk(usage=TokenUsage(prompt_tokens=20, completion_tokens=6, total_tokens=26))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = [{"type": "function", "function": {"name": "search"}}]
        registry.execute = AsyncMock(return_value="Search results: cats are great")

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()
        handler = MagicMock()
        handler.on_text_chunk = AsyncMock()
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()
        handler.on_token_usage = AsyncMock()

        result = await loop.run("Search for cats", conversation, handler=handler)

        assert "cats" in result.text
        assert call_count[0] == 2  # Two LLM calls
        # Tool was called
        registry.execute.assert_called_once()
        handler.on_tool_call.assert_called_once()
        handler.on_tool_result.assert_called_once()

        # Full conversation sequence: system(user) -> user -> assistant(tool_call) -> tool -> assistant
        msgs = conversation.to_openai_messages()
        assert len(msgs) == 4

    @pytest.mark.asyncio
    async def test_max_iterations_exceeded(self):
        """Loop raises MaxIterationsExceeded when limit hit."""
        llm_client = MagicMock()

        async def mock_stream(*args, **kwargs):
            # Always return tool calls, never text
            yield StreamChunk(tool_deltas=[
                {"index": 0, "id": "call_1", "function": {"name": "loop_forever", "arguments": "{}"}}
            ])
            yield StreamChunk(usage=TokenUsage(total_tokens=1))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = [{"type": "function"}]
        registry.execute = AsyncMock(return_value="result")

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry, max_iterations=3)
        conversation = Conversation()

        with pytest.raises(MaxIterationsExceeded) as exc_info:
            await loop.run("test", conversation)
        assert exc_info.value.iterations == 3

    @pytest.mark.asyncio
    async def test_images_passed_to_conversation(self, temp_image_file):
        """Images are added to the user message."""
        llm_client = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield StreamChunk(content="Nice image!")
            yield StreamChunk(usage=TokenUsage(total_tokens=5))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = []

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()

        await loop.run("Describe", conversation, images=[str(temp_image_file)])

        # Verify user message has multimodal content
        msgs = conversation.to_openai_messages()
        user_msg = msgs[0]
        assert user_msg["role"] == "user"
        # With images, content should be a list (multimodal)
        assert isinstance(user_msg["content"], list)

    @pytest.mark.asyncio
    async def test_none_handler(self):
        """Agent loop works without handler."""
        llm_client = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield StreamChunk(content="Response")
            yield StreamChunk(usage=TokenUsage(total_tokens=3))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = []

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()

        result = await loop.run("Hi", conversation, handler=None)
        assert result.text == "Response"

    @pytest.mark.asyncio
    async def test_cumulative_usage_tracking(self):
        """Token usage is accumulated across multiple LLM calls."""
        llm_client = MagicMock()

        call_num = [0]

        async def mock_stream(*args, **kwargs):
            call_num[0] += 1
            if call_num[0] == 1:
                yield StreamChunk(tool_deltas=[
                    {"index": 0, "id": "c1", "function": {"name": "t", "arguments": "{}"}}
                ])
                yield StreamChunk(usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15))
            else:
                yield StreamChunk(content="Done")
                yield StreamChunk(usage=TokenUsage(prompt_tokens=20, completion_tokens=5, total_tokens=25))

        llm_client.chat_stream = mock_stream

        registry = MagicMock()
        registry.to_openai_functions.return_value = [{"type": "function", "function": {"name": "t"}}]
        registry.execute = AsyncMock(return_value="ok")

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()
        handler = MagicMock()
        handler.on_thinking_chunk = AsyncMock()
        handler.on_text_chunk = AsyncMock()
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()
        handler.on_token_usage = AsyncMock()

        result = await loop.run("test", conversation, handler=handler)

        # Usage should be cumulative: 15 + 25 = 40 prompt, 5+5=10 completion, 15+25=40 total
        assert result.usage.prompt_tokens == 30
        assert result.usage.completion_tokens == 10
        assert result.usage.total_tokens == 40

    @pytest.mark.asyncio
    async def test_instant_text_response(self):
        """Agent returns immediately when LLM gives text without tools."""
        llm_client = MagicMock()

        async def mock_stream(*args, **kwargs):
            yield StreamChunk(content="Direct answer")
            yield StreamChunk(usage=TokenUsage(total_tokens=2))

        # Wrap in a MagicMock to track call_count, but make it callable as async generator
        wrapped = MagicMock(side_effect=mock_stream)
        llm_client.chat_stream = wrapped

        registry = MagicMock()
        registry.to_openai_functions.return_value = [{"type": "function"}]

        loop = AgentLoop(llm_client=llm_client, tool_registry=registry)
        conversation = Conversation()

        result = await loop.run("question", conversation)
        assert result.text == "Direct answer"
        # Only one LLM call
        assert wrapped.call_count == 1
