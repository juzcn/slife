"""Tests for Harness tools (_turn_prompt) and the internal trim + marker.

Covers:
- Registration + schema declaration (fixes H3 — Anthropic/Responses validate
  history tool names against the declared tools list).
- _turn_prompt execute output.
- The loop's auto-invoke producing normal tool-call pairs.
- _trim_after_save: internal trim (after a turn is saved) uses real usage,
  appends a runtime trim note, and respects the restore exemption.
- The _ensure_turn_consistent guarantee: an interrupted turn is restored to
  a consistent state — no orphaned tool_calls, and no consecutive user
  messages on the Anthropic wire (which rejects them).
"""

import pytest; pytestmark = pytest.mark.unit

import pytest

from types import SimpleNamespace

from slife.agent.message_history import MessageHistory
from slife.agent.loop import AgentLoop
from slife.tools.factory import create_tools_from_config


def _registry():
    return create_tools_from_config()


def _loop(registry):
    return AgentLoop(llm_client=None, tool_registry=registry, context_window=131072)


class _ReplyLLM:
    """A client that always answers the discriminator with *reply*.

    The rebuild tests drive the path *after* the discriminator, so they need a
    reply that actually asks for something: an empty one is "no recall needed"
    and a missing client is "no reply" — both keep the context.
    """

    def __init__(self, reply: str):
        self.reply = reply

    async def chat(self, messages, **_kwargs):
        msg = SimpleNamespace(content=self.reply)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)]), None


def _registry_with_recall(with_recall: bool = True):
    """A registry carrying ``turn_recall``.

    The discriminator's instruction IS that tool's schema, so the call needs the
    tool registered — in production the memdb plugin provides it, and a process
    without it (memdb down) skips the discriminator entirely.
    """
    from slife.tools.base import Tool

    class _TurnRecall(Tool):
        name = "turn_recall"
        description = "Recall turns into context: a query searches, a time range browses."
        parameters = {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search text."}},
            "required": [],
        }
        category = "memdb"

        async def execute(self, **kwargs): return "{}"

    reg = create_tools_from_config()
    if with_recall:
        reg.register(_TurnRecall())
    return reg


# ── Registration & schema ────────────────────────────────────────────────


class TestRegistration:
    def test_tools_auto_discovered(self):
        reg = _registry()
        names = {t.name for t in reg.list_tools()}
        assert "_turn_prompt" in names
        assert "_sys_trim" not in names  # trim is now an internal mechanism

    def test_declared_in_schema(self):
        """The prompt appears in to_openai_functions() — the H3 fix."""
        reg = _registry()
        fnames = {f["function"]["name"] for f in reg.to_openai_functions()}
        assert "_turn_prompt" in fnames
        assert "_sys_trim" not in fnames

    def test_turn_prompt_category(self):
        reg = _registry()
        assert reg.get("_turn_prompt").category == "Models"


class TestCheckNewInput:
    """_check_new_input — the mid-turn input injector (cut-in mode).

    A zero-argument harness tool: the loop records assistant(tool_calls) with
    EMPTY arguments (no duplication — the message exists once, in the tool
    result), and the tool pulls the first queued message itself via the
    ``extract_injectable`` context hook, returning its bare text.
    """

    @staticmethod
    def _ctx_registry(extract):
        from slife.tools.context import ToolContext

        reg = _registry()
        ctx = ToolContext()
        ctx.extract_injectable = extract  # instance attr — never a bound method
        reg.get("_check_new_input")._ctx = ctx
        return reg

    def test_registered_and_in_schema(self):
        reg = _registry()
        names = {t.name for t in reg.list_tools()}
        assert "_check_new_input" in names
        fnames = {f["function"]["name"] for f in reg.to_openai_functions()}
        assert "_check_new_input" in fnames
        # zero-argument — nothing for the model to choose
        assert reg.get("_check_new_input").parameters.get("properties", {}) == {}

    @pytest.mark.asyncio
    async def test_execute_returns_bare_content(self):
        from slife.a2a.identity import AgentMessage, AgentName
        from slife.tools.context import ToolContext

        reg = _registry()
        content = '[A2A:{"from": "jack", "task_id": "cid-1"}] do X'
        ctx = ToolContext()
        ctx.extract_injectable = lambda: AgentMessage(  # instance attr, not bound
            source=AgentName("jack"), content=content,
        )
        reg.get("_check_new_input")._ctx = ctx

        out = await reg.execute("_check_new_input")
        assert out == content  # the inbox's bare text, no wrapper

    @pytest.mark.asyncio
    async def test_execute_no_ctx_or_empty_returns_notice(self):
        reg = _registry()
        out = await reg.execute("_check_new_input")  # _ctx is None on bare registry
        assert out == "No pending input — nothing to inject at this boundary."

    @pytest.mark.asyncio
    async def test_auto_invoke_records_pair_with_empty_args(self):
        from slife.a2a.identity import AgentMessage, AgentName

        content = '[A2A:{"from": "jack", "task_id": "cid-1"}] do X'
        reg = self._ctx_registry(lambda: AgentMessage(
            source=AgentName("jack"), content=content,
        ))
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("working…")

        await loop._auto_invoke("_check_new_input", {}, conv)

        last = conv.messages[-2:]
        assert last[0]["role"] == "assistant"
        assert last[0]["tool_calls"][0]["function"]["name"] == "_check_new_input"
        assert last[0]["tool_calls"][0]["function"]["arguments"] == "{}"
        assert last[1]["role"] == "tool"
        assert last[1]["content"] == content  # bare text, single copy

    @pytest.mark.asyncio
    async def test_auto_invoke_extract_none_records_notice(self):
        reg = self._ctx_registry(lambda: None)
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("working…")

        await loop._auto_invoke("_check_new_input", {}, conv)

        last = conv.messages[-2:]
        assert last[0]["tool_calls"][0]["function"]["name"] == "_check_new_input"
        assert "No pending input" in last[1]["content"]

    @pytest.mark.asyncio
    async def test_auto_invoke_cancel_guard_skips_extraction(self):
        from slife.a2a.identity import AgentMessage, AgentName

        extracted: list = []

        def _extract():
            extracted.append(1)
            return AgentMessage(source=AgentName("jack"), content="x")

        reg = self._ctx_registry(_extract)
        loop = _loop(reg)
        loop._cancel_event.set()
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("working…")

        await loop._auto_invoke("_check_new_input", {}, conv)

        assert extracted == []  # a cancelled turn never drops the queued message


# ── Tool execution ───────────────────────────────────────────────────────


class TestTurnPrompt:
    @pytest.mark.asyncio
    async def test_renders_status_with_kwargs(self):
        reg = _registry()
        out = await reg.execute("_turn_prompt", context_window=131072, last_context_tokens=50000)
        assert "Context usage" in out
        assert "50,000" in out
        assert "(38.1%)" in out

    @pytest.mark.asyncio
    async def test_renders_default_status_bare(self):
        """Called without args (LLM disobeying) still returns a valid status."""
        reg = _registry()
        out = await reg.execute("_turn_prompt")
        assert "Context usage" in out

    @pytest.mark.asyncio
    async def test_renders_schedule_reminder(self):
        reg = _registry()
        out = await reg.execute(
            "_turn_prompt",
            schedule_status=[{"name": "daily", "due_at": "2026-08-25T09:00:00",
                              "status": "failed"}],
        )
        assert "Scheduled runs not settled" in out
        assert "daily @ 2026-08-25T09:00:00 (failed)" in out
        assert "run_schedule_now" in out

    @pytest.mark.asyncio
    async def test_renders_restart_flag(self):
        reg = _registry()
        out = await reg.execute("_turn_prompt", restarted=True)
        assert "System restarted" in out
        out = await reg.execute("_turn_prompt", restarted=False)
        assert "System restarted" not in out


class TestTurnPromptKwargsRestarted:
    """_turn_prompt_kwargs flags the first prompt after a session restore."""

    def test_flags_restored_history_once(self):
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        loop._just_restored_history = id(conv)  # restore_session marks this

        kwargs = loop._turn_prompt_kwargs(conv, conv.count_tokens())
        assert kwargs.get("restarted") is True
        # The restore marker is consumed by _trim_after_save, not the prompt.
        assert loop._just_restored_history == id(conv)

    def test_no_flag_for_other_histories(self):
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        loop._turn_prompt_kwargs(conv, conv.count_tokens())

        other = MessageHistory(system_prompt="SYS")
        loop._just_restored_history = id(conv)
        kwargs = loop._turn_prompt_kwargs(other, other.count_tokens())
        assert "restarted" not in kwargs


# ── Internal trim after save (_trim_after_save) ──────────────────────────


class TestRecallRebuild:
    """The per-turn context rebuild, and the line between its two outcomes.

    The selection **is** the context, so an empty selection is an empty
    context — the turns in it were judged irrelevant, and keeping them would
    mean the recall never took effect.  What must never happen is a *failure*
    wearing an empty selection's clothes: when the store cannot be asked
    (``None``) or the selection cannot be fetched, the context stays exactly
    as it was.  ``recall_turns`` returning ``[]`` for "could not ask" is what
    made those two indistinguishable.
    """

    TURN = {
        "rowid": 7,
        "user_message": "recalled question",
        "messages": '[{"role": "assistant", "content": "recalled reply"}]',
        "created_at": "2026-09-01T10:00:00+08:00",
    }

    @staticmethod
    def _loop(**kwargs):
        return AgentLoop(
            llm_client=_ReplyLLM('{"since": "yesterday"}'),
            tool_registry=_registry_with_recall(),
            context_window=1000, context_ceiling=0.8, context_floor=0.2,
            rebuild_message=kwargs.get("rebuild", True),
            recall_turns=kwargs.get("recall"),
            turns_by_ids=kwargs.get("turns_by_ids"),
            set_context_turns=kwargs.get("set"),
            clear_context_turns=kwargs.get("clear"),
        )

    @staticmethod
    def _history():
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("old question")
        conv.add_assistant_message("old reply")
        return conv

    @pytest.mark.asyncio
    async def test_empty_recall_clears_the_context(self):
        conv = self._history()
        persisted: list[list[int]] = []
        cleared: list[bool] = []

        async def recall(*_a, **_k):
            return []

        async def save(ids):
            persisted.append(list(ids))
            return True

        async def clear():
            cleared.append(True)
            return True

        loop = self._loop(recall=recall, set=save, clear=clear)
        assert await loop._recall_and_rebuild(conv, "new input") is True

        assert [m["role"] for m in conv.messages] == ["system"], (
            "an empty selection is an empty context — the system prompt alone"
        )
        assert cleared == [True], "and the persisted list is emptied to match"
        assert persisted == [], (
            "set_context_turns refuses an empty list by design (it guards a "
            "partial selection); the clear tool is the write for this case"
        )

    @pytest.mark.asyncio
    async def test_unavailable_recall_keeps_the_context(self):
        """``None`` is "the store could not be asked" — the context stays."""
        conv = self._history()
        before = [dict(m) for m in conv.messages]
        persisted: list[list[int]] = []
        cleared: list[bool] = []

        async def recall(*_a, **_k):
            return None

        async def save(ids):
            persisted.append(list(ids))
            return True

        async def clear():
            cleared.append(True)
            return True

        loop = self._loop(recall=recall, set=save, clear=clear)
        assert await loop._recall_and_rebuild(conv, "new input") is False
        assert conv.messages == before
        assert persisted == [] and cleared == [], (
            "a store that cannot answer must leave both the history and the "
            "persisted list untouched"
        )

    @pytest.mark.asyncio
    async def test_unfetchable_turns_leave_context_untouched(self):
        conv = self._history()
        before = [dict(m) for m in conv.messages]
        persisted: list[list[int]] = []
        cleared: list[bool] = []

        async def recall(*_a, **_k):
            return [7]

        async def turns_by_ids(_ids):
            return []

        async def save(ids):
            persisted.append(list(ids))
            return True

        async def clear():
            cleared.append(True)
            return True

        loop = self._loop(recall=recall, turns_by_ids=turns_by_ids, set=save,
                          clear=clear)
        assert await loop._recall_and_rebuild(conv, "new input") is False
        assert conv.messages == before
        assert persisted == [] and cleared == [], (
            "a selection that cannot be rendered is not an empty selection"
        )

    @pytest.mark.asyncio
    async def test_successful_recall_rebuilds_and_persists(self):
        conv = self._history()
        persisted: list[list[int]] = []

        async def recall(*_a, **_k):
            return [7]

        async def turns_by_ids(_ids):
            return [dict(self.TURN)]

        async def save(ids):
            persisted.append(list(ids))
            return True

        loop = self._loop(recall=recall, turns_by_ids=turns_by_ids, set=save)
        assert await loop._recall_and_rebuild(conv, "new input") is True

        # The context is now the selection, not the old history.
        assert conv.messages[0]["role"] == "system", "the system prompt is preserved"
        assert not any(
            m.get("content") == "old question" for m in conv.messages
        ), "the previous context was replaced, not merged"
        assert any(
            "recalled question" in str(m.get("content")) for m in conv.messages
        )
        assert persisted == [[7]], "the selection is persisted for the next restart"

    @pytest.mark.asyncio
    async def test_disabled_flag_skips_recall_entirely(self):
        conv = self._history()
        before = [dict(m) for m in conv.messages]
        called: list[bool] = []

        async def recall(*_a, **_k):
            called.append(True)
            return [7]

        loop = self._loop(rebuild=False, recall=recall)
        assert await loop._recall_and_rebuild(conv, "new input") is False
        assert called == [], "the legacy mode must not pay for a discriminator call"
        assert conv.messages == before


class TestRecallDiscriminator:
    """The pre-turn discriminator call.

    It is a *discriminator*: one call that decides what this turn needs, and
    nothing else.  It never writes to the history (so it can never become part
    of the conversation, the diary, the TUI, or the next request), and it
    answers with recall parameters or nothing at all.
    """

    REPLY = '{"query": "首经贸 新闻", "since": null, "until": null}'

    class _FakeLLM:
        def __init__(self, reply: str):
            self.reply = reply
            self.sent: list[list[dict]] = []

        async def chat(self, messages, **_kwargs):
            self.sent.append([dict(m) for m in messages])
            msg = SimpleNamespace(content=self.reply)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)]), None

    @staticmethod
    def _loop(llm, registry=None):
        return AgentLoop(
            llm_client=llm,
            tool_registry=registry if registry is not None else
            _registry_with_recall(),
            context_window=1000, context_ceiling=0.8, context_floor=0.2,
            rebuild_message=True,
        )

    @pytest.mark.asyncio
    async def test_the_call_is_never_written_to_the_history(self):
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("old question")
        conv.add_assistant_message("old reply")
        before = [dict(m) for m in conv.messages]
        llm = self._FakeLLM(self.REPLY)

        args = await self._loop(llm)._discriminate_recall(conv, "查一下首经贸新闻")

        assert args == {"query": "首经贸 新闻", "since": None, "until": None}
        assert conv.messages == before, (
            "the discriminator is not part of the conversation — it must not "
            "append the instruction or its own reply to the history"
        )

    @pytest.mark.asyncio
    async def test_the_call_carries_the_context_and_the_instruction(self):
        """The shape of the request, pinned: the agent's **current context**,
        with the instruction in place of the user message.  The discriminator
        judges from the conversation in hand — a follow-up's query has to name
        the subject it refers to, and that subject is in the context, not in
        the input.  Written from the input alone the query retrieved nothing,
        and since a selection *replaces* the context, the turn ran blind."""
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("old question")
        conv.add_assistant_message("old reply")
        llm = self._FakeLLM(self.REPLY)

        await self._loop(llm)._discriminate_recall(conv, "查一下首经贸新闻")

        sent = llm.sent[0]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[0]["content"] == "SYS"
        assert sent[1]["content"] == "old question", "the context is the decision's input"
        assert sent[2]["content"] == "old reply"
        assert "查一下首经贸新闻" in sent[3]["content"]
        assert "turn_recall" in sent[3]["content"], "the tool's schema is the surface"
        assert "\"query\"" in sent[3]["content"]

    @pytest.mark.asyncio
    async def test_the_context_goes_out_without_the_runtime_turn_ids(self):
        """The agent's own messages go out as they are, minus ``_turn_id`` —
        a runtime mapping for the trim, popped on the normal wire path by
        ``to_openai_messages``.  This call does not go through that helper,
        and ``_normalize_messages`` strips only ``is_error``, so the id would
        otherwise reach the provider."""
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("old question")
        conv.messages[-1]["_turn_id"] = 7          # as restore/trim stamp it
        conv.add_assistant_message("old reply")
        llm = self._FakeLLM(self.REPLY)

        await self._loop(llm)._discriminate_recall(conv, "查一下首经贸新闻")

        assert [m["role"] for m in llm.sent[0]] == ["system", "user", "assistant", "user"]
        assert "_turn_id" not in llm.sent[0][1]
        assert llm.sent[0][1]["content"] == "old question"
        assert conv.messages[1].get("_turn_id") == 7, "the live context keeps its mapping"

    @pytest.mark.asyncio
    async def test_no_tool_means_no_call(self, caplog):
        """The schema IS the instruction, so without the tool registered there
        is nothing to ask for: the call is skipped and the context kept —
        rather than spending a model call on a surface it cannot state."""
        import logging

        conv = MessageHistory(system_prompt="SYS")
        llm = self._FakeLLM(self.REPLY)
        loop = self._loop(llm, registry=_registry_with_recall(with_recall=False))

        with caplog.at_level(logging.INFO, logger="slife.agent.loop"):
            args = await loop._discriminate_recall(conv, "查一下首经贸新闻")

        assert args is None
        assert llm.sent == [], "no tool schema, no discriminator call"
        assert any(
            "no_turn_recall_tool" in r.getMessage() for r in caplog.records
        )

    @pytest.mark.asyncio
    async def test_the_execution_is_logged(self, caplog):
        """Every run leaves a trace: the parameters it produced, its latency
        and the shape of the call."""
        import logging

        conv = MessageHistory(system_prompt="SYS")
        llm = self._FakeLLM(self.REPLY)

        with caplog.at_level(logging.INFO, logger="slife.agent.loop"):
            await self._loop(llm)._discriminate_recall(conv, "查一下首经贸新闻")

        line = next(
            r.getMessage() for r in caplog.records
            if "recall_discriminated" in r.getMessage()
        )
        assert "query=首经贸 新闻" in line
        assert "msgs=2" in line
        assert "took_ms=" in line

    @pytest.mark.asyncio
    async def test_an_unparseable_reply_is_logged_and_degrades(self, caplog):
        """Prose instead of JSON is a degradation, not a crash — and it is
        named, so a silent recall is never mistaken for an empty one."""
        import logging

        conv = MessageHistory(system_prompt="SYS")
        llm = self._FakeLLM("I think you should search for the news.")

        with caplog.at_level(logging.WARNING, logger="slife.agent.loop"):
            args = await self._loop(llm)._discriminate_recall(conv, "查新闻")

        assert args is None
        assert any(
            "recall_discriminator_unparsed" in r.getMessage()
            for r in caplog.records
        )


class TestTrimAfterSave:
    """_trim_after_save: called after a turn is saved, uses real usage,
    appends a runtime trim note, and never shreds a restored context.

    The ceiling is the window's safety valve, so it holds in both modes —
    ``rebuild_message`` decides how the context is *chosen*, never how big it
    may grow within a turn."""

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
            # Every saved turn carries its diary rowid on its opening user
            # message (set at save, re-stamped on restore) — the trim reads
            # them to drop the evicted turns from the persisted list.
            conv.messages[-1]["_turn_id"] = i + 1
            conv.add_assistant_message(f"这是第{i}轮的回复，也需要一定长度以参与 token 估算。")
        return conv

    @staticmethod
    def _loop(conv, cfg, **kwargs):
        return AgentLoop(
            llm_client=None, tool_registry=create_tools_from_config(),
            context_window=200, context_ceiling=0.8, context_floor=0.2,
            rebuild_message=kwargs.get("rebuild", False),
            drop_context_turns=kwargs.get("drop"),
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
    async def test_trims_in_rebuild_mode_too(self):
        """The ceiling is not a property of how the context is *chosen*: a
        turn whose tool results outgrew it is compacted in rebuild mode as
        well.  The next recall re-selects, so the eviction bounds the window
        without deciding anything."""
        conv = self._conv(12)
        loop = self._loop(conv, self._cfg(), rebuild=True)
        await self._prime_usage(loop, conv)
        assert conv.count_tokens() > 160  # over 0.8 × 200 ceiling

        await loop._trim_after_save(conv)

        assert len([m for m in conv.messages if m.get("role") == "user"]) < 12
        assert "oldest turns have been removed from context" in conv.messages[-1].get("content", "")

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
    async def test_drops_evicted_turn_ids(self):
        conv = self._conv(12)
        dropped: list[list[int]] = []

        async def drop(turn_ids):
            dropped.append(list(turn_ids))
            return True

        loop = self._loop(conv, self._cfg(), drop=drop)
        await self._prime_usage(loop, conv)
        await loop._trim_after_save(conv)

        assert dropped, "drop_context_turns should be called with the evicted ids"
        survivors = [m for m in conv.messages if m.get("role") == "user"]
        # The ids are EXACT — the turns that are no longer in the history,
        # not a count the store has to re-derive.
        assert dropped[0] == [i + 1 for i in range(12 - len(survivors))]
        # ...and no id that survived was dropped.
        assert not (set(dropped[0]) & {m["_turn_id"] for m in survivors})

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

        # Turn 1: user message + harness _turn_prompt, then cancelled (no reply).
        conv.add_user_message("第一轮：帮我搜一下X")
        await loop._auto_invoke("_turn_prompt", loop._turn_prompt_kwargs(conv, conv.count_tokens()), conv)
        conv._ensure_turn_consistent("")

        # Turn 2: the next user message + fresh _turn_prompt.
        conv.add_user_message("第二轮：继续")
        await loop._auto_invoke("_turn_prompt", loop._turn_prompt_kwargs(conv, conv.count_tokens()), conv)

        self._assert_alternating(conv, "cancelled-then-next")

    @pytest.mark.asyncio
    async def test_auto_invoke_produces_normal_tool_pair(self):
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")

        await loop._auto_invoke("_turn_prompt", loop._turn_prompt_kwargs(conv, conv.count_tokens()), conv)

        last = conv.messages[-2:]
        assert last[0]["role"] == "assistant"
        assert last[0]["tool_calls"][0]["function"]["name"] == "_turn_prompt"
        assert last[1]["role"] == "tool"
        assert "Context usage" in last[1]["content"]

    @pytest.mark.asyncio
    async def test_auto_invoked_prompt_injects_schedule_reminder(self):
        """The loop injects the schedule_provider's open runs into _turn_prompt
        each turn — the reminder rides the existing per-turn prompt pair."""
        reg = _registry()
        loop = _loop(reg)
        loop._schedule_provider = lambda: [
            {"name": "daily", "due_at": "2026-08-25T09:00:00",
             "status": "missed"},
        ]
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")

        await loop._auto_invoke(
            "_turn_prompt", loop._turn_prompt_kwargs(conv, conv.count_tokens()), conv,
        )

        assert "Scheduled runs not settled" in conv.messages[-1]["content"]
        assert "daily @ 2026-08-25T09:00:00 (missed)" in conv.messages[-1]["content"]

    @pytest.mark.asyncio
    async def test_auto_invoked_prompt_injects_orphaned_a2a_tasks(self):
        """The loop injects the a2a_stale_provider's orphaned tasks into
        _turn_prompt each turn — a restart's casualties must reach the model
        before it tries to complete one."""
        reg = _registry()
        loop = _loop(reg)
        loop._a2a_stale_provider = lambda: [
            {"task_id": "ec604319", "peer": "jack",
             "since": "2026-09-19T06:39:07Z"},
        ]
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")

        await loop._auto_invoke(
            "_turn_prompt", loop._turn_prompt_kwargs(conv, conv.count_tokens()), conv,
        )

        content = conv.messages[-1]["content"]
        assert "died with the previous process" in content
        assert "ec604319 from jack" in content
        assert "message_type='message'" in content

    @pytest.mark.asyncio
    async def test_no_orphan_section_without_orphans(self):
        """No provider (or an empty set) leaves the turn prompt unchanged."""
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")

        await loop._auto_invoke(
            "_turn_prompt", loop._turn_prompt_kwargs(conv, conv.count_tokens()), conv,
        )
        assert "died with the previous process" not in conv.messages[-1]["content"]

    def test_context_time_start_change_detected(self):
        """'Context covers' is reported on the first prompt, then only when
        the start time changes (restore sets it, trim advances it)."""
        reg = _registry()
        loop = _loop(reg)
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("hi")

        loop._context_time_start = "2026-01-01T00:00:00+08:00"
        first = loop._turn_prompt_kwargs(conv, conv.count_tokens())
        assert first.get("context_time_start") == "2026-01-01T00:00:00+08:00"

        # Unchanged on the next turn → not reported again.
        second = loop._turn_prompt_kwargs(conv, conv.count_tokens())
        assert "context_time_start" not in second

        # A trim advances the start → reported again.
        loop._context_time_start = "2026-02-01T00:00:00+08:00"
        third = loop._turn_prompt_kwargs(conv, conv.count_tokens())
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



class TestRecallNotNeeded:
    """``{}`` from the discriminator means *no recall is needed*.

    The reply format is a decision, not a default: an empty parameter object
    says the context already in hand is enough.  Reading it as "give me the
    most recent turns" would silently *replace* that context, which is very
    possibly not what the model wanted — so the store is not asked at all.
    """

    class _FakeLLM:
        def __init__(self, reply):
            self.reply = reply
            self.sent = []

        async def chat(self, messages, **_kwargs):
            self.sent.append([dict(m) for m in messages])
            from types import SimpleNamespace
            msg = SimpleNamespace(content=self.reply)
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)]), None

    def _loop(self, reply, **kwargs):
        asked = []

        async def recall(*a, **k):
            asked.append((a, k))
            return []

        sim = kwargs.get("set") or []
        loop = AgentLoop(
            llm_client=self._FakeLLM(reply), tool_registry=_registry_with_recall(),
            context_window=1000, context_ceiling=0.8, context_floor=0.2,
            rebuild_message=True,
            recall_turns=recall,
            turns_by_ids=kwargs.get("turns_by_ids"),
            set_context_turns=lambda ids: sim.append(list(ids)) or True,  # noqa: E731
            clear_context_turns=kwargs.get("clear"),
        )
        return loop, asked, sim

    @pytest.mark.asyncio
    async def test_an_empty_object_keeps_the_context_and_asks_nothing(self):
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("old question")
        conv.add_assistant_message("old reply")
        before = [dict(m) for m in conv.messages]

        loop, asked, persisted = self._loop("{}")
        assert await loop._recall_and_rebuild(conv, "new input") is False

        assert asked == [], "no recall needed means no store call either"
        assert persisted == [], "and nothing is persisted"
        assert conv.messages == before, "the context it judged sufficient stands"

    @pytest.mark.asyncio
    async def test_all_empty_parameters_read_as_no_recall(self):
        """The schema's defaults serialized out loud — same meaning."""
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("old question")
        before = [dict(m) for m in conv.messages]

        loop, asked, _ = self._loop(
            '{"query": "", "since": null, "until": null}'
        )
        assert await loop._recall_and_rebuild(conv, "new input") is False

        assert asked == []
        assert conv.messages == before

    @pytest.mark.asyncio
    async def test_a_time_bound_still_recalls(self):
        """A bound is a request: it goes to the store (time-only branch)."""
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message("old question")
        loop, asked, _ = self._loop('{"since": "yesterday"}')

        await loop._recall_and_rebuild(conv, "new input")

        assert asked and asked[0][0][1] == "yesterday", "the bound reaches the store"
