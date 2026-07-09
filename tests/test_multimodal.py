"""Tests for slife.agent.multimodal — image encoding and file attachment parsing."""

import base64
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from slife.agent.multimodal import (
    encode_image,
    parse_file_attachments,
    _ensure_mimetypes,
    _FILE_PATTERN,
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


# ── parse_file_attachments ────────────────────────────────────────────


class TestParseFileAttachments:
    """Tests for parse_file_attachments function."""

    def test_no_file_directives(self):
        """Text without /file directives passes through."""
        text = "Hello, how are you?"
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == "Hello, how are you?"
        assert paths == []

    def test_single_file_directive(self):
        """Single /file directive is extracted."""
        text = "Look at this:\n/file image.png\nWhat do you think?"
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == "Look at this:\nWhat do you think?"
        assert paths == ["image.png"]

    def test_multiple_file_directives(self):
        """Multiple /file directives all extracted."""
        text = "/file a.png\n/file b.jpg\nDescribe these."
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == "Describe these."
        assert paths == ["a.png", "b.jpg"]

    def test_file_paths_trimmed(self):
        """File paths have whitespace stripped."""
        text = "/file   path with spaces.png   "
        cleaned, paths = parse_file_attachments(text)
        assert paths == ["path with spaces.png"]

    def test_only_file_directives(self):
        """Text containing only /file directives returns empty string."""
        text = "/file img1.png\n/file img2.png"
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == ""
        assert paths == ["img1.png", "img2.png"]

    def test_empty_input(self):
        """Empty string returns empty results."""
        cleaned, paths = parse_file_attachments("")
        assert cleaned == ""
        assert paths == []

    def test_file_directive_not_on_own_line(self):
        """/file directive only matches when on its own line."""
        text = "Some /file image.png inline text"
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == "Some /file image.png inline text"
        assert paths == []

    def test_case_sensitive(self):
        """/file is case-sensitive (must be lowercase)."""
        text = "/File image.png"
        cleaned, paths = parse_file_attachments(text)
        assert paths == []


# ── _FILE_PATTERN ─────────────────────────────────────────────────────


class TestFilePattern:
    """Tests for the regex pattern behavior."""

    def test_pattern_matches_file_directive(self):
        m = _FILE_PATTERN.match("/file path/to/file.txt")
        assert m is not None
        assert m.group(1) == "path/to/file.txt"

    def test_pattern_does_not_match_similar(self):
        assert _FILE_PATTERN.match(" /file leading space") is None
        assert _FILE_PATTERN.match("/files plural") is None
        assert _FILE_PATTERN.match("/file") is None  # No path after


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
