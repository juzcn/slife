"""OAuth 2.0 Device Code Flow for MCP server authentication.

Provides the device-code authorization flow so MCP servers that require
delegated access (GitHub, Google APIs, etc.) can obtain bearer tokens.
Tokens are stored in the OS keyring via credstore and refreshed
transparently before each connection.

Usage::

    from slife.mcp.oauth import get_valid_token, run_device_code_flow, OAuthTokens

    tokens = get_valid_token("my-server")
    if tokens is None:
        tokens = await run_device_code_flow(auth_config, "my-server")
    # tokens.access_token → inject into headers
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time as _time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# ── User-facing output channel ─────────────────────────────────────────
# The device flow runs inside the slife-mcp gateway child, whose stdout is
# CLOSED right after the port signal (server_utils.signal_port) — printing
# there raised "ValueError: I/O operation on closed file" (REVIEW H7).
# All user instructions go to stderr instead, prefixed so the parent's
# MCPWrapperProcess._log_stderr can surface them.
_OAUTH_MARKER = "[OAUTH]"
_OAUTH_ACTION_MARKER = "[OAUTH-ACTION]"


def _emit_user_message(text: str, marker: str = _OAUTH_MARKER) -> None:
    """Write *text* to stderr, one marker-prefixed line per line.

    ``[OAUTH]`` lines are relayed at WARNING by the parent; the single
    ``[OAUTH-ACTION]`` line (URL + code) fires a desktop notification.
    """
    for line in text.splitlines():
        print(f"{marker} {line}", file=sys.stderr, flush=True)

# credstore key prefix for OAuth tokens
_TOKEN_KEY_PREFIX = "mcp_oauth_"

# Polling config
_POLL_INTERVAL = 5.0  # seconds between token endpoint polls
_POLL_TIMEOUT = 300.0  # 5 minutes total before giving up


@dataclass
class OAuthTokens:
    """OAuth 2.0 token bundle returned by the device code flow."""

    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0  # Unix timestamp, 0 = unknown
    token_type: str = "Bearer"


def _credstore_key(server_name: str) -> str:
    """Return the credstore key for a server's OAuth tokens."""
    return f"{_TOKEN_KEY_PREFIX}{server_name}"


def _serialize(t: OAuthTokens) -> str:
    """Serialize tokens to JSON for credstore storage."""
    return json.dumps({
        "access_token": t.access_token,
        "refresh_token": t.refresh_token,
        "expires_at": t.expires_at,
        "token_type": t.token_type,
    }, ensure_ascii=False)


def _deserialize(raw: str) -> OAuthTokens | None:
    """Parse tokens from a JSON string stored in credstore."""
    try:
        data = json.loads(raw)
        return OAuthTokens(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            expires_at=float(data.get("expires_at", 0)),
            token_type=data.get("token_type", "Bearer"),
        )
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def get_valid_token(server_name: str) -> OAuthTokens | None:
    """Return a valid (non-expired) OAuth token for *server_name*, or None.

    Checks the OS keyring via credstore.  A token is valid if its
    ``expires_at`` is at least 60 seconds in the future.  If the
    stored data is malformed, it is treated as expired (returns None).
    """
    try:
        from credstore import get_credential
    except ImportError:
        logger.warning("oauth_credstore_unavailable server=%s", server_name)
        return None

    raw = get_credential(_credstore_key(server_name))
    if not raw:
        return None

    tokens = _deserialize(raw)
    if tokens is None:
        return None

    # Consider tokens expiring within 60s as expired.  expires_at <= 0 means
    # the expiry is unknown/missing — treat it as expired, not valid forever.
    if tokens.expires_at <= 0 or _time.time() + 60 >= tokens.expires_at:
        logger.debug("oauth_token_expired server=%s", server_name)
        return None

    if not tokens.access_token:
        return None

    logger.debug("oauth_token_valid server=%s", server_name)
    return tokens


def _store_tokens(server_name: str, tokens: OAuthTokens) -> None:
    """Persist tokens to the OS keyring via credstore."""
    try:
        from credstore import set_credential
    except ImportError:
        logger.warning("oauth_credstore_unavailable server=%s", server_name)
        return

    raw = _serialize(tokens)
    set_credential(_credstore_key(server_name), raw)
    logger.info("oauth_tokens_stored server=%s", server_name)


def _delete_tokens(server_name: str) -> None:
    """Remove stored tokens (best-effort)."""
    try:
        from credstore import delete_credential
    except ImportError:
        return
    try:
        delete_credential(_credstore_key(server_name))
    except Exception:
        pass


async def run_device_code_flow(auth: dict, server_name: str) -> OAuthTokens:
    """Run the OAuth 2.0 Device Code authorization flow.

    Args:
        auth: Auth configuration dict with keys:
            - ``device_auth_url``: URL to request the device code
            - ``token_url``: URL to poll for the access token
            - ``client_id``: OAuth client identifier
            - ``client_secret``: OAuth client secret (optional)
            - ``scopes``: list of scope strings (optional)
        server_name: MCP server name (used for logging and token storage).

    Returns:
        OAuthTokens on success.

    Raises:
        ConnectionError: If the device auth endpoint is unreachable.
        RuntimeError: If the user did not authorize within the timeout.
        ValueError: If the auth config is incomplete.
    """
    client_id = auth.get("client_id", "")
    client_secret = auth.get("client_secret", "")
    device_auth_url = auth.get("device_auth_url", "")
    token_url = auth.get("token_url", "")
    scopes = auth.get("scopes", [])

    if not client_id or not device_auth_url or not token_url:
        raise ValueError(
            f"OAuth config for '{server_name}' is incomplete. "
            f"Required: client_id, device_auth_url, token_url."
        )

    scope_str = " ".join(scopes) if scopes else ""

    # ── Step 1: Request device code ─────────────────────────────────
    logger.info("oauth_device_request server=%s", server_name)
    body: dict = {
        "client_id": client_id,
        "scope": scope_str,
    }
    if client_secret:
        body["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        try:
            resp = await http.post(
                device_auth_url,
                data=body,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            device_data = resp.json()
        except httpx.HTTPError as e:
            raise ConnectionError(
                f"Failed to request device code from {device_auth_url}: {e}"
            ) from e

    device_code = device_data.get("device_code", "")
    user_code = device_data.get("user_code", "")
    verification_uri = device_data.get("verification_uri", "")
    expires_in = int(device_data.get("expires_in", 300))
    try:
        poll_interval = float(device_data.get("interval", _POLL_INTERVAL))
    except (TypeError, ValueError):
        poll_interval = _POLL_INTERVAL
    # A server returning interval=0 (or negative) must not produce a tight
    # asyncio.sleep(0) loop hammering the token endpoint.
    poll_interval = max(poll_interval, 1.0)

    if not device_code:
        raise RuntimeError(
            f"Device auth response missing device_code: {device_data}"
        )

    # ── Step 2: Show user instructions ──────────────────────────────
    msg_parts = [
        "",
        "═" * 55,
        f"  🔐 OAuth authorization required for [bold]{server_name}[/bold]",
        "",
        f"  1. Open: [underline]{verification_uri}[/underline]",
        f"  2. Enter code: [bold reverse]{user_code}[/bold]",
        "",
        f"  Waiting for authorization (timeout: {expires_in}s)…",
        "═" * 55,
        "",
    ]
    _emit_user_message("\n".join(msg_parts))
    # One compact action line so the parent can fire a desktop
    # notification with the URL + code.
    _emit_user_message(
        f"Open {verification_uri} and enter code: {user_code} "
        f"(authorizes '{server_name}', expires in {expires_in}s)",
        marker=_OAUTH_ACTION_MARKER,
    )

    # ── Step 3: Poll for token ──────────────────────────────────────
    poll_body: dict = {
        "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        "device_code": device_code,
        "client_id": client_id,
    }
    if client_secret:
        poll_body["client_secret"] = client_secret

    deadline = _time.monotonic() + min(expires_in, _POLL_TIMEOUT)

    while _time.monotonic() < deadline:
        await asyncio.sleep(poll_interval)

        try:
            resp = await http.post(
                token_url,
                data=poll_body,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            token_data = resp.json()
        except httpx.HTTPError as e:
            logger.warning("oauth_poll_error server=%s err=%s", server_name, e)
            continue

        if "access_token" in token_data:
            tokens = OAuthTokens(
                access_token=token_data["access_token"],
                refresh_token=token_data.get("refresh_token", ""),
                expires_at=_time.time() + int(token_data.get("expires_in", 3600)),
                token_type=token_data.get("token_type", "Bearer"),
            )
            _store_tokens(server_name, tokens)
            _emit_user_message(f"  ✓ Authorized! Token stored for {server_name}.")
            logger.info("oauth_authorized server=%s", server_name)
            return tokens

        error = token_data.get("error", "")
        if error == "authorization_pending":
            continue  # user hasn't approved yet — keep polling
        elif error == "slow_down":
            poll_interval += 1.0  # server asked us to slow down
            continue
        elif error == "expired_token":
            _delete_tokens(server_name)
            raise RuntimeError(
                f"Device code expired for {server_name}. "
                f"Please restart the authorization."
            )
        elif error in (
            "access_denied", "invalid_client", "invalid_grant",
            "unsupported_grant_type",
        ):
            # RFC 8628 §3.5 terminal errors — the user denied, or the grant
            # is dead.  Abort immediately instead of polling to the deadline
            # (REVIEW S6).  invalid_grant/invalid_client also invalidate the
            # stored tokens.
            if error in ("invalid_grant", "invalid_client"):
                _delete_tokens(server_name)
            raise RuntimeError(
                f"OAuth authorization failed for {server_name}: {error} "
                f"({token_data.get('error_description', '')}). "
                f"Please try again."
            )
        elif error:
            logger.warning(
                "oauth_poll_error server=%s error=%s desc=%s",
                server_name, error, token_data.get("error_description", ""),
            )
            continue

    _delete_tokens(server_name)
    raise RuntimeError(
        f"Authorization timed out for {server_name}. "
        f"Please try again."
    )


async def refresh_access_token(auth: dict, server_name: str) -> OAuthTokens:
    """Refresh an expired access token using the refresh_token.

    Reads the stored refresh token from credstore, exchanges it for
    a new access token, and persists the updated tokens.

    Raises:
        RuntimeError: If no refresh token is available or the refresh fails.
    """
    token_url = auth.get("token_url", "")
    client_id = auth.get("client_id", "")
    client_secret = auth.get("client_secret", "")

    if not token_url:
        raise ValueError(
            f"OAuth config for '{server_name}' missing token_url for refresh."
        )

    existing = get_valid_token(server_name)
    # Even if expired, try to load the stored data for the refresh_token
    try:
        from credstore import get_credential
    except ImportError:
        raise RuntimeError("credstore unavailable — cannot refresh OAuth token.")

    raw = get_credential(_credstore_key(server_name))
    if not raw:
        raise RuntimeError(
            f"No stored tokens for '{server_name}'. Re-run device code flow."
        )

    stored = _deserialize(raw)
    if stored is None or not stored.refresh_token:
        raise RuntimeError(
            f"No refresh token available for '{server_name}'. "
            f"Re-run device code flow."
        )

    logger.info("oauth_refresh server=%s", server_name)

    body: dict = {
        "grant_type": "refresh_token",
        "refresh_token": stored.refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        body["client_secret"] = client_secret

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as http:
        try:
            resp = await http.post(
                token_url,
                data=body,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as e:
            # A 4xx from the token endpoint usually means the grant is dead
            # (invalid_grant / invalid_client) — only then destroy the stored
            # refresh token so the user must re-authorize.
            if e.response.status_code in (400, 401, 403):
                _delete_tokens(server_name)
            raise RuntimeError(
                f"Token refresh failed for '{server_name}': {e}. "
                f"Re-run device code flow."
            ) from e
        except httpx.HTTPError as e:
            # Transient transport failure (connection reset, timeout) — keep
            # the stored refresh token so the next refresh can succeed (REVIEW S6).
            raise RuntimeError(
                f"Token refresh failed for '{server_name}': {e}."
            ) from e

    tokens = OAuthTokens(
        access_token=data["access_token"],
        refresh_token=data.get("refresh_token", stored.refresh_token),
        expires_at=_time.time() + int(data.get("expires_in", 3600)),
        token_type=data.get("token_type", "Bearer"),
    )
    _store_tokens(server_name, tokens)
    logger.info("oauth_refreshed server=%s", server_name)
    return tokens
