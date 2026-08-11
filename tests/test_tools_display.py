"""Tests for slife.tools.display — ShowImageTool."""

import pytest; pytestmark = pytest.mark.unit

import struct
import zlib
from pathlib import Path
from unittest.mock import patch

import pytest

from slife.tools.display import NotifyUserTool, ShowImageTool


# ── helpers ────────────────────────────────────────────────────────────


def _minimal_png() -> bytes:
    """Return bytes of a minimal valid 1x1 red PNG."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        body = chunk_type + data
        crc = struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        return struct.pack(">I", len(data)) + body + crc

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    ihdr = _chunk(b"IHDR", ihdr_data)
    raw_data = b"\x00\xff\x00\x00"  # filter=none, R=255 G=0 B=0
    compressed = zlib.compress(raw_data)
    idat = _chunk(b"IDAT", compressed)
    iend = _chunk(b"IEND", b"")

    return signature + ihdr + idat + iend


class _MockResponse:
    """Async context manager that mimics aiohttp.ClientResponse."""

    def __init__(self, status=200, content_type="image/png", data=b""):
        self.status = status
        self.content_type = content_type
        self._data = data

    async def read(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class _MockSession:
    """Async context manager that mimics aiohttp.ClientSession."""

    def __init__(self, response):
        self._response = response

    def get(self, url, *, timeout=None):
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ── Metadata ───────────────────────────────────────────────────────────


class TestShowImageToolMetadata:
    """Class-level metadata validation for ShowImageTool."""

    def test_name(self):
        assert ShowImageTool.name == "show_image"

    def test_description(self):
        assert "Display an image in the terminal" in ShowImageTool.description

    def test_category(self):
        assert ShowImageTool.category == "Display"

    def test_not_vision_gated(self):
        """Pure UI display — always registered, independent of model vision."""
        assert getattr(ShowImageTool, "_requires_vision", False) is False

    def test_parameters_schema_type(self):
        assert ShowImageTool.parameters["type"] == "object"

    def test_parameters_path_property(self):
        props = ShowImageTool.parameters["properties"]
        assert "path" in props
        assert props["path"]["type"] == "string"

    def test_parameters_path_required(self):
        required = ShowImageTool.parameters.get("required", [])
        assert "path" in required

    def test_to_openai_function(self):
        func = ShowImageTool().to_openai_function()
        assert func["type"] == "function"
        assert func["function"]["name"] == "show_image"
        assert func["function"]["description"] == ShowImageTool.description
        assert func["function"]["parameters"] == ShowImageTool.parameters


# ── Execute — local paths ──────────────────────────────────────────────


class TestShowImageToolExecuteLocal:
    """Execute tests for ShowImageTool with local file paths."""

    @pytest.mark.asyncio
    async def test_execute_with_valid_image_file(self, tmp_path):
        """A valid local PNG returns the [image: ...] format string."""
        png_path = tmp_path / "photo.png"
        png_path.write_bytes(_minimal_png())

        tool = ShowImageTool()
        result = await tool.execute(path=str(png_path))

        assert "[image:" in result
        assert str(png_path.resolve()) in result

    @pytest.mark.asyncio
    async def test_execute_with_missing_file(self, tmp_path):
        """A path that does not exist returns an error."""
        nonexistent = tmp_path / "missing.png"

        tool = ShowImageTool()
        result = await tool.execute(path=str(nonexistent))

        assert result.startswith("Error:")
        assert "file not found" in result

    @pytest.mark.asyncio
    async def test_execute_with_directory_path(self, tmp_path):
        """A directory path returns an error."""
        tool = ShowImageTool()
        result = await tool.execute(path=str(tmp_path))

        assert result.startswith("Error:")
        assert "not a file" in result

    @pytest.mark.asyncio
    async def test_execute_with_unsupported_format(self, tmp_path):
        """A file with an unsupported extension returns an error."""
        txt_path = tmp_path / "notes.txt"
        txt_path.write_text("not an image")

        tool = ShowImageTool()
        result = await tool.execute(path=str(txt_path))

        assert result.startswith("Error:")
        assert "unsupported image format" in result


# ── Execute — URLs ─────────────────────────────────────────────────────


class TestShowImageToolExecuteUrl:
    """Execute tests for ShowImageTool with HTTP(S) URLs."""

    @pytest.mark.asyncio
    async def test_execute_with_http_url(self, tmp_path):
        """An HTTP URL is downloaded, cached, and displayed."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()

        mock_resp = _MockResponse(status=200, content_type="image/png", data=_minimal_png())
        mock_session = _MockSession(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch("slife.paths.get_images_dir", return_value=images_dir):
                tool = ShowImageTool()
                result = await tool.execute(path="http://example.com/photo.png")

        assert "[image:" in result
        assert "http://example.com/photo.png" in result

    @pytest.mark.asyncio
    async def test_execute_with_failed_download(self, tmp_path):
        """A non-200 HTTP status returns an error."""
        images_dir = tmp_path / "images"
        images_dir.mkdir()

        mock_resp = _MockResponse(status=404, content_type="text/html", data=b"Not Found")
        mock_session = _MockSession(mock_resp)

        with patch("aiohttp.ClientSession", return_value=mock_session):
            with patch("slife.paths.get_images_dir", return_value=images_dir):
                tool = ShowImageTool()
                result = await tool.execute(path="http://example.com/missing.png")

        assert result.startswith("Error:")
        assert "HTTP 404" in result

    @pytest.mark.asyncio
    async def test_execute_with_empty_source(self):
        """An empty path produces an error (treated as local — resolves to cwd)."""
        tool = ShowImageTool()
        result = await tool.execute(path="")

        assert result.startswith("Error:")


# ── NotifyUserTool ─────────────────────────────────────────────────────


class TestNotifyUserTool:
    """Desktop notification — a pure UI tool in the Display category."""

    def test_category(self):
        assert NotifyUserTool.category == "Display"

    def test_name(self):
        assert NotifyUserTool.name == "notify_user"

    @pytest.mark.asyncio
    async def test_missing_message(self):
        tool = NotifyUserTool()
        result = await tool.execute(message="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_notification_sent(self):
        tool = NotifyUserTool()
        with patch("slife.platform.desktop_notify"):
            result = await tool.execute(title="Test", message="Hello world")
        assert "Notification sent" in result
        assert "Test" in result
        assert "Hello world" in result
