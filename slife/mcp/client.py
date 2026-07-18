"""MCP client — connects to MCP servers via stdio (child process) transport.

Uses asyncio subprocess + asyncio.Queue adapters + ClientSession.
"""

import asyncio
import logging
from typing import Any

from mcp import ClientSession, types
from mcp.shared.message import SessionMessage

from slife.platform import terminate_process

logger = logging.getLogger(__name__)

# Timeout for MCP initialize handshake — prevents hanging if the
# child process crashes before starting its MCP server loop.
_MCP_INIT_TIMEOUT = 10.0

# ── Stream-closed sentinel ──────────────────────────────────────────
# Pushed into the stdout queue when the child process stdout pipe closes
# unexpectedly, to unblock any consumer waiting on the queue.


class _StreamClosed(Exception):
    """Raised when the child process stdout stream closes unexpectedly."""
    pass


_STREAM_CLOSED = _StreamClosed()


# ── asyncio.Queue adapters — implement anyio stream protocol on asyncio primitives ──

class _ReadAdapter:
    """Wraps asyncio.Queue for ClientSession read stream."""

    def __init__(self, queue: asyncio.Queue, client: "MCPClient"):
        self._queue = queue
        self._client = client

    async def receive(self):
        item = await self._queue.get()
        if isinstance(item, _StreamClosed):
            # The MCP session's _receive_loop logs every Exception as
            # "Unhandled exception in receive loop" to stderr.  During
            # shutdown the child process exits before disconnect() runs,
            # so EOF is expected.  Raise StopAsyncIteration so the
            # receive loop terminates cleanly without noise.
            raise StopAsyncIteration(
                "MCP child process stdout stream closed"
            )
        return item

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

    async def _bridge_lines(self, reader, label: str = "stdout") -> None:
        """Read JSON-RPC lines from a stream reader and push to stdout queue.

        Shared by _bridge_stdout (subprocess) and _bridge_reader (streams).
        """
        assert self._stdout_queue
        # Allow lines up to 10 MB — tool schemas can be large
        reader._limit = 10 * 1024 * 1024
        try:
            while True:
                line = await reader.readline()
                if not line:
                    # EOF — child process stdout closed.
                    # During startup (before MCP handshake completes) or
                    # during operation, this is unexpected → warn + signal.
                    # During shutdown (disconnect() already called) → silent.
                    if self._connected:
                        logger.warning("mcp_bridge_eof source=%s", label)
                        await self._stdout_queue.put(_STREAM_CLOSED)
                    break
                line_str = line.decode("utf-8", errors="replace").strip()
                if not line_str:
                    continue
                try:
                    message = types.JSONRPCMessage.model_validate_json(line_str)
                    await self._stdout_queue.put(SessionMessage(message))
                except Exception:
                    logger.debug(
                        "mcp_bridge_parse_fail source=%s line=%.200s",
                        label, line_str,
                    )
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("mcp_bridge_crashed source=%s", label, exc_info=True)
            await self._stdout_queue.put(_STREAM_CLOSED)

    async def _bridge_reader(self, reader) -> None:
        """Bridge from a raw asyncio StreamReader to the stdout queue."""
        await self._bridge_lines(reader, label="reader")

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
        except (ValueError, OSError, BrokenPipeError):
            # Pipe closed during shutdown
            pass

    async def connect_streams(self, read_stream, write_stream) -> None:
        if self._connected:
            logger.warning("mcp_client_already_connected")
            return
        logger.info("mcp_client_connect transport=streams")

        self._stdout_queue = asyncio.Queue()
        self._stdin_queue = asyncio.Queue()
        self._read_adapter = _ReadAdapter(self._stdout_queue, self)
        self._write_adapter = _WriteAdapter(self._stdin_queue)

        self._read_task = asyncio.create_task(self._bridge_reader(read_stream))
        self._write_task = asyncio.create_task(self._bridge_writer(write_stream))

        try:
            self._session = ClientSession(self._read_adapter, self._write_adapter)
            await self._session.__aenter__()
            await asyncio.wait_for(
                self._session.initialize(), timeout=_MCP_INIT_TIMEOUT,
            )
        except asyncio.TimeoutError:
            raise asyncio.TimeoutError(
                f"MCP initialize timed out after {_MCP_INIT_TIMEOUT}s — "
                f"child process may have crashed during startup"
            )
        except Exception:
            if self._session:
                try:
                    await self._session.__aexit__(None, None, None)
                except Exception:
                    pass
            raise

        self._connected = True
        self._owns_process = False
        logger.info("mcp_client_connected transport=streams")

    async def disconnect(self) -> None:
        self._connected = False
        await self._cancel_bridge_tasks()
        self._reset_state()
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

    async def _terminate_owned_process(self) -> None:
        """Gracefully terminate the child process if we own it."""
        if not self._process or not self._owns_process:
            return
        await terminate_process(self._process, label="mcp_client")
        self._process = None

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
