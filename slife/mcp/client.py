"""MCP client — connects to MCP servers via Streamable HTTP transport.

Uses ``mcp.client.streamable_http.streamablehttp_client`` for the
transport layer and ``mcp.ClientSession`` for the MCP protocol,
managed via ``contextlib.AsyncExitStack`` for correct async-context
nesting.
"""

import asyncio
import logging
from contextlib import AsyncExitStack
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

logger = logging.getLogger(__name__)

_MCP_INIT_TIMEOUT = 10.0
# Retry window: server prints port signal BEFORE uvicorn starts listening,
# so the client may need a few attempts before the socket accepts.
_CONNECT_RETRY_DELAY = 0.1
_CONNECT_RETRY_ATTEMPTS = 30  # 3 seconds total


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

        logger.info("mcp_client_connect transport=streamable-http url=%s", url)

        last_err = None
        for attempt in range(_CONNECT_RETRY_ATTEMPTS):
            try:
                self._exit_stack = AsyncExitStack()
                read_stream, write_stream, _ = await self._exit_stack.enter_async_context(
                    streamablehttp_client(url),
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
            "mcp_client_connected transport=streamable-http url=%s attempts=%d",
            url, attempt + 1,
        )

    async def disconnect(self) -> None:
        """Disconnect from the MCP server and release all resources."""
        self._connected = False
        await self._cleanup()
        logger.info("mcp_client_disconnected")

    async def _cleanup(self) -> None:
        """Close the exit stack, properly exiting all nested contexts.

        The ``streamablehttp_client`` async generator from the MCP library
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
        self._ensure_connected()
        assert self._session is not None  # post-condition of _ensure_connected
        result = await self._session.list_tools()
        return [
            {"name": t.name, "description": t.description or "", "inputSchema": t.inputSchema}
            for t in result.tools
        ]

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        """Call an MCP tool with a configurable timeout.

        Returns the result text on success, or an ``"Error: …"`` string
        on failure — this function NEVER raises, so a single hung MCP
        server can't stall the entire agent loop.  The LLM sees the
        error as a normal tool result and can retry or report it.
        """
        self._ensure_connected()
        assert self._session is not None  # post-condition of _ensure_connected
        args = arguments or {}
        try:
            result = await asyncio.wait_for(
                self._session.call_tool(name, args),
                timeout=self._tool_timeout,
            )
        except asyncio.TimeoutError:
            msg = (
                f"工具 '{name}' 执行超时（{self._tool_timeout}s）。"
                f"MCP 服务器未在规定时间内返回结果，请检查服务器状态或网络连接。"
            )
            logger.warning("mcp_tool_timeout name=%s timeout=%ds", name, self._tool_timeout)
            return f"Error: {msg}"
        except Exception as e:
            msg = (
                f"工具 '{name}' 执行失败：{type(e).__name__}: {e}。"
                f"请检查 MCP 服务器状态。"
            )
            logger.warning("mcp_tool_error name=%s err=%s", name, e)
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
