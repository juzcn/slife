"""Tests for slife_mcp.connection — ConnectionPool, MCPServerConnection, ServerConfig."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import logging
import time as _time
from contextlib import asynccontextmanager, AsyncExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.plugins.mcp_gateway.connection import (
    ServerConfig,
    MCPServerConnection,
    ConnectionPool,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


def _mock_session(tools=(), *, ttl_ms=0, next_cursor=None):
    """A stand-in for the SDK ClientSession holding a ``tools/list`` answer.

    Mirrors the real result's fields — ``tools``, ``ttl_ms`` and
    ``next_cursor`` are all read by the snapshot absorb path.  Bare strings
    are accepted and built into real ``Tool`` objects.
    """
    from mcp.types import Tool

    session = AsyncMock()
    result = MagicMock()
    result.tools = [
        t if not isinstance(t, str)
        else Tool(name=t, description="", inputSchema={"type": "object"})
        for t in tools
    ]
    result.ttl_ms = ttl_ms
    result.next_cursor = next_cursor
    session.list_tools = AsyncMock(return_value=result)
    return session


def _listed(conn, tools=(), *, ttl_ms=0, age_s=0.0):
    """Give *conn* a tool snapshot as if ``tools/list`` had just answered."""
    conn._tools = [
        {"name": t, "description": "", "inputSchema": {"type": "object"}}
        for t in tools
    ]
    conn._tools_fetched_at = _time.monotonic() - age_s
    conn._tools_ttl_ms = ttl_ms
    conn._tools_stale = False
    conn._last_error = None
    return conn


async def _stop(task):
    """Cancel a background task and wait for it to finish."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


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


# ── MCPServerConnection ──────────────────────────────────────────────────────


class TestMCPServerConnectionInit:
    """Tests for MCPServerConnection initialization."""

    def test_initial_state(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)

        assert conn.config is cfg
        # No tool list yet — and that IS the health verdict (there is no
        # separate connection state to be in).
        assert conn.has_tools() is False
        assert conn.tools_ok is False
        assert conn.tool_count == 0
        assert conn.error is None
        assert conn.era is None


class TestMCPServerConnectionSnapshot:
    """Tests for the published facts — the __check row."""

    def test_facts_of_a_never_listed_server(self):
        cfg = ServerConfig(name="test", command="echo", description="D")
        conn = MCPServerConnection(cfg)
        snap = conn.snapshot()
        assert snap["name"] == "test"
        assert snap["transport"] == "stdio"
        assert snap["era"] is None
        assert snap["enabled"] is True
        assert snap["tools_ok"] is False
        assert snap["tool_count"] == 0
        assert snap["tools_age_s"] is None
        assert snap["last_error"] is None
        assert snap["needs_user_auth"] is False
        assert snap["rest_api"] is False   # mcp.servers is the default placement
        # The config view owns these — a fact belongs in exactly one place.
        assert "description" not in snap
        assert "command" not in snap
        assert "args" not in snap

    def test_facts_of_a_listed_server(self):
        conn = _listed(
            MCPServerConnection(ServerConfig(name="s", command="echo")),
            ["a", "b"], ttl_ms=60000, age_s=3.0,
        )
        snap = conn.snapshot()
        assert snap["tools_ok"] is True
        assert snap["tool_count"] == 2
        assert 3.0 <= snap["tools_age_s"] < 10.0
        assert snap["last_error"] is None


class TestMCPServerConnectionListTools:
    """Tests for list_tools."""

    def test_list_tools_empty(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        assert conn.list_tools() == []

    def test_list_tools_cached(self):
        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._tools = [
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
        conn = _listed(MCPServerConnection(cfg), ["t1"])

        await conn.disconnect()

        assert conn.has_tools() is False
        assert conn.tools_ok is False
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
    """Tests for list_servers — the __check payload."""

    def test_list_returns_info_dicts(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="srv1", command="cmd1", description="First")
        conn = _listed(MCPServerConnection(cfg), ["t1", "t2"])
        pool._connections["srv1"] = conn

        servers = pool.list_servers()
        assert len(servers) == 1
        s = servers[0]
        assert s["name"] == "srv1"
        assert s["tools_ok"] is True
        assert s["tool_count"] == 2
        assert s["transport"] == "stdio"
        # Facts only — no state word, no level (the harness interprets).
        assert "status" not in s
        assert "state" not in s
        assert "error" not in s


class TestConnectionPoolListConfigured:
    """Tests for list_configured — static config view, no live state."""

    def test_returns_config_fields_only(self):
        pool = ConnectionPool()
        cfg = ServerConfig(
            name="srv1", command="cmd1", args=["-a"], description="First",
        )
        conn = _listed(MCPServerConnection(cfg), ["t1", "t2"])
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
        assert "tools_ok" not in s
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
    """add_server's read gate — only the enabled flag governs.

    Reading the tool list IS connecting, so there is no separate connect step
    and no persisted healthy verdict to gate on.
    """

    @pytest.mark.asyncio
    async def test_disabled_server_registered_but_not_read(self):
        pool = ConnectionPool()
        with patch(
            "slife.plugins.mcp_gateway.connection.MCPServerConnection.refresh_tools",
            new=AsyncMock(),
        ) as mock_refresh:
            conn = await pool.add_server(
                ServerConfig(name="off", command="npx", enabled=False),
            )
        assert pool.get_server("off") is conn
        assert conn.has_tools() is False
        mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enabled_server_is_read(self):
        pool = ConnectionPool()
        with patch(
            "slife.plugins.mcp_gateway.connection.MCPServerConnection.refresh_tools",
            new=AsyncMock(),
        ) as mock_refresh:
            await pool.add_server(
                ServerConfig(name="ok", command="npx"),
            )
        mock_refresh.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_boot_spawns_without_reading_the_list(self):
        """``read_tools=False`` is the boot shape: transport up, list left to
        the first reader (the host's reconcile asks for one anyway) — boot used
        to pay every server's ``tools/list`` before the gateway was usable."""
        pool = ConnectionPool()
        with (
            patch.object(
                MCPServerConnection, "refresh_tools", new=AsyncMock(),
            ) as mock_refresh,
            patch.object(
                MCPServerConnection, "ensure_session", new=AsyncMock(return_value=True),
            ) as mock_session,
        ):
            conn = await pool.add_server(
                ServerConfig(name="boot", command="npx"), read_tools=False,
            )
        assert pool.get_server("boot") is conn
        mock_session.assert_awaited_once()
        mock_refresh.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_boot_arms_the_repair_when_the_spawn_fails(self):
        """A peer that was down at boot has no list and no reader coming for
        one: the spawn-only path must arm the same background repair a failed
        read arms, or nothing would ever ask it again."""
        pool = ConnectionPool()
        with (
            patch.object(
                MCPServerConnection, "refresh_tools", new=AsyncMock(),
            ),
            patch.object(
                MCPServerConnection, "ensure_session", new=AsyncMock(return_value=False),
            ),
            patch.object(
                MCPServerConnection, "arm_refresh",
            ) as mock_arm,
        ):
            await pool.add_server(
                ServerConfig(name="down", command="npx"), read_tools=False,
            )
        mock_arm.assert_called_once()


class TestConnectionPoolListAllTools:
    """Tests for list_all_tools."""

    def test_empty_for_unknown_server(self):
        pool = ConnectionPool()
        assert pool.list_all_tools("unknown") == []

    def test_empty_for_a_server_with_no_list(self):
        pool = ConnectionPool()
        conn = MCPServerConnection(ServerConfig(name="filesystem", command="npx"))
        pool._connections["filesystem"] = conn
        assert pool.list_all_tools("filesystem") == []

    def test_adds_full_name(self):
        pool = ConnectionPool()
        cfg = ServerConfig(name="filesystem", command="npx")
        conn = _listed(MCPServerConnection(cfg), ["read_file"])
        conn._tools[0]["description"] = "Read a file"
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
    """Tests for HTTP transport establishment (SDK-backed, E2).

    The previous raw JSON-RPC ``_request_http``/SSE-detection implementation
    was consolidated onto the mcp SDK transports (``sse_client`` /
    ``streamable_http_client`` + ``ClientSession``).  These tests exercise the
    SDK-wired path: establishing a session negotiates the peer's protocol era,
    and reading the tool list is what follows from it.
    """

    @pytest.fixture(autouse=True)
    def era_stub(self):
        """Stub the era glue — a mocked session cannot run the SDK probe.

        The stub reports LEGACY, so no listen supervisor is spawned (that
        path has its own test below); the negotiation itself is exercised in
        ``tests/test_mcp_era.py``.
        """
        negotiate = AsyncMock(return_value="2025-11-25")
        with (
            patch("slife.plugins.mcp_gateway.connection.negotiate_era", negotiate),
            patch(
                "slife.plugins.mcp_gateway.connection.peer_era",
                MagicMock(return_value="legacy"),
            ),
        ):
            yield negotiate

    @pytest.mark.asyncio
    async def test_establish_then_list_tools(self, era_stub):
        """Establishment negotiates the peer's era; refresh_tools lists."""
        from mcp.types import Tool

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        session = _mock_session([
            Tool(name="tool1", description="A tool", inputSchema={"type": "object"}),
        ])

        # Short-circuit transport establishment; the session it would have
        # produced is handed over directly.
        async def _already_connected():
            conn._session = session

        conn._connect_http = _already_connected
        assert await conn.refresh_tools() is True

        session.list_tools.assert_awaited_once()
        assert conn.tools_ok is True
        assert conn.tool_count == 1
        assert conn.list_tools()[0]["name"] == "tool1"

    @pytest.mark.asyncio
    async def test_modern_peer_opens_a_listen_watch(self):
        """A modern external server gets a listen stream — at that era the
        session channel carries no change notification at all."""
        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        session = _mock_session()

        async def _already_connected():
            conn._session = session

        conn._connect_http = _already_connected
        watch = AsyncMock()
        with (
            patch(
                "slife.plugins.mcp_gateway.connection.negotiate_era",
                AsyncMock(return_value="2026-07-28"),
            ),
            patch(
                "slife.plugins.mcp_gateway.connection.peer_era",
                MagicMock(return_value="modern"),
            ),
            patch("slife.plugins.mcp_gateway.connection.watch_tools_changed", watch),
        ):
            assert await conn.refresh_tools() is True
            assert conn._watch_task is not None
            watch.assert_called_once()
            await conn._cleanup_resources()      # stops the supervisor

        assert conn._watch_task is None

    @pytest.mark.asyncio
    async def test_call_tool_via_session(self):
        """call_tool returns formatted text from the SDK CallToolResult."""
        from mcp.types import CallToolResult, TextContent

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
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
        """A transport failure in call_tool triggers one rebuild, then retry."""
        from mcp.types import CallToolResult, TextContent

        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)
        session = AsyncMock()
        session.call_tool = AsyncMock(
            side_effect=[ConnectionError("died"), CallToolResult(
                content=[TextContent(type="text", text="recovered")],
            )],
        )
        conn._session = session
        rebuilt = {"n": 0}

        async def fake_ensure_session():
            rebuilt["n"] += 1
            conn._link_dead = False
            conn._session = session
            return True

        conn.ensure_session = fake_ensure_session
        result = await conn.call_tool("g", {})

        assert result == "recovered"
        # One call to check reachability before the call, one to rebuild
        # after the transport failure — and the tool is issued exactly twice.
        assert rebuilt["n"] == 2
        assert session.call_tool.await_count == 2
        # The rebuilt peer may have a different tool surface — the catalog is
        # told to re-read rather than left with the dead one's list.
        assert conn._tools_stale is True

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
    async def test_failed_establishment_records_the_error(self):
        """A transport failure is recorded as the fact __check reports."""
        cfg = ServerConfig(name="http_srv", url="http://remote:8080/mcp")
        conn = MCPServerConnection(cfg)

        async def boom():
            raise ConnectionError("down")

        conn._connect_http = boom
        with patch.object(conn, "_cleanup_resources", new=AsyncMock()):
            assert await conn.refresh_tools() is False

        assert conn.tools_ok is False
        assert "down" in (conn.error or "")
        await _stop(conn._refresh_task)


# ── The tool list: the health check, and its record ───────────────────────


class TestMCPServerConnectionRefresh:
    """Tests for refresh_tools — health is the tool list, not a connection.

    There is no probe: ``tools/list`` is what the catalog needs anyway, so its
    outcome is the verdict and its failure is the record.
    """

    @pytest.mark.asyncio
    async def test_stores_the_snapshot(self, caplog):
        from mcp.types import Tool

        conn = MCPServerConnection(ServerConfig(name="s", url="http://remote/mcp"))
        conn._session = _mock_session(
            [Tool(name="t1", description="A", inputSchema={"type": "object"})],
            ttl_ms=30000,
        )
        with patch.object(conn, "_notify_tools_changed", new=AsyncMock()) as notify:
            await conn.refresh_tools()

        assert conn.tools_ok is True
        assert conn.has_tools() is True
        assert conn.tool_count == 1
        assert conn._tools_ttl_ms == 30000
        assert conn.snapshot()["tools_age_s"] is not None
        notify.assert_awaited()          # the host re-reads the catalog

    @pytest.mark.asyncio
    async def test_a_list_inside_its_ttl_is_served_from_the_snapshot(self):
        """A chatty peer must not cost a re-fetch of a 1239-tool payload."""
        conn = _listed(
            MCPServerConnection(ServerConfig(name="s", command="echo")),
            ["t1"], ttl_ms=60000,
        )
        session = _mock_session()
        conn._session = session

        assert await conn.refresh_tools() is True
        session.list_tools.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_force_bypasses_the_ttl(self):
        conn = _listed(
            MCPServerConnection(ServerConfig(name="s", command="echo")),
            ["t1"], ttl_ms=60000,
        )
        session = _mock_session()
        conn._session = session

        assert await conn.refresh_tools(force=True) is True
        session.list_tools.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_an_expired_list_is_re_read(self):
        conn = _listed(
            MCPServerConnection(ServerConfig(name="s", command="echo")),
            ["t1"], ttl_ms=500, age_s=5.0,
        )
        session = _mock_session()
        conn._session = session

        assert await conn.refresh_tools() is True
        session.list_tools.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_a_change_event_marks_the_list_stale(self):
        conn = _listed(
            MCPServerConnection(ServerConfig(name="s", command="echo")),
            ["t1"], ttl_ms=60000,
        )
        assert conn._needs_fetch() is False

        await conn._handle_notification(
            SimpleNamespace(method="notifications/tools/list_changed"),
        )
        assert conn._needs_fetch() is True

    @pytest.mark.asyncio
    async def test_failure_records_the_error_and_arms_the_retry(self):
        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))
        conn._connect_stdio = AsyncMock(side_effect=ConnectionError("down"))

        assert await conn.refresh_tools() is False
        assert conn.tools_ok is False
        assert "down" in (conn.error or "")
        assert conn._refresh_task is not None and not conn._refresh_task.done()
        await _stop(conn._refresh_task)

    @pytest.mark.asyncio
    async def test_a_hung_list_does_not_hang_the_caller(self, monkeypatch):
        """A server that answers nothing is a failure, not a wedge."""
        import slife.timeouts as _T

        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))

        async def _hang():
            await asyncio.sleep(3600)

        session = _mock_session()
        session.list_tools = _hang
        conn._session = session
        monkeypatch.setattr(_T.timeouts.ready, "list_tools", 0.01)

        assert await conn.refresh_tools() is True or conn.tools_ok is False
        assert conn.tools_ok is False
        await _stop(conn._refresh_task)

    @pytest.mark.asyncio
    async def test_a_truncated_listing_is_reported(self, caplog):
        """We read one page — a peer that paginates must not be silent."""
        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))
        conn._session = _mock_session(["t1"], next_cursor="page2")

        with caplog.at_level(logging.WARNING):
            await conn.refresh_tools()

        assert conn.tool_count == 1
        assert any("truncated" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_transport_fault_is_recorded_and_kicks_a_rebuild(self):
        """The SDK hands us the death signal on the session channel.

        Dropping it (as the handler once did) left a dead link looking exactly
        like a live one until something happened to call it.  The teardown is
        deferred, not done here — this handler runs inside the dying session's
        own task group (see the method's note).
        """
        conn = _listed(MCPServerConnection(ServerConfig(name="s", command="echo")), ["t1"])
        conn._session = _mock_session()

        await conn._handle_notification(ConnectionError("Transport closed"))

        assert conn._link_dead is True
        assert "Transport closed" in (conn.error or "")
        assert conn.tools_ok is False
        assert conn._refresh_task is not None
        await _stop(conn._refresh_task)

    @pytest.mark.asyncio
    async def test_a_dead_link_is_torn_down_by_the_next_establishment(self):
        """The deferred half of the fault path — under the connect lock, in a
        task that is not the dying session's own."""
        conn = _listed(MCPServerConnection(ServerConfig(name="s", command="echo")), ["t1"])
        conn._session = _mock_session()
        conn._link_dead = True

        with patch.object(conn, "_cleanup_resources", new=AsyncMock()) as cleanup:
            assert await conn.ensure_session() is True

        assert cleanup.await_count == 1
        assert conn._link_dead is False


class TestMCPServerConnectionRepair:
    """The background re-list — the one case a request-driven policy misses.

    A server that is down while nobody is calling it has no tool list, and
    with no tool list it is invisible to the catalog — so nothing would ever
    ask again.  The loop exists for that, and only that: it stops the moment a
    list succeeds, so a healthy server is never polled.
    """

    @pytest.mark.asyncio
    async def test_the_retry_stops_once_a_list_succeeds(self):
        from slife.plugins.mcp_gateway import connection as conn_mod

        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))
        attempts = {"n": 0}
        session = _mock_session(["t1"])

        async def _flaky(*, force=False):
            attempts["n"] += 1
            if attempts["n"] == 1:
                conn._session = None
                return False            # refresh_tools records + returns
            conn._session = session
            return True

        conn.refresh_tools = _flaky
        with patch.object(conn_mod, "_REFRESH_RETRY_INITIAL", 0.01):
            await conn._refresh_until_listed()

        assert attempts["n"] == 2      # failed once, succeeded, stopped

    @pytest.mark.asyncio
    async def test_the_loop_stops_when_oauth_needs_a_human(self):
        """The first attempt can discover that this server needs a human —
        the loop must end there, not die with an unretrieved exception."""
        from slife.plugins.mcp_gateway.connection import NeedsUserAuthError

        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))

        async def _needs_auth(*, force=False):
            conn._needs_user_auth = True
            raise NeedsUserAuthError("device flow not completed")

        conn.refresh_tools = _needs_auth
        await conn._refresh_until_listed()   # returns; does not raise

        assert conn._needs_user_auth is True

    @pytest.mark.asyncio
    async def test_no_retry_for_a_disabled_server(self):
        conn = MCPServerConnection(ServerConfig(name="s", command="echo", enabled=False))
        conn._start_refresh_task()
        assert conn._refresh_task is None

    @pytest.mark.asyncio
    async def test_no_retry_when_oauth_needs_a_human(self):
        """F5: a background retry must never re-run the device flow."""
        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))
        conn._needs_user_auth = True
        conn._start_refresh_task()
        assert conn._refresh_task is None

    @pytest.mark.asyncio
    async def test_the_loop_exits_when_the_server_is_disabled(self):
        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))
        conn.refresh_tools = AsyncMock(return_value=False)
        conn.config.enabled = False

        await conn._refresh_until_listed()

        conn.refresh_tools.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_cancelled_read_still_recovers(self):
        """A6 regression, restated for the snapshot model: a read cancelled
        mid-flight (a host tool-timeout on mcp_set) leaves no tool list, so
        the retry must be armed — otherwise the server stays invisible until
        something happens to ask again."""
        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))

        async def _cancel_mid_read():
            raise asyncio.CancelledError

        conn.ensure_session = AsyncMock(side_effect=_cancel_mid_read)
        with pytest.raises(asyncio.CancelledError):
            await conn.refresh_tools()

        assert conn._refresh_task is not None and not conn._refresh_task.done()
        await _stop(conn._refresh_task)

    @pytest.mark.asyncio
    async def test_disconnect_cancels_the_retry(self):
        conn = MCPServerConnection(ServerConfig(name="s", command="echo"))
        cancelled = {"done": False}

        async def fake_retry():
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                cancelled["done"] = True
                raise

        conn._refresh_task = asyncio.create_task(fake_retry())
        await asyncio.sleep(0)  # let the task start

        await conn.disconnect()

        assert cancelled["done"] is True
        assert conn._refresh_task is None

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


class TestMCPServerConnectionLazyReconnect:
    """call_tool establishes a session on demand — the same lazy policy
    ``MCPClient`` uses for plugin links."""

    @pytest.mark.asyncio
    async def test_call_tool_establishes_a_session_lazily(self):
        from mcp.types import CallToolResult, TextContent

        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        established = {"done": False}
        session = AsyncMock()
        session.call_tool = AsyncMock(return_value=CallToolResult(
            content=[TextContent(type="text", text="ok")],
        ))

        async def fake_ensure_session():
            established["done"] = True
            conn._session = session
            return True

        conn.ensure_session = fake_ensure_session

        result = await conn.call_tool("echo", {"m": "x"})
        assert result == "ok"
        assert established["done"] is True

    @pytest.mark.asyncio
    async def test_call_tool_does_not_connect_disabled(self):
        cfg = ServerConfig(name="test", command="echo", enabled=False)
        conn = MCPServerConnection(cfg)
        conn.ensure_session = AsyncMock()

        with pytest.raises(ValueError, match="not connected"):
            await conn.call_tool("echo", {})

        conn.ensure_session.assert_not_called()

    @pytest.mark.asyncio
    async def test_call_tool_refuses_when_oauth_needs_a_human(self):
        from slife.plugins.mcp_gateway.connection import NeedsUserAuthError

        cfg = ServerConfig(name="test", command="echo")
        conn = MCPServerConnection(cfg)
        conn._needs_user_auth = True
        conn.ensure_session = AsyncMock()

        with pytest.raises(NeedsUserAuthError):
            await conn.call_tool("echo", {})

        conn.ensure_session.assert_not_called()


class TestMCPServerConnectionCancelCleanup:
    """A cancelled establishment must tear its transport down.

    An asyncio.timeout around add_server cancels it mid-flight.  Before this,
    the CancelledError path did not run _cleanup_resources — the spawned
    npx/uvx process, http client and stderr relay leaked.
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
                await conn.refresh_tools()

        mock_cleanup.assert_awaited_once()
        assert conn._session is None
        await _stop(conn._refresh_task)


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


class TestMCPServerConnectionNotify:
    """The change funnel fires on EVERY event that may supersede a list.

    The standalone server reads its servers' tool lists asynchronously on
    startup — a listener (a host re-syncing its tool registry) must be told
    about the first read too.  Full-diff registration on the listener side
    keeps the extra notification idempotent.
    """

    @pytest.mark.asyncio
    async def test_first_and_later_events_both_notify(self):
        cb = AsyncMock()
        conn = MCPServerConnection(
            ServerConfig(name="test", command="echo"), on_tools_changed=cb,
        )
        await conn._notify_tools_changed()
        cb.assert_awaited_once()
        await conn._notify_tools_changed()
        assert cb.await_count == 2

    @pytest.mark.asyncio
    async def test_listener_error_is_swallowed(self):
        async def boom(server_name):
            raise RuntimeError("listener failed")

        conn = MCPServerConnection(
            ServerConfig(name="test", command="echo"), on_tools_changed=boom,
        )
        # A failing listener must never propagate into a refresh.
        await conn._notify_tools_changed()
        await conn._notify_tools_changed()

    @pytest.mark.asyncio
    async def test_pool_passes_callback_to_connections(self):
        cb = AsyncMock()
        pool = ConnectionPool(on_tools_changed=cb)
        # enabled=False so add_server doesn't attempt a real read.
        conn = await pool.add_server(
            ServerConfig(name="test", command="echo", enabled=False),
        )
        assert conn._on_tools_changed is cb
