"""Tests for slife.ui.restore — session restore image resolution.

Post-BLOB design: image markers (``[image: <path>]``) point at files
on disk.  Resolution is a simple existence check — file there → render,
file gone → placeholder.
"""

import pytest; pytestmark = pytest.mark.unit

from unittest.mock import MagicMock

from slife.ui.restore import (
    _mount_resolved_image,
    _schedule_image_mounts,
    resolve_pending_images,
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
