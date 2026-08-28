"""Tests for slife_mcp.connection — ConnectionPool, MCPServerConnection, ServerConfig."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_plugin.connection import (
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

    def test_full_config(self):
        cfg = ServerConfig(
            name="myserver",
            command="npx",
            args=["-y", "@modelcontextprotocol/server-filesystem"],
            env={"HOME": "/tmp"},
            description="My filesystem server",
        )
        assert cfg.command == "npx"
        assert len(cfg.args) == 2
        assert cfg.env == {"HOME": "/tmp"}


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
        assert conn.tool_count == 0
        assert conn.error is None


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
        assert s["description"] == "First"


class TestConnectionPoolListConfigured:
    """Tests for list_configured — static config view, no live state."""

    def test_returns_config_fields_only(self):
        pool = ConnectionPool()
        cfg = ServerConfig(
            name="srv1", command="cmd1", args=["-a"], description="First",
        )
        conn = MCPServerConnection(cfg)
        conn._tools_cache = [{"name": "t1"}, {"name": "t2"}]
        conn._status = ServerStatus.CONNECTED
        conn._error = "boom"
        pool._connections["srv1"] = conn

        servers = pool.list_configured()
        assert len(servers) == 1
        s = servers[0]
        assert s["name"] == "srv1"
        assert s["transport"] == "stdio"
        assert s["command"] == "cmd1"
        assert s["args"] == ["-a"]
        assert s["url"] == ""
        assert s["enabled"] is True
        assert s["description"] == "First"
        # No live state — those belong to list_servers / __mcp_connection_status
        assert "status" not in s
        assert "state" not in s
        assert "tool_count" not in s
        assert "error" not in s
        assert "active" not in s

    def test_omits_secret_holding_fields(self):
        pool = ConnectionPool()
        cfg = ServerConfig(
            name="srv", url="http://localhost:8080/mcp",
            headers={"Authorization": "Bearer secret"},
            env={"API_KEY": "sk-secret"},
            auth={"client_id": "x"},
        )
        conn = MCPServerConnection(cfg)
        pool._connections["srv"] = conn

        servers = pool.list_configured()
        assert len(servers) == 1
        s = servers[0]
        assert s["transport"] == "http"
        for secret_field in ("env", "headers", "auth"):
            assert secret_field not in s


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


class TestConnectionPoolCallTool:
    """Tests for call_tool."""

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
    async def test_request_http_sse_stream_response(self):
        """A streamable server streaming text/event-stream is parsed."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        async def _sse_lines():
            yield "event: message"
            yield 'data: {"jsonrpc": "2.0", "id": 1, "result": "ok"}'
            yield ""

        mock_client = MagicMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.headers = {"content-type": "text/event-stream"}
        resp.raise_for_status = MagicMock()
        resp.aiter_lines = _sse_lines
        resp.aclose = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        conn._http_client = mock_client

        result = await conn._request_http({
            "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {},
        })

        assert result == "ok"
        # The stream is owned by the reader and closed once the response
        # is consumed (same contract as _read_sse_stream, REVIEW H1).
        resp.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_request_http_sse_stream_jsonrpc_error(self):
        """An SSE-streamed response carrying a JSON-RPC error is raised."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        async def _sse_lines():
            yield (
                'data: {"jsonrpc": "2.0", "id": 1, '
                '"error": {"code": -32601, "message": "Method not found"}}'
            )
            yield ""

        mock_client = MagicMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.headers = {"content-type": "text/event-stream"}
        resp.raise_for_status = MagicMock()
        resp.aiter_lines = _sse_lines
        resp.aclose = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        conn._http_client = mock_client

        with pytest.raises(Exception, match="MCP error"):
            await conn._request_http({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {},
            })

    @pytest.mark.asyncio
    async def test_request_http_sse_stream_no_matching_id(self):
        """An SSE stream without a matching response id raises ConnectionError."""
        import httpx

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        async def _sse_lines():
            # Only a server-initiated notification arrives, then EOF.
            yield 'data: {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}'
            yield ""

        mock_client = MagicMock(spec=httpx.AsyncClient)
        resp = MagicMock()
        resp.headers = {"content-type": "text/event-stream"}
        resp.raise_for_status = MagicMock()
        resp.aiter_lines = _sse_lines
        resp.aclose = AsyncMock()
        mock_client.post = AsyncMock(return_value=resp)
        conn._http_client = mock_client

        with pytest.raises(ConnectionError, match="no response"):
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

        await conn._notify("notifications/initialized", {})
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
    async def test_connect_resets_stale_session_id(self):
        """A reconnect must start a fresh initialize — a stale mcp-session-id
        from the previous transport must not be sent (REVIEW C2 re-opening).

        The health monitor and call_tool reconnect through ``connect()`` after
        ``_cleanup_resources()``, which never clears the session id; only a
        fresh ``connect()`` can guarantee the initialize carries none.
        """
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.DISCONNECTED
        conn._session_id = "stale-from-previous-transport"

        async def boom():
            raise RuntimeError("transport failed")

        conn._connect_stdio = boom
        await conn.connect()  # connect() swallows transport errors → FAILED

        assert conn._session_id is None
        assert conn.status == ServerStatus.FAILED

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

        _connect_http creates a single httpx client already carrying the
        resolved headers, so the SSE detection GET and subsequent
        Streamable HTTP POST requests all inherit them (REVIEW M7).
        """
        import httpx

        cfg = ServerConfig(
            name="http_srv",
            url="http://remote:8080/mcp",
            headers={"Authorization": "Bearer mytoken"},
        )
        conn = MCPServerConnection(cfg)

        # Mock the SSE detection send() to raise, falling through
        # to Streamable HTTP where config.headers are already on the client.
        mock_client1 = MagicMock(spec=httpx.AsyncClient)
        mock_client1.send = MagicMock(side_effect=ConnectionError("refused"))
        mock_client1.aclose = AsyncMock()

        with patch.object(httpx, "AsyncClient") as mock_cls:
            mock_cls.side_effect = [mock_client1]

            await conn._connect_http()

            # A single AsyncClient is created, carrying the resolved headers.
            assert mock_cls.call_count == 1
            first_kwargs = mock_cls.call_args_list[0].kwargs
            assert first_kwargs["headers"]["Authorization"] == "Bearer mytoken"

            # SSE detection GET carries custom headers + Accept
            mock_client1.build_request.assert_called_once()
            req_kwargs = mock_client1.build_request.call_args.kwargs
            assert req_kwargs["headers"]["Authorization"] == "Bearer mytoken"
            assert req_kwargs["headers"]["Accept"] == "text/event-stream"

            # Sent as a streamed request
            mock_client1.send.assert_called_once()
            assert mock_client1.send.call_args.kwargs["stream"] is True

    @pytest.mark.asyncio
    async def test_sse_connect_stream_stays_open(self):
        """SSE detection response stays open after _connect_http returns.

        Regression for REVIEW H1: the response must be owned by the
        _read_sse_stream task (which closes it), not closed on function
        return as the old ``async with stream(...)`` did.
        """
        import httpx

        cfg = ServerConfig(
            name="sse_srv",
            url="http://remote:8080/mcp",
        )
        conn = MCPServerConnection(cfg)

        # Simulated SSE event stream: JSON-object endpoint event, then stays open.
        async def sse_lines():
            yield "event: endpoint"
            yield 'data: {"uri": "/mcp-message", "type": "endpoint"}'
            yield ""
            await asyncio.sleep(3600)  # keep the stream alive

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_lines = sse_lines
        mock_resp.aclose = AsyncMock()

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.build_request.return_value = MagicMock()
        mock_client.send = AsyncMock(return_value=mock_resp)

        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            await conn._connect_http()

        assert conn._sse_mode is True
        assert conn._sse_message_url == "http://remote:8080/mcp-message"
        assert conn._sse_task is not None and not conn._sse_task.done()
        # _connect_http must NOT have closed the response — the reader owns it.
        mock_resp.aclose.assert_not_called()

        # Cleanup: cancelling the reader task closes the response it owns.
        conn._sse_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await conn._sse_task
        mock_resp.aclose.assert_called_once()

    @pytest.mark.asyncio
    async def test_sse_connect_bare_path_endpoint(self):
        """SSE endpoint event sent as a bare path (non-JSON) is accepted.

        The reader passes non-JSON payloads through raw, and _connect_http
        resolves the path against the base URL.
        """
        import httpx

        cfg = ServerConfig(
            name="sse_srv",
            url="http://remote:8080/mcp",
        )
        conn = MCPServerConnection(cfg)

        async def sse_lines():
            yield "event: endpoint"
            yield "data: /mcp-message"
            yield ""
            await asyncio.sleep(3600)

        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "text/event-stream"}
        mock_resp.aiter_lines = sse_lines
        mock_resp.aclose = AsyncMock()

        mock_client = MagicMock(spec=httpx.AsyncClient)
        mock_client.build_request.return_value = MagicMock()
        mock_client.send = AsyncMock(return_value=mock_resp)

        with patch.object(httpx, "AsyncClient", return_value=mock_client):
            await conn._connect_http()

        assert conn._sse_mode is True
        assert conn._sse_message_url == "http://remote:8080/mcp-message"

        conn._sse_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await conn._sse_task


# ── Health check / reconnect (REVIEW C2) ──────────────────────────────────


class TestMCPServerConnectionPing:
    """Tests for ping()."""

    @pytest.mark.asyncio
    async def test_ping_false_when_not_connected(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        assert await conn.ping() is False

    @pytest.mark.asyncio
    async def test_ping_success(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn._request = AsyncMock(return_value={})
        assert await conn.ping() is True
        conn._request.assert_called_once_with("ping", {})

    @pytest.mark.asyncio
    async def test_ping_transport_error(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn._request = AsyncMock(side_effect=ConnectionError("server died"))
        assert await conn.ping() is False

    @pytest.mark.asyncio
    async def test_ping_hung_server_times_out(self):
        """A hung server (no ping answer) makes ping() False, not hang."""
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        conn._request = _hang
        assert await conn.ping(timeout=0.01) is False


class TestMCPServerConnectionHealthMonitor:
    """Tests for the background health monitor."""

    @pytest.mark.asyncio
    async def test_reconnects_a_dead_server(self):
        """CONNECTED + unresponsive → marked DISCONNECTED, then reconnected."""
        from mcp_plugin import connection as conn_mod

        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn._error = None

        calls = {"n": 0}

        async def fake_ping():
            calls["n"] += 1
            return calls["n"] > 1  # first ping fails (server died), then recovers

        async def fake_connect():
            conn._status = ServerStatus.CONNECTED
            conn._error = None

        conn.ping = fake_ping
        conn.connect = fake_connect

        with patch.object(conn_mod, "_HEALTH_CHECK_INTERVAL", 0.01):
            task = asyncio.create_task(conn._health_monitor())
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert calls["n"] >= 2
        assert conn.status == ServerStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_exits_when_server_disabled(self):
        """A deliberately-disabled server stops the monitor, no reconnect."""
        from mcp_plugin import connection as conn_mod

        cfg = ServerConfig(name="test", command="echo", enabled=False)
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        conn.ping = AsyncMock(return_value=True)
        conn.connect = AsyncMock()

        with patch.object(conn_mod, "_HEALTH_CHECK_INTERVAL", 0.01):
            await conn._health_monitor()

        conn.connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_retries_a_failed_initial_connect(self):
        """A server in FAILED state is retried (with backoff) until it recovers."""
        from mcp_plugin import connection as conn_mod

        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.FAILED
        conn._error = "boom"
        conn.ping = AsyncMock(return_value=True)

        calls = {"n": 0}

        async def fake_connect():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("still down")
            conn._status = ServerStatus.CONNECTED
            conn._error = None

        conn.connect = fake_connect

        with patch.object(conn_mod, "_HEALTH_CHECK_INTERVAL", 0.01), \
                patch.object(conn_mod, "_RECONNECT_BACKOFF_INITIAL", 0.01):
            task = asyncio.create_task(conn._health_monitor())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        assert calls["n"] >= 2
        assert conn.status == ServerStatus.CONNECTED

    @pytest.mark.asyncio
    async def test_connect_failure_starts_monitor(self):
        """A failed initial connect still spawns the health monitor."""
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._connect_stdio = AsyncMock(side_effect=ConnectionError("down"))

        await conn.connect()

        assert conn.status == ServerStatus.FAILED
        assert conn._health_task is not None and not conn._health_task.done()

        conn._health_task.cancel()
        try:
            await conn._health_task
        except asyncio.CancelledError:
            pass
        conn._health_task = None

    @pytest.mark.asyncio
    async def test_disconnect_cancels_health_monitor(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        cancelled = {"done": False}

        async def fake_monitor():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled["done"] = True
                raise

        conn._health_task = asyncio.create_task(fake_monitor())
        await asyncio.sleep(0)  # let the task start

        await conn.disconnect()

        assert cancelled["done"] is True
        assert conn._health_task is None


class TestMCPServerConnectionLazyReconnect:
    """Tests for call_tool's lazy reconnect of a DISCONNECTED server."""

    @pytest.mark.asyncio
    async def test_call_tool_reconnects_disconnected_server(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.DISCONNECTED
        reconnected = {"done": False}

        async def fake_connect():
            reconnected["done"] = True
            conn._status = ServerStatus.CONNECTED

        async def fake_request(_method, _params):
            return {"content": [{"type": "text", "text": "ok"}]}

        conn.connect = fake_connect
        conn._request = fake_request

        result = await conn.call_tool("echo", {"m": "x"})
        assert result == "ok"
        assert reconnected["done"] is True

    @pytest.mark.asyncio
    async def test_call_tool_does_not_reconnect_disabled(self):
        cfg = ServerConfig(name="test", command="echo", enabled=False)
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.DISCONNECTED
        conn.connect = AsyncMock()

        with pytest.raises(ValueError, match="not connected"):
            await conn.call_tool("echo", {})

        conn.connect.assert_not_called()


class TestMCPServerConnectionTreeKill:
    """REVIEW M4 — stdio teardown kills the whole process tree, not just the
    direct child (npx/uvx grandchildren survive on Windows)."""

    @pytest.mark.asyncio
    async def test_cleanup_kills_process_tree(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        proc = MagicMock()
        proc.stdin = None
        conn._process = proc

        with patch("mcp_plugin.connection.terminate_process") as mock_term, \
                patch("mcp_plugin.connection.kill_process_tree") as mock_tree:
            await conn._cleanup_resources()

        mock_tree.assert_awaited_once_with(proc)
        mock_term.assert_awaited_once()
        assert conn._process is None


class TestMCPServerConnectionReconnectNotify:
    """on_connected fires on EVERY successful connect (first and reconnects).

    The standalone server connects asynchronously from mcp-plugin.json5 on
    startup — a listener (a host re-syncing its tool registry) must be told
    about first connects too.  Full-diff registration on the listener side
    keeps the extra notification idempotent.
    """

    @pytest.mark.asyncio
    async def test_first_connect_notifies(self):
        cb = AsyncMock()
        conn = MCPServerConnection(
            ServerConfig(name="test", command="echo"), on_connected=cb,
        )
        await conn._fire_on_reconnect()
        cb.assert_awaited_once()
        assert conn._ever_connected is True

    @pytest.mark.asyncio
    async def test_reconnect_notifies(self):
        cb = AsyncMock()
        conn = MCPServerConnection(
            ServerConfig(name="test", command="echo"), on_connected=cb,
        )
        await conn._fire_on_reconnect()  # first connect — notifies
        cb.assert_awaited_once()
        await conn._fire_on_reconnect()  # reconnect — notifies again
        cb.assert_awaited()

    @pytest.mark.asyncio
    async def test_recovery_after_failed_initial_connect_notifies(self):
        """A failed INITIAL connect (mcp_set saw status=failed) must notify on
        the health monitor's later recovery — the caller skipped registration."""
        cb = AsyncMock()
        conn = MCPServerConnection(
            ServerConfig(name="test", command="echo"), on_connected=cb,
        )
        conn._notify_on_next_success = True  # prior initial connect failed

        await conn._fire_on_reconnect()

        cb.assert_awaited_once()
        assert conn._ever_connected is True
        assert conn._notify_on_next_success is False

    @pytest.mark.asyncio
    async def test_listener_error_is_swallowed(self):
        async def boom(server_name):
            raise RuntimeError("listener failed")

        conn = MCPServerConnection(
            ServerConfig(name="test", command="echo"), on_connected=boom,
        )
        # A failing listener must never propagate into connect().
        await conn._fire_on_reconnect()
        await conn._fire_on_reconnect()
        assert conn._ever_connected is True

    @pytest.mark.asyncio
    async def test_pool_passes_callback_to_connections(self):
        cb = AsyncMock()
        pool = ConnectionPool(on_connected=cb)
        # enabled=False so add_server doesn't attempt a real connect.
        conn = await pool.add_server(
            ServerConfig(name="test", command="echo", enabled=False),
        )
        assert conn._on_connected is cb
