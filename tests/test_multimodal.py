"""Tests for Slife.agent.multimodal — image encoding."""

import base64
from pathlib import Path
from unittest.mock import patch

import pytest

from slife.agent.multimodal import (
    encode_image,
    _ensure_mimetypes,
)


# ── encode_image ──────────────────────────────────────────────────────


class TestEncodeImage:
    """Tests for encode_image function."""

    def test_png_image(self, tmp_path):
        """PNG image is base64-encoded correctly."""
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake png content")

        result = encode_image(img)

        assert result["type"] == "image_url"
        assert result["image_url"]["url"].startswith("data:image/png;base64,")
        b64_data = result["image_url"]["url"].split(",", 1)[1]
        decoded = base64.b64decode(b64_data)
        assert decoded == b"\x89PNG\r\n\x1a\nfake png content"

    def test_jpeg_image(self, tmp_path):
        """JPEG image gets correct MIME type."""
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0 fake jpeg")

        result = encode_image(img)
        assert result["image_url"]["url"].startswith("data:image/jpeg;base64,")

    def test_webp_image(self, tmp_path):
        """WebP image gets correct MIME type."""
        img = tmp_path / "image.webp"
        img.write_bytes(b"RIFF....WEBP fake webp")

        result = encode_image(img)
        assert result["image_url"]["url"].startswith("data:image/webp;base64,")

    def test_file_not_found(self):
        """Raises FileNotFoundError for missing files."""
        with pytest.raises(FileNotFoundError) as exc_info:
            encode_image("/nonexistent/image.png")
        assert "Image not found" in str(exc_info.value)

    def test_not_a_file(self, tmp_path):
        """Raises ValueError for directories."""
        with pytest.raises(ValueError) as exc_info:
            encode_image(tmp_path)
        assert "Not a file" in str(exc_info.value)

    def test_unknown_extension_defaults_to_png(self, tmp_path):
        """Files with unknown extensions default to image/png."""
        img = tmp_path / "image.xyzzy"
        img.write_bytes(b"some binary content")

        result = encode_image(img)
        assert result["image_url"]["url"].startswith("data:image/png;base64,")

    def test_no_extension_defaults_to_png(self, tmp_path):
        """Files with no extension default to image/png."""
        img = tmp_path / "noext"
        img.write_bytes(b"binary data here")

        result = encode_image(img)
        assert result["image_url"]["url"].startswith("data:image/png;base64,")

    def test_gif_image(self, tmp_path):
        """GIF gets correct MIME type."""
        img = tmp_path / "anim.gif"
        img.write_bytes(b"GIF89a fake gif")

        result = encode_image(img)
        assert result["image_url"]["url"].startswith("data:image/gif;base64,")

    def test_non_image_mime_defaults_to_png(self, tmp_path):
        """Files with non-image MIME type default to image/png."""
        import mimetypes
        # Force mimetypes to be initialized
        mimetypes.init()
        # Create a file with no recognized image extension
        img = tmp_path / "data.bin"
        img.write_bytes(b"binary data")
        # Override guess_type to return a non-image MIME
        with patch("mimetypes.guess_type", return_value=("application/octet-stream", None)):
            result = encode_image(img)
            assert result["image_url"]["url"].startswith("data:image/png;base64,")


# ── _ensure_mimetypes ─────────────────────────────────────────────────


class TestEnsureMimetypes:
    """Tests for _ensure_mimetypes helper."""

    def test_initializes_once(self):
        """Calling twice should be safe (idempotent)."""
        import mimetypes
        # Force uninitialized state
        mimetypes.inited = False
        _ensure_mimetypes()
        assert mimetypes.inited is True
        # Second call should not fail
        _ensure_mimetypes()
        assert mimetypes.inited is True

    def test_already_initialized(self):
        """Should be a no-op when already initialized."""
        import mimetypes
        mimetypes.init()
        assert mimetypes.inited is True
        _ensure_mimetypes()
        assert mimetypes.inited is True

    def test_already_initialized_does_not_reinit(self):
        """When mimetypes is already initialized, init() is not called again."""
        import mimetypes
        mimetypes.init()
        with patch.object(mimetypes, "init") as mock_init:
            _ensure_mimetypes()
            mock_init.assert_not_called()


# ── encode_image additional edge cases ──────────────────────────────────────


class TestEncodeImageEdgeCases:
    """Additional edge case tests for encode_image."""

    def test_str_path_accepted(self, tmp_path):
        """encode_image accepts a string path, not just Path."""
        img = tmp_path / "photo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake png")
        result = encode_image(str(img))
        assert result["type"] == "image_url"
        assert result["image_url"]["url"].startswith("data:image/png;base64,")

    def test_svg_mime_kept_as_image(self, tmp_path):
        """SVG files with image/svg+xml MIME type are preserved as image/ type.

        The guard only redirects non-image MIME types to image/png.
        """
        img = tmp_path / "icon.svg"
        img.write_bytes(b"<svg></svg>")
        result = encode_image(img)
        # SVG MIME is image/svg+xml — starts with "image/"
        assert result["image_url"]["url"].startswith("data:image/svg+xml;base64,")

    def test_file_not_found_with_path_object(self):
        """FileNotFoundError with a Path object."""
        with pytest.raises(FileNotFoundError) as exc_info:
            encode_image(Path("/completely/made/up/image.png"))
        assert "Image not found" in str(exc_info.value)

    def test_not_a_file_with_explicit_dir(self, tmp_path):
        """ValueError when path points to a directory with trailing content."""
        subdir = tmp_path / "images"
        subdir.mkdir()
        with pytest.raises(ValueError) as exc_info:
            encode_image(subdir)
        assert "Not a file" in str(exc_info.value)
