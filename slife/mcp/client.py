"""MCP client — connects to MCP servers via Streamable HTTP transport.

Uses ``mcp.client.streamable_http.streamable_http_client`` for the
transport layer and ``mcp.ClientSession`` for the MCP protocol,
managed via ``contextlib.AsyncExitStack`` for correct async-context
nesting.
"""

import asyncio
import logging
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

logger = logging.getLogger(__name__)

_MCP_INIT_TIMEOUT = 10.0
# Retry window: server prints port signal BEFORE uvicorn starts listening,
# so the client may need a few attempts before the socket accepts.
_CONNECT_RETRY_DELAY = 0.1
_CONNECT_RETRY_ATTEMPTS = 30  # 3 seconds total


# ── Binary → temp file helper ──────────────────────────────────────

# Image file magic bytes for format detection
_IMAGE_MAGIC: dict[bytes, str] = {
    b"\x89PNG\r\n\x1a\n": ".png",
    b"\xff\xd8\xff": ".jpg",
    b"GIF87a": ".gif",
    b"GIF89a": ".gif",
    b"RIFF": ".webp",       # RIFF....WEBP — checked separately below
    b"BM": ".bmp",
}


def _guess_image_extension(data: bytes) -> str | None:
    """Detect image format from magic bytes, returning e.g. ``".png"``."""
    for magic, ext in _IMAGE_MAGIC.items():
        if data[:len(magic)] == magic:
            if ext == ".webp" and data[8:12] != b"WEBP":
                continue
            return ext
    return None


def _try_save_image_bytes(data: bytes) -> str | None:
    """Save *data* to a temp file if it looks like an image.

    Returns the absolute path, or ``None`` if the data is not a
    recognised image format or saving fails.
    """
    ext = _guess_image_extension(data)
    if ext is None:
        return None
    try:
        tmp = tempfile.NamedTemporaryFile(
            suffix=ext, delete=False, dir=tempfile.gettempdir(),
        )
        tmp.write(data)
        tmp.close()
        return str(Path(tmp.name).resolve())
    except Exception:
        return None


class MCPClient:
    """MCP client for connecting to Slife plugin servers via Streamable HTTP."""

    def __init__(self, tool_timeout: float = 60.0):
        self._session: ClientSession | None = None
        self._connected: bool = False
        self._exit_stack: AsyncExitStack | None = None
        self._tool_timeout = tool_timeout

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self, url: str) -> None:
        """Connect to an MCP server via Streamable HTTP transport.

        Retries on connection failure — the server may still be starting
        (the port signal is sent before uvicorn begins accepting).
        """
        if self._connected:
            logger.warning("mcp_client_already_connected")
            return

        logger.info("mcp_client_connect transport=%s url=%s", "streamable-http", url)

        last_err = None
        attempt: int = -1
        for attempt in range(_CONNECT_RETRY_ATTEMPTS):
            try:
                self._exit_stack = AsyncExitStack()
                read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                    streamable_http_client(url),
                )
                self._session = await self._exit_stack.enter_async_context(
                    ClientSession(read_stream, write_stream),
                )
                await asyncio.wait_for(
                    self._session.initialize(), timeout=_MCP_INIT_TIMEOUT,
                )
                break  # success
            except (
                ConnectionError,
                OSError,
                asyncio.TimeoutError,
                asyncio.CancelledError,
            ) as e:
                last_err = e
                await self._cleanup()
                if attempt < _CONNECT_RETRY_ATTEMPTS - 1:
                    await asyncio.sleep(_CONNECT_RETRY_DELAY)
            except Exception:
                await self._cleanup()
                raise

        if not self._session:
            raise ConnectionError(
                f"Failed to connect to {url} after "
                f"{_CONNECT_RETRY_ATTEMPTS} attempts: {last_err}"
            )

        self._connected = True
        logger.info(
            "mcp_client_connected transport=%s url=%s attempts=%d",
            "streamable-http", url, attempt + 1,
        )

    async def disconnect(self) -> None:
        """Disconnect from the MCP server and release all resources."""
        self._connected = False
        await self._cleanup()
        logger.info("mcp_client_disconnected")

    async def _cleanup(self) -> None:
        """Close the exit stack, properly exiting all nested contexts.

        The ``streamable_http_client`` async generator from the MCP library
        uses ``anyio.create_task_group()`` internally.  When the connection
        fails during setup (before ``session.initialize()`` succeeds), the
        TaskGroup's cancel-scope cleanup can raise ``BaseExceptionGroup``
        or ``RuntimeError`` (task mismatch) — both escape the bare
        ``except Exception`` and need to be swallowed explicitly.

        A zero-sleep after ``aclose()`` lets the event loop deliver any
        pending generator finalisation callbacks so they don't fire during
        garbage collection and crash the process.
        """
        if self._exit_stack:
            try:
                await self._exit_stack.aclose()
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug("cleanup_cancel_scope_suppressed err=%s", e)
                else:
                    raise
            except (Exception, BaseExceptionGroup):
                pass
            # Give pending generator-finalisation callbacks a chance to run
            # in the current task instead of during GC.
            try:
                await asyncio.sleep(0)
            except Exception:
                pass
            self._exit_stack = None
        self._session = None

    async def list_tools(self) -> list[dict]:
        """Return tools from the connected MCP server.

        Wraps ``session.list_tools()`` with a timeout so a hung
        Streamable HTTP session can't block the caller indefinitely.
        On Windows / ProactorEventLoop, concurrent sessions to the
        same server may hang if the underlying SSE transport gets
        into a bad state — the timeout ensures we fail fast instead
        of blocking for 30+ seconds.
        """
        self._ensure_connected()
        assert self._session is not None  # post-condition of _ensure_connected
        # list_tools is a local Streamable HTTP call — should complete
        # in under 15s even with hundreds of tools.  Cap the timeout
        # so a stuck SSE session on a subagent doesn't outlive the
        # parent's 30s spawn timeout.
        list_timeout = min(self._tool_timeout, 20.0)
        try:
            result = await asyncio.wait_for(
                self._session.list_tools(),
                timeout=list_timeout,
            )
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"list_tools timed out after {list_timeout}s — "
                f"the MCP server may have a stuck SSE session"
            )
        return [
            {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call an MCP tool.

        Returns the result text on success, or an ``"Error: …"`` string
        on failure — this function NEVER raises, so a single hung MCP
        server can't stall the entire agent loop.  The LLM sees the
        error as a normal tool result and can retry or report it.

        Timeout enforcement is handled by the Agent Loop (``agent/loop.py``)
        via ``asyncio.wait_for`` — this method does NOT apply its own
        timeout, so per-call overrides (e.g. ``call_tool_with_timeout``)
        propagate correctly.
        """
        self._ensure_connected()
        assert self._session is not None  # post-condition of _ensure_connected
        args = arguments or {}
        try:
            result = await self._session.call_tool(name, args)
        except Exception as e:
            msg = (
                f"Tool '{name}' failed: {type(e).__name__}: {e}. "
                f"Check the MCP server status."
            )
            logger.info("mcp_tool_error name=%s err=%s", name, e)
            return f"Error: {msg}"

        if getattr(result, "isError", False):
            parts: list[str] = []
            for block in result.content:
                if hasattr(block, "text"):
                    parts.append(block.text)  # type: ignore[union-attr]
            return "Error: " + "\n".join(parts)

        parts: list[str] = []
        for block in result.content:
            if hasattr(block, "text"):
                parts.append(block.text)  # type: ignore[union-attr]
            elif hasattr(block, "data"):
                img_path = _try_save_image_bytes(block.data)  # type: ignore[union-attr]
                if img_path is not None:
                    parts.append(f"[image: {img_path}]")
                else:
                    parts.append(f"[binary data: {len(block.data)} bytes]")  # type: ignore[union-attr]
            else:
                parts.append(str(block))
        return "\n".join(parts)

    async def ping(self) -> bool:
        if self._session is None:
            return False
        try:
            await self._session.send_ping()
            return True
        except Exception:
            return False

    def _ensure_connected(self) -> None:
        if not self._connected or self._session is None:
            raise RuntimeError("MCP client is not connected.")
