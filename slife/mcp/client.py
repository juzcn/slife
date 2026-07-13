"""MCP client — connects to slife-mcp wrapper via stdio or HTTP transport.

Uses asyncio subprocess + asyncio.Queue adapters + ClientSession.
"""

import asyncio
import logging
import os
import shutil
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.stdio import get_default_environment
from mcp.shared.message import SessionMessage

from slife.logfmt import get_session_id
from slife.platform import IS_WINDOWS

logger = logging.getLogger(__name__)

DEFAULT_WRAPPER_URL = "http://127.0.0.1:9876/mcp"


def _resolve_command(command: str) -> str:
    if IS_WINDOWS and not command.lower().endswith((".exe", ".cmd", ".bat")):
        resolved = shutil.which(command) or shutil.which(command + ".cmd") or shutil.which(command + ".exe")
        if resolved:
            return resolved
    return command


# ── asyncio.Queue adapters — implement anyio stream protocol on asyncio primitives ──

class _ReadAdapter:
    """Wraps asyncio.Queue for ClientSession read stream."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    async def receive(self):
        return await self._queue.get()

    async def aclose(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.receive()


class _WriteAdapter:
    """Wraps asyncio.Queue for ClientSession write stream."""

    def __init__(self, queue: asyncio.Queue):
        self._queue = queue

    async def send(self, msg):
        await self._queue.put(msg)

    async def aclose(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


# ── MCPClient ────────────────────────────────────────────────────────


class MCPClient:
    """MCP client for the slife-mcp wrapper."""

    def __init__(self):
        self._session: ClientSession | None = None
        self._connected: bool = False
        self._owns_process: bool = False
        self._process: asyncio.subprocess.Process | None = None
        self._transport: Any = None  # HTTP transport context manager
        self._read_task: asyncio.Task | None = None
        self._write_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stdin_queue: asyncio.Queue | None = None
        self._stdout_queue: asyncio.Queue | None = None
        self._read_adapter: _ReadAdapter | None = None
        self._write_adapter: _WriteAdapter | None = None

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect_stdio(
        self, command: str, args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        """Connect by spawning the slife-mcp wrapper as a child process."""
        if self._connected:
            logger.warning("mcp_client_already_connected")
            return

        exe = _resolve_command(command)
        merged_env = get_default_environment()
        merged_env["SLIFE_SESSION_ID"] = get_session_id()
        if env:
            merged_env = {**merged_env, **env}

        logger.info("mcp_client_connect transport=stdio cmd=%s", exe)

        self._process = await asyncio.create_subprocess_exec(
            exe, *(args or []),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env or None,
        )

        self._stdout_queue = asyncio.Queue()
        self._stdin_queue = asyncio.Queue()
        self._read_adapter = _ReadAdapter(self._stdout_queue)
        self._write_adapter = _WriteAdapter(self._stdin_queue)

        self._read_task = asyncio.create_task(self._bridge_stdout())
        self._write_task = asyncio.create_task(self._bridge_stdin())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        self._session = ClientSession(self._read_adapter, self._write_adapter)
        await self._session.__aenter__()
        await self._session.initialize()

        self._connected = True
        self._owns_process = True
        logger.info("mcp_client_connected transport=stdio")

    async def _bridge_stdout(self) -> None:
        assert self._process and self._process.stdout and self._stdout_queue
        # Allow lines up to 10 MB — tool schemas can be large
        self._process.stdout._limit = 10 * 1024 * 1024
        try:
            while True:
                line = await self._process.stdout.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    message = types.JSONRPCMessage.model_validate_json(line_str)
                    await self._stdout_queue.put(SessionMessage(message))
                except Exception:
                    logger.warning(
                        "mcp_bridge_parse_fail line=%.200s",
                        line_str,
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("mcp_bridge_stdout_crashed", exc_info=True)

    async def _bridge_stdin(self) -> None:
        assert self._process and self._process.stdin and self._stdin_queue
        try:
            while True:
                session_message = await self._stdin_queue.get()
                json_str = session_message.message.model_dump_json(
                    by_alias=True, exclude_none=True
                )
                self._process.stdin.write((json_str + "\n").encode("utf-8"))
                await self._process.stdin.drain()
        except asyncio.CancelledError:
            pass

    async def _bridge_reader(self, reader) -> None:
        """Bridge from a raw asyncio StreamReader to the stdout queue."""
        # Allow lines up to 10 MB — tool schemas can be large
        reader._limit = 10 * 1024 * 1024
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    message = types.JSONRPCMessage.model_validate_json(line_str)
                    await self._stdout_queue.put(SessionMessage(message))
                except Exception:
                    logger.debug(
                        "Failed to parse reader line as JSON-RPC: %s",
                        line_str[:200], exc_info=True,
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("mcp_bridge_reader_crashed", exc_info=True)

    async def _bridge_writer(self, writer) -> None:
        """Bridge from the stdin queue to a raw asyncio StreamWriter."""
        assert self._stdin_queue
        try:
            while True:
                session_message = await self._stdin_queue.get()
                json_str = session_message.message.model_dump_json(
                    by_alias=True, exclude_none=True
                )
                writer.write((json_str + "\n").encode("utf-8"))
                await writer.drain()
        except asyncio.CancelledError:
            pass

    async def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
        except asyncio.CancelledError:
            pass

    async def connect_http(self, url: str = DEFAULT_WRAPPER_URL) -> None:
        if self._connected:
            logger.warning("mcp_client_already_connected")
            return
        logger.info("mcp_client_connect transport=http url=%s", url)
        from mcp.client.streamable_http import streamablehttp_client
        self._transport = streamablehttp_client(url)
        read_stream, write_stream, _ = await self._transport.__aenter__()
        self._session = ClientSession(read_stream, write_stream)
        await self._session.__aenter__()
        await self._session.initialize()
        self._connected = True
        self._owns_process = False
        logger.info("mcp_client_connected transport=http")

    async def connect_streams(self, read_stream, write_stream) -> None:
        if self._connected:
            logger.warning("mcp_client_already_connected")
            return
        logger.info("mcp_client_connect transport=streams")

        self._stdout_queue = asyncio.Queue()
        self._stdin_queue = asyncio.Queue()
        self._read_adapter = _ReadAdapter(self._stdout_queue)
        self._write_adapter = _WriteAdapter(self._stdin_queue)

        self._read_task = asyncio.create_task(self._bridge_reader(read_stream))
        self._write_task = asyncio.create_task(self._bridge_writer(write_stream))

        self._session = ClientSession(self._read_adapter, self._write_adapter)
        await self._session.__aenter__()
        await self._session.initialize()
        self._connected = True
        self._owns_process = False
        logger.info("mcp_client_connected transport=streams")

    async def disconnect(self) -> None:
        self._connected = False
        await self._cancel_bridge_tasks()
        self._reset_state()
        await self._cleanup_transport()
        await self._terminate_owned_process()
        logger.info("mcp_client_disconnected")

    async def _cancel_bridge_tasks(self) -> None:
        """Cancel all bridge tasks and wait for them to finish."""
        for task in (self._read_task, self._write_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    def _reset_state(self) -> None:
        """Reset all connection-related state to initial values."""
        self._read_task = self._write_task = self._stderr_task = None
        self._stdout_queue = self._stdin_queue = None
        self._read_adapter = self._write_adapter = None
        self._session = None  # Skip __aexit__ to avoid anyio cancel scope issues

    async def _cleanup_transport(self) -> None:
        """Clean up HTTP transport if present."""
        if self._transport:
            try:
                await self._transport.__aexit__(None, None, None)
            except Exception:
                pass
            self._transport = None

    async def _terminate_owned_process(self) -> None:
        """Gracefully terminate the child process if we own it."""
        if not self._process or not self._owns_process:
            return
        try:
            if self._process.returncode is None:
                # Close stdin to signal EOF to the wrapper
                if self._process.stdin:
                    try:
                        self._process.stdin.close()
                    except Exception:
                        pass

                # Wait briefly for graceful exit, then escalate
                try:
                    await asyncio.wait_for(self._process.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    self._process.terminate()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        self._process.kill()
                        await self._process.wait()
        except ProcessLookupError:
            pass
        self._process = None

    @staticmethod
    async def is_wrapper_running(url: str = DEFAULT_WRAPPER_URL) -> bool:
        """Check if the slife-mcp wrapper is already running.

        Probes the /mcp endpoint — any HTTP response (even an error)
        means the server is listening. Only a connection failure means
        the server is not running.

        Uses a short timeout (0.5s) since this is a localhost probe.
        """
        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                resp = await client.get(url)
                return resp.status_code < 500
        except Exception:
            return False

    async def list_tools(self) -> list[dict]:
        self._ensure_connected()
        result = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        self._ensure_connected()
        args = arguments or {}
        result = await self._session.call_tool(name, args)
        parts: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)
            elif hasattr(block, "data"):
                parts.append(f"[binary data: {len(block.data)} bytes]")
            else:
                parts.append(str(block))
        return "\n".join(parts)

    async def ping(self) -> bool:
        try:
            await self._session.send_ping()
            return True
        except Exception:
            return False

    def _ensure_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError("MCP client is not connected.")
