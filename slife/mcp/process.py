"""MCP wrapper process lifecycle management.

Spawns the slife-mcp child process on agent startup and ensures
clean shutdown on exit. Supports auto-restart on crash.
"""

import asyncio
import logging
import os
import sys

from slife.logfmt import get_session_id
from slife.platform import IS_WINDOWS, terminate_process

logger = logging.getLogger(__name__)

# Default wrapper module path
_DEFAULT_SERVER_MODULE = "slife.plugins.mcp.server"


class MCPWrapperProcess:
    """Manages the slife-mcp child process lifecycle.

    Usage:
        wrapper = MCPWrapperProcess()
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
        server_module: str | None = None,
    ):
        """
        Args:
            command: Executable to run (default: sys.executable).
            args: Override command args. If None, defaults to
                  ['-m', 'slife_mcp.server'].
            server_module: Python module for the wrapper server
                  (default: 'slife_mcp.server').
        """
        self._command = command if command is not None else sys.executable
        if args is not None:
            self._args = args
        else:
            module = server_module or _DEFAULT_SERVER_MODULE
            self._args = ["-m", module]
        self._process: asyncio.subprocess.Process | None = None
        self._running: bool = False

    @property
    def is_running(self) -> bool:
        return self._running and self._process is not None

    @property
    def pid(self) -> int | None:
        if self._process:
            return self._process.pid
        return None

    async def start(self) -> None:
        """Start the slife-mcp wrapper child process.

        The process communicates via stdin/stdout (MCP stdio transport).
        Logs from the wrapper go to stderr and are captured.
        """
        if self._running:
            logger.warning("wrapper_already_running pid=%s", self.pid)
            return

        logger.info(
            "wrapper_start cmd=%s args=%s", self._command, " ".join(self._args)
        )

        try:
            # Pass session ID so wrapper can correlate its logs with ours
            env = dict(os.environ)
            env["SLIFE_SESSION_ID"] = get_session_id()

            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            self._running = True
            logger.info("wrapper_started pid=%s", self._process.pid)

            # Start a background task to log stderr output
            asyncio.create_task(self._log_stderr())

        except FileNotFoundError as e:
            logger.error(
                "wrapper_exec_not_found cmd=%s err=%s",
                self._command,
                e,
            )
            self._running = False
            raise
        except Exception as e:
            logger.error("wrapper_start_failed err=%s", e)
            self._running = False
            raise

    async def create_client(self) -> "MCPClient":
        """Create an MCPClient connected to the running wrapper process.

        The client uses the process's existing stdin/stdout streams.
        Disconnecting the client does NOT stop the process — call stop()
        separately to terminate the wrapper.
        """
        from slife.mcp.client import MCPClient

        if not self._process or not self._running:
            raise RuntimeError(
                "MCP wrapper is not running. Call start() first."
            )

        # Check if the child process already died (e.g. startup crash).
        # This prevents hanging in connect_streams() waiting for a
        # handshake response from a dead process.
        returncode = self._process.returncode
        if returncode is not None:
            stderr_tail = ""
            if self._process.stderr:
                try:
                    remaining = await self._process.stderr.read()
                    stderr_tail = remaining.decode(
                        "utf-8", errors="replace",
                    )[-2000:]
                except Exception:
                    pass
            raise RuntimeError(
                f"MCP child process (pid={self._process.pid}) exited "
                f"with code {returncode} before completing the MCP "
                f"handshake. stderr:\n{stderr_tail}"
            )

        client = MCPClient()
        await client.connect_streams(
            read_stream=self._process.stdout,
            write_stream=self._process.stdin,
        )
        return client

    async def stop(self) -> None:
        """Stop the MCP wrapper child process gracefully."""
        if not self._process or not self._running:
            return

        logger.info("wrapper_stop pid=%s", self._process.pid)
        await terminate_process(
            self._process, graceful_timeout=5.0, label="mcp_wrapper",
        )
        logger.info("wrapper_killed pid=%s", self._process.pid if self._process else "?")
        self._running = False
        self._process = None

    async def _log_stderr(self) -> None:
        """Read and log stderr from the wrapper process.

        All lines at DEBUG — wrapper stderr is diagnostic only.
        Errors are communicated via the MCP protocol on stdout;
        leaking stderr to the parent terminal would pollute the TUI.

        Filters out:
        - FastMCP ASCII/Unicode box-drawing banner art
        - Subprocess log lines that are already in the subprocess's
          own log file (they have the ``[LEVEL]`` marker) — only
          log lines without that pattern, like raw tracebacks.
        """
        import re
        from slife.logfmt import read_stderr_lines

        # Subprocess log lines already go to their own log file.
        # Only relay lines that are NOT from the slife logger:
        # they lack the "HH:MM:SS [LEVEL] logger_name" prefix.
        # stderr format is: HH:MM:SS [%(levelname)-5s] logger_name | ...
        # -5s left-pads: "INFO " (4 + space), "DEBUG" (5), "ERROR" (5), "WARNING" (7)
        _SUBPROCESS_LOG = re.compile(
            r'^\d{2}:\d{2}:\d{2}\s+\[(?:DEBUG|INFO ?|WARN(?:ING)?|ERROR)\]\s+\S+'
        )

        # FastMCP v3 uses Unicode box-drawing and ASCII art banners.
        # Typical content: box borders, "FastMCP 3.x", "gofastmcp.com",
        # "Deploy free:", emoji server name lines.
        _BANNER_MARKERS = (
            "gofastmcp.com",
            "Deploy free:",
            "FastMCP ",
        )
        _BANNER_CHARS = set(
            "─│└├┬┴╭╮╯╰▀▄█▌▐░▒▓"
        )

        async for text in read_stderr_lines(
            self._process, lambda: self._running,
        ):
            stripped = text.strip()
            if not stripped:
                continue

            # Suppress FastMCP box-drawing/ASCII art banners.
            # Strategy: block any line with (a) box-drawing chars, (b) Unicode
            # replacement chars from encoding failures, (c) known banner text,
            # or (d) purely decorative lines (spaces + box edges).
            if any(c in stripped for c in _BANNER_CHARS):
                continue
            if "�" in stripped:  # mojibake from CP65001 ↔ UTF-8 mismatch
                continue
            if all(c in " |│+" for c in stripped):  # ASCII box edges too
                continue
            if any(m in stripped for m in _BANNER_MARKERS):
                continue

            # Subprocess has its own log file — don't duplicate
            if _SUBPROCESS_LOG.match(stripped):
                continue

            logger.debug("[wrapper] %s", text)
