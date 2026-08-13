"""Tests for the memfiles plugin server — a standard Streamable HTTP plugin.

Mocks the ngrok tunnel (no network) and exercises the MCP tool functions
directly, following the test_mqtt_plugin.py pattern.  Covers the token
registry, expose_file / save_content_or_files, the harness-only tools,
and the custom ``GET /share/{file_id}`` HTTP route.
"""

import pytest; pytestmark = pytest.mark.unit


import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import slife.plugins.memfiles.server as plugin


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reset registry + index path + plugin port so each test is isolated."""
    plugin._reset_registry()
    plugin._INDEX_PATH = None
    plugin._PLUGIN_PORT = 12345
    yield
    plugin._reset_registry()
    plugin._INDEX_PATH = None


def _active_tunnel(url="https://slife.ngrok-free.dev"):
    """Patch the tunnel so it reports active with the given public URL."""
    return patch.multiple(
        plugin,
        is_active=MagicMock(return_value=True),
        public_url=MagicMock(return_value=url),
        share_url_for=MagicMock(side_effect=lambda fid: f"{url}/share/{fid}"),
    )


def _offline_tunnel():
    """Patch the tunnel so it reports inactive.

    Also clear ``_PLUGIN_PORT`` so ``_ensure_tunnel`` short-circuits without
    a real ngrok start attempt — without this the offline test spent ~9s
    trying to reach the actual ngrok service.
    """
    return patch.multiple(
        plugin,
        is_active=MagicMock(return_value=False),
        public_url=MagicMock(return_value=None),
        share_url_for=MagicMock(return_value=None),
        _PLUGIN_PORT=0,
    )


# ═══════════════════════════════════════════════════════════════════════
# Token registry
# ═══════════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_returns_30_char_hex(self):
        tok = plugin._register_file("/some/file.txt")
        assert len(tok) == 30
        assert all(c in "0123456789abcdef" for c in tok)

    def test_roundtrip(self):
        tok = plugin._register_file("/data/report.pdf")
        assert plugin._lookup_file(tok) == "/data/report.pdf"

    def test_dedup_same_path(self):
        t1 = plugin._register_file("/tmp/a.txt")
        t2 = plugin._register_file("/tmp/a.txt")
        assert t1 == t2

    def test_distinct_paths(self):
        t1 = plugin._register_file("/a.txt")
        t2 = plugin._register_file("/b.txt")
        assert t1 != t2

    def test_unknown_token(self):
        assert plugin._lookup_file("deadbeef") is None


# ═══════════════════════════════════════════════════════════════════════
# Filename helpers
# ═══════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_slugify(self):
        assert plugin._slugify("Project Notes 2026!") == "project-notes-2026"
        assert plugin._slugify("--hello--") == "hello"

    def test_unique_path_no_conflict(self, tmp_path):
        assert plugin._unique_path(tmp_path, "notes", ".md") == tmp_path / "notes.md"

    def test_unique_path_conflict(self, tmp_path):
        (tmp_path / "notes.md").write_text("x")
        assert plugin._unique_path(tmp_path, "notes", ".md") == tmp_path / "notes_1.md"

    def test_extract_title(self):
        assert plugin._extract_title("# Welcome\n\nBody") == "Welcome"
        assert plugin._extract_title("no heading") is None


# ═══════════════════════════════════════════════════════════════════════
# expose_file
# ═══════════════════════════════════════════════════════════════════════


class TestExposeFile:
    @pytest.mark.asyncio
    async def test_active_tunnel_returns_url(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"pngdata")
        with _active_tunnel():
            result = await plugin.expose_file(path=str(f))
        assert "Public URL for photo.png" in result
        assert "https://slife.ngrok-free.dev/share/" in result

    @pytest.mark.asyncio
    async def test_offline_tunnel_returns_error(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"pngdata")
        with _offline_tunnel():
            result = await plugin.expose_file(path=str(f))
        assert result.startswith("Error:")
        assert "file sharing service is not available" in result

    @pytest.mark.asyncio
    async def test_missing_file(self):
        with _active_tunnel():
            result = await plugin.expose_file(path="D:\\nonexistent\\x.png")
        assert result.startswith("Error:")
        assert "file not found" in result

    @pytest.mark.asyncio
    async def test_directory(self, tmp_path):
        with _active_tunnel():
            result = await plugin.expose_file(path=str(tmp_path))
        assert result.startswith("Error:")
        assert "not a file" in result

    @pytest.mark.asyncio
    async def test_url_drops_after_register(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"pdf")
        with patch.multiple(
            plugin,
            is_active=MagicMock(return_value=True),
            share_url_for=MagicMock(return_value=None),
        ):
            result = await plugin.expose_file(path=str(f))
        assert result.startswith("Error:")
        assert "became unavailable" in result


# ═══════════════════════════════════════════════════════════════════════
# save_content_or_files
# ═══════════════════════════════════════════════════════════════════════


class TestSaveContentOrFiles:
    @pytest.mark.asyncio
    async def test_save_content(self, tmp_path):
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        with patch.object(plugin, "get_memfiles_dir", return_value=mem_dir), _active_tunnel():
            result = await plugin.save_content_or_files(
                content="# Hello\n\nWorld.", title="note", tags=["demo"],
            )
        md = list(mem_dir.glob("*.md"))
        assert len(md) == 1
        assert "# Hello" in md[0].read_text(encoding="utf-8")
        assert "Saved:" in result
        assert "URL:" in result

    @pytest.mark.asyncio
    async def test_save_content_auto_title(self, tmp_path):
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        with patch.object(plugin, "get_memfiles_dir", return_value=mem_dir), _active_tunnel():
            result = await plugin.save_content_or_files(
                content="# Welcome To My Notes\n\nBody.",
            )
        md = list(mem_dir.glob("*.md"))
        assert len(md) == 1
        assert md[0].name == "welcome-to-my-notes.md"
        assert "Saved:" in result

    @pytest.mark.asyncio
    async def test_save_path_copies(self, tmp_path):
        src = tmp_path / "original.txt"
        src.write_text("original content")
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        with patch.object(plugin, "get_memfiles_dir", return_value=mem_dir), _active_tunnel():
            result = await plugin.save_content_or_files(path=str(src))
        assert src.exists()
        copied = [f for f in mem_dir.iterdir() if f.name != "index.json"]
        assert len(copied) == 1
        assert copied[0].read_text() == "original content"
        assert "Saved:" in result

    @pytest.mark.asyncio
    async def test_save_url_downloads(self, tmp_path):
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()

        def _fake_aiohttp(*a, **kw):
            class _Resp:
                status = 200
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return None
                async def read(self):
                    return b"<html>Page</html>"
            class _Sess:
                def __init__(self, *a, **kw):
                    self._resp = _Resp()
                async def __aenter__(self):
                    return self
                async def __aexit__(self, *a):
                    return None
                def get(self, *a, **kw):
                    return self._resp
            return _Sess()

        with patch.object(plugin, "get_memfiles_dir", return_value=mem_dir), _active_tunnel(), \
             patch("aiohttp.ClientSession", _fake_aiohttp):
            # Public IP literal — no DNS dependency (the SSRF guard resolves
            # the host before the (mocked) fetch).
            result = await plugin.save_content_or_files(
                url="https://8.8.8.8/slides.html",
            )
        files = [f for f in mem_dir.iterdir() if f.name != "index.json"]
        assert len(files) == 1
        assert files[0].read_bytes() == b"<html>Page</html>"
        assert "URL:" in result

    @pytest.mark.asyncio
    async def test_save_sharing_offline_still_succeeds(self, tmp_path):
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        with patch.object(plugin, "get_memfiles_dir", return_value=mem_dir), _offline_tunnel():
            result = await plugin.save_content_or_files(content="Offline note")
        assert "Saved:" in result
        assert "sharing offline" in result

    @pytest.mark.asyncio
    async def test_no_source(self):
        result = await plugin.save_content_or_files()
        assert result.startswith("Error:")
        assert "provide one of" in result

    @pytest.mark.asyncio
    async def test_empty_content(self):
        result = await plugin.save_content_or_files(content="   ")
        assert result.startswith("Error:")
        assert "content is empty" in result


# ── SSRF guard for _save_url ───────────────────────────────────────────


class TestSaveUrlPublicGuard:
    """_save_url only fetches publicly reachable http(s) URLs — loopback /
    LAN / cloud-metadata targets are refused (REVIEW §1-1)."""

    def test_allows_public_http(self):
        assert plugin._reject_non_public_url("http://8.8.8.8/x") is None
        assert plugin._reject_non_public_url("https://1.1.1.1") is None

    def test_rejects_non_http_schemes(self):
        assert plugin._reject_non_public_url("ftp://example.com/x")
        assert plugin._reject_non_public_url("file:///etc/passwd")
        assert plugin._reject_non_public_url("gopher://localhost/1")

    def test_rejects_loopback(self):
        assert plugin._reject_non_public_url("http://127.0.0.1:8080/admin")
        assert plugin._reject_non_public_url("http://[::1]/x")
        assert plugin._reject_non_public_url("http://localhost/admin")

    def test_rejects_cloud_metadata(self):
        assert plugin._reject_non_public_url(
            "http://169.254.169.254/latest/meta-data/"
        )

    def test_rejects_private_and_link_local(self):
        assert plugin._reject_non_public_url("http://10.0.0.1/")
        assert plugin._reject_non_public_url("http://192.168.1.1/")
        assert plugin._reject_non_public_url("http://172.16.0.1/")

    @pytest.mark.asyncio
    async def test_save_url_refuses_non_public(self, tmp_path):
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        with patch.object(plugin, "get_memfiles_dir", return_value=mem_dir):
            result = await plugin.save_content_or_files(
                url="http://169.254.169.254/latest/meta-data/"
            )
        assert result.startswith("Error: refusing URL")


# ═══════════════════════════════════════════════════════════════════════
# Harness-only tools
# ═══════════════════════════════════════════════════════════════════════


class TestHarnessTools:
    @pytest.mark.asyncio
    async def test_tunnel_status_active(self):
        with _active_tunnel():
            raw = await getattr(plugin, "__tunnel_status")()
        data = json.loads(raw)
        assert data["active"] is True
        assert data["url"] == "https://slife.ngrok-free.dev"

    @pytest.mark.asyncio
    async def test_tunnel_status_offline(self):
        with _offline_tunnel():
            raw = await getattr(plugin, "__tunnel_status")()
        data = json.loads(raw)
        assert data["active"] is False
        assert "hint" in data

    @pytest.mark.asyncio
    async def test_register_file(self, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        with _active_tunnel():
            raw = await getattr(plugin, "__register_file")(str(f))
        data = json.loads(raw)
        assert len(data["file_id"]) == 30
        assert data["url"] == f"https://slife.ngrok-free.dev/share/{data['file_id']}"
        assert plugin._lookup_file(data["file_id"]) == str(f.resolve())


# ═══════════════════════════════════════════════════════════════════════
# Custom HTTP route — GET /share/{file_id}
# ═══════════════════════════════════════════════════════════════════════


def _request(file_id: str):
    req = MagicMock()
    req.path_params = {"file_id": file_id}
    return req


class TestShareRoute:
    @pytest.mark.asyncio
    async def test_unknown_token_403(self):
        resp = await plugin.handle_share(_request("deadbeef"))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_file_404(self, tmp_path):
        f = tmp_path / "gone.pdf"
        f.write_bytes(b"x")
        tok = plugin._register_file(str(f))
        f.unlink()
        resp = await plugin.handle_share(_request(tok))
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_streams_file(self, tmp_path):
        payload = b"file-content-bytes"
        f = tmp_path / "data.txt"
        f.write_bytes(payload)
        tok = plugin._register_file(str(f))

        resp = await plugin.handle_share(_request(tok))
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/plain")
        assert "ngrok-skip-browser-warning" in resp.headers

        body = b"".join([c async for c in resp.body_iterator])
        assert body == payload

    @pytest.mark.asyncio
    async def test_sets_content_length(self, tmp_path):
        payload = b"1234567890"
        f = tmp_path / "ten.bin"
        f.write_bytes(payload)
        tok = plugin._register_file(str(f))

        resp = await plugin.handle_share(_request(tok))
        assert resp.headers["Content-Length"] == "10"

    @pytest.mark.asyncio
    async def test_non_ascii_filename_no_500(self, tmp_path):
        """A CJK filename must not blow up the Content-Disposition header.

        Regression: HTTP headers are Latin-1 — a non-ASCII filename used to
        raise UnicodeEncodeError in Starlette's init_headers (HTTP 500).
        """
        payload = b"\xe4\xb8\xad\xe6\x96\x87"  # some bytes
        f = tmp_path / "报告.pdf"      # 报告.pdf
        f.write_bytes(payload)
        tok = plugin._register_file(str(f))

        resp = await plugin.handle_share(_request(tok))
        assert resp.status_code == 200
        cd = resp.headers["Content-Disposition"]
        # RFC 5987 percent-encoded form carries the real name
        assert "filename*=UTF-8''" in cd
        assert quote("报告.pdf") in cd
        body = b"".join([c async for c in resp.body_iterator])
        assert body == payload

    def test_content_disposition_ascii(self):
        assert plugin._content_disposition("photo.png") == (
            'inline; filename="photo.png"'
        )

    def test_content_disposition_non_ascii(self):
        cd = plugin._content_disposition("报告.pdf")
        assert 'filename="' in cd
        assert "filename*=UTF-8''" in cd
