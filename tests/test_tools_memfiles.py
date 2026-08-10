"""Tests for slife.tools.memfiles — ExposeFileTool, SaveToMemfilesTool,
IncludeImageTool, and helper functions."""

import pytest; pytestmark = pytest.mark.unit


import json
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from slife.tools.memfiles import (
    ExposeFileTool,
    SaveToMemfilesTool,
    IncludeImageTool,
    _slugify,
    _unique_path,
    _extract_title,
    _load_index,
    _save_index,
    _add_index_entry,
)


# ── Resetting the index path between tests ───────────────────────────────


@pytest.fixture(autouse=True)
def _reset_index_module_state():
    """Reset the module-level _INDEX_PATH cache so each test is isolated."""
    import slife.tools.memfiles as _mod

    _mod._INDEX_PATH = None
    yield
    _mod._INDEX_PATH = None


# ── aiohttp mock helper ────────────────────────────────────────────────


def _make_mock_aiohttp(status=200, body=b"", exc=None):
    """Create a mock aiohttp.ClientSession that works as an async context manager.

    Returns a callable that replaces ``aiohttp.ClientSession`` so that::

        async with aiohttp.ClientSession() as session:
            async with session.get(url, ...) as resp:
                ...

    works within tests.
    """
    mock_resp = MagicMock()
    mock_resp.status = status
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=None)
    if exc:
        mock_resp.read = AsyncMock(side_effect=exc)
    else:
        mock_resp.read = AsyncMock(return_value=body)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_resp
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    return lambda *a, **kw: mock_session


# ═══════════════════════════════════════════════════════════════════════════
# Helper function tests
# ═══════════════════════════════════════════════════════════════════════════


class TestSlugify:
    """Tests for _slugify."""

    def test_lowercase_and_strip(self):
        assert _slugify("Hello World") == "hello-world"

    def test_removes_special_characters(self):
        assert _slugify("Project Notes 2026!") == "project-notes-2026"

    def test_collapse_multiple_spaces_and_dashes(self):
        assert _slugify("foo   ---bar") == "foo-bar"

    def test_trims_leading_trailing_dashes(self):
        assert _slugify("--hello--") == "hello"

    def test_truncates_to_120_chars(self):
        long_text = "a" * 200
        result = _slugify(long_text)
        assert len(result) == 120
        assert result == "a" * 120


class TestUniquePath:
    """Tests for _unique_path."""

    def test_no_conflict_returns_original(self, tmp_path):
        result = _unique_path(tmp_path, "notes", ".md")
        assert result == tmp_path / "notes.md"
        assert not result.exists()

    def test_conflict_adds_underscore_2(self, tmp_path):
        (tmp_path / "notes.md").write_text("existing")
        result = _unique_path(tmp_path, "notes", ".md")
        assert result == tmp_path / "notes_1.md"

    def test_multiple_conflicts_keeps_incrementing(self, tmp_path):
        (tmp_path / "notes.md").write_text("a")
        (tmp_path / "notes_1.md").write_text("b")
        (tmp_path / "notes_2.md").write_text("c")
        result = _unique_path(tmp_path, "notes", ".md")
        assert result == tmp_path / "notes_3.md"


class TestExtractTitle:
    """Tests for _extract_title."""

    def test_finds_first_heading(self):
        assert _extract_title("# Welcome\n\nContent here.") == "Welcome"

    def test_returns_none_for_no_heading(self):
        assert _extract_title("Just some text.") is None
        assert _extract_title("") is None

    def test_heading_with_extra_whitespace(self):
        assert _extract_title("#   Spaced Title   \n\nBody") == "Spaced Title"

    def test_only_matches_at_line_start(self):
        assert _extract_title("text # not a heading") is None


class TestIndexHelpers:
    """Tests for _load_index, _save_index, _add_index_entry."""

    def test_load_index_returns_empty_list_for_missing_file(self, tmp_path):
        import slife.tools.memfiles as _mod

        idx_file = tmp_path / "index.json"
        _mod._INDEX_PATH = idx_file
        assert _load_index() == []

    def test_load_index_returns_parsed_json(self, tmp_path):
        import slife.tools.memfiles as _mod

        idx_file = tmp_path / "index.json"
        idx_file.write_text(
            json.dumps([{"title": "Test", "filename": "test.md"}]),
            encoding="utf-8",
        )
        _mod._INDEX_PATH = idx_file
        entries = _load_index()
        assert len(entries) == 1
        assert entries[0]["title"] == "Test"

    def test_load_index_returns_empty_on_corrupt_json(self, tmp_path):
        import slife.tools.memfiles as _mod

        idx_file = tmp_path / "index.json"
        idx_file.write_text("not valid json{{{", encoding="utf-8")
        _mod._INDEX_PATH = idx_file
        assert _load_index() == []

    def test_save_index_writes_json(self, tmp_path):
        import slife.tools.memfiles as _mod

        idx_file = tmp_path / "index.json"
        _mod._INDEX_PATH = idx_file
        _save_index([{"title": "Saved", "filename": "saved.md"}])
        assert idx_file.exists()
        data = json.loads(idx_file.read_text(encoding="utf-8"))
        assert data[0]["title"] == "Saved"

    def test_save_index_creates_parent_directory(self, tmp_path):
        import slife.tools.memfiles as _mod

        idx_file = tmp_path / "sub" / "nested" / "index.json"
        _mod._INDEX_PATH = idx_file
        _save_index([{"title": "Deep"}])
        assert idx_file.exists()

    def test_add_index_entry_appends_with_required_fields(self, tmp_path):
        import slife.tools.memfiles as _mod

        idx_file = tmp_path / "index.json"
        _mod._INDEX_PATH = idx_file

        _add_index_entry(
            "My Note", "my-note.md", ["tag1", "tag2"], "content"
        )

        entries = _load_index()
        assert len(entries) == 1
        entry = entries[0]
        assert entry["title"] == "My Note"
        assert entry["filename"] == "my-note.md"
        assert entry["tags"] == ["tag1", "tag2"]
        assert entry["source"] == "content"
        assert "saved_at" in entry

    def test_add_index_entry_handles_empty_tags(self, tmp_path):
        import slife.tools.memfiles as _mod

        idx_file = tmp_path / "index.json"
        _mod._INDEX_PATH = idx_file

        _add_index_entry("Note", "note.md", [], "url")
        entries = _load_index()
        assert entries[0]["tags"] == []

    def test_add_index_entry_appends_to_existing(self, tmp_path):
        import slife.tools.memfiles as _mod

        idx_file = tmp_path / "index.json"
        idx_file.write_text(
            json.dumps([{"title": "Old"}]), encoding="utf-8"
        )
        _mod._INDEX_PATH = idx_file

        _add_index_entry("New", "new.md", [], "path")
        entries = _load_index()
        assert len(entries) == 2
        assert entries[0]["title"] == "Old"
        assert entries[1]["title"] == "New"


# ═══════════════════════════════════════════════════════════════════════════
# ExposeFileTool
# ═══════════════════════════════════════════════════════════════════════════


class TestExposeFileTool:
    """Tests for ExposeFileTool metadata and execute."""

    def test_name(self):
        assert ExposeFileTool.name == "expose_file"

    def test_category(self):
        assert ExposeFileTool.category == "MemFiles"

    def test_requires_tunnel(self):
        assert ExposeFileTool._requires_tunnel is True

    def test_description(self):
        assert "Expose a local file" in ExposeFileTool.description
        assert "ngrok tunnel" in ExposeFileTool.description

    def test_parameters_schema(self):
        params = ExposeFileTool.parameters
        assert params["type"] == "object"
        assert "path" in params["properties"]
        assert params["required"] == ["path"]

    @pytest.mark.asyncio
    async def test_execute_with_active_tunnel(self, tmp_path):
        """When tunnel is active, returns the public URL."""
        test_file = tmp_path / "photo.png"
        test_file.write_text("fake image data")

        with patch(
            "slife.memfiles.tunnel.is_active", return_value=True
        ), patch(
            "slife.memfiles.tunnel.share_url_for",
            return_value="https://example.ngrok.app/share/abc123",
        ), patch(
            "slife.memfiles.token.register_file", return_value="abc123"
        ):
            tool = ExposeFileTool()
            result = await tool.execute(path=str(test_file))

        assert "Public URL for photo.png" in result
        assert "https://example.ngrok.app/share/abc123" in result

    @pytest.mark.asyncio
    async def test_execute_offline_tunnel(self, tmp_path):
        """When tunnel is inactive, returns an error."""
        test_file = tmp_path / "notes.txt"
        test_file.write_text("content")

        with patch("slife.memfiles.tunnel.is_active", return_value=False):
            tool = ExposeFileTool()
            result = await tool.execute(path=str(test_file))

        assert result.startswith("Error:")
        assert "file sharing service is not available" in result

    @pytest.mark.asyncio
    async def test_execute_missing_file(self):
        tool = ExposeFileTool()
        result = await tool.execute(path="D:\\nonexistent\\file.png")
        assert result.startswith("Error:")
        assert "file not found" in result

    @pytest.mark.asyncio
    async def test_execute_directory_not_file(self, tmp_path):
        tool = ExposeFileTool()
        result = await tool.execute(path=str(tmp_path))
        assert result.startswith("Error:")
        assert "not a file" in result

    @pytest.mark.asyncio
    async def test_execute_tunnel_down_after_register(self, tmp_path):
        """share_url_for returns None after tunnel goes down."""
        test_file = tmp_path / "doc.pdf"
        test_file.write_text("pdf content")

        with patch(
            "slife.memfiles.tunnel.is_active", return_value=True
        ), patch(
            "slife.memfiles.tunnel.share_url_for", return_value=None
        ), patch(
            "slife.memfiles.token.register_file", return_value="abc123"
        ):
            tool = ExposeFileTool()
            result = await tool.execute(path=str(test_file))

        assert result.startswith("Error:")
        assert "file sharing service became unavailable" in result


# ═══════════════════════════════════════════════════════════════════════════
# SaveToMemfilesTool
# ═══════════════════════════════════════════════════════════════════════════


class TestSaveToMemfilesToolMeta:
    """Tests for SaveToMemfilesTool class-level attributes."""

    def test_name(self):
        assert SaveToMemfilesTool.name == "save_content_or_files"

    def test_category(self):
        assert SaveToMemfilesTool.category == "MemFiles"

    def test_no_subagent_skip(self):
        """Subagent filtering was removed — no _subagent_skip attribute."""
        assert not hasattr(SaveToMemfilesTool, "_subagent_skip")

    def test_description(self):
        assert "Save content" in SaveToMemfilesTool.description
        assert "memfiles/" in SaveToMemfilesTool.description

    def test_parameters_schema(self):
        params = SaveToMemfilesTool.parameters
        assert params["type"] == "object"
        props = params["properties"]
        assert "content" in props
        assert "url" in props
        assert "path" in props
        assert "title" in props
        assert "tags" in props
        assert params["required"] == []


class TestSaveToMemfilesExecute:
    """Tests for SaveToMemfilesTool.execute."""

    @pytest.mark.asyncio
    async def test_save_content_creates_md_file(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "slife.memfiles.token.register_file", return_value="tok123"
        ), patch(
            "slife.memfiles.tunnel.share_url_for",
            return_value="https://ngrok.example/share/tok123",
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(
                content="# Hello\n\nWorld.",
                title="test-note",
                tags=["demo"],
            )

        md_files = list(mem_dir.glob("*.md"))
        assert len(md_files) == 1
        content = md_files[0].read_text(encoding="utf-8")
        assert "# Hello" in content
        assert "World." in content
        assert "Saved:" in result
        assert "URL:" in result

    @pytest.mark.asyncio
    async def test_save_content_auto_extracts_title(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "slife.memfiles.token.register_file", return_value="tok123"
        ), patch(
            "slife.memfiles.tunnel.share_url_for",
            return_value="https://ngrok.example/share/tok123",
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(
                content="# Welcome To My Notes\n\nContent here.",
            )

        md_files = list(mem_dir.glob("*.md"))
        assert len(md_files) == 1
        assert md_files[0].name == "welcome-to-my-notes.md"
        assert "Saved:" in result

    @pytest.mark.asyncio
    async def test_save_content_untitled_fallback(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "slife.memfiles.token.register_file", return_value="tok123"
        ), patch(
            "slife.memfiles.tunnel.share_url_for",
            return_value="https://ngrok.example/share/tok123",
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(content="Just some text, no heading.")

        md_files = list(mem_dir.glob("*.md"))
        assert len(md_files) == 1
        assert md_files[0].name == "untitled.md"
        assert "Saved:" in result

    @pytest.mark.asyncio
    async def test_save_url_downloads_file(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "slife.memfiles.token.register_file", return_value="tok456"
        ), patch(
            "slife.memfiles.tunnel.share_url_for",
            return_value="https://ngrok.example/share/tok456",
        ), patch(
            "aiohttp.ClientSession",
            _make_mock_aiohttp(body=b"<html>Page</html>"),
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(
                url="https://example.com/article/slides.html",
                title="",
            )

        files = [f for f in mem_dir.iterdir() if f.name != "index.json"]
        assert len(files) == 1
        assert files[0].read_bytes() == b"<html>Page</html>"
        assert "Saved:" in result
        assert "URL:" in result

    @pytest.mark.asyncio
    async def test_save_url_http_error(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "aiohttp.ClientSession", _make_mock_aiohttp(status=404)
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(
                url="https://example.com/missing"
            )

        assert result.startswith("Error:")
        assert "HTTP 404" in result

    @pytest.mark.asyncio
    async def test_save_url_download_exception(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "aiohttp.ClientSession",
            _make_mock_aiohttp(exc=Exception("Connection refused")),
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(
                url="https://down.example/broken"
            )

        assert result.startswith("Error:")
        assert "Connection refused" in result

    @pytest.mark.asyncio
    async def test_save_path_copies_file(self, tmp_path):
        src_file = tmp_path / "original.txt"
        src_file.write_text("original content")
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "slife.memfiles.token.register_file", return_value="tok789"
        ), patch(
            "slife.memfiles.tunnel.share_url_for",
            return_value="https://ngrok.example/share/tok789",
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(path=str(src_file))

        # Original still exists
        assert src_file.exists()
        # Copy is in mem_dir
        copied = [f for f in mem_dir.iterdir() if f.name != "index.json"]
        assert len(copied) == 1
        assert copied[0].read_text() == "original content"
        assert "Saved:" in result

    @pytest.mark.asyncio
    async def test_save_path_missing_file(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(
                path=str(tmp_path / "nonexistent.txt")
            )

        assert result.startswith("Error:")
        assert "file not found" in result

    @pytest.mark.asyncio
    async def test_save_path_directory(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(path=str(tmp_path))

        assert result.startswith("Error:")
        assert "not a file" in result

    @pytest.mark.asyncio
    async def test_save_duplicate_filename_adds_counter(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()
        (mem_dir / "notes.md").write_text("first")

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "slife.memfiles.token.register_file", return_value="tok999"
        ), patch(
            "slife.memfiles.tunnel.share_url_for",
            return_value="https://ngrok.example/share/tok999",
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(
                content="second", title="notes"
            )

        md_files = sorted(mem_dir.glob("*.md"))
        assert len(md_files) == 2
        assert md_files[1].name == "notes_1.md"
        assert "Saved:" in result

    @pytest.mark.asyncio
    async def test_save_empty_content(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(content="   ")

        assert result.startswith("Error:")
        assert "content is empty" in result

    @pytest.mark.asyncio
    async def test_save_no_source_provided(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute()

        assert result.startswith("Error:")
        assert "provide one of: content, url, or path" in result

    @pytest.mark.asyncio
    async def test_save_multiple_sources(self, tmp_path):
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(
                content="hello", url="https://example.com"
            )

        assert result.startswith("Error:")
        assert "provide only one source" in result
        assert "content" in result
        assert "url" in result

    @pytest.mark.asyncio
    async def test_save_sharing_offline_still_succeeds(self, tmp_path):
        """When tunnel is offline, the file is still saved locally."""
        mem_dir = tmp_path / "memfiles"
        mem_dir.mkdir()

        with patch(
            "slife.tools.memfiles.get_memfiles_dir", return_value=mem_dir
        ), patch(
            "slife.memfiles.token.register_file", return_value="tok"
        ), patch(
            "slife.memfiles.tunnel.share_url_for", return_value=None
        ):
            tool = SaveToMemfilesTool()
            result = await tool.execute(content="Offline note")

        assert "Saved:" in result
        assert "sharing offline" in result
        assert len(list(mem_dir.glob("*.md"))) == 1


# ═══════════════════════════════════════════════════════════════════════════
# IncludeImageTool
# ═══════════════════════════════════════════════════════════════════════════


class TestIncludeImageTool:
    """Tests for IncludeImageTool."""

    def test_name(self):
        assert IncludeImageTool.name == "include_image"

    def test_category(self):
        assert IncludeImageTool.category == "MemFiles"

    def test_requires_vision(self):
        assert IncludeImageTool._requires_vision is True

    def test_description(self):
        assert "Include an image" in IncludeImageTool.description

    def test_parameters_schema(self):
        params = IncludeImageTool.parameters
        assert params["type"] == "object"
        assert "source" in params["properties"]
        assert params["required"] == ["source"]

    @pytest.mark.asyncio
    async def test_execute_calls_include_image_url(self):
        fake_block = {"type": "image_url", "image_url": {"url": "data:..."}}

        with patch(
            "slife.agent.multimodal.include_image_url",
            return_value=fake_block,
        ):
            tool = IncludeImageTool()
            result = await tool.execute(source="D:\\photo.jpg")

        assert result == "Image included: D:\\photo.jpg"

    @pytest.mark.asyncio
    async def test_execute_invalid_source(self):
        with patch(
            "slife.agent.multimodal.include_image_url", return_value=None
        ):
            tool = IncludeImageTool()
            result = await tool.execute(source="D:\\missing.jpg")

        assert result.startswith("Error:")
        assert "cannot read image" in result

    @pytest.mark.asyncio
    async def test_execute_with_conversation_context(self):
        """When _ctx.conversation is set, images are injected."""
        fake_block = {"type": "image_url", "image_url": {"url": "data:..."}}

        mock_conv = MagicMock()
        mock_conv.inject_images_to_last_user = MagicMock()

        mock_ctx = MagicMock()
        mock_ctx.conversation = mock_conv

        with patch(
            "slife.agent.multimodal.include_image_url",
            return_value=fake_block,
        ):
            tool = IncludeImageTool()
            object.__setattr__(tool, "_ctx", mock_ctx)
            result = await tool.execute(source="D:\\img.jpg")

        assert result == "Image included: D:\\img.jpg"
        mock_conv.inject_images_to_last_user.assert_called_once_with(
            [fake_block]
        )
