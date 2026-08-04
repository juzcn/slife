"""Ngrok tunnel lifecycle management.

Starts an ngrok HTTP tunnel to a local port so the media server is
reachable from the public internet.  LLM APIs (OpenAI, Anthropic, etc.)
can then fetch images via HTTPS URLs instead of inline base64 data URIs.

Requires an ngrok auth token stored in the OS credential store under
the name ``NGROK_AUTHTOKEN``.  Register at https://ngrok.com/signup
and store it via::

    credential_check NGROK_AUTHTOKEN
    inject_credential NGROK_AUTHTOKEN
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# ── Module-level state ───────────────────────────────────────────────

_tunnel: Any = None  # pyngrok.NgrokTunnel (no public type stubs)
_public_url: str | None = None
_ngrok_module: Any = None  # cached import


# ── Public API ───────────────────────────────────────────────────────


def media_url_for(image_id: str) -> str | None:
    """Build a public media URL for an image BLOB.

    Returns ``{public_url}/media/{image_id}`` when the tunnel is
    active, or ``None`` when no tunnel is running (callers should
    fall back to base64 data URIs).
    """
    if _public_url is None:
        return None
    return f"{_public_url}/media/{image_id}"


def is_active() -> bool:
    """Return True if the ngrok tunnel is currently running."""
    return _public_url is not None


# ── Lifecycle ────────────────────────────────────────────────────────


def start_tunnel(port: int) -> str:
    """Start an ngrok HTTP tunnel to *port*.

    Reads the auth token from the OS credential store
    (``NGROK_AUTHTOKEN``).  Returns the public URL.

    Raises ``RuntimeError`` if the token is missing or ngrok fails
    to start.
    """
    global _tunnel, _public_url, _ngrok_module

    if _public_url is not None:
        logger.warning("tunnel_already_running url=%s", _public_url)
        return _public_url

    token = _get_auth_token()
    if not token:
        raise RuntimeError(
            "ngrok auth token not found. Register at https://ngrok.com/signup, "
            "then store the token via: credential_check NGROK_AUTHTOKEN"
        )

    _ngrok_module = _import_ngrok()
    _ngrok_module.set_auth_token(token)
    _tunnel = _ngrok_module.connect(port, "http")
    _public_url = str(_tunnel.public_url).rstrip("/")
    os.environ["SLIFE_MEDIA_URL"] = _public_url

    logger.info("tunnel_started port=%s url=%s", port, _public_url)
    return _public_url


def stop_tunnel() -> None:
    """Stop the ngrok tunnel and clear the public URL."""
    global _tunnel, _public_url, _ngrok_module

    if _public_url is None:
        return

    if _ngrok_module is not None:
        try:
            _ngrok_module.disconnect(_public_url)
            logger.info("tunnel_stopped url=%s", _public_url)
        except Exception as e:  # noqa: BLE001 — best-effort cleanup, never re-raise
            logger.warning("tunnel_stop_error err=%s", e)

    _tunnel = None
    _public_url = None
    _ngrok_module = None
    os.environ.pop("SLIFE_MEDIA_URL", None)


# ── Internal ─────────────────────────────────────────────────────────


def _import_ngrok() -> Any:
    """Import pyngrok, raising a clear error if not installed."""
    try:
        from pyngrok import ngrok  # pyright: ignore[reportMissingImports]
        return ngrok
    except ImportError:
        raise RuntimeError(
            "pyngrok is not installed. Run: uv pip install pyngrok"
        )


def _get_auth_token() -> str | None:
    """Read the ngrok auth token from the OS credential store."""
    try:
        from credstore import get_credential  # pyright: ignore[reportMissingImports]

        token = get_credential("NGROK_AUTHTOKEN")
        if token:
            return token
    except (ImportError, OSError, ValueError):
        logger.debug("credstore_read_failed", exc_info=True)

    # Fallback: environment variable
    return os.environ.get("NGROK_AUTHTOKEN")
