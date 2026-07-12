"""MCP wrapper process lifecycle management.

Spawns the slife-mcp child process on agent startup and ensures
clean shutdown on exit. Supports auto-restart on crash.
"""

import asyncio
import logging
import signal
import sys

from slife.platform import IS_WINDOWS

logger = logging.getLogger(__name__)

# Default wrapper module path
_DEFAULT_SERVER_MODULE = "slife_mcp.server"


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
            logger.warning("MCP wrapper is already running (pid=%s).", self.pid)
            return

        logger.info(
            "Starting MCP wrapper: %s %s", self._command, " ".join(self._args)
        )

        try:
            self._process = await asyncio.create_subprocess_exec(
                self._command,
                *self._args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            self._running = True
            logger.info("MCP wrapper started (pid=%s).", self._process.pid)

            # Start a background task to log stderr output
            asyncio.create_task(self._log_stderr())

        except FileNotFoundError as e:
            logger.error(
                "Failed to start MCP wrapper: executable '%s' not found. %s",
                self._command,
                e,
            )
            self._running = False
            raise
        except Exception as e:
            logger.error("Failed to start MCP wrapper: %s", e)
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

        client = MCPClient()
        await client.connect_streams(
            read_stream=self._process.stdout,
            write_stream=self._process.stdin,
        )
        return client

    async def stop(self) -> None:
        """Stop the MCP wrapper child process gracefully.

        Sends SIGTERM first, then SIGKILL after a timeout if the process
        doesn't exit.
        """
        if not self._process or not self._running:
            return

        logger.info("Stopping MCP wrapper (pid=%s)...", self._process.pid)

        try:
            # Close stdin first to signal the process
            if self._process.stdin:
                try:
                    self._process.stdin.close()
                except Exception:
                    pass

            # Graceful termination
            if IS_WINDOWS:
                self._process.terminate()
            else:
                self._process.send_signal(signal.SIGTERM)

            # Wait for graceful exit
            try:
                await asyncio.wait_for(self._process.wait(), timeout=5.0)
                logger.info("MCP wrapper exited gracefully.")
            except asyncio.TimeoutError:
                logger.warning(
                    "MCP wrapper didn't exit, force killing..."
                )
                self._process.kill()
                await self._process.wait()
                logger.info("MCP wrapper killed.")
        except ProcessLookupError:
            # Process already exited
            logger.debug("MCP wrapper process already gone.")
        except Exception as e:
            logger.error("Error stopping MCP wrapper: %s", e)
        finally:
            self._running = False
            self._process = None

    async def _log_stderr(self) -> None:
        """Read and log stderr from the wrapper process.

        Logs at INFO level so startup failures (like uv file-lock
        errors on Windows) are visible without needing --debug.
        """
        if not self._process or not self._process.stderr:
            return

        try:
            while self._running and self._process and self._process.stderr:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    # Log errors/warnings prominently, debug for the rest
                    if any(marker in text.lower() for marker in ("error", "traceback", "fail", "exception")):
                        logger.warning("[wrapper] %s", text)
                    else:
                        logger.info("[wrapper] %s", text)
        except Exception as e:
            logger.debug("Stderr reader stopped: %s", e)
