"""Tests for slife_mcp.connection — ConnectionPool, MCPServerConnection, ServerConfig."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife_mcp.connection import (
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
