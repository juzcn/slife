"""Tests for slife_mcp.connection — ConnectionPool, MCPServerConnection, ServerConfig."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
from contextlib import asynccontextmanager, AsyncExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.plugins.mcp_gateway.connection import (
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

    def test_transport_defaults_stdio(self):
        cfg = ServerConfig(name="test")
        assert cfg.transport == "stdio"


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
        # No live state — those belong to list_servers / __check
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


class TestConnectionPoolAddServerGate:
    """add_server's connect gate — only the enabled flag governs.  Connecting IS
    the probe; there is no persisted healthy verdict to gate on anymore."""

    @pytest.mark.asyncio
    async def test_disabled_server_registered_but_not_connected(self):
        pool = ConnectionPool()
        with patch(
            "slife.plugins.mcp_gateway.connection.MCPServerConnection.connect",
            new=AsyncMock(),
        ) as mock_connect:
            conn = await pool.add_server(
                ServerConfig(name="off", command="npx", enabled=False),
            )
        assert pool.get_server("off") is conn
        assert conn.status == ServerStatus.DISCONNECTED
        mock_connect.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enabled_server_connects(self):
        pool = ConnectionPool()
        with patch(
            "slife.plugins.mcp_gateway.connection.MCPServerConnection.connect",
            new=AsyncMock(),
        ) as mock_connect:
            await pool.add_server(
                ServerConfig(name="ok", command="npx"),
            )
        mock_connect.assert_awaited_once()


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
    """Tests for HTTP transport connection lifecycle (SDK-backed, E2).

    The previous raw JSON-RPC ``_request_http``/SSE-detection implementation
    was consolidated onto the mcp SDK transports (``sse_client`` /
    ``streamable_http_client`` + ``ClientSession``).  These tests exercise the
    SDK-wired path: connect() drives the session's initialize/list_tools, and
    the transport fallback logic still resolves config.url/headers.
    """

    @staticmethod
    def _mock_session(tools=None, initialize_result=None):
        from mcp.types import Tool
        session = AsyncMock()
        session.initialize = AsyncMock(return_value=initialize_result)
        session.send_notification = AsyncMock()
        result = MagicMock()
        result.tools = tools or []
        session.list_tools = AsyncMock(return_value=result)
        return session

    @pytest.mark.asyncio
    async def test_connect_runs_handshake_and_discovers_tools(self):
        """connect() drives the SDK session's initialize + list_tools."""
        from mcp.types import Tool
        from mcp.types import TextContent

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        session = self._mock_session(
            tools=[Tool(name="tool1", description="A tool", inputSchema={"type": "object"})],
        )
        conn._session = session

        # Short-circuit transport establishment; the handshake runs in connect().
        async def _already_connected():
            return

        conn._connect_http = _already_connected
        with patch.object(conn, "_cleanup_resources", new=AsyncMock()):
            with patch.object(conn, "_health_monitor", new=AsyncMock()):
                await conn.connect()

        session.initialize.assert_awaited_once()
        session.list_tools.assert_awaited_once()
        assert conn.status == ServerStatus.CONNECTED
        assert conn.tool_count == 1
        assert conn.list_tools()[0]["name"] == "tool1"

    @pytest.mark.asyncio
    async def test_call_tool_via_session(self):
        """call_tool returns formatted text from the SDK CallToolResult."""
        from mcp.types import CallToolResult, TextContent

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=CallToolResult(
            content=[TextContent(type="text", text="hello")],
        ))
        conn._session = session

        result = await conn.call_tool("greet", {"name": "world"})
        assert result == "hello"
        session.call_tool.assert_awaited_once_with("greet", {"name": "world"})

    @pytest.mark.asyncio
    async def test_call_tool_error_is_marked(self):
        """A server isError result surfaces as 'Error: …'."""
        from mcp.types import CallToolResult, TextContent

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=CallToolResult(
            content=[TextContent(type="text", text="nope")],
            isError=True,
        ))
        conn._session = session

        result = await conn.call_tool("do", {})
        assert result.startswith("Error:")

    @pytest.mark.asyncio
    async def test_transport_error_reconnect(self):
        """A transport failure in call_tool triggers one reconnect, then retry."""
        from mcp.types import CallToolResult, TextContent

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        session = AsyncMock()
        session.call_tool = AsyncMock(
            side_effect=[ConnectionError("died"), CallToolResult(
                content=[TextContent(type="text", text="recovered")],
            )],
        )
        conn._session = session
        reconnected = {"n": 0}

        async def fake_connect():
            reconnected["n"] += 1
            conn._session = session
            conn._status = ServerStatus.CONNECTED

        conn.connect = fake_connect
        with patch.object(conn, "_cleanup_resources", new=AsyncMock()):
            result = await conn.call_tool("g", {})

        assert result == "recovered"
        assert reconnected["n"] == 1
        assert session.call_tool.await_count == 2

    @pytest.mark.asyncio
    async def test_http_headers_passed_to_client(self):
        """Custom config.headers reach the SDK streamable client (REVIEW M7).

        The SDK's ``streamable_http_client`` accepts a pre-built httpx2 client;
        ``_connect_http`` must construct it WITH the resolved config.headers and
        then fall through to that transport when SSE is unsupported.
        """
        import httpx2
        from slife.plugins.mcp_gateway import connection as conn_mod

        cfg = ServerConfig(
            name="http_srv",
            url="http://remote:8080/mcp",
            headers={"Authorization": "Bearer mytoken"},
        )
        conn = MCPServerConnection(cfg)

        @asynccontextmanager
        async def _sse_fail(url, headers=None, **kw):
            yield None
            raise RuntimeError("not SSE")

        # SSE unsupported → fall through to streamable with the header client.
        entered = {}

        @asynccontextmanager
        async def _streamable_entry(url, http_client=None, **kw):
            entered["url"] = url
            entered["http_client"] = http_client
            yield (AsyncMock(), AsyncMock())

        mock_http = MagicMock(spec=httpx2.AsyncClient)
        with patch.object(httpx2, "AsyncClient", return_value=mock_http) as mock_client_cls, \
             patch.object(conn_mod, "streamable_http_client", new=_streamable_entry), \
             patch.object(conn_mod, "sse_client", new=_sse_fail):
            await conn._connect_http()

        assert entered["http_client"] is conn._http_client
        assert conn._http_client is not None
        # The http client was constructed with the resolved config.headers.
        build_call = mock_client_cls.call_args
        assert build_call.kwargs["headers"]["Authorization"] == "Bearer mytoken"

    @pytest.mark.asyncio
    async def test_sse_transport_selected_when_supported(self):
        """A server that answers SSE goes down the sse_client path."""
        from slife.plugins.mcp_gateway import connection as conn_mod

        cfg = ServerConfig(name="sse_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        entered = {}
        @asynccontextmanager
        async def _sse_entry(url, headers=None, **kw):
            entered["url"] = url
            entered["headers"] = headers
            yield (AsyncMock(), AsyncMock())
        with patch.object(conn_mod, "sse_client", new=_sse_entry):
            await conn._connect_http()

        assert entered["url"] == "http://remote:8080/mcp"
        assert conn._sse_mode is True
        assert conn._session is not None

    @pytest.mark.asyncio
    async def test_disconnect_closes_transport_client(self):
        """disconnect closes the SDK httpx2 client."""
        import httpx2

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        mock_client = MagicMock(spec=httpx2.AsyncClient)
        mock_client.aclose = AsyncMock()
        conn._http_client = mock_client
        stack = AsyncExitStack()
        conn._exit_stack = stack

        await conn.disconnect()

        mock_client.aclose.assert_called_once()
        assert conn._http_client is None
        assert conn._exit_stack is None

    @pytest.mark.asyncio
    async def test_connect_failure_sets_failed_state(self):
        """A transport establishment failure → FAILED (and cleanup ran)."""
        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        async def boom():
            raise ConnectionError("down")

        conn._connect_http = boom
        with patch.object(conn, "_cleanup_resources", new=AsyncMock()):
            await conn.connect()

        assert conn.status == ServerStatus.FAILED
        assert "down" in (conn.error or "")


# ── Health check / reconnect (REVIEW C2) ──────────────────────────────────


class TestMCPServerConnectionPing:
    """Tests for ping() — now via the SDK session's send_ping (E2)."""

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
        session = AsyncMock()
        session.send_ping = AsyncMock()
        conn._session = session
        assert await conn.ping() is True
        session.send_ping.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_ping_transport_error(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        session = AsyncMock()
        session.send_ping = AsyncMock(side_effect=ConnectionError("server died"))
        conn._session = session
        assert await conn.ping() is False

    @pytest.mark.asyncio
    async def test_ping_hung_server_times_out(self):
        """A hung server (no ping answer) makes ping() False, not hang."""
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.CONNECTED
        session = AsyncMock()

        async def _hang(*_args, **_kwargs):
            await asyncio.sleep(3600)

        session.send_ping = _hang
        conn._session = session
        assert await conn.ping(timeout=0.01) is False


class TestMCPServerConnectionHealthMonitor:
    """Tests for the background health monitor."""

    @pytest.mark.asyncio
    async def test_reconnects_a_dead_server(self):
        """CONNECTED + unresponsive → marked DISCONNECTED, then reconnected."""
        from slife.plugins.mcp_gateway import connection as conn_mod

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
        from slife.plugins.mcp_gateway import connection as conn_mod

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
        from slife.plugins.mcp_gateway import connection as conn_mod

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
    @pytest.mark.asyncio
    async def test_needs_user_auth_pauses_auto_reconnect(self):
        """F5 regression: once OAuth needs a human, the health monitor must
        NOT keep re-running connect() (each reconnect would re-run the device
        flow and pop another desktop prompt).  It sleeps through the interval
        instead."""
        from slife.plugins.mcp_gateway import connection as conn_mod

        cfg = ServerConfig(name="test", command="echo", enabled=True)
        conn = MCPServerConnection(cfg)
        conn._status = ServerStatus.FAILED
        conn._needs_user_auth = True

        connect_calls = {"n": 0}

        async def fake_connect():
            connect_calls["n"] += 1
            conn._status = ServerStatus.CONNECTED

        conn.connect = fake_connect

        with patch.object(conn_mod, "_HEALTH_CHECK_INTERVAL", 0.01):
            task = asyncio.create_task(conn._health_monitor())
            await asyncio.sleep(0.05)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # The monitor paused — connect() was never (re)called.
        assert connect_calls["n"] == 0
        assert conn.status == ServerStatus.FAILED

    @pytest.mark.asyncio
    async def test_ensure_oauth_token_sets_needs_user_auth(self):
        """F5: a refresh failure that requires the device flow marks the
        connection needs_user_auth, and a SECOND attempt does not re-run the
        device flow (it raises NeedsUserAuthError instead)."""
        from slife.plugins.mcp_gateway.connection import NeedsUserAuthError
        from slife.plugins.mcp_gateway import oauth as oauth_mod

        cfg = ServerConfig(
            name="test", command="echo",
            auth={"type": "oauth", "client_id": "cid"},
        )
        conn = MCPServerConnection(cfg)

        flow_ok = {"token": None}
        fake_tokens = type("T", (), {"token_type": "Bearer", "access_token": "tok"})()

        async def fake_flow(auth, server_name):
            if flow_ok["token"] is None:
                raise RuntimeError("user never authorized")
            return fake_tokens

        with patch.object(oauth_mod, "get_valid_token", return_value=None), \
             patch.object(oauth_mod, "refresh_access_token",
                          AsyncMock(side_effect=RuntimeError("refresh revoked"))), \
             patch.object(oauth_mod, "run_device_code_flow", new=fake_flow):
            # First attempt: flow runs, fails → needs_user_auth set.
            with pytest.raises(NeedsUserAuthError):
                await conn._ensure_oauth_token()
            assert conn._needs_user_auth is True

            # Second attempt: flag already set → device flow NOT re-run.
            with pytest.raises(NeedsUserAuthError, match="needs OAuth re-authorization"):
                await conn._ensure_oauth_token()
            assert flow_ok["token"] is None  # fake never re-invoked with a token

            # Manual recovery: re-adding the server clears the flag.
            conn._needs_user_auth = False
            flow_ok["token"] = object()
            await conn._ensure_oauth_token()
            assert conn._needs_user_auth is False
            assert conn.config.headers["Authorization"] == "Bearer tok"

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

        session = AsyncMock()
        from mcp.types import CallToolResult, TextContent
        session.call_tool = AsyncMock(return_value=CallToolResult(
            content=[TextContent(type="text", text="ok")],
        ))
        conn._session = session

        conn.connect = fake_connect

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

class TestMCPServerConnectionCancelCleanup:
    """A cancelled connect must tear its transport down.

    An asyncio.timeout around add_server/connect cancels connect()
    mid-flight.  Before this, the CancelledError path did
    not run _cleanup_resources — the spawned npx/uvx process, http client
    and stderr relay leaked.
    """

    @pytest.mark.asyncio
    async def test_cancel_during_connect_cleans_up_resources(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)

        async def _cancel_mid_connect():
            raise asyncio.CancelledError

        conn._connect_stdio = _cancel_mid_connect
        with patch.object(conn, "_cleanup_resources", new=AsyncMock()) as mock_cleanup:
            with pytest.raises(asyncio.CancelledError):
                await conn.connect()

        mock_cleanup.assert_awaited_once()
        assert conn.status == ServerStatus.DISCONNECTED

    @pytest.mark.asyncio
    async def test_cancel_after_connected_resets_to_disconnected(self):
        """A6 regression: connect() marks CONNECTED *before* the post-connect
        sync.  If that sync is cancelled (a host tool-timeout on mcp_set), the
        status must drop back to DISCONNECTED — never stay CONNECTED over a
        torn-down transport (a half-open wedge where __check reports running
        and call_tool skips lazy reconnect) — and a health monitor must be
        (re)armed so the DISCONNECTED state recovers in the background."""
        from mcp.types import Tool

        cfg = ServerConfig(name="test", command="echo")
        cfg.enabled = True
        conn = MCPServerConnection(cfg)
        conn._disconnecting = False
        conn._health_task = None

        async def _connect_ok():
            pass

        session = AsyncMock()
        session.initialize = AsyncMock()
        session.send_notification = AsyncMock()
        list_result = MagicMock()
        list_result.tools = []
        session.list_tools = AsyncMock(return_value=list_result)
        conn._session = session

        async def _cancel_mid_sync():
            raise asyncio.CancelledError

        conn._connect_stdio = _connect_ok
        conn._fire_on_reconnect = _cancel_mid_sync
        with patch.object(conn, "_cleanup_resources", new=AsyncMock()):
            with pytest.raises(asyncio.CancelledError):
                await conn.connect()

        assert conn.status == ServerStatus.DISCONNECTED
        assert conn._health_task is not None and not conn._health_task.done()
        conn._health_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await conn._health_task

class TestMCPServerConnectionStdio:
    """stdio teardown + spawn semantics — now delegated to the SDK's
    stdio_client (E2), which spawns the subprocess itself and manages the
    process tree.  These verify our stdio wiring hands the SDK the right
    command/args/env."""

    @pytest.mark.asyncio
    async def test_connect_stdio_builds_StdioServerParameters(self):
        """_connect_stdio passes command/args/env to the SDK stdio client."""
        from slife.plugins.mcp_gateway import connection as conn_mod

        cfg = ServerConfig(
            name="test", command="npx", args=["-y", "srv"], env={"FOO": "bar"},
        )
        conn = MCPServerConnection(cfg)

        saw = {}
        @asynccontextmanager
        async def _stdio_entry(params, errlog=None, **kw):
            saw["params"] = params
            saw["errlog"] = errlog
            yield (AsyncMock(), AsyncMock())
        with patch.object(conn_mod, "stdio_client", new=_stdio_entry):
            await conn._connect_stdio()

        # resolve_command may absolutize npx → npx.CMD on Windows.
        assert saw["params"].command.replace("\\", "/").endswith(("npx", "npx.CMD"))
        assert saw["params"].args == ["-y", "srv"]
        assert saw["params"].env["FOO"] == "bar"
        # errlog is a real OS file — the subprocess machinery needs a fileno.
        assert saw["errlog"] is conn._stderr_dump
        assert conn._session is not None
        await conn._cleanup_resources()  # cancel the drain task, close temp file

    @pytest.mark.asyncio
    async def test_cleanup_closes_exit_stack(self):
        """_cleanup_resources tears down the SDK exit stack + http client."""
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        stack = AsyncExitStack()
        stack.aclose = AsyncMock()
        conn._exit_stack = stack
        mock_client = MagicMock()
        mock_client.aclose = AsyncMock()
        conn._http_client = mock_client

        await conn._cleanup_resources()

        stack.aclose.assert_awaited_once()
        mock_client.aclose.assert_called_once()
        assert conn._exit_stack is None
        assert conn._session is None
        assert conn._http_client is None


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
    async def test_listener_error_is_swallowed(self):
        async def boom(server_name):
            raise RuntimeError("listener failed")

        conn = MCPServerConnection(
            ServerConfig(name="test", command="echo"), on_connected=boom,
        )
        # A failing listener must never propagate into connect().
        await conn._fire_on_reconnect()
        await conn._fire_on_reconnect()

    @pytest.mark.asyncio
    async def test_pool_passes_callback_to_connections(self):
        cb = AsyncMock()
        pool = ConnectionPool(on_connected=cb)
        # enabled=False so add_server doesn't attempt a real connect.
        conn = await pool.add_server(
            ServerConfig(name="test", command="echo", enabled=False),
        )
        assert conn._on_connected is cb
