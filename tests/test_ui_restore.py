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
)


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


# ── Mount scheduling ──────────────────────────────────────────────────


class TestMountScheduling:
    def test_staggered_timers_scheduled(self):
        app = MagicMock()
        cv, aw = MagicMock(), MagicMock()
        resolved = [
            ("/tmp/a.png", "[image: /tmp/a.png]", cv, aw),
            (None, "[image: /tmp/b.png]", cv, aw),
        ]

        _schedule_image_mounts(app, resolved)

        delays = [c.args[0] for c in app.set_timer.call_args_list]
        assert delays == [pytest.approx(0.5), pytest.approx(0.7)]

    def test_timer_callback_mounts_widget_after_anchor(self):
        app = MagicMock()
        cv, aw = MagicMock(), MagicMock()
        _schedule_image_mounts(app, [(None, "/tmp/ghost.png", cv, aw)])

        callback = app.set_timer.call_args_list[0].args[1]
        callback()

        cv.mount.assert_called_once()
        assert cv.mount.call_args.kwargs.get("after") is aw
        cv.call_after_refresh.assert_called_once()

    def test_placeholder_mounted_for_missing_image(self):
        """resolved=None mounts the broken-file fallback."""
        cv, aw = MagicMock(), MagicMock()
        _mount_resolved_image(None, "/tmp/ghost.png", cv, aw)
        widget = cv.mount.call_args.args[0]
        assert "chat-image-fallback" in widget.classes
