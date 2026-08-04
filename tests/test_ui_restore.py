"""Tests for slife.ui.restore — session restore image resolution chain.

Design contract under test: the diary_images BLOB table is the single
source of truth; cache files are ephemeral.  Resolution chain per
marker: BLOB → original file on disk (legacy) → placeholder (None).
"""

import pytest; pytestmark = pytest.mark.unit


import pytest
from unittest.mock import MagicMock

from slife.ui.restore import (
    _blob_extension,
    _mount_resolved_image,
    _schedule_image_mounts,
    resolve_pending_images,
)


# ── Fixtures ──────────────────────────────────────────────────────────


async def _make_blob_db(db_path, rows):
    """Create a diary_images table with (image_id, data, mime, file_name) rows."""
    import aiosqlite

    conn = await aiosqlite.connect(str(db_path))
    await conn.execute(
        """CREATE TABLE IF NOT EXISTS diary_images (
            image_id  TEXT PRIMARY KEY,
            data      BLOB NOT NULL,
            mime_type TEXT NOT NULL DEFAULT 'image/png',
            file_name TEXT NOT NULL DEFAULT '',
            file_size INTEGER NOT NULL DEFAULT 0
        )"""
    )
    for iid, data, mime, name in rows:
        await conn.execute(
            "INSERT OR REPLACE INTO diary_images "
            "(image_id, data, mime_type, file_name, file_size) "
            "VALUES (?, ?, ?, ?, ?)",
            (iid, data, mime, name, len(data)),
        )
    await conn.commit()
    await conn.close()


@pytest.fixture
def images_dir(tmp_path, monkeypatch):
    """Redirect get_images_dir() to a temp dir — never touch the real cache."""
    target = tmp_path / "images"
    monkeypatch.setattr("slife.paths.get_images_dir", lambda: target)
    return target


# ── Resolution chain ──────────────────────────────────────────────────


class TestResolvePendingImages:
    """BLOB → original file → placeholder resolution."""

    @pytest.mark.asyncio
    async def test_blob_hit_restores_without_cache_file(self, tmp_path, images_dir):
        """The core bug: cache file gone, BLOB present → still restores."""
        db_path = tmp_path / "memory.db"
        await _make_blob_db(db_path, [
            ("uuid-1", b"PNGDATA", "image/png", "uuid-1.png"),
        ])
        marker = str(tmp_path / "gone" / "uuid-1.png")  # does NOT exist
        cv, aw = MagicMock(), MagicMock()

        result = await resolve_pending_images([(marker, cv, aw)], db_path=db_path)

        assert len(result) == 1
        resolved, marker_out, cv_out, aw_out = result[0]
        assert marker_out == marker
        assert cv_out is cv and aw_out is aw
        expected = str((images_dir / "uuid-1.png").resolve())
        assert resolved == expected
        assert (images_dir / "uuid-1.png").read_bytes() == b"PNGDATA"

    @pytest.mark.asyncio
    async def test_no_blob_falls_back_to_existing_file(self, tmp_path, images_dir):
        """Legacy marker (no BLOB) but file exists → render original in place."""
        db_path = tmp_path / "memory.db"
        await _make_blob_db(db_path, [])
        legacy = tmp_path / "legacy-pic.png"
        legacy.write_bytes(b"\x89PNG original")

        result = await resolve_pending_images(
            [(str(legacy), MagicMock(), MagicMock())], db_path=db_path,
        )

        resolved = result[0][0]
        assert resolved == str(legacy.resolve())
        # Not copied into the images dir — original rendered in place.
        assert not images_dir.exists() or not any(images_dir.iterdir())

    @pytest.mark.asyncio
    async def test_no_blob_no_file_returns_none(self, tmp_path, images_dir):
        """Nothing available → None (caller mounts the ⚠ placeholder)."""
        db_path = tmp_path / "memory.db"
        await _make_blob_db(db_path, [])
        marker = str(tmp_path / "lost" / "ghost.png")

        result = await resolve_pending_images(
            [(marker, MagicMock(), MagicMock())], db_path=db_path,
        )

        assert result[0][0] is None

    @pytest.mark.asyncio
    async def test_missing_db_degrades_to_disk_chain(self, tmp_path, images_dir):
        """DB absent entirely: file fallback works, otherwise placeholder."""
        legacy = tmp_path / "legacy.jpg"
        legacy.write_bytes(b"jpegdata")
        db_path = tmp_path / "does-not-exist.db"

        result = await resolve_pending_images([
            (str(legacy), MagicMock(), MagicMock()),
            (str(tmp_path / "ghost.png"), MagicMock(), MagicMock()),
        ], db_path=db_path)

        assert result[0][0] == str(legacy.resolve())
        assert result[1][0] is None

    @pytest.mark.asyncio
    async def test_repeated_marker_resolved_once(self, tmp_path, images_dir):
        """Same marker in several turns → one write, one path per spec."""
        db_path = tmp_path / "memory.db"
        await _make_blob_db(db_path, [
            ("uuid-9", b"DATA9", "image/png", "uuid-9.png"),
        ])
        marker = str(tmp_path / "gone" / "uuid-9.png")
        cv1, aw1, cv2, aw2 = (MagicMock() for _ in range(4))

        result = await resolve_pending_images([
            (marker, cv1, aw1),
            (marker, cv2, aw2),
        ], db_path=db_path)

        assert len(result) == 2
        assert result[0][0] == result[1][0] is not None
        assert result[0][2] is cv1 and result[1][2] is cv2
        assert (images_dir / "uuid-9.png").read_bytes() == b"DATA9"

    @pytest.mark.asyncio
    async def test_same_id_different_marker_paths_share_write(self, tmp_path, images_dir):
        """Old/new cache dirs referencing the same BLOB id write once."""
        db_path = tmp_path / "memory.db"
        await _make_blob_db(db_path, [
            ("uuid-7", b"DATA7", "image/png", "uuid-7.png"),
        ])
        old_marker = str(tmp_path / "old-images" / "uuid-7.png")
        new_marker = str(tmp_path / "logs" / "images" / "uuid-7.png")

        result = await resolve_pending_images([
            (old_marker, MagicMock(), MagicMock()),
            (new_marker, MagicMock(), MagicMock()),
        ], db_path=db_path)

        assert result[0][0] == result[1][0] is not None
        assert list(images_dir.iterdir()) == [images_dir / "uuid-7.png"]

    @pytest.mark.asyncio
    async def test_blob_ext_from_mime_when_file_name_empty(self, tmp_path, images_dir):
        """Extension falls back to mime_type when file_name has none."""
        db_path = tmp_path / "memory.db"
        await _make_blob_db(db_path, [
            ("uuid-j", b"JPEG", "image/jpeg", ""),
        ])
        marker = str(tmp_path / "gone" / "uuid-j.png")

        result = await resolve_pending_images(
            [(marker, MagicMock(), MagicMock())], db_path=db_path,
        )

        assert result[0][0] == str((images_dir / "uuid-j.jpg").resolve())

    @pytest.mark.asyncio
    async def test_empty_pending(self, images_dir):
        assert await resolve_pending_images([]) == []


# ── Extension picker ──────────────────────────────────────────────────


class TestBlobExtension:
    def test_prefers_file_name_suffix(self):
        assert _blob_extension("photo.jpg", "image/png", ".png") == ".jpg"

    def test_falls_back_to_mime(self):
        assert _blob_extension("", "image/jpeg", ".png") == ".jpg"

    def test_falls_back_to_marker_suffix(self):
        assert _blob_extension("", "unknown/type", ".gif") == ".gif"

    def test_defaults_to_png(self):
        assert _blob_extension("", "", "") == ".png"


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
        """resolved=None mounts the broken-file fallback (never drops)."""
        cv, aw = MagicMock(), MagicMock()
        _mount_resolved_image(None, "/tmp/ghost.png", cv, aw)
        widget = cv.mount.call_args.args[0]
        assert "chat-image-fallback" in widget.classes
