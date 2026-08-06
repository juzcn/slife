"""Tests for slife.agent.multimodal — image URL generation for vision APIs."""

import base64
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
    """Tests for prepare_image_url — base64 data-URI generation."""

    def test_file_not_found_returns_none(self, tmp_path):
        result = prepare_image_url(tmp_path / "nonexistent.png")
        assert result is None

    def test_not_a_file_returns_none(self, tmp_path):
        result = prepare_image_url(tmp_path)  # directory, not file
        assert result is None

    def test_str_path_accepted(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        result = prepare_image_url(str(img))
        assert result is not None
        assert result["type"] == "image_url"
        assert result["image_url"]["url"].startswith("data:image/png;base64,")

    def test_path_object_accepted(self, tmp_path):
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")

        result = prepare_image_url(img)  # Path, not str
        assert result is not None
        assert result["type"] == "image_url"

    def test_image_generates_base64_data_url(self, tmp_path):
        img = tmp_path / "photo.png"
        data = b"\x89PNG\r\n\x1a\nfake png content"
        img.write_bytes(data)

        result = prepare_image_url(img)
        assert result is not None
        assert result["type"] == "image_url"
        url = result["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        # Verify the base64 payload decodes back to original.
        encoded = url[len("data:image/png;base64,"):]
        assert base64.b64decode(encoded) == data

    def test_jpg_detected_from_magic(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")

        result = prepare_image_url(img)
        assert result is not None
        url = result["image_url"]["url"]
        assert url.startswith("data:image/")

    def test_https_url_passthrough(self):
        result = prepare_image_url("https://example.com/photo.png")
        assert result is not None
        assert result["type"] == "image_url"
        assert result["image_url"]["url"] == "https://example.com/photo.png"

    def test_http_url_passthrough(self):
        result = prepare_image_url("http://example.com/photo.jpg")
        assert result is not None
        assert result["image_url"]["url"] == "http://example.com/photo.jpg"

    def test_non_image_mime_still_works(self, tmp_path):
        """MIME type not starting with 'image/' is force-corrected to image/png."""
        img = tmp_path / "data.bin"
        img.write_bytes(b"binary stuff")

        with patch("mimetypes.guess_type", return_value=("application/octet-stream", None)):
            result = prepare_image_url(img)

        assert result is not None
        assert result["type"] == "image_url"
        assert result["image_url"]["url"].startswith("data:image/png;base64,")

    def test_read_error_returns_none(self, tmp_path):
        img = tmp_path / "unreadable.png"
        img.write_bytes(b"data")

        with patch.object(Path, "read_bytes", side_effect=OSError("permission denied")):
            result = prepare_image_url(img)

        assert result is None
