"""Tests for mcp_plugin.server — wrapper-server tool registration.

Regression test for a decorator-detachment bug: the
``@mcp.tool(name="mcp_set")`` decorator must bind to the
``mcp_set`` function.  When a helper (``_server_config_equal``)
was accidentally placed between the decorator and the function, the tool
was registered with the helper's ``(a, b)`` signature, so every startup
auto-connect call failed pydantic validation ("Missing required argument
'a'") and no external MCP server could load.
"""

import pytest; pytestmark = pytest.mark.unit


import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_plugin.connection import ServerConfig, ServerStatus


@pytest.fixture
def restore_root_logger():
    """Importing the server reconfigures logging — restore it afterwards."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)


def _import_mcp_server():
    """Import the wrapper server fresh, stubbing the logging side-effect."""
    sys.modules.pop("mcp_plugin.server", None)
    with patch(
        "mcp_plugin.server_runtime.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        return importlib.import_module("mcp_plugin.server")


class TestAutoConnectConfigured:
    """_auto_connect_configured must register EVERY configured server in the
    pool — disabled ones registered but NOT connected — so mcp_list matches
    the config count.  A disabled server must not silently vanish from the
    listing (BUGS.md #5)."""

    @pytest.mark.asyncio
    async def test_registers_enabled_and_disabled(self, restore_root_logger):
        srv = _import_mcp_server()
        pool = MagicMock()
        pool.add_server = AsyncMock()
        fake_config = MagicMock()
        fake_config.load_config.return_value = {
            "servers": {
                "serper": {"command": "echo"},
                "disabled_svc": {"command": "echo", "enabled": False},
            },
        }
        fake_config.resolve_server_config.side_effect = (
            lambda name, entry: ServerConfig(
                name=name, command="echo", enabled=entry.get("enabled", True),
            )
        )
        with (
            patch.object(srv, "_pool", pool),
            patch.object(srv, "plugin_config", fake_config),
        ):
            await srv._auto_connect_configured()

        added = {c[0][0].name: c[0][0] for c in pool.add_server.await_args_list}
        assert set(added) == {"serper", "disabled_svc"}
        assert added["serper"].enabled is True
        assert added["disabled_svc"].enabled is False


class TestPersistEntry:
    """A server definition persists to config via add_server_entry."""

    def test_persist_entry_calls_add_server_entry(self, restore_root_logger):
        srv = _import_mcp_server()
        fake_cfg = MagicMock()
        with patch.object(srv, "plugin_config", fake_cfg):
            srv._persist_entry(
                "srv", "echo", [], None, "", None, "desc", None, None,
            )
        fake_cfg.add_server_entry.assert_called_once()


class TestMcpSetEnabled:
    """mcp_set_enabled toggles connect state — the connect attempt itself is
    the probe (no persisted healthy verdict gates enable anymore)."""

    @pytest.mark.asyncio
    async def test_enable_connected_returns_connected(self, restore_root_logger):
        import json as _json

        srv = _import_mcp_server()
        conn = MagicMock()
        conn.config = ServerConfig(name="live", command="echo")
        conn.status = ServerStatus.CONNECTED
        conn.list_tools.return_value = []
        pool = MagicMock()
        pool.get_server.return_value = conn
        with (
            patch.object(srv, "_pool", pool),
            patch.object(srv, "_ensure_store", AsyncMock(return_value=None)),
        ):
            result = await srv.mcp_set_enabled(name="live", enabled=True)
        parsed = _json.loads(result)
        assert parsed["status"] == "connected"

    @pytest.mark.asyncio
    async def test_disable_disconnects(self, restore_root_logger):
        import json as _json

        srv = _import_mcp_server()
        conn = MagicMock()
        conn.status = ServerStatus.CONNECTED
        pool = MagicMock()
        pool.get_server.return_value = conn
        pool.disconnect_server = AsyncMock()
        with (
            patch.object(srv, "_pool", pool),
            patch.object(srv, "_ensure_store", AsyncMock(return_value=None)),
        ):
            result = await srv.mcp_set_enabled(name="live", enabled=False)
        parsed = _json.loads(result)
        assert parsed["status"] == "disabled"
        pool.disconnect_server.assert_called_once()


class TestAddServerToolRegistration:
    """mcp_set must be registered with its real signature."""

    @pytest.mark.asyncio
    async def test_add_server_has_real_parameters(self, restore_root_logger):
        srv = _import_mcp_server()
        tools = await srv.mcp.list_tools()
        by_name = {t.name: t for t in tools}

        assert "mcp_set" in by_name
        props = by_name["mcp_set"].parameters.get("properties", {})
        # The real function's parameters.  The helper had only (a, b) —
        # if the decorator is mis-bound these are all absent.
        for expected in (
            "name", "command", "args", "env", "url",
            "headers", "description", "enabled",
        ):
            assert expected in props, f"missing param: {expected}"
        assert by_name["mcp_set"].parameters.get("required") == ["name"]

    @pytest.mark.asyncio
    async def test_helper_not_exposed_as_tool(self, restore_root_logger):
        srv = _import_mcp_server()
        tools = await srv.mcp.list_tools()
        names = {t.name for t in tools}
        assert "_server_config_equal" not in names

    @pytest.mark.asyncio
    async def test_expected_tool_set(self, restore_root_logger):
        srv = _import_mcp_server()
        tools = await srv.mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "mcp_set",
            "mcp_set_enabled",
            "mcp_remove",
            "mcp_list",
            "mcp_list_tools",
            "mcp_tool_search",
            "__mcp_call_tool",
            "__check",
            "__mcp_get_tool",
        }

    @pytest.mark.asyncio
    async def test_mcp_set_rejects_reserved_builtin_names(self, restore_root_logger):
        """REVIEW C8 — an external server cannot take a built-in plugin name,
        or its tools would collide/misroute in the harness namespace."""
        import json as _json

        srv = _import_mcp_server()
        for reserved in ("mcp", "memdb", "wechat", "memfiles", "a2a", "media"):
            result = await getattr(srv, "mcp_set")(name=reserved, command="echo")
            parsed = _json.loads(result)
            assert parsed.get("status") == "error", reserved
            assert "reserved" in parsed.get("error", ""), reserved

    @pytest.mark.asyncio
    async def test_lifespan_shuts_down_pool(self, restore_root_logger):
        """REVIEW M3 — the plugin lifespan releases the connection pool on
        server shutdown, so HTTP/SSE/stdio connections don't leak."""
        srv = _import_mcp_server()
        with patch.object(srv, "_pool") as mock_pool:
            mock_pool.shutdown = AsyncMock()
            async with srv._mcp_lifespan(None):
                pass
            mock_pool.shutdown.assert_awaited_once()


class TestWrapperNotifyToolsChanged:
    """Reconnect notifications: session capture + tools/list_changed broadcast."""

    @pytest.mark.asyncio
    async def test_pool_is_wired_to_notify(self, restore_root_logger):
        srv = _import_mcp_server()
        # The pool fires on_connected(server_name) → catalog sync + notify.
        assert srv._pool._on_connected is srv._on_connected

    def test_capture_session_accumulates(self, restore_root_logger):
        srv = _import_mcp_server()
        srv._active_sessions.clear()
        fake_ctx = MagicMock()
        fake_ctx.session = object()
        srv._capture_session(fake_ctx)
        assert len(srv._active_sessions) == 1
        srv._capture_session(fake_ctx)  # idempotent for the same session
        assert len(srv._active_sessions) == 1
        srv._capture_session(None)  # no request context → no-op
        assert len(srv._active_sessions) == 1

    @pytest.mark.asyncio
    async def test_notify_no_sessions_is_noop(self, restore_root_logger):
        srv = _import_mcp_server()
        srv._active_sessions.clear()
        await srv._notify_tools_changed()  # must not raise

    @pytest.mark.asyncio
    async def test_notify_drops_dead_sessions(self, restore_root_logger):
        srv = _import_mcp_server()
        alive = MagicMock()
        alive.send_tool_list_changed = AsyncMock()
        dead = MagicMock()
        dead.send_tool_list_changed = AsyncMock(side_effect=RuntimeError("gone"))
        srv._active_sessions = {alive, dead}

        await srv._notify_tools_changed()

        alive.send_tool_list_changed.assert_awaited_once()
        dead.send_tool_list_changed.assert_awaited_once()
        # The dead session was dropped; the alive one is kept for next time.
        assert srv._active_sessions == {alive}

    @pytest.mark.asyncio
    async def test_notify_calls_send_tool_list_changed(self, restore_root_logger):
        srv = _import_mcp_server()
        sess = MagicMock()
        sess.send_tool_list_changed = AsyncMock()
        srv._active_sessions = {sess}

        await srv._notify_tools_changed()

        sess.send_tool_list_changed.assert_awaited_once()


class TestMCPListToolsSingleRead:
    """mcp_list_tools — a single live read.  The in-memory catalog is a direct
    projection of the connection pool, so there is no persisted catalog to
    compare against (the drift/stale machinery is gone entirely)."""

    @staticmethod
    async def _list(srv, *, connected=True, live=None, live_raise=""):
        """Call mcp_list_tools with a patched pool (no catalog store involved)."""
        import json as _json

        conn = MagicMock()
        conn.status = ServerStatus.CONNECTED if connected else ServerStatus.DISCONNECTED
        pool = MagicMock()
        pool.get_server.return_value = conn
        if live_raise:
            pool.list_all_tools.side_effect = RuntimeError(live_raise)
        else:
            pool.list_all_tools.return_value = live or []

        with patch.object(srv, "_pool", pool):
            raw = await srv.mcp_list_tools(server="fs")
        return _json.loads(raw)

    @staticmethod
    def _live(name, desc=""):
        return {"name": name, "description": desc, "inputSchema": {"type": "object"}}

    @pytest.mark.asyncio
    async def test_connected_lists_live_tools(self, restore_root_logger):
        srv = _import_mcp_server()
        live = [self._live("a", "read a"), self._live("b", "read b")]
        out = await self._list(srv, live=live)

        assert out["status"] == "ok"
        assert out["connected"] is True
        assert out["tools"] == live
        assert out["tool_count"] == 2
        assert out["note"] == "catalog rebuilt live in memory"
        assert "stale" not in out
        assert "hint" not in out

    @pytest.mark.asyncio
    async def test_connected_empty_tools(self, restore_root_logger):
        srv = _import_mcp_server()
        out = await self._list(srv, live=[])
        assert out["connected"] is True
        assert out["tools"] == []
        assert out["tool_count"] == 0

    @pytest.mark.asyncio
    async def test_not_connected_returns_note(self, restore_root_logger):
        srv = _import_mcp_server()
        out = await self._list(srv, connected=False)
        assert out["connected"] is False
        assert out["tools"] == []
        assert out["tool_count"] == 0
        assert "not connected" in out["note"]

    @pytest.mark.asyncio
    async def test_live_read_failure_reports_unavailable(self, restore_root_logger):
        """A live read exception on a connected server ⇒ 'MCP unavailable'."""
        srv = _import_mcp_server()
        out = await self._list(srv, connected=True, live_raise="pool boom")

        assert out["status"] == "error"
        assert "MCP unavailable" in out["error"]
        assert "pool boom" in out["error"]


class TestCaptureClientEmbeddings:
    """The initialize middleware stores clientInfo.other.embeddings (standard
    handshake params) for the semantic warm-up."""

    def test_captures_embeddings_from_initialize(self, restore_root_logger):
        import asyncio as _asyncio
        from types import SimpleNamespace

        from mcp.types import (
            ClientCapabilities,
            Implementation,
            InitializeRequest,
            InitializeRequestParams,
        )

        srv = _import_mcp_server()
        req = InitializeRequest(
            params=InitializeRequestParams(
                protocolVersion="2025-06-18",
                capabilities=ClientCapabilities(),
                clientInfo=Implementation(
                    name="slife", version="0.1.0",
                ).model_copy(update={"other": {"embeddings": {"base_url": "http://host.example/v1"}}}),
            )
        )
        mw = srv._CaptureClientEmbeddings()
        context = SimpleNamespace(message=req)

        async def call_next(ctx):
            return None

        srv._client_embeddings = None
        _asyncio.run(mw.on_initialize(context, call_next))
        assert srv._client_embeddings == {"base_url": "http://host.example/v1"}
        srv._client_embeddings = None

    def test_no_embeddings_keeps_fallback(self, restore_root_logger):
        import asyncio as _asyncio
        from types import SimpleNamespace

        from mcp.types import (
            ClientCapabilities,
            Implementation,
            InitializeRequest,
            InitializeRequestParams,
        )

        srv = _import_mcp_server()
        req = InitializeRequest(
            params=InitializeRequestParams(
                protocolVersion="2025-06-18",
                capabilities=ClientCapabilities(),
                clientInfo=Implementation(name="other", version="1.0"),
            )
        )
        mw = srv._CaptureClientEmbeddings()
        context = SimpleNamespace(message=req)

        async def call_next(ctx):
            return None

        srv._client_embeddings = None
        _asyncio.run(mw.on_initialize(context, call_next))
        assert srv._client_embeddings is None
