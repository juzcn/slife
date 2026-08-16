"""Ngrok tunnel lifecycle management — owned by the memfiles plugin.

The plugin (not the slife harness) starts the ngrok tunnel to its own
local port so the memfiles service is reachable from the public internet.
LLM APIs (OpenAI, Anthropic, etc.) can then fetch images via HTTPS URLs
instead of inline base64 data URIs.

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
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────

_MAX_RETRIES = 3
_RETRY_DELAY = 2.0  # seconds

# A start attempt stuck longer than this is considered dead (its daemon
# thread is hung in credstore/forward) — a fresh attempt may supersede it.
# The stale thread is harmless: daemon threads die with the process.
_TUNNEL_START_TIMEOUT = 45.0  # seconds


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
        self._starting_at: float | None = None  # monotonic time of the in-flight start
        self._start_gen: int = 0  # bumped per accepted start attempt (stale ownership)
        self._start_lock = threading.Lock()  # serializes guard mutation only
        # Set once a start attempt has concluded unsuccessfully (retries
        # exhausted, missing token, SDK absent).  Cleared on a successful
        # start or an explicit stop.  Lets the harness report "tunnel down"
        # as a terminal state instead of racing a still-running attempt.
        self._failed: bool = False

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

    # ── Status ─────────────────────────────────────────────────────

    def status(self) -> dict[str, str]:
        """Report the tunnel's current state to the harness.

        ``active`` — a public URL is live; ``starting`` — a start attempt is
        in flight (the eager start is deliberately fire-and-forget, so the
        harness sees this transient state and waits); ``failed`` — the last
        start attempt concluded unsuccessfully (terminal, safe to report);
        ``idle`` — no attempt has been made (e.g. plugin loaded without a
        port, subagent reusing the main agent's tunnel).
        """
        if self._public_url is not None:
            return {"state": "active", "url": self._public_url}
        if self._starting:
            return {"state": "starting", "url": ""}
        if self._failed:
            return {"state": "failed", "url": ""}
        return {"state": "idle", "url": ""}

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
            logger.warning("tunnel_already_running url=%s", self._public_url)
            return self._public_url

        # Guard against concurrent start attempts (e.g. executor + monitor).
        # A start stuck longer than _TUNNEL_START_TIMEOUT is considered dead
        # (its daemon thread is hung in credstore/forward) — supersede it so
        # the tunnel isn't wedged in "already in progress" forever.
        with self._start_lock:
            if self._starting:
                elapsed = time.monotonic() - (self._starting_at or time.monotonic())
                if elapsed < _TUNNEL_START_TIMEOUT:
                    logger.debug("tunnel_start_already_in_progress")
                    raise RuntimeError("Tunnel start already in progress")
                logger.warning(
                    "tunnel_start_stale_superseded elapsed=%.0fs timeout=%.0fs",
                    elapsed, _TUNNEL_START_TIMEOUT,
                )
            self._start_gen += 1
            gen = self._start_gen
            self._starting = True
            self._starting_at = time.monotonic()
        try:
            return self._do_start(port)
        finally:
            # Only the current owner clears the guard — a superseded thread
            # finishing later must not clobber the newer attempt's state.
            with self._start_lock:
                if self._start_gen == gen:
                    self._starting = False
                    self._starting_at = None

    def _do_start(self, port: int) -> str:
        """Internal start logic (caller holds _starting guard).

        Uses endpoint pooling so multiple slife instances (WSL + Windows,
        sub-agents on different machines, etc.) can share the same ngrok
        dev domain.  ngrok load-balances across all online agents.
        """
        # Pessimistic: any exit below marks the attempt a terminal failure
        # (missing token, SDK absent, retries exhausted).  Cleared only on
        # success, so ``status()`` reports "failed" once the attempt ends.
        self._failed = True

        token = _read_auth_token()
        if not token:
            raise RuntimeError(
                "ngrok auth token not found. Register at https://ngrok.com/signup, "
                "then store the token via: credential_check NGROK_AUTHTOKEN"
            )

        self._ngrok = _import_ngrok()

        last_error: Exception | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                self._listener = self._ngrok.forward(
                    f"localhost:{port}", authtoken=token, pooling_enabled=True,
                )
                self._public_url = str(self._listener.url()).rstrip("/")
                os.environ["SLIFE_MEMFILES_URL"] = self._public_url
                self._failed = False
                logger.info(
                    "tunnel_started port=%s url=%s attempt=%d",
                    port, self._public_url, attempt,
                )
                return self._public_url
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

    def stop(self) -> None:
        """Disconnect the tunnel."""
        # A stopped tunnel is never "failed" — clear the flag even when
        # there is nothing to disconnect (the early return below).
        self._failed = False
        if self._public_url is None or self._ngrok is None:
            return

        try:
            self._ngrok.disconnect(self._public_url)
            logger.info("tunnel_disconnected url=%s", self._public_url)
        except Exception as e:
            logger.warning("tunnel_disconnect_error err=%s", e)

        self._listener = None
        self._public_url = None
        os.environ.pop("SLIFE_MEMFILES_URL", None)

    # ── Health monitor ──────────────────────────────────────────────

    def start_monitor(self, port: int, on_tunnel_up=None) -> None:
        """Spawn a background monitor that restarts the tunnel on failure.

        When *on_tunnel_up* is provided, it is called (no arguments) if
        the monitor successfully starts the tunnel after a retry.
        """
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
        self._monitor_task = asyncio.ensure_future(
            self._run_monitor(port, on_tunnel_up)
        )
        logger.debug("tunnel_monitor_started port=%s", port)

    def stop_monitor(self) -> None:
        """Cancel the health monitor."""
        if self._monitor_task is not None and not self._monitor_task.done():
            self._monitor_task.cancel()
            self._monitor_task = None

    async def _run_monitor(self, port: int, on_tunnel_up=None) -> None:
        """Background task: one-shot retry if the initial start failed."""
        await asyncio.sleep(2.0)  # let the daemon-thread handshake finish

        # Only retry if the initial start failed — the embedded SDK cannot
        # silently crash, so no continuous health-ping is needed.
        if self._public_url is not None:
            return

        if self._monitor_retries > 0:
            return

        logger.info(
            "tunnel_monitor_retry port=%s phase=initial_start", port,
        )
        from slife.threads import run_daemon
        try:
            await run_daemon(self.start, port, name="ngrok-tunnel-retry")
            logger.info(
                "tunnel_initial_start_ok port=%s url=%s",
                port, self._public_url,
            )
            self._monitor_retries = 0
            if on_tunnel_up:
                on_tunnel_up()
        except Exception as e:
            self._monitor_retries += 1
            logger.warning(
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
        logger.warning("credstore_read_failed")
    return os.environ.get("NGROK_AUTHTOKEN")


# ── Singleton ──────────────────────────────────────────────────────────

_tunnel = NgrokTunnel()


# ── Module-level API (used by the memfiles plugin server) ──────────────


def share_url_for(file_id: str) -> str | None:
    return _tunnel.share_url_for(file_id)


def public_url() -> str | None:
    return _tunnel.public_url


def is_active() -> bool:
    return _tunnel.is_active


def status() -> dict[str, str]:
    return _tunnel.status()


def start_tunnel(port: int) -> str:
    return _tunnel.start(port)


def stop_tunnel() -> None:
    _tunnel.stop()


def start_monitor(port: int, on_tunnel_up=None) -> None:
    _tunnel.start_monitor(port, on_tunnel_up)


def stop_monitor() -> None:
    _tunnel.stop_monitor()
