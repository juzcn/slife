"""Tests for slife.ui.restore — session restore from turn-based memory.

Files are never rendered in-terminal; ``@``-attachments still rebuild
vision content blocks, but no image markers or mount scheduling exist.
"""

import json

import pytest; pytestmark = pytest.mark.unit

from unittest.mock import MagicMock

from slife.agent.message_history import MessageHistory
from slife.ui.restore import (
    restore_session,
    tool_result_is_error,
)


# ── Tool error state on restore ─────────────────────────────────────


class TestToolResultIsError:
    """The loop persists ``is_error`` on every tool result; restore reads
    the flag directly and never re-derives it from content."""

    def test_flag_true_wins_over_content(self):
        msg = {"role": "tool", "content": "all good", "is_error": True}
        assert tool_result_is_error(msg) is True

    def test_flag_false_wins_over_error_text(self):
        # The loop's recorded verdict is authoritative even when the
        # content happens to start with "Error".
        msg = {"role": "tool", "content": "Error-looking stdout", "is_error": False}
        assert tool_result_is_error(msg) is False

    def test_error_content_without_flag_is_not_error(self):
        # No is_error key → not an error; content is never inspected.
        msg = {"role": "tool", "content": "Error: Edit 1: old_string not found."}
        assert tool_result_is_error(msg) is False

    def test_empty_content_without_flag(self):
        msg = {"role": "tool", "content": ""}
        assert tool_result_is_error(msg) is False

    def test_interrupted_marker_marked_by_repair(self):
        # _ensure_turn_consistent tags synthetic results with the flag.
        msg = {
            "role": "tool",
            "content": "(Tool execution interrupted)",
            "is_error": True,
        }
        assert tool_result_is_error(msg) is True


# ── Empty assistant messages on restore ──────────────────────────────


class TestRestoreSkipsEmptyAssistantMessages:
    """An assistant message persisted with no text and no thinking is an
    intermediate tool-iteration — it exists in storage only so the LLM
    context stays correct.  Live streaming never created a message widget
    for it (``on_tool_call`` mounts just a ToolCallWidget), so restore
    must not render the empty ``…`` placeholder either — the ToolCallWidgets
    alone show the work.  Non-reasoning models hit this on every
    tool-iteration; reasoning models mask it because ``thinking`` renders.
    """

    @staticmethod
    def _turn(msgs, user="hi"):
        return {
            "user_message": user,
            "messages": json.dumps(msgs, ensure_ascii=False),
            "images": "",
            "channel": "human",
        }

    def _build(self):
        app = MagicMock()
        chat_view = app.query_one.return_value
        am = MagicMock()
        chat_view.add_assistant_message.return_value = am
        conv = MagicMock()
        conv.messages = []
        conv._ensure_turn_consistent.return_value = 0
        config = MagicMock()
        config.active_model.context_window = 100_000
        config.context_ceiling = 0.8
        return app, conv, config, chat_view, am

    async def _restore(self, app, conv, config, turns):
        await restore_session(
            app, {"turns": turns, "budget": 1_000_000},
            conv, config, "agent", "Jack> ",
        )

    @pytest.mark.asyncio
    async def test_textless_tool_iteration_shows_only_tool_widget(self):
        """Empty tool-iteration message → no ``…`` widget, tool widget only."""
        app, conv, config, chat_view, am = self._build()
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
        turns = [self._turn([
            {"role": "assistant", "content": "", "tool_calls": [tool_call]},
            {"role": "tool", "tool_call_id": "call_1", "content": "42",
             "is_error": False},
            {"role": "assistant", "content": "The answer is 42."},
        ])]
        await self._restore(app, conv, config, turns)

        # Only the text-bearing final message gets a message widget.
        chat_view.add_assistant_message.assert_called_once()
        am.append_thinking.assert_not_called()
        am.append_text.assert_called_once_with("The answer is 42.")
        am.finalize.assert_called_once_with(intermediate=False)

        # The tool iteration still renders its tool widget.
        mounts = [c.args[0] for c in chat_view.mount.call_args_list]
        assert [w.tool_name for w in mounts] == ["read_file"]

    @pytest.mark.asyncio
    async def test_fully_empty_message_skipped(self):
        """No text, no thinking, no tool calls → nothing at all."""
        app, conv, config, chat_view, am = self._build()
        turns = [self._turn([
            {"role": "assistant", "content": "", "tool_calls": []},
        ])]
        await self._restore(app, conv, config, turns)

        chat_view.add_assistant_message.assert_not_called()
        chat_view.mount.assert_not_called()

    @pytest.mark.asyncio
    async def test_harness_message_still_skipped(self):
        """``_sys_note`` is context-only — never widgets."""
        app, conv, config, chat_view, am = self._build()
        tcs = [{
            "id": "h1", "type": "function",
            "function": {"name": "_sys_note", "arguments": "{}"},
        }]
        turns = [self._turn([
            {"role": "assistant", "content": "", "tool_calls": tcs},
            {"role": "tool", "tool_call_id": "h1", "content": "ok",
             "is_error": False},
        ])]
        await self._restore(app, conv, config, turns)

        chat_view.add_assistant_message.assert_not_called()
        chat_view.mount.assert_not_called()

    @pytest.mark.asyncio
    async def test_thinking_only_message_still_shows_widget(self):
        """Reasoning models: empty text but real thinking still renders."""
        app, conv, config, chat_view, am = self._build()
        turns = [self._turn([
            {"role": "assistant", "content": "", "thinking": "inner reasoning"},
        ])]
        await self._restore(app, conv, config, turns)

        chat_view.add_assistant_message.assert_called_once()
        am.append_thinking.assert_called_once_with("inner reasoning")
        am.append_text.assert_not_called()

    @pytest.mark.asyncio
    async def test_interrupted_turn_renders_turn_interrupted(self):
        """An interrupted tool turn is repaired by _ensure_turn_consistent,
        which closes it with an explicit ``(Turn interrupted)`` message —
        content is non-empty, so it renders on restore (not silence, and
        not the ``…`` placeholder).  Only the text-less tool iteration
        skips its (already widget-less) message.
        """
        app = MagicMock()
        chat_view = app.query_one.return_value
        am = MagicMock()
        chat_view.add_assistant_message.return_value = am
        conv = MessageHistory(system_prompt=None)  # real — exercises the repair
        config = MagicMock()
        config.active_model.context_window = 100_000
        config.context_ceiling = 0.8

        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": "{}"},
        }
        # Orphaned tool call, no result — the turn was interrupted mid-tool.
        turns = [self._turn([
            {"role": "assistant", "content": "", "tool_calls": [tool_call]},
        ])]
        await self._restore(app, conv, config, turns)

        # The repair closed the history in place.
        assert conv.messages[-1]["role"] == "assistant"
        assert conv.messages[-1]["content"] == "(Turn interrupted)"
        # …and tagged the synthetic tool result as the interruption error.
        assert [m for m in conv.messages if m.get("role") == "tool"] \
            == [{
                "role": "tool", "tool_call_id": "call_1",
                "content": "(Tool execution interrupted)", "is_error": True,
            }]

        # "(Turn interrupted)" is the one rendered message widget.
        chat_view.add_assistant_message.assert_called_once()
        am.append_text.assert_called_once_with("(Turn interrupted)")
        am.finalize.assert_called_once_with(intermediate=False)
        am.append_thinking.assert_not_called()
        # The tool iteration rendered its tool widget, not a message widget.
        mounts = [c.args[0] for c in chat_view.mount.call_args_list]
        assert [w.tool_name for w in mounts] == ["read_file"]


# ── Turn headers on restore ──────────────────────────────────────────


class TestRestoreTurnHeader:
    """Each restored user message carries a ``[INFO: {"turn_id": N, …}]``
    footnote — id + begin → end — concatenated into the message text so the
    LLM can tell old turns apart and reference them by turn id, and the human
    reads it in the TUI.  Generated at restore only; heartbeat turns get
    none."""

    @staticmethod
    def _turn(user, rowid=27, created="2026-08-10T14:03:05+08:00",
              completed="2026-08-10T14:05:12+08:00", channel="human",
              channel_data="{}", msgs=None):
        return {
            "rowid": rowid,
            "user_message": user,
            "messages": json.dumps(msgs or [
                {"role": "assistant", "content": "ok"},
            ], ensure_ascii=False),
            "images": "",
            "channel": channel,
            "channel_data": channel_data,
            "created_at": created,
            "completed_at": completed,
        }

    def _build(self):
        app = MagicMock()
        chat_view = app.query_one.return_value
        am = MagicMock()
        chat_view.add_assistant_message.return_value = am
        conv = MessageHistory(system_prompt=None)
        config = MagicMock()
        config.active_model.context_window = 100_000
        config.context_ceiling = 0.8
        return app, conv, config, chat_view

    async def _restore(self, app, conv, config, turns):
        await restore_session(
            app, {"turns": turns, "budget": 1_000_000},
            conv, config, "agent", "Jack> ",
        )

    @pytest.mark.asyncio
    async def test_header_injected_into_restored_user_message(self):
        app, conv, config, chat_view = self._build()
        await self._restore(app, conv, config, [self._turn("switch model")])

        user = conv.messages[0]
        assert user["role"] == "user"
        # Concatenated inline footnote — one text part, no separate marker.
        assert user["content"] == (
            'switch model [INFO: {"turn_id": 27, "begin": "2026-08-10 14:03", '
            '"end": "14:05"}]'
        )
        # TUI bubble shows the same footnote the model sees.
        chat_view.add_user_message.assert_called_once()
        assert chat_view.add_user_message.call_args.args[0] == user["content"]

    @pytest.mark.asyncio
    async def test_header_without_completed_at(self):
        app, conv, config, chat_view = self._build()
        turn = self._turn("hi")
        del turn["completed_at"]
        await self._restore(app, conv, config, [turn])

        assert conv.messages[0]["content"] == (
            'hi [INFO: {"turn_id": 27, "begin": "2026-08-10 14:03"}]'
        )

    @pytest.mark.asyncio
    async def test_heartbeat_turn_gets_no_header(self):
        app, conv, config, chat_view = self._build()
        await self._restore(app, conv, config, [
            self._turn("[Heartbeat] click.  Reply per your contract."),
        ])

        assert conv.messages[0]["content"] == (
            "[Heartbeat] click.  Reply per your contract."
        )
        # Heartbeat turns render nowhere in the chat.
        chat_view.add_user_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedule_trigger_turn_gets_no_header(self):
        app, conv, config, chat_view = self._build()
        await self._restore(app, conv, config, [
            self._turn("[Schedule daily_diary] 定时任务触发。", channel="schedule"),
        ])

        # The synthetic trigger carries no turn header.
        assert conv.messages[0]["content"] == "[Schedule daily_diary] 定时任务触发。"
        # The trigger renders nowhere in the chat.
        chat_view.add_user_message.assert_not_called()

    @pytest.mark.asyncio
    async def test_schedule_trigger_reply_renders_as_scheduler(self):
        app, conv, config, chat_view = self._build()
        await self._restore(app, conv, config, [
            self._turn(
                "[Schedule daily_diary] 定时任务触发。",
                channel="schedule",
                msgs=[{"role": "assistant", "content": "已派发定时任务。"}],
            ),
        ])
        # The dispatch confirmation surfaces as a scheduler message (📅
        # scheduled), not autonomous (⚡ autonomous) — cron fires are
        # scheduler-driven, not autonomous acts.
        from slife.ui.i18n import t
        chat_view.add_assistant_message.assert_called_once()
        kwargs = chat_view.add_assistant_message.call_args.kwargs
        assert kwargs.get("name_prefix") == t("schedule_prefix")
        am = chat_view.add_assistant_message.return_value
        am.append_text.assert_called_once_with("已派发定时任务。")

    @pytest.mark.asyncio
    async def test_turn_without_identity_stays_plain(self):
        app, conv, config, chat_view = self._build()
        turn = self._turn("hi")
        del turn["rowid"]
        del turn["created_at"]
        del turn["completed_at"]
        await self._restore(app, conv, config, [turn])

        assert conv.messages[0]["content"] == "hi"

    @pytest.mark.asyncio
    async def test_a2a_turn_renders_peer_name_prefix(self):
        """A2A turns restore with the peer name from the channel payload."""
        from slife.ui.i18n import t

        app, conv, config, chat_view = self._build()
        turn = self._turn("GO", channel="Jack",
                          channel_data='{"agent_name": "Jack"}')
        await self._restore(app, conv, config, [turn])

        # Needs the peer name from the channel payload, not a fallback.
        chat_view.add_user_message.assert_called_once()
        kwargs = chat_view.add_user_message.call_args.kwargs
        assert kwargs["prefix"] == "A2A(Jack)"
        call_kwargs = chat_view.add_user_message.call_args
        assert call_kwargs.args[0].startswith("GO")

    @pytest.mark.asyncio
    async def test_a2a_turn_name_from_peer_identity_string(self):
        """An a2a row whose channel is the peer name (to_db format) renders."""
        from slife.ui.i18n import t

        app, conv, config, chat_view = self._build()
        turn = self._turn("GO", channel="desk-01")
        await self._restore(app, conv, config, [turn])

        kwargs = chat_view.add_user_message.call_args.kwargs
        assert kwargs["prefix"] == "A2A(desk-01)"

    @pytest.mark.asyncio
    async def test_subagent_turn_renders_name_prefix(self):
        """Subagent turns restore with the worker name in the prefix."""
        from slife.ui.i18n import t

        app, conv, config, chat_view = self._build()
        turn = self._turn("done", channel="subagent",
                          channel_data='{"name": "researcher"}')
        await self._restore(app, conv, config, [turn])

        kwargs = chat_view.add_user_message.call_args.kwargs
        assert kwargs["prefix"] == t("subagent_prefix", name="researcher")
