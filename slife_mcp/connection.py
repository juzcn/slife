"""MCP connection pool — persistent connections to external MCP servers.

Uses raw JSON-RPC over asyncio subprocess pipes. Avoids anyio and
ClientSession entirely to prevent TaskGroup conflicts with FastMCP.
"""

import asyncio
import json
import logging
import shutil
import sys
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ServerStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


def _resolve_command(command: str) -> str:
    if sys.platform == "win32" and not command.lower().endswith((".exe", ".cmd", ".bat")):
        resolved = shutil.which(command) or shutil.which(command + ".cmd") or shutil.which(command + ".exe")
        if resolved:
            return resolved
    return command


@dataclass
class ServerConfig:
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    description: str = ""
    active: bool = True  # False = connected but tools not disclosed yet


class MCPServerConnection:
    """Persistent MCP client connection using raw JSON-RPC over pipes.

    Spawns the server via asyncio subprocess. Sends/receives JSON-RPC
    directly — no ClientSession, no anyio, no TaskGroup conflicts.
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self._status = ServerStatus.DISCONNECTED
        self._active = config.active
        self._process: asyncio.subprocess.Process | None = None
        self._next_id: int = 0
        self._lock = asyncio.Lock()
        self._tools_cache: list[dict] = []
        self._error: str | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_buffer: list[str] = []

    @property
    def status(self) -> ServerStatus:
        return self._status

    @property
    def active(self) -> bool:
        return self._active

    @property
    def tool_count(self) -> int:
        return len(self._tools_cache)

    @property
    def error(self) -> str | None:
        return self._error

    def set_active(self, value: bool) -> None:
        """Toggle whether this server's tools are disclosed."""
        self._active = value

    async def connect(self) -> None:
        if self._status == ServerStatus.CONNECTED:
            logger.info("Server '%s' already connected.", self.config.name)
            return

        self._status = ServerStatus.CONNECTING
        self._error = None
        self._stderr_buffer.clear()
        logger.info(
            "Connecting to MCP server '%s': %s %s",
            self.config.name, self.config.command, " ".join(self.config.args),
        )

        try:
            exe = _resolve_command(self.config.command)

            # Build environment
            import os as _os
            env = dict(_os.environ)
            if self.config.env:
                env.update(self.config.env)

            self._process = await asyncio.create_subprocess_exec(
                exe, *self.config.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env or None,
            )

            self._stderr_task = asyncio.create_task(self._drain_stderr())

            # MCP initialize handshake
            init_result = await self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "slife-mcp", "version": "0.1.0"},
            })

            # Validate protocol version
            server_info = init_result.get("serverInfo", {})
            logger.info(
                "Server '%s' initialized: %s %s",
                self.config.name,
                server_info.get("name", "unknown"),
                server_info.get("version", ""),
            )

            # Send initialized notification
            self._notify("notifications/initialized", {})

            # Discover tools
            tools_result = await self._request("tools/list", {})
            self._tools_cache = [
                {
                    "name": t.get("name", ""),
                    "description": t.get("description", ""),
                    "inputSchema": t.get("inputSchema", {"type": "object", "properties": {}}),
                }
                for t in tools_result.get("tools", [])
            ]

            self._status = ServerStatus.CONNECTED
            logger.info(
                "Server '%s' connected — %d tools discovered.",
                self.config.name, len(self._tools_cache),
            )

        except Exception as e:
            self._status = ServerStatus.FAILED
            # Include stderr output in the error so the LLM can understand
            # why the server failed (e.g. missing required arguments).
            stderr_tail = "".join(self._stderr_buffer[-20:]).strip()
            if stderr_tail:
                self._error = f"{e}\n\n[server stderr]\n{stderr_tail}"
            else:
                self._error = str(e)
            logger.error("Failed to connect to '%s': %s", self.config.name, e)
            await self._cleanup_resources()

    async def _request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        assert self._process and self._process.stdin and self._process.stdout

        async with self._lock:
            self._next_id += 1
            req_id = self._next_id

            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }
            line = json.dumps(request, ensure_ascii=False) + "\n"
            self._process.stdin.write(line.encode("utf-8"))
            await self._process.stdin.drain()

            # Read responses until we get one with matching id
            while True:
                resp_line = await self._process.stdout.readline()
                if not resp_line:
                    raise ConnectionError(f"Server '{self.config.name}' closed connection")

                try:
                    response = json.loads(resp_line.decode("utf-8", errors="replace"))
                except json.JSONDecodeError:
                    logger.debug("Invalid JSON from '%s': %.100s", self.config.name, resp_line)
                    continue

                if response.get("id") == req_id:
                    if "error" in response:
                        raise Exception(
                            f"MCP error from '{self.config.name}': {response['error']}"
                        )
                    return response.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        assert self._process and self._process.stdin
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        line = json.dumps(notification, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))

    async def _drain_stderr(self) -> None:
        assert self._process and self._process.stderr
        try:
            while True:
                line = await self._process.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    self._stderr_buffer.append(text + "\n")
                    logger.debug("[%s stderr] %s", self.config.name, text)
        except asyncio.CancelledError:
            pass

    async def disconnect(self) -> None:
        logger.info("Disconnecting from '%s'...", self.config.name)
        await self._cleanup_resources()
        self._status = ServerStatus.DISCONNECTED
        self._tools_cache = []
        logger.info("Server '%s' disconnected.", self.config.name)

    async def _cleanup_resources(self) -> None:
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None

        # Notify drain
        if self._process and self._process.stdin:
            try:
                self._process.stdin.write(b'')
                await self._process.stdin.drain()
            except Exception:
                pass

        if self._process:
            try:
                if self._process.returncode is None:
                    self._process.terminate()
                    try:
                        await asyncio.wait_for(self._process.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        self._process.kill()
                        await self._process.wait()
            except ProcessLookupError:
                pass
            self._process = None

    def list_tools(self) -> list[dict]:
        return list(self._tools_cache)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        if self._status != ServerStatus.CONNECTED or self._process is None:
            raise ValueError(
                f"Server '{self.config.name}' is not connected (status: {self._status.value})"
            )

        logger.debug("Calling tool '%s' on '%s'", tool_name, self.config.name)
        result = await self._request("tools/call", {
            "name": tool_name,
            "arguments": arguments,
        })

        # Format content blocks
        parts: list[str] = []
        for block in result.get("content", []):
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "resource":
                parts.append(f"[resource: {block.get('resource', {})}]")
            else:
                parts.append(json.dumps(block))
        return "\n".join(parts) if parts else json.dumps(result)


class ConnectionPool:
    """Manages a collection of MCP server connections."""

    def __init__(self):
        self._connections: dict[str, MCPServerConnection] = {}

    async def add_server(self, config: ServerConfig) -> MCPServerConnection:
        if config.name in self._connections:
            logger.info("Replacing existing server '%s'...", config.name)
            await self.remove_server(config.name)
        conn = MCPServerConnection(config=config)
        self._connections[config.name] = conn
        await conn.connect()
        return conn

    async def remove_server(self, name: str) -> None:
        conn = self._connections.pop(name, None)
        if conn is None:
            return
        await conn.disconnect()

    def get_server(self, name: str) -> MCPServerConnection | None:
        return self._connections.get(name)

    def list_servers(self) -> list[dict]:
        return [
            {
                "name": name, "status": conn.status.value,
                "tool_count": conn.tool_count, "error": conn.error,
                "command": conn.config.command, "args": conn.config.args,
                "description": conn.config.description,
                "active": conn.active,
            }
            for name, conn in self._connections.items()
        ]

    def list_all_tools(self, server_name: str) -> list[dict]:
        """List all tools from a specific server, regardless of active state."""
        conn = self._connections.get(server_name)
        if conn is None or conn.status != ServerStatus.CONNECTED:
            return []
        return [
            {**tool, "server": server_name, "full_name": f"{server_name}__{tool['name']}"}
            for tool in conn.list_tools()
        ]

    async def activate_server(self, name: str) -> dict:
        """Activate a connected-but-inactive server and return its tools.

        Returns:
            dict with status, server, tool_count, tools list.
        """
        conn = self._connections.get(name)
        if conn is None:
            return {"status": "error", "server": name, "error": f"Server '{name}' not found."}
        if conn.status != ServerStatus.CONNECTED:
            return {"status": "error", "server": name, "error": f"Server '{name}' is not connected (status: {conn.status.value})."}
        if conn.active:
            tools = conn.list_tools()
            return {
                "status": "already_active",
                "server": name,
                "tool_count": len(tools),
                "tools": [t["name"] for t in tools],
            }
        conn.set_active(True)
        tools = conn.list_tools()
        return {
            "status": "activated",
            "server": name,
            "tool_count": len(tools),
            "tools": [t["name"] for t in tools],
        }

    def check_server(self, name: str) -> dict:
        """Return status snapshot for a single server.

        Returns:
            dict with name, status, active, tool_count, description, error.
        """
        conn = self._connections.get(name)
        if conn is None:
            return {"name": name, "status": "not_found"}
        return {
            "name": name,
            "status": conn.status.value,
            "active": conn.active,
            "tool_count": conn.tool_count,
            "description": conn.config.description,
            "error": conn.error,
        }

    async def deactivate_server(self, name: str) -> dict:
        """Deactivate a connected server, hiding its tools from discovery.

        Returns:
            dict with status, server, tool_count.
        """
        conn = self._connections.get(name)
        if conn is None:
            return {"status": "error", "server": name, "error": f"Server '{name}' not found."}
        if not conn.active:
            return {"status": "already_inactive", "server": name, "tool_count": conn.tool_count}
        conn.set_active(False)
        return {"status": "deactivated", "server": name, "tool_count": conn.tool_count}

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict) -> str:
        conn = self._connections.get(server_name)
        if conn is None:
            return f"Error: Server '{server_name}' not found."
        try:
            return await conn.call_tool(tool_name, arguments)
        except Exception as e:
            logger.error("Tool call failed: %s/%s: %s", server_name, tool_name, e)
            return f"Error calling '{tool_name}' on '{server_name}': {e}"

    async def shutdown(self) -> None:
        logger.info("Shutting down connection pool (%d servers)...", len(self._connections))
        for name in list(self._connections.keys()):
            await self.remove_server(name)
        logger.info("Connection pool shut down.")
