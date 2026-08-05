"""Ngrok tunnel lifecycle management.

Starts an ngrok HTTP tunnel to a local port so the memfiles service is
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
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Module-level state ───────────────────────────────────────────────

_tunnel: Any = None  # pyngrok.NgrokTunnel (no public type stubs)
_public_url: str | None = None
_ngrok_module: Any = None  # cached import


# ── Public API ───────────────────────────────────────────────────────


def _get_public_url() -> str | None:
    """Return the public tunnel URL from module state or env var fallback.

    Subagents inherit ``SLIFE_MEMFILES_URL`` from the main agent's env
    but have their own copy of this module — the module-level ``_public_url``
    is ``None``.  The env var fallback lets subagents build share URLs
    through the main agent's tunnel.
    """
    if _public_url is not None:
        return _public_url
    return os.environ.get("SLIFE_MEMFILES_URL")


def share_url_for(file_id: str) -> str | None:
    """Build a public URL for a shared local file.

    Returns ``{public_url}/share/{file_id}`` when the tunnel
    is active, or ``None`` when no tunnel is running.
    """
    url = _get_public_url()
    if url is None:
        return None
    return f"{url}/share/{file_id}"


def is_active() -> bool:
    """Return True if the ngrok tunnel is currently running."""
    return _get_public_url() is not None


# ── Lifecycle ────────────────────────────────────────────────────────

# ngrok region — "jp" (Japan) gives the lowest latency from China while
# still providing a stable public endpoint.  "ap" is a broader fallback.
# The default "us" region is often unreachable from behind the GFW.
_DEFAULT_REGION = "jp"

# Number of retries with 2s backoff when tunnel creation fails because
# the ngrok session is still stabilising.
_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # seconds


def start_tunnel(port: int) -> str:
    """Start an ngrok HTTP tunnel to *port*.

    Reads the auth token from the OS credential store
    (``NGROK_AUTHTOKEN``).  Configures ngrok for the Asia-Pacific
    region and retries up to 3 times with backoff for unstable
    network conditions (e.g. China → global ngrok servers).

    Returns the public URL.

    Raises ``RuntimeError`` if the token is missing or ngrok fails
    to start after all retries.
    """
    global _tunnel, _public_url, _ngrok_module

    if _public_url is not None:
        logger.debug("tunnel_already_running url=%s", _public_url)
        return _public_url

    token = _get_auth_token()
    if not token:
        raise RuntimeError(
            "ngrok auth token not found. Register at https://ngrok.com/signup, "
            "then store the token via: credential_check NGROK_AUTHTOKEN"
        )

    _ngrok_module = _import_ngrok()
    _ngrok_module.set_auth_token(token)

    # Build pyngrok config tuned for China network conditions:
    #   - region=jp:  lowest-latency node from China
    #   - startup_timeout=30:  generous startup window
    #   - request_timeout=10:  longer than default 4s for high-latency paths
    try:
        from pyngrok.conf import PyngrokConfig  # pyright: ignore[reportMissingImports]
        pyngrok_config = PyngrokConfig(
            region=_DEFAULT_REGION,
            startup_timeout=30,
            request_timeout=10,
        )
    except ImportError:
        pyngrok_config = None

    last_error: Exception | None = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            _tunnel = _ngrok_module.connect(
                port, "http", pyngrok_config=pyngrok_config,
            )
            _public_url = str(_tunnel.public_url).rstrip("/")
            os.environ["SLIFE_MEMFILES_URL"] = _public_url
            logger.info(
                "tunnel_started port=%s url=%s attempt=%d region=%s",
                port, _public_url, attempt, _DEFAULT_REGION,
            )
            return _public_url
        except Exception as e:
            last_error = e
            if attempt < _MAX_RETRIES:
                delay = _RETRY_DELAY * attempt
                logger.warning(
                    "tunnel_retry attempt=%d/%d delay=%.1fs err=%s",
                    attempt, _MAX_RETRIES, delay, e,
                )
                time.sleep(delay)

    raise RuntimeError(
        f"Failed to start ngrok tunnel after {_MAX_RETRIES} attempts: "
        f"{last_error}"
    )


def stop_tunnel() -> None:
    """Stop the ngrok tunnel and kill the ngrok process.

    Kills the ngrok process (not just disconnects the tunnel) so the
    next slife session starts with a clean ngrok state.  Without this,
    the orphaned ngrok process may hold stale session data, causing
    "failed to reconnect session" errors on the next startup.
    """
    global _tunnel, _public_url, _ngrok_module

    if _public_url is None:
        return

    if _ngrok_module is not None:
        try:
            _ngrok_module.disconnect(_public_url)
            logger.info("tunnel_disconnected url=%s", _public_url)
        except Exception as e:
            # Expected during force-shutdown — ngrok may already be dead
            logger.debug("tunnel_disconnect_error err=%s", e)
        try:
            _ngrok_module.kill()
            logger.info("ngrok_process_killed")
        except Exception as e:
            # Expected during force-shutdown — process may already be gone
            logger.debug("ngrok_kill_error err=%s", e)

    _tunnel = None
    _public_url = None
    _ngrok_module = None
    os.environ.pop("SLIFE_MEMFILES_URL", None)


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
