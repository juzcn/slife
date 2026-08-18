"""Tests for slife.ui.restore — session restore image resolution.

Post-BLOB design: image markers (``[image: <path>]``) point at files
on disk.  Resolution is a simple existence check — file there → render,
file gone → placeholder.
"""

import json

import pytest; pytestmark = pytest.mark.unit

from unittest.mock import MagicMock

from slife.agent.conversation import Conversation
from slife.ui.restore import (
    _mount_resolved_image,
    _schedule_image_mounts,
    resolve_pending_images,
    restore_session,
    tool_result_is_error,
)


# ── Tool error state on restore ─────────────────────────────────────


class TestToolResultIsError:
    """Persisted ``is_error`` flag wins; legacy turns fall back to the
    live loop's heuristic so pre-flag errors don't render as done."""

    def test_flag_true_wins_over_content(self):
        msg = {"role": "tool", "content": "all good", "is_error": True}
        assert tool_result_is_error(msg) is True

    def test_flag_false_wins_over_error_text(self):
        # The loop's recorded verdict is authoritative even when the
        # content happens to start with "Error".
        msg = {"role": "tool", "content": "Error-looking stdout", "is_error": False}
        assert tool_result_is_error(msg) is False

    def test_legacy_error_content_detected(self):
        msg = {"role": "tool", "content": "Error: Edit 1: old_string not found."}
        assert tool_result_is_error(msg) is True

    def test_legacy_ok_content_not_error(self):
        msg = {"role": "tool", "content": "Search results here."}
        assert tool_result_is_error(msg) is False

    def test_legacy_empty_content(self):
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


# ── Resolution ──────────────────────────────────────────────────────────


class TestResolvePendingImages:
    """File-exists → path, file-gone → None."""

    @pytest.mark.asyncio
    async def test_file_exists_returns_path(self, tmp_path):
        existing = tmp_path / "photo.png"
        existing.write_bytes(b"PNG")

        result = await resolve_pending_images(
            [(str(existing), MagicMock(), MagicMock())],
        )

        assert result[0][0] == str(existing.resolve())

    @pytest.mark.asyncio
    async def test_file_missing_returns_none(self, tmp_path):
        marker = str(tmp_path / "gone.png")

        result = await resolve_pending_images(
            [(marker, MagicMock(), MagicMock())],
        )

        assert result[0][0] is None

    @pytest.mark.asyncio
    async def test_mixed_batch(self, tmp_path):
        existing = tmp_path / "a.jpg"
        existing.write_bytes(b"jpeg")
        ghost = str(tmp_path / "ghost.png")

        result = await resolve_pending_images([
            (str(existing), MagicMock(), MagicMock()),
            (ghost, MagicMock(), MagicMock()),
        ])

        assert result[0][0] == str(existing.resolve())
        assert result[1][0] is None

    @pytest.mark.asyncio
    async def test_empty_pending(self):
        assert await resolve_pending_images([]) == []


# ── Restored image mounting ──────────────────────────────────────────


class TestScheduleImageMounts:
    """Restore-jitter fix: the first image mounts synchronously;
    each subsequent image is scheduled via ``app.set_timer`` from the
    previous callback, giving each ``HalfcellImage`` its own event-loop
    tick for a compositor cycle.  Only the final image triggers a
    scroll-to-end."""

    def test_single_image_mounts_and_scrolls(self):
        """One image: synchronous mount → refresh → scroll."""
        app, cv, aw = MagicMock(), MagicMock(), MagicMock()
        _schedule_image_mounts(app, cv, [("/tmp/a.png", "[image: a]", cv, aw)])

        # First image mounted synchronously.
        assert cv.mount.call_count == 1
        # call_after_refresh scheduled for final scroll.
        cv.call_after_refresh.assert_called_once()

        # Fire the scroll callback.
        cv.call_after_refresh.call_args[0][0]()
        assert cv.mount.call_count == 1  # no extra mounts

    def test_multiple_images_chain_via_timers(self):
        """First image synchronous; image 2+3 via timer chain; final scroll."""
        app, cv, aw = MagicMock(), MagicMock(), MagicMock()
        resolved = [
            ("/tmp/a.png", "[image: a]", cv, aw),
            ("/tmp/b.png", "[image: b]", cv, aw),
            ("/tmp/c.png", "[image: c]", cv, aw),
        ]
        _schedule_image_mounts(app, cv, resolved)

        # Image 0 mounted synchronously.
        assert cv.mount.call_count == 1
        # Image 1 scheduled via timer (not scroll — not last).
        assert app.set_timer.call_count == 1
        assert cv.call_after_refresh.call_count == 0  # not last yet

        # Fire timer → mounts image 1, schedules image 2.
        app.set_timer.call_args_list[0][0][1]()
        assert cv.mount.call_count == 2
        assert app.set_timer.call_count == 2

        # Fire timer → mounts image 2 (last), schedules scroll.
        app.set_timer.call_args_list[1][0][1]()
        assert cv.mount.call_count == 3
        assert cv.call_after_refresh.call_count == 1  # final scroll

        # Fire scroll callback.
        cv.call_after_refresh.call_args[0][0]()
        assert cv.mount.call_count == 3  # no extra

    def test_mounts_after_anchor_widget(self):
        """Image is mounted *after* the ToolCallWidget anchor."""
        app, cv, aw = MagicMock(), MagicMock(), MagicMock()
        _schedule_image_mounts(app, cv, [(None, "/tmp/ghost.png", cv, aw)])

        cv.mount.assert_called_once()
        assert cv.mount.call_args.kwargs.get("after") is aw

    def test_placeholder_mounted_for_missing_image(self):
        """resolved=None mounts the broken-file fallback widget."""
        cv, aw = MagicMock(), MagicMock()
        _mount_resolved_image(None, "/tmp/ghost.png", cv, aw)
        widget = cv.mount.call_args.args[0]
        assert "chat-image-fallback" in widget.classes

    def test_empty_resolved_just_scrolls(self):
        """No images → scroll immediately, no timer or refresh."""
        app, cv = MagicMock(), MagicMock()
        _schedule_image_mounts(app, cv, [])
        cv.scroll_end.assert_called_once_with(animate=False)
        app.set_timer.assert_not_called()
        cv.call_after_refresh.assert_not_called()


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
        """``_sys_note``/``_sys_trim`` are context-only — never widgets."""
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
        conv = Conversation(system_prompt=None)  # real — exercises the repair
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

        # The repair closed the conversation in place.
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
