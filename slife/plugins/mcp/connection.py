"""MCP connection pool — persistent connections to external MCP servers.

Supports two transports:
  - stdio: spawn server as subprocess, raw JSON-RPC over pipes
  - http:  POST JSON-RPC to a Streamable HTTP MCP endpoint

Avoids anyio and ClientSession entirely to prevent TaskGroup conflicts
with FastMCP.
"""

import asyncio
import json
import logging
import os
import subprocess as _subprocess
import time as _time
from dataclasses import dataclass, field
from enum import Enum

import httpx

from slife.platform import resolve_command, terminate_process

logger = logging.getLogger(__name__)

# Pattern for embedded ${VAR} references in arg strings
import re as _re
_ENV_REF = _re.compile(r"\$\{(\w+)\}")


def _is_env_ref(value: str) -> bool:
    """True if value is a pure ``${VAR}`` reference (no surrounding text)."""
    return bool(_ENV_REF.fullmatch(value))


def _resolve_embedded_refs(value: str) -> str:
    """Resolve embedded ``${VAR}`` refs through os.environ → credstore."""
    from slife.config import _try_credstore_lookup

    def _replace(m):
        var = m.group(1)
        env_val = os.environ.get(var)
        if env_val:
            return env_val
        cred_val = _try_credstore_lookup(var)
        if cred_val:
            return cred_val
        return m.group(0)  # unresolved — leave as-is
    return _ENV_REF.sub(_replace, value)


class ServerStatus(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    FAILED = "failed"


@dataclass
class ServerConfig:
    name: str
    command: str = ""                       # stdio: executable to spawn
    args: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None
    url: str = ""                           # http: MCP endpoint URL
    headers: dict[str, str] | None = None   # http: extra request headers
    enabled: bool = True  # False = don't auto-connect at startup
    description: str = ""
    active: bool = True  # False = connected but tools not disclosed yet

    @property
    def transport(self) -> str:
        """Return the transport mode: 'http' or 'stdio'."""
        return "http" if self.url else "stdio"


class MCPServerConnection:
    """Persistent MCP client connection using raw JSON-RPC.

    Supports two transports:
      - stdio: spawn server as subprocess, JSON-RPC over pipes
      - http:  POST JSON-RPC to a Streamable HTTP MCP endpoint

    No ClientSession, no anyio, no TaskGroup conflicts.
    """

    def __init__(self, config: ServerConfig):
        self.config = config
        self._status = ServerStatus.DISCONNECTED
        self._active = config.active
        self._process: asyncio.subprocess.Process | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._session_id: str | None = None
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
            logger.info("mcp_already_connected server=%s", self.config.name)
            return

        self._status = ServerStatus.CONNECTING
        self._error = None
        self._stderr_buffer.clear()
        t0 = _time.monotonic()
        transport = self.config.transport
        logger.info(
            "mcp_connect server=%s transport=%s",
            self.config.name, transport,
        )

        try:
            if transport == "stdio":
                await self._connect_stdio()
            else:
                await self._connect_http()

            # MCP initialize handshake (transport-agnostic)
            init_result = await self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "slife-mcp", "version": "0.1.0"},
            })

            server_info = init_result.get("serverInfo", {})
            logger.debug(
                "mcp_initialized server=%s remote=%s ver=%s",
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
            elapsed = (_time.monotonic() - t0) * 1000
            logger.info(
                "mcp_connected server=%s tools=%d took_ms=%.0f",
                self.config.name, len(self._tools_cache), elapsed,
            )

            # Run post-connect setup (best-effort, never blocks on failure)
            await self._post_connect_setup()

        except Exception as e:
            self._status = ServerStatus.FAILED
            stderr_tail = "".join(self._stderr_buffer[-20:]).strip()
            if stderr_tail:
                self._error = f"{e}\n\n[server stderr]\n{stderr_tail}"
            else:
                self._error = str(e)
            logger.error("mcp_connect_failed server=%s err=%s", self.config.name, e)
            await self._cleanup_resources()

    async def _connect_stdio(self) -> None:
        """Spawn server as subprocess and set up pipe I/O."""
        from slife.config import _resolve_env_or_credstore

        exe = resolve_command(self.config.command)
        env = dict(os.environ)
        if self.config.env:
            for key, value in self.config.env.items():
                env[key] = _resolve_env_or_credstore(value)

        # Resolve ${VAR} references in args (e.g. "Authorization: Bearer ${GITHUB_TOKEN}")
        resolved_args = [
            _resolve_env_or_credstore(arg) if _is_env_ref(arg)
            else _resolve_embedded_refs(arg)
            for arg in self.config.args
        ]

        self._process = await asyncio.create_subprocess_exec(
            exe, *resolved_args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env or None,
        )
        self._stderr_task = asyncio.create_task(self._drain_stderr())

    async def _connect_http(self) -> None:
        """Create HTTP client for Streamable HTTP transport."""
        base_headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.config.headers:
            base_headers.update(self.config.headers)

        self._http_client = httpx.AsyncClient(
            headers=base_headers,
            timeout=httpx.Timeout(30.0),
        )

    async def _post_connect_setup(self) -> None:
        """Run server-specific post-connect setup (best-effort).

        On Windows, the ``mcp-server-fetch`` package's ``readabilipy``
        dependency cannot detect ``npm`` because Python's ``subprocess.run``
        on Windows only tries ``.exe`` extensions via ``CreateProcess``,
        and ``npm`` only ships as ``npm.cmd``.

        Pre-installing the ``node_modules`` into ``readabilipy``'s
        ``javascript`` directory lets ``have_node()`` succeed without
        ever calling ``have_npm()``, sidestepping the detection bug.
        """
        if self.config.name != "fetch":
            return

        try:
            # Locate readabilipy inside the uvx-managed environment
            result = _subprocess.run(
                [
                    "uvx", "--from", "mcp-server-fetch", "python", "-c",
                    "import readabilipy, os; print(os.path.dirname(readabilipy.__file__))",
                ],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                return
            readabilipy_dir = result.stdout.strip()
            if not readabilipy_dir or not os.path.isdir(readabilipy_dir):
                return

            jsdir = os.path.join(readabilipy_dir, "javascript")
            if not os.path.isdir(jsdir):
                return

            # Already installed — nothing to do
            if os.path.isdir(os.path.join(jsdir, "node_modules")):
                logger.debug("readabilipy node_modules already present")
                return

            logger.info(
                "fetch_npm_install jsdir=%s", jsdir,
            )
            npm_cmd = ["cmd", "/c", "npm", "install"]
            install = _subprocess.run(
                npm_cmd, cwd=jsdir,
                capture_output=True, text=True, timeout=60,
            )
            if install.returncode == 0:
                logger.info("fetch_npm_install_done")
            else:
                logger.debug(
                    "fetch_npm_install_fail rc=%d stderr=%s",
                    install.returncode, install.stderr[:200],
                )
        except Exception:
            # Best-effort — never let setup failure block the connection
            pass

    async def _request(self, method: str, params: dict) -> dict:
        """Send a JSON-RPC request and wait for the response."""
        async with self._lock:
            self._next_id += 1
            req_id = self._next_id

            request = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": params,
            }

            if self.config.transport == "stdio":
                return await self._request_stdio(request, req_id)
            else:
                return await self._request_http(request)

    async def _request_stdio(self, request: dict, req_id: int) -> dict:
        """Send JSON-RPC over subprocess pipes and wait for matching response."""
        assert self._process and self._process.stdin and self._process.stdout
        line = json.dumps(request, ensure_ascii=False) + "\n"
        self._process.stdin.write(line.encode("utf-8"))
        await self._process.stdin.drain()

        while True:
            resp_line = await self._process.stdout.readline()
            if not resp_line:
                raise ConnectionError(f"Server '{self.config.name}' closed connection")

            try:
                response = json.loads(resp_line.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                logger.debug("mcp_invalid_json server=%s line=%.100s", self.config.name, resp_line)
                continue

            if response.get("id") == req_id:
                if "error" in response:
                    raise Exception(
                        f"MCP error from '{self.config.name}': {response['error']}"
                    )
                return response.get("result", {})

    async def _request_http(self, request: dict) -> dict:
        """Send JSON-RPC via HTTP POST and parse the response."""
        assert self._http_client is not None

        headers = {}
        if self._session_id:
            headers["mcp-session-id"] = self._session_id

        try:
            resp = await self._http_client.post(
                self.config.url, json=request, headers=headers,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise ConnectionError(
                f"HTTP error from '{self.config.name}': {e}"
            ) from e

        # Extract session ID from response header (first initialize response)
        sid = resp.headers.get("mcp-session-id")
        if sid and not self._session_id:
            self._session_id = sid

        try:
            response = resp.json()
        except ValueError as e:
            raise ConnectionError(
                f"Invalid JSON from '{self.config.name}': {e}"
            ) from e

        if "error" in response:
            raise Exception(
                f"MCP error from '{self.config.name}': {response['error']}"
            )
        return response.get("result", {})

    def _notify(self, method: str, params: dict) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        if self.config.transport == "stdio":
            assert self._process and self._process.stdin
            line = json.dumps(notification, ensure_ascii=False) + "\n"
            self._process.stdin.write(line.encode("utf-8"))
        else:
            assert self._http_client is not None
            headers = {}
            if self._session_id:
                headers["mcp-session-id"] = self._session_id
            asyncio.create_task(
                self._http_client.post(
                    self.config.url, json=notification, headers=headers,
                )
            )

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
                    logger.debug("mcp_stderr server=%s line=%s", self.config.name, text)
        except asyncio.CancelledError:
            pass

    async def disconnect(self) -> None:
        logger.info("mcp_disconnect server=%s", self.config.name)
        await self._cleanup_resources()
        self._status = ServerStatus.DISCONNECTED
        self._tools_cache = []
        self._session_id = None
        logger.info("mcp_disconnected server=%s", self.config.name)

    async def _cleanup_resources(self) -> None:
        # -- stdio cleanup --
        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except asyncio.CancelledError:
                pass
        self._stderr_task = None

        if self._process and self._process.stdin:
            try:
                self._process.stdin.write(b'')
                await self._process.stdin.drain()
            except Exception:
                pass

        await terminate_process(self._process, label=f"mcp_conn:{self.config.name}")
        self._process = None

        # -- http cleanup --
        if self._http_client is not None:
            # Best-effort session termination
            if self._session_id:
                try:
                    await self._http_client.delete(
                        self.config.url,
                        headers={"mcp-session-id": self._session_id},
                    )
                except Exception:
                    pass
            await self._http_client.aclose()
            self._http_client = None

    def list_tools(self) -> list[dict]:
        return list(self._tools_cache)

    async def call_tool(self, tool_name: str, arguments: dict) -> str:
        if self._status != ServerStatus.CONNECTED:
            raise ValueError(
                f"Server '{self.config.name}' is not connected (status: {self._status.value})"
            )

        logger.debug("mcp_tool_call server=%s tool=%s", self.config.name, tool_name)
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
            logger.info("mcp_replace server=%s", config.name)
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
                "name": name,
                "state": "running" if conn.status == ServerStatus.CONNECTED else "stopped",
                "status": conn.status.value,
                "enabled": conn.config.enabled,
                "tool_count": conn.tool_count, "error": conn.error,
                "transport": conn.config.transport,
                "command": conn.config.command, "args": conn.config.args,
                "url": conn.config.url,
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
            logger.error("mcp_tool_call_failed server=%s tool=%s err=%s", server_name, tool_name, e)
            return f"Error calling '{tool_name}' on '{server_name}': {e}"

    async def shutdown(self) -> None:
        logger.info("mcp_shutdown servers=%d", len(self._connections))
        for name in list(self._connections.keys()):
            await self.remove_server(name)
        logger.info("mcp_shutdown_done")
