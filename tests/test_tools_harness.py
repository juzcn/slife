"""Tests for Harness tools (_sys_note) and the internal trim + marker.

Covers:
- Registration + schema declaration (fixes H3 — Anthropic/Responses validate
  history tool names against the declared tools list).
- _sys_note execute output.
- The loop's auto-invoke producing normal tool-call pairs.
- _trim_after_save: internal trim (after a turn is saved) uses real usage,
  appends a runtime trim note, and respects the restore exemption.
- The _ensure_turn_consistent guarantee: an interrupted turn is restored to
  a consistent state — no orphaned tool_calls, and no consecutive user
  messages on the Anthropic wire (which rejects them).
"""

import pytest; pytestmark = pytest.mark.unit

import pytest

from slife.agent.message_history import MessageHistory
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
        assert "_sys_trim" not in names  # trim is now an internal mechanism

    def test_declared_in_schema(self):
        """The note appears in to_openai_functions() — the H3 fix."""
        reg = _registry()
        fnames = {f["function"]["name"] for f in reg.to_openai_functions()}
        assert "_sys_note" in fnames
        assert "_sys_trim" not in fnames

    def test_sys_note_category(self):
        reg = _registry()
        assert reg.get("_sys_note").category == "Models"


# ── Tool execution ───────────────────────────────────────────────────────


class TestSysNote:
    @pytest.mark.asyncio
    async def test_renders_status_with_kwargs(self):
        reg = _registry()
        out = await reg.execute("_sys_note", context_window=131072, last_context_tokens=50000)
        assert "Context usage" in out
        assert "50,000" in out
        assert "(38.1%)" in out

    @pytest.mark.asyncio
    async def test_renders_default_status_bare(self):
        """Called without args (LLM disobeying) still returns a valid status."""
        reg = _registry()
        out = await reg.execute("_sys_note")
        assert "Context usage" in out


# ── Internal trim after save (_trim_after_save) ──────────────────────────


class TestTrimAfterSave:
    """_trim_after_save: called after a turn is saved, uses real usage,
    appends a runtime trim note, and never shreds a restored context."""

    @staticmethod
    def _cfg():
        from slife.config import Config, ModelConfig
        return Config(
            models=[ModelConfig(ref="t/m", provider="t", api_model="m",
                                display_name="M", api_key="k",
                                context_window=200, supports_vision=False)],
            active_model_ref="t/m", tools=[], agent_name="test",
        )

    @staticmethod
    def _conv(turns):
        conv = MessageHistory(system_prompt="SYS")
        for i in range(turns):
            conv.add_user_message(f"第{i}轮：一段比较长的用户输入内容，用来撑大Context usage估计。")
            conv.add_assistant_message(f"这是第{i}轮的回复，也需要一定长度以参与 token 估算。")
        return conv

    @staticmethod
    def _loop(conv, cfg, **kwargs):
        return AgentLoop(
            llm_client=None, tool_registry=create_tools_from_config(),
            context_window=200, context_ceiling=0.8, context_floor=0.2,
            advance_context_start=kwargs.get("advance"),
        )

    async def _prime_usage(self, loop, conv):
        """Simulate the just-finished API call's real usage for this conv."""
        from slife.agent.llm_client import TokenUsage
        loop._usage_by_history[id(conv)] = TokenUsage(
            prompt_tokens=conv.count_tokens(), total_tokens=conv.count_tokens(),
        )

    @pytest.mark.asyncio
    async def test_trims_to_floor_when_over_ceiling(self):
        conv = self._conv(12)
        loop = self._loop(conv, self._cfg())
        await self._prime_usage(loop, conv)
        assert conv.count_tokens() > 160  # over 0.8 × 200 ceiling

        await loop._trim_after_save(conv)

        # oldest turns removed (each turn carries one user message)
        assert len([m for m in conv.messages if m.get("role") == "user"]) < 12
        # trim note appended to the last assistant message
        assert "oldest turns have been removed from context" in conv.messages[-1].get("content", "")
        # no tool-call pair was produced (internal mechanism, not a tool)
        assert not any(m.get("tool_calls") for m in conv.messages)

    @pytest.mark.asyncio
    async def test_no_trim_when_under_ceiling(self):
        conv = self._conv(1)
        loop = self._loop(conv, self._cfg())
        await self._prime_usage(loop, conv)
        assert conv.count_tokens() <= 160

        await loop._trim_after_save(conv)

        assert len([m for m in conv.messages if m.get("role") == "user"]) == 1
        assert not any("oldest turns have been removed from context" in (m.get("content") or "") for m in conv.messages)

    @pytest.mark.asyncio
    async def test_advances_context_start(self):
        conv = self._conv(12)
        advanced: list[int] = []

        async def advance(count):
            advanced.append(count)
            return True

        loop = self._loop(conv, self._cfg(), advance=advance)
        await self._prime_usage(loop, conv)
        await loop._trim_after_save(conv)

        assert advanced, "advance_context_start should be called with the removed count"
        users = len([m for m in conv.messages if m.get("role") == "user"])
        assert advanced[0] >= 12 - users  # advanced by at least the removed turns

    @pytest.mark.asyncio
    async def test_restored_context_not_shredded_on_first_turn(self):
        """A freshly-restored history is a pre-exit state — the first
        trim after restore must not compact it (even over ceiling)."""
        conv = self._conv(12)
        loop = self._loop(conv, self._cfg())
        loop._just_restored_history = id(conv)
        await self._prime_usage(loop, conv)
        assert conv.count_tokens() > 160

        await loop._trim_after_save(conv)

        # The marker is consumed and nothing was trimmed.
        assert loop._just_restored_history is None
        assert len([m for m in conv.messages if m.get("role") == "user"]) == 12
        assert not any("oldest turns have been removed from context" in (m.get("content") or "") for m in conv.messages)

    @pytest.mark.asyncio
    async def test_second_turn_after_restore_trims(self):
        """Once the restore marker is consumed, the live rules apply."""
        conv = self._conv(12)
        loop = self._loop(conv, self._cfg())
        loop._just_restored_history = id(conv)
        await self._prime_usage(loop, conv)
        # First save consumes the marker without trimming...
        await loop._trim_after_save(conv)
        assert loop._just_restored_history is None
        # ...but the second save trims (real usage still over ceiling).
        await self._prime_usage(loop, conv)
        await loop._trim_after_save(conv)
        assert len([m for m in conv.messages if m.get("role") == "user"]) < 12
        assert "oldest turns have been removed from context" in conv.messages[-1].get("content", "")

    @pytest.mark.asyncio
    async def test_trim_resets_time_start_when_dates_exhausted(self):
        """When a trim pops every tracked turn date, 'Context covers' must
        reset to the current turn — not point at a turn that was removed."""
        conv = self._conv(12)
        loop = self._loop(conv, self._cfg())
        # Simulate: only ONE tracked turn date exists (a fresh session where
        # _context_time_start holds the very first turn and nothing else).
        loop._context_time_start = "2026-08-01 10:00:00"
        loop._context_turn_dates = ["2026-08-01 10:05:00"]
        await self._prime_usage(loop, conv)

        await loop._trim_after_save(conv)

        # The single tracked date was popped; the range must not point at it.
        assert loop._context_turn_dates == []
        assert loop._context_time_start != "2026-08-01 10:05:00"
        assert loop._context_time_start  # reset to a fresh current-turn stamp


# ── Auto-invoke + consecutive-user fix ───────────────────────────────────


class TestConsecutiveUserFix:
    """A cancelled turn must not leave the history ending on a user role."""

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
        conv = MessageHistory(system_prompt="SYS")

        # Turn 1: user message + harness _sys_note, then cancelled (no reply).
        conv.add_user_message("第一轮：帮我搜一下X")
        await loop._auto_invoke("_sys_note", loop._footer_kwargs(conv, conv.count_tokens()), conv)
        conv._ensure_turn_consistent("")

        # Turn 2: the next user message + fresh _sys_note.
        conv.add_user_message("第二轮：继续")
        await loop._auto_invoke("_sys_note", loop._footer_kwargs(conv, conv.count_tokens()), conv)

        self._assert_alternating(conv, "cancelled-then-next")

    @pytest.mark.asyncio
    async def test_auto_invoke_produces_normal_tool_pair(self):
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")

        await loop._auto_invoke("_sys_note", loop._footer_kwargs(conv, conv.count_tokens()), conv)

        last = conv.messages[-2:]
        assert last[0]["role"] == "assistant"
        assert last[0]["tool_calls"][0]["function"]["name"] == "_sys_note"
        assert last[1]["role"] == "tool"
        assert "Context usage" in last[1]["content"]

    def test_context_time_start_change_detected(self):
        """'Context covers' is reported on the first footer, then only when
        the start time changes (restore sets it, trim advances it)."""
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")

        loop._context_time_start = "2026-01-01T00:00:00+08:00"
        first = loop._footer_kwargs(conv, conv.count_tokens())
        assert first.get("context_time_start") == "2026-01-01T00:00:00+08:00"

        # Unchanged on the next turn → not reported again.
        second = loop._footer_kwargs(conv, conv.count_tokens())
        assert "context_time_start" not in second

        # A trim advances the start → reported again.
        loop._context_time_start = "2026-02-01T00:00:00+08:00"
        third = loop._footer_kwargs(conv, conv.count_tokens())
        assert third.get("context_time_start") == "2026-02-01T00:00:00+08:00"

    def test_ensure_turn_consistent_appends_assistant(self):
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        # history ends on a user message → close it.
        conv._ensure_turn_consistent("(Turn interrupted)")
        assert conv.messages[-1]["role"] == "assistant"
        assert conv.messages[-1]["content"] == "(Turn interrupted)"

    def test_ensure_turn_consistent_noop_when_assistant(self):
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        conv.add_assistant_message("reply")
        conv._ensure_turn_consistent("")
        # No closing message added — already ends on assistant.
        assert conv.messages[-1]["content"] == "reply"

    def test_ensure_turn_consistent_repairs_orphaned_call(self):
        """An interrupted turn ending on an orphaned tool_call is repaired.

        The orphaned assistant tool_call gets a synthetic tool result, and
        because that makes the turn end on a tool role (user on the
        Anthropic wire), a closing assistant is appended too — so the turn
        is consistent and no consecutive user would reach the API.
        """
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")
        # Turn interrupted mid-tool-call: assistant tool_call, no result.
        conv.add_assistant_message(
            "",
            tool_calls=[
                {"id": "orphan1", "type": "function",
                 "function": {"name": "search", "arguments": "{}"}},
            ],
        )

        conv._ensure_turn_consistent("(Turn interrupted)")

        # system + user + assistant(orphan) + tool(synthetic) + assistant(closing)
        roles = [m["role"] for m in conv.messages]
        assert roles == ["system", "user", "assistant", "tool", "assistant"]
        # synthetic result targets the orphaned call
        assert conv.messages[3]["tool_call_id"] == "orphan1"
        assert "interrupted" in conv.messages[3]["content"]
        # closing assistant keeps roles alternating
        assert conv.messages[-1]["role"] == "assistant"

