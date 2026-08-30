"""Tests for the sharefile plugin — public file sharing.

These tests moved out of test_memfiles_plugin.py when the sharing
functionality (token registry, ``share_file``, the ngrok tunnel, the custom
``GET /share/{file_id}`` HTTP route) was extracted into the standalone
``sharefile`` plugin.  The memfiles plugin is now cabinet-only and covered
by test_memfiles_plugin.py.

Mocks the ngrok tunnel (no network) and exercises the MCP tool functions
directly, following the test_mqtt_plugin.py pattern.  Covers the token
registry, ``share_file`` (with a mocked tunnel), the internal tools
(``__check`` / ``__register_file``), and the ``GET /share/{file_id}``
HTTP route including the SSRF-adjacent filename encoding (RFC 5987).
The ngrok tunnel lifecycle itself is covered in test_sharefile_tunnel.py.
"""

import pytest; pytestmark = pytest.mark.unit


import json
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import slife.plugins.sharefile.server as plugin


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset registry + plugin port each test."""
    plugin._reset_registry()
    plugin._PLUGIN_PORT = 12345
    yield
    plugin._reset_registry()


def _active_tunnel(url="https://slife.ngrok-free.dev"):
    """Patch the plugin's tunnel instance so it reports active."""
    tunnel = MagicMock()
    tunnel.is_active = True
    tunnel.share_url_for.side_effect = lambda fid: f"{url}/share/{fid}"
    tunnel.status.return_value = {"state": "active", "url": url}
    return patch.multiple(plugin, _tunnel=tunnel)


def _offline_tunnel(state="failed"):
    """Patch the plugin's tunnel instance so it reports inactive.

    Also clear ``_PLUGIN_PORT`` so ``_ensure_tunnel`` short-circuits without
    a real ngrok start attempt — without this the offline test spent ~9s
    trying to reach the actual ngrok service.
    """
    tunnel = MagicMock()
    tunnel.is_active = False
    tunnel.share_url_for.return_value = None
    tunnel.status.return_value = {"state": state, "url": ""}
    return patch.multiple(plugin, _tunnel=tunnel, _PLUGIN_PORT=0)


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
# share_file
# ═══════════════════════════════════════════════════════════════════════


class TestShareFile:
    @pytest.mark.asyncio
    async def test_active_tunnel_returns_url(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"pngdata")
        with _active_tunnel():
            result = await plugin.share_file(path=str(f))
        assert "Public URL for photo.png" in result
        assert "https://slife.ngrok-free.dev/share/" in result

    @pytest.mark.asyncio
    async def test_offline_tunnel_returns_error(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"pngdata")
        with _offline_tunnel():
            result = await plugin.share_file(path=str(f))
        assert result.startswith("Error:")
        assert "file sharing service is not available" in result

    @pytest.mark.asyncio
    async def test_missing_file(self):
        with _active_tunnel():
            result = await plugin.share_file(path="D:\\nonexistent\\x.png")
        assert result.startswith("Error:")
        assert "file not found" in result

    @pytest.mark.asyncio
    async def test_directory(self, tmp_path):
        with _active_tunnel():
            result = await plugin.share_file(path=str(tmp_path))
        assert result.startswith("Error:")
        assert "not a file" in result

    @pytest.mark.asyncio
    async def test_url_drops_after_register(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"pdf")
        tunnel = MagicMock()
        tunnel.is_active = True
        tunnel.share_url_for.return_value = None
        with patch.multiple(plugin, _tunnel=tunnel):
            result = await plugin.share_file(path=str(f))
        assert result.startswith("Error:")
        assert "became unavailable" in result


# ═══════════════════════════════════════════════════════════════════════
# Internal tools
# ═══════════════════════════════════════════════════════════════════════


class TestInternalTools:
    @pytest.mark.asyncio
    async def test_tunnel_status_active(self):
        with _active_tunnel():
            raw = await getattr(plugin, "__check")()
        data = json.loads(raw)
        assert data["active"] is True
        assert data["state"] == "active"
        assert data["url"] == "https://slife.ngrok-free.dev"

    @pytest.mark.asyncio
    async def test_tunnel_status_failed(self):
        with _offline_tunnel(state="failed"):
            raw = await getattr(plugin, "__check")()
        data = json.loads(raw)
        assert data["active"] is False
        assert data["state"] == "failed"
        assert "hint" in data

    @pytest.mark.asyncio
    async def test_tunnel_status_starting(self):
        """A start attempt still in flight is reported as 'starting', so the
        harness waits rather than misreading it as tunnel down."""
        with _offline_tunnel(state="starting"):
            raw = await getattr(plugin, "__check")()
        data = json.loads(raw)
        assert data["active"] is False
        assert data["state"] == "starting"

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
