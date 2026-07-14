"""Mosquitto broker lifecycle management.

Optional — when the user does not want to run mosquitto separately,
slife can probe for an existing broker and spawn one if none is found.
Follows the same pattern as ``MCPWrapperProcess`` (slife/mcp/process.py).
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys

from slife.platform import IS_WINDOWS

logger = logging.getLogger(__name__)


class BrokerManager:
    """Manage a mosquitto child process.

    Usage::

        mgr = BrokerManager("/usr/sbin/mosquitto", ["-c", "mosquitto.conf"])
        await mgr.ensure()    # probes first, spawns only if needed
        # ... use A2A ...
        await mgr.stop()
    """

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        host: str = "localhost",
        port: int = 1883,
    ):
        self._command = command or "mosquitto"
        self._args = args or []
        self._host = host
        self._port = port
        self._process: asyncio.subprocess.Process | None = None
        self._running: bool = False

    # ── Ensure ────────────────────────────────────────────────────────

    async def ensure(self) -> None:
        """Probe for a running broker; spawn one if none is listening."""
        if await self._probe():
            logger.info(
                "broker_found host=%s port=%d", self._host, self._port,
            )
            return

        logger.info(
            "broker_not_found host=%s port=%d — spawning", self._host, self._port,
        )
        await self._spawn()
        # Give mosquitto a moment to start listening
        for _ in range(20):
            await asyncio.sleep(0.25)
            if await self._probe():
                logger.info("broker_ready pid=%s", self._process.pid)
                return
        raise RuntimeError("Broker did not start listening in time")

    async def stop(self) -> None:
        """Stop the spawned broker process."""
        if not self._process or not self._running:
            return

        logger.info("broker_stop pid=%s", self._process.pid)
        try:
            if IS_WINDOWS:
                self._process.terminate()
            else:
                import signal
                self._process.send_signal(signal.SIGTERM)

            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
                logger.info("broker_exited pid=%s", self._process.pid)
            except asyncio.TimeoutError:
                logger.warning("broker_force_kill pid=%s", self._process.pid)
                self._process.kill()
                await self._process.wait()
        except ProcessLookupError:
            pass
        finally:
            self._running = False
            self._process = None

    # ── Internals ─────────────────────────────────────────────────────

    async def _probe(self) -> bool:
        """Check whether something is listening on broker_host:broker_port."""
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(self._host, self._port),
                timeout=1.0,
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _spawn(self) -> None:
        """Start mosquitto as a child process."""
        logger.info("broker_spawn cmd=%s args=%s", self._command, self._args)
        self._process = await asyncio.create_subprocess_exec(
            self._command,
            *self._args,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._running = True

        # Background stderr reader
        asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """Log mosquitto stderr output."""
        from slife.logfmt import read_stderr_lines

        async for text in read_stderr_lines(
            self._process, lambda: self._running,
        ):
            logger.debug("[mosquitto] %s", text)
