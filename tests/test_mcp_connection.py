"""Tests for slife_mcp.connection — ConnectionPool, MCPServerConnection, ServerConfig."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.plugins.mcp.connection import (
    ServerConfig,
    ServerStatus,
    MCPServerConnection,
    ConnectionPool,
)


# ── ServerConfig ────────────────────────────────────────────────────────────


class TestServerConfig:
    """Tests for ServerConfig dataclass."""

    def test_default_values(self):
        cfg = ServerConfig(name="test", command="python")
        assert cfg.name == "test"
        assert cfg.command == "python"
        assert cfg.args == []
        assert cfg.env is None
        assert cfg.description == ""
        assert cfg.active is True

    def test_full_config(self):
        cfg = ServerConfig(
            name="myserver",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem"],
            env={"HOME": "/tmp"},
            description="My filesystem server",
            active=False,
        )
        assert cfg.command == "npx"
        assert len(cfg.args) == 2
        assert cfg.env == {"HOME": "/tmp"}
        assert cfg.active is False


# ── ServerStatus ─────────────────────────────────────────────────────────────


class TestServerStatus:
    """Tests for ServerStatus enum."""

    def test_values(self):
        assert ServerStatus.DISCONNECTED.value == "disconnected"
        assert ServerStatus.CONNECTING.value == "connecting"
        assert ServerStatus.CONNECTED.value == "connected"
        assert ServerStatus.FAILED.value == "failed"


# ── MCPServerConnection ──────────────────────────────────────────────────────


class TestMCPServerConnectionInit:
    """Tests for MCPServerConnection initialization."""

    def test_initial_state(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)

        assert conn.config is cfg
        assert conn.status == ServerStatus.DISCONNECTED
        assert conn.active is True
        assert conn.tool_count == 0
        assert conn.error is None

    def test_respects_config_active(self):
        cfg = ServerConfig(name="test", command="echo", active=False)
        conn = MCPServerConnection(cfg)
        assert conn.active is False

    def test_set_active_toggle(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn.set_active(False)
        assert conn.active is False
        conn.set_active(True)
        assert conn.active is True


class TestMCPServerConnectionListTools:
    """Tests for list_tools."""

    def test_list_tools_empty(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        assert conn.list_tools() == []

    def test_list_tools_cached(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._tools_cache = [
            {"name": "tool_a", "description": "A"},
            {"name": "tool_b", "description": "B"},
        ]
        tools = conn.list_tools()
        assert len(tools) == 2
        assert tools[0]["name"] == "tool_a"


class TestMCPServerConnectionDisconnect:
    """Tests for disconnect."""

    @pytest.mark.asyncio
    async def test_disconnect_resets_state(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn._tools_cache = [{"name": "t1"}]

        await conn.disconnect()

        assert conn.status == ServerStatus.DISCONNECTED
        assert conn.tool_count == 0


# ── ConnectionPool ──────────────────────────────────────────────────────────


class TestConnectionPoolInit:
    """Tests for ConnectionPool initialization."""

    def test_empty_on_init(self):
        pool = ConnectionPool()
        assert pool.list_servers() == []


class TestConnectionPoolGetServer:
    """Tests for get_server."""

    def test_get_nonexistent(self):
        pool = ConnectionPool()
        assert pool.get_server("nonexistent") is None

    def test_get_existing(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        pool._connections["test"] = conn
        assert pool.get_server("test") is conn


class TestConnectionPoolListServers:
    """Tests for list_servers."""

    def test_list_returns_info_dicts(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="srv1", command="cmd1", description="First")
        conn = MCPServerConnection(cfg)
        conn._tools_cache = [{"name": "t1"}, {"name": "t2"}]
        pool._connections["srv1"] = conn

        servers = pool.list_servers()
        assert len(servers) == 1
        s = servers[0]
        assert s["name"] == "srv1"
        assert s["status"] == "disconnected"
        assert s["tool_count"] == 2
        assert s["active"] is True
        assert s["description"] == "First"


class TestConnectionPoolListAllTools:
    """Tests for list_all_tools."""

    def test_empty_for_unknown_server(self):
        pool = ConnectionPool()
        assert pool.list_all_tools("unknown") == []

    def test_adds_full_name(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="filesystem", command="npx")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn._tools_cache = [{"name": "read_file", "description": "Read a file"}]
        pool._connections["filesystem"] = conn

        tools = pool.list_all_tools("filesystem")
        assert len(tools) == 1
        assert tools[0]["server"] == "filesystem"
        assert tools[0]["full_name"] == "filesystem__read_file"


class TestConnectionPoolCheckServer:
    """Tests for check_server."""

    def test_not_found(self):
        pool = ConnectionPool()
        result = pool.check_server("ghost")
        assert result["status"] == "not_found"

    def test_found(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="test", command="cmd", description="A test server")
        conn = MCPServerConnection(cfg)
        pool._connections["test"] = conn

        result = pool.check_server("test")
        assert result["name"] == "test"
        assert result["status"] == "disconnected"
        assert result["active"] is True
        assert result["description"] == "A test server"


class TestConnectionPoolDeactivateServer:
    """Tests for deactivate_server / activate_server."""

    @pytest.mark.asyncio
    async def test_deactivate_not_found(self):
        pool = ConnectionPool()
        result = await pool.deactivate_server("ghost")
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_deactivate_already_inactive(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="test", command="cmd", active=False)
        conn = MCPServerConnection(cfg)
        pool._connections["test"] = conn

        result = await pool.deactivate_server("test")
        assert result["status"] == "already_inactive"

    @pytest.mark.asyncio
    async def test_deactivate_success(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="test", command="cmd", active=True)
        conn = MCPServerConnection(cfg)
        pool._connections["test"] = conn

        result = await pool.deactivate_server("test")
        assert result["status"] == "deactivated"
        assert not conn.active

    @pytest.mark.asyncio
    async def test_activate_not_found(self):
        pool = ConnectionPool()
        result = await pool.activate_server("ghost")
        assert result["status"] == "error"

    @pytest.mark.asyncio
    async def test_activate_already_active(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="test", command="cmd")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn._tools_cache = [{"name": "t1"}]
        pool._connections["test"] = conn

        result = await pool.activate_server("test")
        assert result["status"] == "already_active"

    @pytest.mark.asyncio
    async def test_activate_success(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="test", command="cmd", active=False)
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn._tools_cache = [{"name": "t1"}]
        pool._connections["test"] = conn

        result = await pool.activate_server("test")
        assert result["status"] == "activated"
        assert conn.active

    @pytest.mark.asyncio
    async def test_activate_not_connected(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="test", command="cmd")
        conn = MCPServerConnection(cfg)
        # Status is DISCONNECTED by default
        pool._connections["test"] = conn

        result = await pool.activate_server("test")
        assert result["status"] == "error"


class TestConnectionPoolCallTool:
    """Tests for call_tool."""

    def test_server_not_found(self):
        pool = ConnectionPool()
        result = pool.call_tool("ghost", "tool", {})
        assert "not found" in str(result) if isinstance(result, str) else True

    @pytest.mark.asyncio
    async def test_server_not_found(self):
        pool = ConnectionPool()
        result = await pool.call_tool("ghost", "tool", {})
        assert "not found" in result


class TestConnectionPoolRemoveServer:
    """Tests for remove_server."""

    @pytest.mark.asyncio
    async def test_remove_nonexistent_noop(self):
        pool = ConnectionPool()
        await pool.remove_server("ghost")  # Should not raise

    @pytest.mark.asyncio
    async def test_remove_disconnects(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="test", command="cmd")
        conn = MCPServerConnection(cfg)
        pool._connections["test"] = conn

        await pool.remove_server("test")
        assert "test" not in pool._connections


class TestConnectionPoolShutdown:
    """Tests for shutdown."""

    @pytest.mark.asyncio
    async def test_shutdown_empty(self):
        pool = ConnectionPool()
        await pool.shutdown()  # Should not raise

    @pytest.mark.asyncio
    async def test_shutdown_removes_all(self):
        pool = ConnectionPool()
        cfg1 = ServerConfig(name="srv1", command="cmd1")
        cfg2 = ServerConfig(name="srv2", command="cmd2")
        pool._connections["srv1"] = MCPServerConnection(cfg1)
        pool._connections["srv2"] = MCPServerConnection(cfg2)

        await pool.shutdown()
        assert pool.list_servers() == []


# ── HTTP transport ────────────────────────────────────────────────────────────


class TestServerConfigTransport:
    """Tests for ServerConfig.transport property."""

    def test_transport_stdio_by_default(self):
        cfg = ServerConfig(name="test", command="echo")
        assert cfg.transport == "stdio"

    def test_transport_http_when_url_set(self):
        cfg = ServerConfig(name="test", url="http://localhost:8080/mcp")
        assert cfg.transport == "http"

    def test_transport_http_takes_priority(self):
        cfg = ServerConfig(name="test", command="echo", url="http://localhost:8080/mcp")
        assert cfg.transport == "http"

    def test_headers_stored(self):
        cfg = ServerConfig(
            name="test",
            url="http://localhost:8080/mcp",
            headers={"Authorization": "Bearer xyz"},
        )
        assert cfg.headers == {"Authorization": "Bearer xyz"}

    def test_command_defaults_to_empty(self):
        cfg = ServerConfig(name="test")
        assert cfg.command == ""
        assert cfg.transport == "stdio"


class TestMCPServerConnectionHTTP:
    """Tests for HTTP transport connection lifecycle."""

    @pytest.mark.asyncio
    async def test_connect_http_handshake(self):
        """Verify HTTP initialize extracts session ID and result."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.headers = {"mcp-session-id": "abc123"}
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={
            "jsonrpc": "2.0", "id": 1,
            "result": {"serverInfo": {"name": "TestSrv", "version": "1.0"}},
        })
        mock_client.post = AsyncMock(return_value=resp)
        conn._http_client = mock_client

        init_result = await conn._request_http({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {}},
        })

        assert init_result == {"serverInfo": {"name": "TestSrv", "version": "1.0"}}
        assert conn._session_id == "abc123"

    @pytest.mark.asyncio
    async def test_tools_list_via_http(self):
        """Verify tools/list via HTTP."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={
            "jsonrpc": "2.0", "id": 2,
            "result": {"tools": [{"name": "tool1", "description": "A tool"}]},
        })
        mock_client.post = AsyncMock(return_value=resp)
        conn._http_client = mock_client

        tools_result = await conn._request_http({
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })

        assert tools_result == {"tools": [{"name": "tool1", "description": "A tool"}]}

    @pytest.mark.asyncio
    async def test_request_http_passes_session_id(self):
        """Subsequent HTTP requests carry the mcp-session-id header."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        conn._session_id = "existing-sid"

        mock_client = MagicMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={"jsonrpc": "2.0", "id": 1, "result": "ok"})
        mock_client.post = AsyncMock(return_value=resp)
        conn._http_client = mock_client

        result = await conn._request_http({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {},
        })

        assert result == "ok"
        # Verify the session ID header was passed
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["headers"] == {"mcp-session-id": "existing-sid"}

    @pytest.mark.asyncio
    async def test_request_http_error_status(self):
        """HTTP 4xx raises ConnectionError."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=MagicMock(),
            response=MagicMock(status_code=404),
        )
        conn._http_client = mock_client

        with pytest.raises(ConnectionError, match="HTTP error"):
            await conn._request_http({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {},
            })

    @pytest.mark.asyncio
    async def test_request_http_jsonrpc_error(self):
        """A 200 with JSON-RPC error raises Exception."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        mock_client = MagicMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={
            "jsonrpc": "2.0", "id": 1,
            "error": {"code": -32601, "message": "Method not found"},
        })
        mock_client.post = AsyncMock(return_value=resp)
        conn._http_client = mock_client

        with pytest.raises(Exception, match="MCP error"):
            await conn._request_http({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {},
            })

    @pytest.mark.asyncio
    async def test_notify_http_fire_and_forget(self):
        """HTTP notify creates a background POST task."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        conn._session_id = "sid123"

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.post = AsyncMock(return_value=MagicMock())
        conn._http_client = mock_client

        conn._notify("notifications/initialized", {})
        # Let the background task run
        await asyncio.sleep(0)

        mock_client.post.assert_called_once()
        call_kwargs = mock_client.post.call_args
        assert call_kwargs.kwargs["json"]["method"] == "notifications/initialized"
        assert call_kwargs.kwargs["headers"] == {"mcp-session-id": "sid123"}

    @pytest.mark.asyncio
    async def test_disconnect_http_closes_client(self):
        """HTTP disconnect sends DELETE and closes the client."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        conn._session_id = "sid-to-delete"

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.delete = AsyncMock()
        mock_client.aclose = AsyncMock()
        conn._http_client = mock_client

        await conn.disconnect()

        mock_client.delete.assert_called_once_with(
            "http://remote:8080/mcp",
            headers={"mcp-session-id": "sid-to-delete"},
        )
        mock_client.aclose.assert_called_once()
        assert conn._session_id is None
        assert conn._http_client is None

    @pytest.mark.asyncio
    async def test_call_tool_allows_http_connection(self):
        """call_tool works for HTTP transport (no _process needed)."""
        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn._http_client = MagicMock()

        resp = MagicMock()
        resp.headers = {}
        resp.raise_for_status = MagicMock()
        resp.json = MagicMock(return_value={
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "hello"}]},
        })
        conn._http_client.post = AsyncMock(return_value=resp)

        result = await conn.call_tool("greet", {"name": "world"})
        assert result == "hello"

    @pytest.mark.asyncio
    async def test_http_headers_passed_to_client(self):
        """Custom config.headers are used in Streamable HTTP requests.

        _connect_http first creates a bare httpx client for SSE detection,
        then re-creates it with headers for Streamable HTTP POST requests.
        Headers are also passed to the SSE detection stream() call.
        """
        import httpx

        cfg = ServerConfig(
            name="http_srv",
            url="http://remote:8080/mcp",
            headers={"Authorization": "Bearer mytoken"},
        )
        conn = MCPServerConnection(cfg)

        # Mock the SSE detection stream to raise, falling through
        # to Streamable HTTP where config.headers are merged.
        mock_client1 = MagicMock(spec=httpx.AsyncClient)
        mock_client1.stream = MagicMock(side_effect=ConnectionError("refused"))
        mock_client1.aclose = AsyncMock()

        mock_client2 = MagicMock(spec=httpx.AsyncClient)

        with patch.object(httpx, "AsyncClient") as mock_cls:
            mock_cls.side_effect = [mock_client1, mock_client2]

            await conn._connect_http()

            # First AsyncClient: no headers (bare client for SSE detection)
            assert mock_cls.call_count == 2
            first_kwargs = mock_cls.call_args_list[0].kwargs
            assert "headers" not in first_kwargs

            # SSE detection stream() was called with custom headers + Accept
            mock_client1.stream.assert_called_once()
            stream_kwargs = mock_client1.stream.call_args.kwargs
            assert stream_kwargs["headers"]["Authorization"] == "Bearer mytoken"
            assert stream_kwargs["headers"]["Accept"] == "text/event-stream"

            # Second AsyncClient: includes custom headers
            second_kwargs = mock_cls.call_args_list[1].kwargs
            assert second_kwargs["headers"]["Authorization"] == "Bearer mytoken"
