"""Tests for the sharefile plugin — public file sharing.

These tests moved out of test_memfiles_plugin.py when the sharing
functionality (token registry, ``share_file``, the ngrok tunnel, the custom
``GET /share/{file_id}`` HTTP route) was extracted into the standalone
``sharefile`` plugin.  The memfiles plugin is now cabinet-only and covered
by test_memfiles_plugin.py.

Mocks the ngrok tunnel (no network) and exercises the MCP tool functions
directly, following the test_mqtt_plugin.py pattern.  Covers the token
registry, ``share_file`` (with a mocked tunnel), the internal tool
(``__check``), and the ``GET /share/{file_id}``
HTTP route including the SSRF-adjacent filename encoding (RFC 5987).
The ngrok tunnel lifecycle itself is covered in test_sharefile_tunnel.py.
"""

import pytest; pytestmark = pytest.mark.unit


import json
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import slife.plugins.sharefile.server as plugin


# ── Fixtures ────────────────────────────────────────────────────────────


def _clear_registry() -> None:
    """Empty the share-token registry between tests.

    The registry is module-global (a token IS the share link), so a token
    minted by one test would otherwise still resolve in the next.
    """
    plugin._registry.clear()
    plugin._path_to_token.clear()


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset registry + plugin port each test."""
    _clear_registry()
    plugin._PLUGIN_PORT = 12345
    yield
    _clear_registry()


def _active_tunnel(url="https://slife.ngrok-free.dev"):
    """Patch the plugin's tunnel instance so it reports active and reachable."""
    tunnel = MagicMock()
    tunnel.is_active = True
    tunnel.is_reachable.return_value = True
    tunnel.share_url_for.side_effect = lambda fid: f"{url}/share/{fid}"
    tunnel.status.return_value = {"state": "active", "url": url}
    return patch.multiple(plugin, _tunnel=tunnel)


def _unreachable_tunnel(url="https://slife.ngrok-free.dev"):
    """A tunnel whose URL is published but which the edge cannot serve — the
    state that answers every request with HTTP 530."""
    tunnel = MagicMock()
    tunnel.is_active = True          # the URL exists…
    tunnel.is_reachable.return_value = False   # …and nothing is behind it
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
    tunnel.is_reachable.return_value = False
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
        assert plugin._lookup_entry(tok)["path"] == "/data/report.pdf"

    def test_dedup_same_path(self):
        t1 = plugin._register_file("/tmp/a.txt")
        t2 = plugin._register_file("/tmp/a.txt")
        assert t1 == t2

    def test_distinct_paths(self):
        t1 = plugin._register_file("/a.txt")
        t2 = plugin._register_file("/b.txt")
        assert t1 != t2

    def test_unknown_token(self):
        assert plugin._lookup_entry("deadbeef") is None


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
        tunnel.is_reachable.return_value = True
        tunnel.share_url_for.return_value = None
        with patch.multiple(plugin, _tunnel=tunnel):
            result = await plugin.share_file(path=str(f))
        assert result.startswith("Error:")
        assert "became unavailable" in result

    @pytest.mark.asyncio
    async def test_unreachable_tunnel_refuses_instead_of_handing_out_a_link(
        self, tmp_path,
    ):
        """A published URL is not a working one: a transport that lost the edge
        answers every request to it with HTTP 530, and the caller only finds
        out later — from an LLM that could not fetch the file.  Refusing here
        is the whole point of asking the edge rather than the URL."""
        f = tmp_path / "photo.png"
        f.write_bytes(b"pngdata")
        with _unreachable_tunnel():
            result = await plugin.share_file(path=str(f))
        assert result.startswith("Error:")
        assert "530" in result


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
        assert data["reachable"] is True
        assert data["state"] == "active"
        assert data["url"] == "https://slife.ngrok-free.dev"

    @pytest.mark.asyncio
    async def test_tunnel_status_reachable_is_independent_of_active(self):
        """'A URL exists' and 'that URL would be served' are different facts —
        the harness reads them separately or it reads an outage as healthy."""
        with _unreachable_tunnel():
            raw = await getattr(plugin, "__check")()
        data = json.loads(raw)
        assert data["active"] is True       # the URL is published…
        assert data["reachable"] is False   # …and the edge is not serving it

    @pytest.mark.asyncio
    async def test_tunnel_status_failed(self):
        with _offline_tunnel(state="failed"):
            raw = await getattr(plugin, "__check")()
        data = json.loads(raw)
        assert data["active"] is False
        assert data["state"] == "failed"
        assert "reason" in data

    @pytest.mark.asyncio
    async def test_tunnel_status_starting(self):
        """A start attempt still in flight is reported as 'starting', so the
        harness waits rather than misreading it as tunnel down."""
        with _offline_tunnel(state="starting"):
            raw = await getattr(plugin, "__check")()
        data = json.loads(raw)
        assert data["active"] is False
        assert data["state"] == "starting"


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
        # The share route is provider-agnostic — it carries no tunnel-specific
        # headers (an ngrok bypass header here would be dead weight: ngrok's
        # edge decides before the request ever reaches this handler).
        assert not [h for h in resp.headers if "ngrok" in h.lower()]

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


class TestShareSecurity:
    """D4: a share is pinned to the exact file registered — a replaced path is
    refused, credentials are never published, and links can be revoked."""

    @pytest.mark.asyncio
    async def test_replaced_file_refused(self, tmp_path):
        """A path replaced after registration (different bytes/size) must not
        serve the new content to holders of the old token."""
        f = tmp_path / "data.txt"
        f.write_bytes(b"original-content")
        tok = plugin._register_file(str(f))
        f.write_bytes(b"replaced with different content !!!")
        resp = await plugin.handle_share(_request(tok))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_refuses_private_credential(self, tmp_path):
        f = tmp_path / "id_ed25519"
        f.write_bytes(b"private key material")
        result = await plugin.share_file(path=str(f))
        assert result.startswith("Error")
        assert "credential" in result

    @pytest.mark.asyncio
    async def test_refuses_dotenv(self, tmp_path):
        f = tmp_path / ".env"
        f.write_bytes(b"SECRET=abc")
        result = await plugin.share_file(path=str(f))
        assert result.startswith("Error")
        assert "credential" in result

    @pytest.mark.asyncio
    async def test_unshare_revokes_link(self, tmp_path):
        f = tmp_path / "ok.txt"
        f.write_bytes(b"hi")
        tok = plugin._register_file(str(f))
        assert (await plugin.handle_share(_request(tok))).status_code == 200

        out = await plugin.sharefile_unshare(file_id=tok)
        assert "[OK]" in out
        assert plugin._lookup_entry(tok) is None
        assert (await plugin.handle_share(_request(tok))).status_code == 403

    @pytest.mark.asyncio
    async def test_unshare_unknown_is_error(self):
        out = await plugin.sharefile_unshare(file_id="deadbeef")
        assert out.startswith("Error")
