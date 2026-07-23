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
import time as _time
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

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

    # Consider tokens expiring within 60s as expired
    if tokens.expires_at > 0 and _time.time() + 60 >= tokens.expires_at:
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
    poll_interval = float(device_data.get("interval", _POLL_INTERVAL))

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
    print("\n".join(msg_parts), flush=True)

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
            print(f"  ✓ Authorized! Token stored for {server_name}.\n", flush=True)
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
        except httpx.HTTPError as e:
            _delete_tokens(server_name)
            raise RuntimeError(
                f"Token refresh failed for '{server_name}': {e}. "
                f"Re-run device code flow."
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
