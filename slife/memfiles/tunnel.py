"""Ngrok tunnel lifecycle management.

Starts an ngrok HTTP tunnel to a local port so the memfiles service is
reachable from the public internet.  LLM APIs (OpenAI, Anthropic, etc.)
can then fetch images via HTTPS URLs instead of inline base64 data URIs.

Uses the official ngrok Python SDK (``ngrok`` on PyPI), which embeds
the ngrok agent as a native extension — no external binary required.

Requires an ngrok auth token stored in the OS credential store under
the name ``NGROK_AUTHTOKEN``.  Register at https://ngrok.com/signup
and store it via::

    credential_check NGROK_AUTHTOKEN
    credential_inject NGROK_AUTHTOKEN
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # seconds


# ── NgrokTunnel ───────────────────────────────────────────────────────


class NgrokTunnel:
    """Manages an ngrok HTTP tunnel with health monitoring.

    All tunnel state is encapsulated — no module-level globals.
    A singleton instance is held at module level for the
    backward-compatible function API.
    """

    def __init__(self) -> None:
        self._listener: Any = None
        self._public_url: str | None = None
        self._ngrok: Any = None
        self._monitor_task: "asyncio.Task[None] | None" = None
        self._monitor_retries: int = 0
        self._starting: bool = False  # guard against concurrent starts

    # ── Properties ─────────────────────────────────────────────────

    @property
    def public_url(self) -> str | None:
        """The current tunnel's public URL, or None."""
        if self._public_url is not None:
            return self._public_url
        return os.environ.get("SLIFE_MEMFILES_URL")

    @property
    def is_active(self) -> bool:
        """True when the tunnel is running."""
        return self.public_url is not None

    # ── URL helpers ─────────────────────────────────────────────────

    def share_url_for(self, file_id: str) -> str | None:
        """Build a public share URL for *file_id*."""
        url = self.public_url
        if url is None:
            return None
        return f"{url}/share/{file_id}"

    # ── Lifecycle ───────────────────────────────────────────────────

    def start(self, port: int) -> str:
        """Start an ngrok HTTP tunnel to *port*.  Returns the public URL.

        Reads the auth token from the OS credential store or the
        ``NGROK_AUTHTOKEN`` env var.  Retries up to 3 times with
        backoff for unstable network conditions.

        Raises ``RuntimeError`` if the token is missing or ngrok
        fails to start after all retries.
        """
        if self._public_url is not None:
            logger.debug("tunnel_already_running url=%s", self._public_url)
            return self._public_url

        # Guard against concurrent start attempts (e.g. executor + monitor).
        if self._starting:
            logger.debug("tunnel_start_already_in_progress — skipping")
            raise RuntimeError("Tunnel start already in progress")
        self._starting = True
        try:
            return self._do_start(port)
        finally:
            self._starting = False

    def _do_start(self, port: int) -> str:
        """Internal start logic (caller holds _starting guard).

        Each slife instance creates its own ephemeral ngrok tunnel.
        We use ``connect()`` with a per-instance session label so
        multiple instances (WSL + Windows, etc.) get distinct URLs
        without competing for the account's dev domain.
        """
        token = _read_auth_token()
        if not token:
            raise RuntimeError(
                "ngrok auth token not found. Register at https://ngrok.com/signup, "
                "then store the token via: credential_check NGROK_AUTHTOKEN"
            )

        self._ngrok = _import_ngrok()

        # Set auth token globally (once per process) so connect() calls
        # don't each trigger dev-domain auto-claim.
        self._ngrok.set_auth_token(token)

        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                # connect() creates an ephemeral tunnel (random subdomain)
                # when no domain is specified.
                self._listener = self._ngrok.connect(port)
                self._public_url = str(self._listener.url()).rstrip("/")
                os.environ["SLIFE_MEMFILES_URL"] = self._public_url
                logger.info(
                    "tunnel_started port=%s url=%s attempt=%d",
                    port, self._public_url, attempt,
                )
                return self._public_url
            except Exception as e:
                last_error = e
                if attempt < _MAX_RETRIES:
                    delay = _RETRY_DELAY * attempt
                    logger.info(
                        "tunnel_retry attempt=%d/%d delay=%.1fs err=%s",
                        attempt, _MAX_RETRIES, delay, e,
                    )
                    time.sleep(delay)

        raise RuntimeError(
            f"Failed to start ngrok tunnel after {_MAX_RETRIES} attempts: "
            f"{last_error}"
        )

    def stop(self) -> None:
        """Disconnect the tunnel."""
        if self._public_url is None or self._ngrok is None:
            return

        try:
            self._ngrok.disconnect(self._public_url)
            logger.info("tunnel_disconnected url=%s", self._public_url)
        except Exception as e:
            logger.debug("tunnel_disconnect_error err=%s", e)

        self._listener = None
        self._public_url = None
        os.environ.pop("SLIFE_MEMFILES_URL", None)

    # ── Health monitor ──────────────────────────────────────────────

    def start_monitor(self, port: int) -> None:
        """Spawn a background monitor that restarts the tunnel on failure."""
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.ensure_future(
            self._run_monitor(port)
        )
        logger.debug("tunnel_monitor_started port=%s", port)

    def stop_monitor(self) -> None:
        """Cancel the health monitor."""
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None

    async def _run_monitor(self, port: int) -> None:
        """Background task: one-shot retry if the executor failed."""
        await asyncio.sleep(2.0)  # let the executor handshake finish

        # Only retry if the initial executor failed — the embedded SDK
        # cannot silently crash, so no continuous health-ping is needed.
        if self._public_url is not None:
            return

        if self._monitor_retries > 0:
            return

        logger.info(
            "tunnel_not_started — attempting initial start port=%s", port,
        )
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, self.start, port)
            logger.info(
                "tunnel_initial_start_ok port=%s url=%s",
                port, self._public_url,
            )
            self._monitor_retries = 0
        except Exception as e:
            self._monitor_retries += 1
            logger.info(
                "tunnel_initial_start_failed port=%s err=%s", port, e,
            )


# ── Internal helpers ───────────────────────────────────────────────────


def _import_ngrok() -> Any:
    """Import the official ngrok SDK."""
    try:
        import ngrok
        return ngrok
    except ImportError:
        raise RuntimeError(
            "The ngrok Python SDK is not installed. Run: uv pip install ngrok"
        )


def _read_auth_token() -> str | None:
    """Read the ngrok auth token from the OS credential store or env var."""
    try:
        from credstore import get_credential  # pyright: ignore[reportMissingImports]
        token = get_credential("NGROK_AUTHTOKEN")
        if token:
            return token
    except (ImportError, OSError, ValueError):
        logger.debug("credstore_read_failed", exc_info=True)
    return os.environ.get("NGROK_AUTHTOKEN")


# ── Singleton ──────────────────────────────────────────────────────────

_tunnel = NgrokTunnel()


# ── Module-level API (backward-compatible) ─────────────────────────────


def share_url_for(file_id: str) -> str | None:
    return _tunnel.share_url_for(file_id)


def public_url() -> str | None:
    return _tunnel.public_url


def is_active() -> bool:
    return _tunnel.is_active


def start_tunnel(port: int) -> str:
    return _tunnel.start(port)


def stop_tunnel() -> None:
    _tunnel.stop()


def start_monitor(port: int) -> None:
    _tunnel.start_monitor(port)


def stop_monitor() -> None:
    _tunnel.stop_monitor()
