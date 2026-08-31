"""Tests for AnthropicBackend — message/tool adaptation, build_kwargs, streaming."""

import pytest; pytestmark = pytest.mark.unit

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.config import ModelConfig
from slife.agent.llm_backends.anthropic import AnthropicBackend
from slife.agent.llm_client import TokenUsage, StreamChunk


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def anthropic_cfg():
    return ModelConfig(
        ref="anthropic/claude-sonnet",
        provider="anthropic",
        api_model="claude-sonnet-4-20250514",
        display_name="Claude Sonnet 4",
        api_key="sk-ant-test",
        base_url="https://api.anthropic.com",
        api="anthropic-messages",
        max_tokens=8192,
        context_window=200000,
        temperature=0.7,
        top_p=1.0,
        thinking_enabled=False,
    )


@pytest.fixture(scope="module")
def anthropic_thinking_cfg():
    return ModelConfig(
        ref="bailian/qwen3.8-max",
        provider="bailian",
        api_model="qwen3.8-max",
        display_name="Qwen3.8 Max",
        api_key="sk-test",
        base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
        api="anthropic-messages",
        max_tokens=131072,
        context_window=983616,
        temperature=0.7,
        top_p=1.0,
        thinking_enabled=True,
        compat={"thinkingFormat": "openai"},
    )


# ── Message Adaptation ────────────────────────────────────────────────


class TestOaMsgsToAnthropic:
    """Tests for _oa_msgs_to_anthropic — OpenAI → Anthropic message adaptation."""

    def test_system_prompt_extracted(self):
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi"},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        assert system == [
            {"type": "text", "text": "You are helpful.",
             "cache_control": {"type": "ephemeral"}},
        ]
        assert len(converted) == 1
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hi"

    def test_multiple_system_blocks_last_gets_cache_control(self):
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "system", "content": "Be concise."},
            {"role": "user", "content": "OK"},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        assert system == [
            {"type": "text", "text": "Be helpful."},
            {"type": "text", "text": "Be concise.",
             "cache_control": {"type": "ephemeral"}},
        ]
        assert len(converted) == 1

    def test_system_no_cache_control_when_disabled(self):
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "OK"},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(
            messages, cache_control=False,
        )
        assert system == [{"type": "text", "text": "Be helpful."}]

    def test_user_text_passthrough(self):
        messages = [{"role": "user", "content": "Hello world"}]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        assert system is None
        assert converted[0]["role"] == "user"
        assert converted[0]["content"] == "Hello world"

    def test_user_multimodal_image_conversion(self):
        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "What is this?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
            ],
        }]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        blocks = converted[0]["content"]
        assert blocks[0] == {"type": "text", "text": "What is this?"}
        assert blocks[1]["type"] == "image"
        assert blocks[1]["source"]["type"] == "base64"
        assert blocks[1]["source"]["media_type"] == "image/png"
        assert blocks[1]["source"]["data"] == "abc123"

    def test_assistant_text_to_content_blocks(self):
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "Hello!", "tool_calls": None},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        assistant = converted[1]
        assert assistant["role"] == "assistant"
        assert isinstance(assistant["content"], list)
        assert assistant["content"][0] == {"type": "text", "text": "Hello!"}

    def test_assistant_tool_calls_to_tool_use(self):
        messages = [
            {"role": "user", "content": "search cats"},
            {"role": "assistant", "content": "Searching...", "tool_calls": [
                {"id": "tc_1", "type": "function",
                 "function": {"name": "web_search", "arguments": '{"query":"cats"}'}},
                {"id": "tc_2", "type": "function",
                 "function": {"name": "fetch", "arguments": '{"url":"http://x"}'}},
            ]},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        blocks = converted[1]["content"]
        # text block
        assert blocks[0]["type"] == "text"
        assert blocks[0]["text"] == "Searching..."
        # tool_use blocks
        assert blocks[1]["type"] == "tool_use"
        assert blocks[1]["id"] == "tc_1"
        assert blocks[1]["name"] == "web_search"
        assert blocks[1]["input"] == {"query": "cats"}
        assert blocks[2]["type"] == "tool_use"
        assert blocks[2]["id"] == "tc_2"
        assert blocks[2]["name"] == "fetch"

    def test_assistant_none_content_with_tool_calls(self):
        messages = [
            {"role": "user", "content": "search"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc", "type": "function",
                 "function": {"name": "echo", "arguments": '{}'}},
            ]},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        blocks = converted[1]["content"]
        # No text block when content is None
        assert blocks[0]["type"] == "tool_use"

    def test_tool_result_conversion(self):
        messages = [
            {"role": "user", "content": "search"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "search", "arguments": '{"q":"test"}'}},
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": "Results found."},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        tool_msg = converted[2]
        assert tool_msg["role"] == "user"
        assert tool_msg["content"][0]["type"] == "tool_result"
        assert tool_msg["content"][0]["tool_use_id"] == "call_1"
        assert tool_msg["content"][0]["content"] == "Results found."

    def test_full_conversation_flow(self):
        messages = [
            {"role": "system", "content": "Be helpful."},
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "Let me calculate.", "tool_calls": [
                {"id": "tc1", "type": "function",
                 "function": {"name": "calculator", "arguments": '{"expr":"2+2"}'}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "4"},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        assert system == [
            {"type": "text", "text": "Be helpful.",
             "cache_control": {"type": "ephemeral"}},
        ]
        assert len(converted) == 3
        assert converted[0]["role"] == "user"
        assert converted[1]["role"] == "assistant"
        assert converted[2]["role"] == "user"  # tool result

    def test_multi_tool_results_coalesced_into_one_user(self):
        """REVIEW W1 — two tool results become ONE user block, not two
        consecutive ``user`` messages (strict-alternation endpoints 400)."""
        messages = [
            {"role": "user", "content": "run both"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function",
                 "function": {"name": "a", "arguments": "{}"}},
                {"id": "tc2", "type": "function",
                 "function": {"name": "b", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "result 1"},
            {"role": "tool", "tool_call_id": "tc2", "content": "result 2"},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        roles = [m["role"] for m in converted]
        assert roles == ["user", "assistant", "user"]  # strict alternation
        tool_user = converted[2]
        assert tool_user["role"] == "user"
        results = tool_user["content"]
        assert [r["type"] for r in results] == ["tool_result", "tool_result"]
        assert results[0]["tool_use_id"] == "tc1"
        assert results[0]["content"] == "result 1"
        assert results[1]["tool_use_id"] == "tc2"
        assert results[1]["content"] == "result 2"

    def test_tool_result_then_user_text_merged(self):
        """REVIEW W1 — a user text message right after tool results is
        merged into the same user block (still one user message)."""
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function",
                 "function": {"name": "a", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "done"},
            {"role": "user", "content": "then what?"},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        roles = [m["role"] for m in converted]
        assert roles == ["user", "assistant", "user"]  # 3, not 4 — merged
        blocks = converted[2]["content"]
        assert blocks[0]["type"] == "tool_result"
        assert blocks[1] == {"type": "text", "text": "then what?"}

    def test_tool_results_followed_by_assistant_still_alternate(self):
        """REVIEW W1 — tool results flushed before a following assistant
        message keep user/assistant alternation."""
        messages = [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "tc1", "type": "function",
                 "function": {"name": "a", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "r"},
            {"role": "assistant", "content": "done."},
        ]
        system, converted = AnthropicBackend._oa_msgs_to_anthropic(messages)
        roles = [m["role"] for m in converted]
        assert roles == ["user", "assistant", "user", "assistant"]
        assert converted[2]["content"][0]["type"] == "tool_result"


# ── Tool Adaptation ───────────────────────────────────────────────────


class TestOaToolsToAnthropic:
    """Tests for _oa_tools_to_anthropic."""

    def test_basic_conversion(self):
        tools = [
            {"type": "function", "function": {
                "name": "echo",
                "description": "Echoes input.",
                "parameters": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
            }},
        ]
        result = AnthropicBackend._oa_tools_to_anthropic(tools)
        assert len(result) == 1
        assert result[0]["name"] == "echo"
        assert result[0]["description"] == "Echoes input."
        assert result[0]["input_schema"] == {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

    def test_keeps_meta_params(self):
        """Harness meta-params are visible to the model on every backend
        (three-backend consistency: _timeout/_async/_approve work the same
        on Anthropic, Responses, and OpenAI-completions)."""
        tools = [
            {"type": "function", "function": {
                "name": "run",
                "description": "Runs something.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "arg1": {"type": "string"},
                        "_timeout": {"type": "integer", "description": "Harness-injected"},
                        "_async": {"type": "boolean"},
                    },
                    "required": ["arg1"],
                },
            }},
        ]
        result = AnthropicBackend._oa_tools_to_anthropic(tools)
        props = result[0]["input_schema"]["properties"]
        assert "arg1" in props
        assert "_timeout" in props
        assert "_async" in props

    def test_empty_tools(self):
        assert AnthropicBackend._oa_tools_to_anthropic([]) == []

    def test_multiple_tools(self):
        tools = [
            {"type": "function", "function": {"name": "a", "description": "A", "parameters": {}}},
            {"type": "function", "function": {"name": "b", "description": "B", "parameters": {}}},
        ]
        result = AnthropicBackend._oa_tools_to_anthropic(tools)
        assert len(result) == 2
        assert [t["name"] for t in result] == ["a", "b"]


# ── Build Kwargs ──────────────────────────────────────────────────────


class TestAnthropicBuildKwargs:
    """Tests for AnthropicBackend._build_kwargs."""

    def test_basic_kwargs(self, anthropic_cfg):
        backend = AnthropicBackend(anthropic_cfg)
        kw = backend._build_kwargs([{"role": "user", "content": "hi"}], None)
        assert kw["model"] == "claude-sonnet-4-20250514"
        assert kw["max_tokens"] == 8192
        # Sampling params ride inside extra_body since SDK 1.x dropped the
        # typed temperature/top_p kwargs (still valid request-body fields).
        assert kw["extra_body"] == {"temperature": 0.7, "top_p": 1.0}
        assert "temperature" not in kw
        assert "top_p" not in kw
        assert "system" not in kw
        assert "tools" not in kw

    def test_with_system_prompt(self, anthropic_cfg):
        """Official Anthropic endpoint → system block list with cache_control."""
        backend = AnthropicBackend(anthropic_cfg)
        kw = backend._build_kwargs(
            [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "hi"}],
            None,
        )
        assert kw["system"] == [
            {"type": "text", "text": "Be helpful.",
             "cache_control": {"type": "ephemeral"}},
        ]
        assert len(kw["messages"]) == 1  # system stripped from messages

    def test_with_system_prompt_compat_endpoint_no_cache(self, anthropic_thinking_cfg):
        """Bailian/Qwen Anthropic-compatible endpoint → no cache_control."""
        backend = AnthropicBackend(anthropic_thinking_cfg)
        kw = backend._build_kwargs(
            [{"role": "system", "content": "Be helpful."}, {"role": "user", "content": "hi"}],
            None,
        )
        assert kw["system"] == [{"type": "text", "text": "Be helpful."}]

    def test_with_tools(self, anthropic_cfg):
        backend = AnthropicBackend(anthropic_cfg)
        tools = [{"type": "function", "function": {"name": "echo", "description": "E", "parameters": {}}}]
        kw = backend._build_kwargs([{"role": "user", "content": "hi"}], tools)
        assert "tools" in kw
        assert kw["tools"][0]["name"] == "echo"
        assert kw["tools"][0]["input_schema"] == {"type": "object"}

    def test_thinking_enabled_skipped_for_openai_compat(self, anthropic_thinking_cfg):
        """When compat.thinkingFormat is 'openai', thinking param is NOT sent."""
        backend = AnthropicBackend(anthropic_thinking_cfg)
        kw = backend._build_kwargs([{"role": "user", "content": "hi"}], None)
        assert "thinking" not in kw

    def test_thinking_enabled_standard_anthropic(self):
        """Standard Anthropic model with thinking sends thinking param."""
        cfg = ModelConfig(
            ref="anthropic/claude-sonnet",
            provider="anthropic",
            api_model="claude-sonnet-4-20250514",
            display_name="Claude Sonnet 4",
            api_key="sk-ant-test",
            api="anthropic-messages",
            max_tokens=8192,
            thinking_enabled=True,
        )
        backend = AnthropicBackend(cfg)
        kw = backend._build_kwargs([{"role": "user", "content": "hi"}], None)
        assert "thinking" in kw
        assert kw["thinking"]["type"] == "enabled"
        assert kw["thinking"]["budget_tokens"] == 4096  # max_tokens // 2

    def test_thinking_disabled(self, anthropic_cfg):
        backend = AnthropicBackend(anthropic_cfg)
        kw = backend._build_kwargs([{"role": "user", "content": "hi"}], None)
        assert "thinking" not in kw

    def test_temperature_zero_not_sent(self, anthropic_cfg):
        # Anthropic doesn't accept temperature=0; should omit it
        cfg = ModelConfig(
            ref="test/m", provider="test", api_model="m", display_name="M",
            api_key="k", api="anthropic-messages", temperature=0.0,
        )
        backend = AnthropicBackend(cfg)
        kw = backend._build_kwargs([{"role": "user", "content": "hi"}], None)
        # temperature=0 must never reach the body; top_p may still ride along.
        body = kw.get("extra_body", {})
        assert "temperature" not in body


# ── System cache_control guard ─────────────────────────────────────────


class TestUseSystemCacheControl:
    """_use_system_cache_control — default by endpoint, overridable via compat."""

    def _backend(self, **kw):
        cfg = ModelConfig(
            ref="test/m", provider="anthropic", api_model="m", display_name="M",
            api_key="k", api="anthropic-messages", **kw,
        )
        return AnthropicBackend(cfg)

    def test_default_on_for_official_anthropic(self):
        backend = self._backend(base_url="https://api.anthropic.com")
        assert backend._use_system_cache_control() is True

    def test_default_off_for_compat_endpoint(self):
        backend = self._backend(
            base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
        )
        assert backend._use_system_cache_control() is False

    def test_compat_override_forces_off(self):
        backend = self._backend(
            base_url="https://api.anthropic.com",
            compat={"cacheControl": False},
        )
        assert backend._use_system_cache_control() is False

    def test_compat_override_forces_on(self):
        backend = self._backend(
            base_url="https://token-plan.cn-beijing.maas.aliyuncs.com/apps/anthropic",
            compat={"cacheControl": True},
        )
        assert backend._use_system_cache_control() is True


# ── Chat Streaming ────────────────────────────────────────────────────


class TestAnthropicChatStream:
    """Tests for AnthropicBackend.chat_stream — mocked event stream."""

    @pytest.mark.asyncio
    async def test_stream_text_chunks(self, anthropic_cfg):
        backend = AnthropicBackend(anthropic_cfg)
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        events = _make_async_iter([
            _AE("message_start", message=_AMessage(input_tokens=10)),
            _AE("content_block_start", index=0, content_block=_ABlock("text")),
            _AE("content_block_delta", index=0, delta=_ADelta("text_delta", text="Hello")),
            _AE("content_block_delta", index=0, delta=_ADelta("text_delta", text=" world")),
            _AE("content_block_stop", index=0),
            _AE("message_delta", usage=_AUsage(0, 5)),
        ])
        mock_stream.__aiter__ = lambda s: events

        with patch.object(backend.client.messages, "stream", return_value=mock_stream):
            chunks = [c async for c in backend.chat_stream(
                [{"role": "user", "content": "hi"}]
            )]

        contents = [c.content for c in chunks if c.content]
        assert "".join(contents) == "Hello world"
        usages = [c.usage for c in chunks if c.usage]
        assert len(usages) == 1
        assert usages[0].prompt_tokens == 10
        assert usages[0].completion_tokens == 5
        assert usages[0].total_tokens == 15

    @pytest.mark.asyncio
    async def test_stream_thinking_chunks(self, anthropic_thinking_cfg):
        backend = AnthropicBackend(anthropic_thinking_cfg)
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        events = _make_async_iter([
            _AE("message_start", message=_AMessage(input_tokens=20)),
            _AE("content_block_start", index=0, content_block=_ABlock("thinking")),
            _AE("content_block_delta", index=0, delta=_ADelta("thinking_delta", thinking="Let me think...")),
            _AE("content_block_delta", index=0, delta=_ADelta("thinking_delta", thinking=" about this.")),
            _AE("content_block_stop", index=0),
            _AE("message_delta", usage=_AUsage(0, 10)),
        ])
        mock_stream.__aiter__ = lambda s: events

        with patch.object(backend.client.messages, "stream", return_value=mock_stream):
            chunks = [c async for c in backend.chat_stream(
                [{"role": "user", "content": "hi"}]
            )]

        thinkings = [c.thinking for c in chunks if c.thinking]
        assert "".join(thinkings) == "Let me think... about this."

    @pytest.mark.asyncio
    async def test_stream_tool_use_deltas(self, anthropic_cfg):
        backend = AnthropicBackend(anthropic_cfg)
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        events = _make_async_iter([
            _AE("message_start", message=_AMessage(input_tokens=5)),
            _AE("content_block_start", index=0, content_block=_ABlock("tool_use", id="call_1", name="web_search")),
            _AE("content_block_delta", index=0, delta=_ADelta("input_json_delta", partial_json='{"query"')),
            _AE("content_block_delta", index=0, delta=_ADelta("input_json_delta", partial_json=': "cats"}')),
            _AE("content_block_stop", index=0),
            _AE("message_delta", usage=_AUsage(0, 3)),
        ])
        mock_stream.__aiter__ = lambda s: events

        with patch.object(backend.client.messages, "stream", return_value=mock_stream):
            chunks = [c async for c in backend.chat_stream(
                [{"role": "user", "content": "search cats"}]
            )]

        tool_chunks = [c for c in chunks if c.tool_deltas]
        assert len(tool_chunks) == 2
        # First delta
        assert tool_chunks[0].tool_deltas[0]["id"] == "call_1"
        assert tool_chunks[0].tool_deltas[0]["function"]["name"] == "web_search"

    @pytest.mark.asyncio
    async def test_stream_cancellation(self, anthropic_cfg):
        import asyncio
        backend = AnthropicBackend(anthropic_cfg)
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_stream)
        mock_stream.__aexit__ = AsyncMock(return_value=None)
        cancel = asyncio.Event()
        cancel.set()  # cancel immediately

        events = _make_async_iter([
            _AE("message_start", message=_AMessage(input_tokens=1)),
        ])
        mock_stream.__aiter__ = lambda s: events

        with patch.object(backend.client.messages, "stream", return_value=mock_stream):
            chunks = [c async for c in backend.chat_stream(
                [{"role": "user", "content": "hi"}], cancel_event=cancel
            )]
        # Should yield nothing after cancellation check
        assert len(chunks) == 0  # cancelled before first event


# ── Mock helpers for Anthropic streaming events ──────────────────────


def _make_async_iter(items):
    async def gen():
        for item in items:
            yield item
    return gen()


class _AE:
    """Mock Anthropic streaming event."""
    __slots__ = ("type", "index", "content_block", "delta", "message", "usage")
    def __init__(self, type, index=0, content_block=None, delta=None, message=None, usage=None):
        self.type = type
        self.index = index
        self.content_block = content_block
        self.delta = delta
        self.message = message
        self.usage = usage


class _ABlock:
    __slots__ = ("type", "id", "name")
    def __init__(self, type, id=None, name=None):
        self.type = type
        self.id = id
        self.name = name


class _ADelta:
    __slots__ = ("type", "text", "thinking", "partial_json")
    def __init__(self, type, text=None, thinking=None, partial_json=None):
        self.type = type
        self.text = text
        self.thinking = thinking
        self.partial_json = partial_json


class _AUsage:
    __slots__ = ("input_tokens", "output_tokens")
    def __init__(self, input_tokens=0, output_tokens=0):
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _AMessage:
    """Wrapper for message_start event's .message attribute."""
    __slots__ = ("usage",)
    def __init__(self, input_tokens=0):
        self.usage = _AUsage(input_tokens=input_tokens)
