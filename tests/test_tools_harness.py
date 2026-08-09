"""Tests for Harness tools (_sys_note / _sys_trim) and the consecutive-user fix.

Covers:
- Registration + schema declaration (fixes H3 — Anthropic/Responses validate
  history tool names against the declared tools list).
- _sys_note / _sys_trim execute output.
- The loop's auto-invoke producing normal tool-call pairs.
- The _ensure_turn_closed guarantee: a cancelled turn followed by the next
  user message must still alternate user/assistant on the Anthropic wire.
"""

import pytest; pytestmark = pytest.mark.unit

import pytest

from slife.agent.conversation import Conversation
from slife.agent.loop import AgentLoop
from slife.tools.factory import create_tools_from_config


def _registry():
    return create_tools_from_config()


def _loop(registry):
    return AgentLoop(llm_client=None, tool_registry=registry, context_window=131072)


# ── Registration & schema ────────────────────────────────────────────────


class TestRegistration:
    def test_tools_auto_discovered(self):
        reg = _registry()
        names = {t.name for t in reg.list_tools()}
        assert "_sys_note" in names
        assert "_sys_trim" in names

    def test_declared_in_schema(self):
        """Both tools appear in to_openai_functions() — the H3 fix."""
        reg = _registry()
        fnames = {f["function"]["name"] for f in reg.to_openai_functions()}
        assert "_sys_note" in fnames
        assert "_sys_trim" in fnames

    def test_harness_category(self):
        reg = _registry()
        assert reg.get("_sys_note").category == "Harness"
        assert reg.get("_sys_trim").category == "Harness"


# ── Tool execution ───────────────────────────────────────────────────────


class TestSysNote:
    @pytest.mark.asyncio
    async def test_renders_status_with_kwargs(self):
        reg = _registry()
        out = await reg.execute("_sys_note", context_window=131072, last_context_tokens=50000)
        assert "上下文占用" in out
        assert "50,000" in out
        assert "(38.1%)" in out

    @pytest.mark.asyncio
    async def test_renders_default_status_bare(self):
        """Called without args (LLM disobeying) still returns a valid status."""
        reg = _registry()
        out = await reg.execute("_sys_note")
        assert "上下文占用" in out


class TestSysTrim:
    """_sys_trim is the trim action itself — no condition check, trims to floor."""

    @staticmethod
    def _cfg():
        from slife.config import Config, ModelConfig
        return Config(
            models=[ModelConfig(ref="t/m", provider="t", api_model="m",
                                display_name="M", api_key="k",
                                context_window=200, supports_vision=False)],
            active_model_ref="t/m", tools=[], agent_id="test",
        )

    @classmethod
    def _tool(cls, conv, cfg):
        from slife.tools.context import ToolContext
        from slife.tools.harness import SysTrimTool
        tool = SysTrimTool()
        object.__setattr__(tool, "_ctx", ToolContext(conversation=conv, config=cfg))
        return tool

    def _conv(self, turns=12):
        conv = Conversation(system_prompt="SYS")
        for i in range(turns):
            conv.add_user_message(f"第{i}轮：一段比较长的用户输入内容，用来撑大上下文占用估计。")
            conv.add_assistant_message(f"这是第{i}轮的回复，也需要一定长度以参与 token 估算。")
        return conv

    @pytest.mark.asyncio
    async def test_trims_oldest_turns_to_floor(self):
        conv = self._conv(12)
        out = await self._tool(conv, self._cfg()).execute(memory_saved=True)
        assert "已裁剪" in out
        assert "memory_search" in out
        # oldest turns removed (each turn carries one user message)
        assert len([m for m in conv.messages if m.get("role") == "user"]) < 12

    @pytest.mark.asyncio
    async def test_no_trim_when_already_below_floor(self):
        conv = self._conv(1)
        out = await self._tool(conv, self._cfg()).execute()
        assert "无可裁剪" in out
        assert len([m for m in conv.messages if m.get("role") == "user"]) == 1


# ── Inline percentage gate → _sys_trim ───────────────────────────────────


class TestTurnTrim:
    """run() shares one context-usage value between _sys_note and the trim gate."""

    @staticmethod
    def _cfg():
        from slife.config import Config, ModelConfig
        return Config(
            models=[ModelConfig(ref="t/m", provider="t", api_model="m",
                                display_name="M", api_key="k",
                                context_window=200, supports_vision=False)],
            active_model_ref="t/m", tools=[], agent_id="test",
        )

    @staticmethod
    def _conv(turns):
        conv = Conversation(system_prompt="SYS")
        for i in range(turns):
            conv.add_user_message(f"第{i}轮：一段比较长的用户输入内容，用来撑大上下文占用估计。")
            conv.add_assistant_message(f"这是第{i}轮的回复，也需要一定长度以参与 token 估算。")
        return conv

    @staticmethod
    def _loop(conv, cfg):
        from unittest.mock import MagicMock
        from slife.tools.context import ToolContext
        from slife.agent.llm_client import StreamChunk
        reg = create_tools_from_config(ctx=ToolContext(conversation=conv, config=cfg))
        llm = MagicMock()
        llm.model_config.thinking_enabled = False

        async def mock_stream(messages, tools, **kwargs):
            yield StreamChunk(content="ok")
        llm.chat_stream = mock_stream

        return AgentLoop(llm_client=llm, tool_registry=reg, context_window=200)

    @pytest.mark.asyncio
    async def test_invokes_sys_trim_when_over_ceiling(self):
        conv = self._conv(12)
        loop = self._loop(conv, self._cfg())
        assert conv.count_tokens() > 160  # over 0.8 × 200 ceiling

        await loop.run("hi", conv)

        assert any(
            m.get("role") == "assistant" and m.get("tool_calls")
            and m["tool_calls"][0]["function"]["name"] == "_sys_trim"
            for m in conv.messages
        )
        # 12 old turns + the new "hi" turn → trimmed well below.
        assert len([m for m in conv.messages if m.get("role") == "user"]) < 12

    @pytest.mark.asyncio
    async def test_no_sys_trim_pair_when_under_ceiling(self):
        conv = self._conv(1)
        loop = self._loop(conv, self._cfg())
        assert conv.count_tokens() <= 160

        await loop.run("hi", conv)

        # No _sys_trim pair at all — fewer tool-call messages when nothing trims.
        assert not any(
            m.get("role") == "assistant" and m.get("tool_calls")
            and m["tool_calls"][0]["function"]["name"] == "_sys_trim"
            for m in conv.messages
        )


# ── Auto-invoke + consecutive-user fix ───────────────────────────────────


class TestConsecutiveUserFix:
    """A cancelled turn must not leave the conversation ending on a user role."""

    def _anthropic_roles(self, conv):
        from slife.agent.llm_backends.anthropic import AnthropicBackend
        _, msgs = AnthropicBackend._oa_msgs_to_anthropic(conv.to_openai_messages())
        return [m["role"] for m in msgs]

    def _assert_alternating(self, conv, label):
        roles = self._anthropic_roles(conv)
        for i in range(len(roles) - 1):
            assert roles[i] != roles[i + 1], (
                f"{label}: consecutive {roles[i]!r} roles on wire: {roles}"
            )

    @pytest.mark.asyncio
    async def test_cancelled_turn_then_next_user_alternates(self):
        reg = _registry()
        loop = _loop(reg)
        conv = Conversation(system_prompt="SYS")

        # Turn 1: user message + harness _sys_note, then cancelled (no reply).
        conv.add_user_message("第一轮：帮我搜一下X")
        await loop._auto_invoke("_sys_note", loop._footer_kwargs(conv, conv.count_tokens()), conv)
        loop._ensure_turn_closed(conv, "")

        # Turn 2: the next user message + fresh _sys_note.
        conv.add_user_message("第二轮：继续")
        await loop._auto_invoke("_sys_note", loop._footer_kwargs(conv, conv.count_tokens()), conv)

        self._assert_alternating(conv, "cancelled-then-next")

    @pytest.mark.asyncio
    async def test_auto_invoke_produces_normal_tool_pair(self):
        reg = _registry()
        loop = _loop(reg)
        conv = Conversation(system_prompt="SYS")
        conv.add_user_message("hi")

        await loop._auto_invoke("_sys_note", loop._footer_kwargs(conv, conv.count_tokens()), conv)

        last = conv.messages[-2:]
        assert last[0]["role"] == "assistant"
        assert last[0]["tool_calls"][0]["function"]["name"] == "_sys_note"
        assert last[1]["role"] == "tool"
        assert "上下文占用" in last[1]["content"]

    def test_ensure_turn_closed_appends_assistant(self):
        reg = _registry()
        loop = _loop(reg)
        conv = Conversation(system_prompt="SYS")
        conv.add_user_message("hi")
        # conversation ends on a user message → close it.
        loop._ensure_turn_closed(conv, "（本轮无输出）")
        assert conv.messages[-1]["role"] == "assistant"
        assert conv.messages[-1]["content"] == "（本轮无输出）"

    def test_ensure_turn_closed_noop_when_assistant(self):
        reg = _registry()
        loop = _loop(reg)
        conv = Conversation(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.add_assistant_message("reply")
        loop._ensure_turn_closed(conv, "")
        # No closing message added — already ends on assistant.
        assert conv.messages[-1]["content"] == "reply"

