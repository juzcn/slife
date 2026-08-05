"""Tests for slife.agent.multimodal — image URL generation for vision APIs."""

import pytest; pytestmark = pytest.mark.unit

from pathlib import Path
from unittest.mock import patch

import pytest

from slife.agent.multimodal import prepare_image_url, _ensure_mimetypes


# ── _ensure_mimetypes ─────────────────────────────────────────────────


class TestEnsureMimetypes:
    """Tests for _ensure_mimetypes helper."""

    def test_initializes_once(self):
        import mimetypes
        mimetypes.inited = False
        _ensure_mimetypes()
        assert mimetypes.inited is True
        _ensure_mimetypes()
        assert mimetypes.inited is True

    def test_already_initialized_does_not_reinit(self):
        import mimetypes
        mimetypes.init()
        with patch.object(mimetypes, "init") as mock_init:
            _ensure_mimetypes()
            mock_init.assert_not_called()


# ── prepare_image_url ───────────────────────────────────────────────


class TestPrepareImageUrl:
    """Tests for prepare_image_url — generates sharing URLs via in-memory registry."""

    def test_file_not_found_returns_none(self, tmp_path):
        result = prepare_image_url(tmp_path / "nonexistent.png")
        assert result is None

    def test_not_a_file_returns_none(self, tmp_path):
        result = prepare_image_url(tmp_path)
        assert result is None

    def test_image_generates_url_block(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake png")

        with patch("slife.agent.multimodal.register_file", return_value="a1b2c3d4e5f6g7h8"), \
             patch("slife.agent.multimodal.share_url_for", return_value="https://test.ngrok.io/share/a1b2c3d4e5f6g7h8"):
            result = prepare_image_url(img)

        assert result is not None
        assert result["type"] == "image_url"
        url = result["image_url"]["url"]
        assert url == "https://test.ngrok.io/share/a1b2c3d4e5f6g7h8"

    def test_tunnel_offline_returns_none(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff")

        with patch("slife.agent.multimodal.register_file", return_value="abc"), \
             patch("slife.agent.multimodal.share_url_for", return_value=None):
            result = prepare_image_url(img)

        assert result is None

    def test_non_image_mime_still_works(self, tmp_path):
        """Non-image MIME types are force-corrected to image/png."""
        img = tmp_path / "data.bin"
        img.write_bytes(b"binary")

        with patch("slife.agent.multimodal.register_file", return_value="abc"), \
             patch("slife.agent.multimodal.share_url_for", return_value="https://x/share/abc"):
            with patch("mimetypes.guess_type", return_value=("application/octet-stream", None)):
                result = prepare_image_url(img)

        assert result is not None
        assert result["type"] == "image_url"

    def test_str_path_accepted(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        with patch("slife.agent.multimodal.register_file", return_value="xyz"), \
             patch("slife.agent.multimodal.share_url_for", return_value="https://x/share/xyz"):
            result = prepare_image_url(str(img))

        assert result is not None
