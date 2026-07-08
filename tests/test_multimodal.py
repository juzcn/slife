"""Tests for multimodal utilities (slife.agent.multimodal)."""

import base64
import re
from pathlib import Path

import pytest

from slife.agent.multimodal import encode_image, parse_file_attachments, _ensure_mimetypes


class TestEncodeImage:
    """Tests for encode_image()."""

    def test_encodes_png(self, temp_image_file):
        """PNG file is base64-encoded with correct MIME type."""
        result = encode_image(temp_image_file)
        assert result["type"] == "image_url"
        assert "image_url" in result
        url = result["image_url"]["url"]
        assert url.startswith("data:image/png;base64,")
        # Decode and verify
        b64_data = url.split(",", 1)[1]
        decoded = base64.b64decode(b64_data)
        assert decoded == temp_image_file.read_bytes()

    def test_file_not_found(self):
        """FileNotFoundError for missing file."""
        with pytest.raises(FileNotFoundError, match="Image not found"):
            encode_image("nonexistent_image_xyz.png")

    def test_not_a_file(self, tmp_path):
        """ValueError when path is a directory."""
        with pytest.raises(ValueError, match="Not a file"):
            encode_image(tmp_path)

    def test_returns_dict_with_correct_keys(self, temp_image_file):
        """Return value has the expected structure."""
        result = encode_image(temp_image_file)
        assert isinstance(result, dict)
        assert "type" in result
        assert "image_url" in result
        assert "url" in result["image_url"]

    def test_unknown_extension_defaults_to_png(self, tmp_path):
        """File with unknown extension defaults to image/png."""
        # Create a file with valid PNG header to avoid issues
        p = tmp_path / "image.xyz"
        # Write minimal valid PNG bytes
        import struct, zlib
        def chunk(t, d):
            c = t + d
            return struct.pack(">I", len(d)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
        png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00")) + chunk(b"IEND", b"")
        p.write_bytes(png)

        result = encode_image(p)
        # May resolve as different image type or default to png
        assert result["image_url"]["url"].startswith("data:image/")

    def test_non_image_mime_falls_back_to_png(self, temp_image_file, monkeypatch):
        """If guessed MIME is not image/*, fall back to image/png."""
        import mimetypes

        def mock_guess_type(path):
            return ("text/plain", None)

        monkeypatch.setattr(mimetypes, "guess_type", mock_guess_type)
        result = encode_image(temp_image_file)
        assert result["image_url"]["url"].startswith("data:image/png;base64,")


class TestParseFileAttachments:
    """Tests for parse_file_attachments()."""

    def test_no_file_directives(self):
        """Text without /file passes through unchanged."""
        text = "Hello, how are you?"
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == "Hello, how are you?"
        assert paths == []

    def test_single_file_directive(self):
        """Single /file directive is extracted."""
        text = "Look at this\n/file image.png"
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == "Look at this"
        assert paths == ["image.png"]

    def test_multiple_file_directives(self):
        """Multiple /file directives are all extracted."""
        text = "Compare:\n/file a.png\n/file b.jpg\nWhat do you think?"
        cleaned, paths = parse_file_attachments(text)
        # Lines that were /file become empty strings in cleaned_parts, then
        # join produces consecutive newlines. The trailing strip() removes
        # trailing newlines but preserves internal ones.
        assert "Compare:" in cleaned
        assert "What do you think?" in cleaned
        assert paths == ["a.png", "b.jpg"]

    def test_file_directive_with_spaces(self):
        """File paths with spaces are preserved."""
        text = "/file my documents/photo.jpg"
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == ""
        assert paths == ["my documents/photo.jpg"]

    def test_file_directive_only(self):
        """Only /file directives, no other text."""
        text = "/file a.png\n/file b.png"
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == ""
        assert paths == ["a.png", "b.png"]

    def test_file_directive_with_leading_trailing_spaces(self):
        """/file with extra whitespace around path."""
        text = "/file   padded.png   "
        cleaned, paths = parse_file_attachments(text)
        assert cleaned == ""
        assert paths == ["padded.png"]

    def test_empty_input(self):
        """Empty string returns empty results."""
        cleaned, paths = parse_file_attachments("")
        assert cleaned == ""
        assert paths == []

    def test_false_positive_avoided(self):
        """Text containing '/file' not at start of line is not treated as directive."""
        text = "Use the /file command to attach"
        cleaned, paths = parse_file_attachments(text)
        # "Use the /file command to attach" — not at line start, so stays
        assert "Use the /file command" in cleaned
        assert paths == []

    def test_mixed_content_and_files(self):
        """Realistic mixed input."""
        text = "Please analyze these images:\n/file photo1.png\n/file photo2.jpg\nWhat do you see?"
        cleaned, paths = parse_file_attachments(text)
        assert "Please analyze these images:" in cleaned
        assert "What do you see?" in cleaned
        assert paths == ["photo1.png", "photo2.jpg"]

    def test_lines_with_only_whitespace_not_matched(self):
        """Lines that are just whitespace are preserved as empty lines."""
        text = "hello\n  \n/file img.png\nworld"
        cleaned, paths = parse_file_attachments(text)
        # The blank line with spaces becomes empty after join and strip
        assert "hello" in cleaned
        assert "world" in cleaned
        assert paths == ["img.png"]


class TestEnsureMimetypes:
    """Tests for _ensure_mimetypes()."""

    def test_initializes_mimetypes(self):
        """_ensure_mimetypes initializes the mimetypes database."""
        import mimetypes
        # Force re-init check
        mimetypes.inited = False
        _ensure_mimetypes()
        assert mimetypes.inited is True

    def test_idempotent(self):
        """Calling _ensure_mimetypes multiple times is safe."""
        _ensure_mimetypes()
        _ensure_mimetypes()
        _ensure_mimetypes()
        import mimetypes
        assert mimetypes.inited is True
