"""Tests for slife.mcp.oauth — device code flow and token management."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import json
import time as _time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from slife.mcp.oauth import (
    OAuthTokens,
    get_valid_token,
    run_device_code_flow,
    refresh_access_token,
    _store_tokens,
    _credstore_key,
    _serialize,
    _deserialize,
)


# ── Helpers ───────────────────────────────────────────────────────────

AUTH = {
    "type": "oauth",
    "client_id": "test-client-id",
    "client_secret": "test-client-secret",
    "device_auth_url": "https://example.com/device",
    "token_url": "https://example.com/token",
    "scopes": ["read", "write"],
}

AUTH_NO_SECRET = {
    "type": "oauth",
    "client_id": "test-client-id",
    "device_auth_url": "https://example.com/device",
    "token_url": "https://example.com/token",
    "scopes": [],
}


def make_tokens(expires_in: int = 3600) -> OAuthTokens:
    return OAuthTokens(
        access_token="gh_token_abc123",
        refresh_token="gh_refresh_xyz789",
        expires_at=_time.time() + expires_in,
        token_type="Bearer",
    )


# ── OAuthTokens serialization ─────────────────────────────────────────


class TestOAuthTokensSerialize:
    def test_roundtrip(self):
        tokens = make_tokens()
        raw = _serialize(tokens)
        parsed = _deserialize(raw)
        assert parsed is not None
        assert parsed.access_token == tokens.access_token
        assert parsed.refresh_token == tokens.refresh_token
        assert parsed.token_type == "Bearer"

    def test_deserialize_empty(self):
        assert _deserialize("") is None

    def test_deserialize_bad_json(self):
        assert _deserialize("{not json}") is None

    def test_deserialize_partial(self):
        raw = json.dumps({"access_token": "tok"})
        parsed = _deserialize(raw)
        assert parsed is not None
        assert parsed.access_token == "tok"
        assert parsed.refresh_token == ""


# ── Credstore persistence ─────────────────────────────────────────────


class TestStoreTokens:
    def test_store_and_retrieve(self):
        tokens = make_tokens()
        with patch("credstore.set_credential") as mock_set:
            _store_tokens("test-server", tokens)
            mock_set.assert_called_once()
            key = mock_set.call_args[0][0]
            assert _credstore_key("test-server") in key

    def test_get_valid_token_returns_none_when_not_stored(self):
        with patch("credstore.get_credential", return_value=None):
            result = get_valid_token("no-such-server")
            assert result is None

    def test_get_valid_token_returns_tokens_when_valid(self):
        tokens = make_tokens(expires_in=3600)
        raw = _serialize(tokens)
        with patch("credstore.get_credential", return_value=raw):
            result = get_valid_token("test-server")
            assert result is not None
            assert result.access_token == tokens.access_token

    def test_get_valid_token_expired(self):
        tokens = make_tokens(expires_in=30)  # expires in 30s (< 60s buffer)
        raw = _serialize(tokens)
        with patch("credstore.get_credential", return_value=raw):
            result = get_valid_token("test-server")
            assert result is None


# ── Device code flow ──────────────────────────────────────────────────


def _make_http_mock(*responses: dict | Exception):
    """Build a mock httpx.AsyncClient that returns *responses* from post().

    Each response is either a dict (→ ``resp.json()`` returns it) or an
    Exception (→ ``resp.raise_for_status()`` raises it).
    """
    mock_http = MagicMock()
    mock_http.__aenter__ = AsyncMock(return_value=mock_http)
    mock_http.__aexit__ = AsyncMock(return_value=None)

    call_count = 0
    responses_list = list(responses)

    async def _post(url, **kwargs):
        nonlocal call_count
        if call_count >= len(responses_list):
            raise RuntimeError(f"exhausted responses at index {call_count}")
        item = responses_list[call_count]
        call_count += 1
        if isinstance(item, Exception):
            raise item
        resp = MagicMock()
        resp.json.return_value = item
        resp.raise_for_status = MagicMock()
        return resp

    mock_http.post = _post
    return mock_http


class TestDeviceCodeFlow:

    @pytest.mark.asyncio
    async def test_successful_flow(self):
        """Full device code flow: request → poll → receive token."""
        device_data = {
            "device_code": "dc_123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://example.com/device",
            "expires_in": 300,
            "interval": 1,
        }
        token_data = {
            "access_token": "gh_token_abc",
            "refresh_token": "gh_refresh_xyz",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_http = _make_http_mock(device_data, token_data)

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("slife.mcp.oauth._store_tokens") as mock_store, \
             patch("slife.mcp.oauth.asyncio.sleep", AsyncMock()):
            result = await run_device_code_flow(AUTH, "test-server")

        assert result.access_token == "gh_token_abc"
        assert result.refresh_token == "gh_refresh_xyz"
        mock_store.assert_called_once()

    def test_emit_user_message_uses_stderr_with_marker(self, capsys):
        """REVIEW H7: instructions go to stderr (stdout is closed in the
        gateway child), prefixed so the parent can surface them."""
        from slife.mcp.oauth import (
            _emit_user_message, _OAUTH_MARKER, _OAUTH_ACTION_MARKER,
        )

        _emit_user_message("line one\nline two")
        _emit_user_message("Open https://x enter code: ABC-123", marker=_OAUTH_ACTION_MARKER)

        captured = capsys.readouterr()
        assert captured.out == ""  # nothing on stdout — the closed channel
        assert f"{_OAUTH_MARKER} line one" in captured.err
        assert f"{_OAUTH_MARKER} line two" in captured.err
        assert f"{_OAUTH_ACTION_MARKER} Open https://x enter code: ABC-123" in captured.err

    @pytest.mark.asyncio
    async def test_flow_completes_with_closed_stdout(self, monkeypatch):
        """REVIEW H7: with stdout closed (gateway child), the flow no longer
        raises 'I/O operation on closed file' — it completes via stderr."""
        class _ClosedStdout:
            """Behaves like the gateway child's closed stdout."""
            def write(self, *_a):
                raise ValueError("I/O operation on closed file")
            def flush(self):
                pass

        monkeypatch.setattr("sys.stdout", _ClosedStdout())

        device_data = {
            "device_code": "dc_123",
            "user_code": "ABCD-1234",
            "verification_uri": "https://example.com/device",
            "expires_in": 300,
            "interval": 1,
        }
        token_data = {
            "access_token": "gh_token_abc",
            "refresh_token": "gh_refresh_xyz",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_http = _make_http_mock(device_data, token_data)

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("slife.mcp.oauth._store_tokens") as mock_store, \
             patch("slife.mcp.oauth.asyncio.sleep", AsyncMock()):
            result = await run_device_code_flow(AUTH, "test-server")

        assert result.access_token == "gh_token_abc"
        mock_store.assert_called_once()

    def test_oauth_markers_match_wrapper_detection(self):
        """The gateway emits and the wrapper detects the SAME markers."""
        from slife.mcp import oauth
        from slife.mcp import process
        assert oauth._OAUTH_MARKER == process._OAUTH_MARKER
        assert oauth._OAUTH_ACTION_MARKER == process._OAUTH_ACTION_MARKER

    @pytest.mark.asyncio
    async def test_incomplete_auth_config(self):
        with pytest.raises(ValueError, match="incomplete"):
            await run_device_code_flow(
                {"type": "oauth", "client_id": ""}, "bad-server"
            )

    @pytest.mark.asyncio
    async def test_poll_until_success(self):
        """Device code flow where the first poll gets authorization_pending."""
        device_data = {
            "device_code": "dc_456",
            "user_code": "WXYZ-9999",
            "verification_uri": "https://example.com/device",
            "expires_in": 300,
            "interval": 1,
        }
        pending = {"error": "authorization_pending"}
        token_data = {
            "access_token": "tok_after_pending",
            "refresh_token": "ref_after_pending",
            "expires_in": 3600,
        }
        mock_http = _make_http_mock(device_data, pending, token_data)

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("slife.mcp.oauth._store_tokens") as mock_store, \
             patch("slife.mcp.oauth.asyncio.sleep", AsyncMock()):
            result = await run_device_code_flow(AUTH, "test-server")

        assert result.access_token == "tok_after_pending"

    @pytest.mark.asyncio
    async def test_expired_device_code(self):
        """Device code expired during polling."""
        device_data = {
            "device_code": "dc_expired",
            "user_code": "DEAD-BEEF",
            "verification_uri": "https://example.com/device",
            "expires_in": 300,
            "interval": 1,
        }
        expired = {"error": "expired_token"}
        mock_http = _make_http_mock(device_data, expired)

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("slife.mcp.oauth._delete_tokens") as mock_delete, \
             patch("slife.mcp.oauth.asyncio.sleep", AsyncMock()):
            with pytest.raises(RuntimeError, match="expired"):
                await run_device_code_flow(AUTH, "test-server")

    @pytest.mark.asyncio
    async def test_poll_aborts_on_access_denied(self):
        """REVIEW S6: RFC 8628 terminal errors abort immediately, not at timeout."""
        device_data = {
            "device_code": "dc_deny",
            "user_code": "DENY-9999",
            "verification_uri": "https://example.com/device",
            "expires_in": 300,
            "interval": 1,
        }
        denied = {"error": "access_denied", "error_description": "user said no"}
        mock_http = _make_http_mock(device_data, denied)

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("slife.mcp.oauth.asyncio.sleep", AsyncMock()):
            with pytest.raises(RuntimeError, match="access_denied"):
                await run_device_code_flow(AUTH, "test-server")


# ── Token refresh ─────────────────────────────────────────────────────


class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_successful_refresh(self):
        stored = make_tokens(expires_in=1)  # expired
        raw = _serialize(stored)

        mock_http = MagicMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=None)
        refresh_resp = MagicMock()
        refresh_resp.json.return_value = {
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        mock_http.post = AsyncMock(return_value=refresh_resp)

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("credstore.get_credential", return_value=raw), \
             patch("slife.mcp.oauth._store_tokens") as mock_store:
            result = await refresh_access_token(AUTH, "test-server")

        assert result.access_token == "new_access"
        assert result.refresh_token == "new_refresh"
        mock_store.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_stored_tokens(self):
        with patch("credstore.get_credential", return_value=None):
            with pytest.raises(RuntimeError, match="No stored tokens"):
                await refresh_access_token(AUTH, "test-server")

    @pytest.mark.asyncio
    async def test_no_refresh_token(self):
        tokens = OAuthTokens(access_token="tok", refresh_token="")
        raw = _serialize(tokens)
        with patch("credstore.get_credential", return_value=raw):
            with pytest.raises(RuntimeError, match="No refresh token"):
                await refresh_access_token(AUTH, "test-server")

    @pytest.mark.asyncio
    async def test_transport_error_keeps_stored_tokens(self):
        """REVIEW S6: a transient network failure must NOT delete the token."""
        raw = _serialize(make_tokens(expires_in=1))

        mock_http = MagicMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=None)
        mock_http.post = AsyncMock(side_effect=httpx.ConnectError("connection reset"))

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("credstore.get_credential", return_value=raw), \
             patch("slife.mcp.oauth._delete_tokens") as mock_delete:
            with pytest.raises(RuntimeError, match="refresh failed"):
                await refresh_access_token(AUTH, "test-server")

        mock_delete.assert_not_called()

    @pytest.mark.asyncio
    async def test_invalid_grant_deletes_tokens(self):
        """REVIEW S6: a 400 (invalid_grant) invalidates the stored token."""
        raw = _serialize(make_tokens(expires_in=1))

        mock_http = MagicMock()
        mock_http.__aenter__ = AsyncMock(return_value=mock_http)
        mock_http.__aexit__ = AsyncMock(return_value=None)
        mock_http.post = AsyncMock(return_value=httpx.Response(
            400, request=httpx.Request("POST", "https://token.example.com"),
        ))

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("credstore.get_credential", return_value=raw), \
             patch("slife.mcp.oauth._delete_tokens") as mock_delete:
            with pytest.raises(RuntimeError, match="refresh failed"):
                await refresh_access_token(AUTH, "test-server")

        mock_delete.assert_called_once()


# ── slow_down handling ────────────────────────────────────────────────


class TestSlowDown:
    @pytest.mark.asyncio
    async def test_slow_down_increases_interval(self):
        """slow_down error increases poll interval, then succeeds."""
        device_data = {
            "device_code": "dc_slow",
            "user_code": "SLOW-DOWN",
            "verification_uri": "https://example.com/device",
            "expires_in": 300,
            "interval": 1,
        }
        slow_down = {"error": "slow_down"}
        token_data = {
            "access_token": "tok_slow",
            "expires_in": 3600,
        }
        mock_http = _make_http_mock(device_data, slow_down, token_data)

        with patch("slife.mcp.oauth.httpx.AsyncClient", return_value=mock_http), \
             patch("slife.mcp.oauth._store_tokens"), \
             patch("slife.mcp.oauth.asyncio.sleep", AsyncMock()):
            result = await run_device_code_flow(AUTH, "test-server")

        assert result.access_token == "tok_slow"
