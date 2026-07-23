"""MCP wrapper process lifecycle management.

Spawns the plugin child process on agent startup and ensures
clean shutdown on exit.  The child starts a Streamable HTTP server on a
dynamically-assigned port; this wrapper discovers the port via a
one-line JSON signal on stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slife.mcp.client import MCPClient

from slife.logfmt import get_session_id
from slife.platform import IS_WINDOWS, terminate_process

logger = logging.getLogger(__name__)

# Default wrapper module path
_DEFAULT_SERVER_MODULE = "slife.plugins.mcp.server"


class MCPWrapperProcess:
    """Manages the plugin child process lifecycle.

    Usage:
        wrapper = MCPWrapperProcess(args=["-m", "slife.plugins.mcp.server"])
        await wrapper.start()
        client = await wrapper.create_client()
        # ... use client ...
        await client.disconnect()
        await wrapper.stop()
    """

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
    ):
        """
        Args:
            command: Executable to run (default: sys.executable).
            args: Command args. If None, defaults to
                  ``['-m', 'slife.plugins.mcp.server']``.
        """
        self._command = command if command is not None else sys.executable
        if args is not None:
            self._args = list(args)
        else:
            self._args = ["-m", _DEFAULT_SERVER_MODULE]
        self._process: asyncio.subprocess.Process | None = None
        self._running: bool = False
        self._port: int = 0

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    @property
    def pid(self) -> int | None:
        if self._process:
            return self._process.pid
        return None

    @property
    def port(self) -> int:
        """The port the plugin Streamable HTTP server is listening on."""
        return self._port

    async def start(self) -> None:
        """Start the plugin child process and discover its port.

        The child prints ``{"port": <N>}`` to stdout as its first and
        only stdout output, then closes stdout.  We read that line to
        learn the dynamically-assigned port.
        """
        if self._running:
            logger.warning("wrapper_already_running pid=%s", self.pid)
            return

        logger.info(
            "wrapper_start cmd=%s args=%s", self._command, " ".join(self._args)
        )

        try:
            env = dict(os.environ)
            env["SLIFE_SESSION_ID"] = get_session_id()

            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._running = True
            logger.info("wrapper_started pid=%s", self._process.pid)

            # Read the port signal from stdout (single JSON line)
            await self._read_port_signal()

            # Start background stderr draining
            asyncio.create_task(self._log_stderr())

        except FileNotFoundError as e:
            logger.error("wrapper_exec_not_found cmd=%s err=%s", self._command, e)
            self._running = False
            raise
        except Exception as e:
            logger.error("wrapper_start_failed err=%s", e)
            self._running = False
            raise

    async def _read_port_signal(self) -> None:
        """Read the port-discovery JSON line from child stdout."""
        assert self._process and self._process.stdout

        try:
            line = await asyncio.wait_for(
                self._process.stdout.readline(), timeout=10.0,
            )
        except asyncio.TimeoutError:
            raise RuntimeError(
                "Plugin process did not send port signal within 10s"
            )

        if not line:
            # EOF before port signal — child crashed
            stderr_tail = ""
            if self._process.stderr:
                try:
                    remaining = await self._process.stderr.read()
                    stderr_tail = remaining.decode("utf-8", errors="replace")[-2000:]
                except Exception:
                    pass
            raise RuntimeError(
                f"Plugin process (pid={self._process.pid}) exited before "
                f"sending port signal. stderr:\n{stderr_tail}"
            )

        try:
            data = json.loads(line.decode("utf-8", errors="replace"))
            self._port = int(data["port"])
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            raise RuntimeError(
                f"Invalid port signal from plugin: {line[:200].strip()!r}"
            ) from e

        logger.info("wrapper_port pid=%s port=%s", self._process.pid, self._port)

    async def create_client(self, tool_timeout: float = 60.0) -> "MCPClient":
        """Create an MCPClient connected to the plugin's Streamable HTTP endpoint.

        Disconnecting the client does NOT stop the process — call stop()
        separately to terminate the plugin.
        """
        from slife.mcp.client import MCPClient

        if not self._process or not self._running:
            raise RuntimeError(
                "Plugin wrapper is not running. Call start() first."
            )

        if not self._port:
            raise RuntimeError("Plugin port not discovered. Call start() first.")

        # Check if the child process already died
        returncode = self._process.returncode
        if returncode is not None:
            stderr_tail = ""
            if self._process.stderr:
                try:
                    remaining = await self._process.stderr.read()
                    stderr_tail = remaining.decode("utf-8", errors="replace")[-2000:]
                except Exception:
                    pass
            raise RuntimeError(
                f"Plugin child process (pid={self._process.pid}) exited "
                f"with code {returncode} before Streamable HTTP connection. "
                f"stderr:\n{stderr_tail}"
            )

        url = f"http://127.0.0.1:{self._port}/mcp"
        client = MCPClient(tool_timeout=tool_timeout)
        await client.connect(url)
        return client

    async def stop(self) -> None:
        """Stop the plugin child process.

        Uses a short graceful window — the process is a local child
        with no unsaved state, so we don't need to wait long.  A fast
        kill keeps Ctrl‑C exit snappy.
        """
        if not self._process or not self._running:
            return

        # Signal stderr drain to stop BEFORE killing the process.
        # This prevents "unclosed transport" ResourceWarning from the
        # proactor event loop on Windows when the pipe is GC'd mid-read.
        self._running = False
        await asyncio.sleep(0)

        logger.info("wrapper_stop pid=%s", self._process.pid)
        await terminate_process(
            self._process, graceful_timeout=1.0, force_timeout=2.0,
            label="mcp_wrapper",
        )
        logger.info(
            "wrapper_killed pid=%s",
            self._process.pid if self._process else "?",
        )
        self._process = None
        self._port = 0

    async def _log_stderr(self) -> None:
        """Read and log stderr from the plugin process.

        All lines at DEBUG — stderr is diagnostic only.
        Filters out FastMCP banner art, uvicorn startup messages,
        and subprocess log lines that already go to the plugin's
        own log file.  Everything else is relayed at DEBUG so it
        never reaches the terminal (console handler is WARNING+).
        """
        import re
        from slife.logfmt import read_stderr_lines

        # Matches slife logger output: "HH:MM:SS [LEVEL] logger_name ..."
        _SUBPROCESS_LOG = re.compile(
            r'^\d{2}:\d{2}:\d{2}\s+\[(?:DEBUG|INFO ?|WARN(?:ING)?|ERROR)\]\s+\S+'
        )

        # Matches uvicorn log output: "LEVEL:     message"
        _UVICORN_LOG = re.compile(r'^(?:INFO|WARNING|ERROR|DEBUG)\s*:')

        _BANNER_MARKERS = (
            "gofastmcp.com",
            "Deploy free:",
            "FastMCP ",
        )
        _BANNER_CHARS = set("─│└├┬┴╭╮╯╰▀▄█▌▐░▒▓")

        async for text in read_stderr_lines(
            self._process, lambda: self._running,
        ):
            stripped = text.strip()
            if not stripped:
                continue

            if any(c in stripped for c in _BANNER_CHARS):
                continue
            if "�" in stripped:
                continue
            if all(c in " |│+" for c in stripped):
                continue
            if any(m in stripped for m in _BANNER_MARKERS):
                continue
            if _SUBPROCESS_LOG.match(stripped):
                continue
            if _UVICORN_LOG.match(stripped):
                continue

            # Only relay lines that don't match any filter.
            # All at DEBUG — never reaches the terminal.
            logger.debug("[wrapper] %s", text)
