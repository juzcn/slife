"""Tests for slife.plugins.mcp_gateway.server — wrapper-server tool registration.

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

from slife.plugins.mcp_gateway.connection import ServerConfig


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
    sys.modules.pop("slife.plugins.mcp_gateway.server", None)
    with patch(
        "slife.server_utils.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        return importlib.import_module("slife.plugins.mcp_gateway.server")


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
            "mcp": {
                "servers": {
                    "serper": {"command": "echo"},
                    "disabled_svc": {"command": "echo", "enabled": False},
                },
            },
        }
        # tools.json5 section view — the merged mcp.servers (+legacy) read
        # the boot path must use; the test locks the section format so a
        # regression back to the pre-restructure top-level ``servers`` key
        # fails here.
        fake_config._servers_dict.side_effect = (
            lambda raw: raw.get("mcp", {}).get("servers", {})
            or raw.get("servers", {})
        )
        # No ``rest-api`` section in this config, and the set must be a real
        # iterable — the boot path asks once for the names placement says are
        # REST APIs.
        fake_config.rest_api_names.return_value = set()
        fake_config.resolve_server_config.side_effect = (
            lambda name, entry, **kw: ServerConfig(
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
        fake_cfg.server_section.return_value = None
        with patch.object(srv, "plugin_config", fake_cfg):
            srv._persist_entry(
                "srv", "echo", [], None, "", None, "desc", None, None,
            )
        fake_cfg.add_server_entry.assert_called_once()

    def test_empty_fields_are_not_written(self, restore_root_logger):
        """A stdio server has no ``url``, a public REST API has no ``auth`` —
        an empty field is noise in a file people read and hand-edit, and
        ``url: ""`` even reads as a claim that there is one."""
        srv = _import_mcp_server()
        fake_cfg = MagicMock()
        fake_cfg.server_section.return_value = None
        with patch.object(srv, "plugin_config", fake_cfg):
            srv._persist_entry(
                "srv", "echo", [], {}, "", None, "", None, None,
            )
        entry = fake_cfg.add_server_entry.call_args[0][1]
        assert entry == {"command": "echo"}

    def test_an_existing_entry_keeps_its_own_section(self, restore_root_logger):
        """Upserting a REST API must write back into ``rest-api``: defaulting
        to ``mcp`` left a second copy under ``mcp.servers``, where the
        enable/disable path finds it first and flips the copy nobody reads."""
        srv = _import_mcp_server()
        fake_cfg = MagicMock()
        fake_cfg.server_section.return_value = "rest-api"
        with patch.object(srv, "plugin_config", fake_cfg):
            srv._persist_entry(
                "github", "uvx", ["mcp-openapi-proxy"], {"A": "b"}, "", None,
                "desc", None, None,
            )
        assert fake_cfg.add_server_entry.call_args.kwargs["section"] == "rest-api"

    def test_a_new_entry_defaults_to_the_mcp_section(self, restore_root_logger):
        srv = _import_mcp_server()
        fake_cfg = MagicMock()
        fake_cfg.server_section.return_value = None
        with patch.object(srv, "plugin_config", fake_cfg):
            srv._persist_entry(
                "fresh", "echo", None, None, "", None, "", None, None,
            )
        assert fake_cfg.add_server_entry.call_args.kwargs["section"] == "mcp"


class TestMcpSetEnabled:
    """mcp_set_enabled toggles the server — enabling reads its tool list, and
    the read itself is the check (no persisted healthy verdict to gate on)."""

    @pytest.mark.asyncio
    async def test_enable_with_a_list_returns_connected(self, restore_root_logger):
        import json as _json

        srv = _import_mcp_server()
        conn = MagicMock()
        conn.config = ServerConfig(name="live", command="echo")
        conn.tools_ok = True
        conn.list_tools.return_value = []
        pool = MagicMock()
        pool.get_server.return_value = conn
        with patch.object(srv, "_pool", pool):
            result = await srv.mcp_set_enabled(name="live", enabled=True)
        parsed = _json.loads(result)
        assert parsed["status"] == "connected"

    @pytest.mark.asyncio
    async def test_disable_disconnects(self, restore_root_logger):
        import json as _json

        srv = _import_mcp_server()
        conn = MagicMock()
        pool = MagicMock()
        pool.get_server.return_value = conn
        pool.disconnect_server = AsyncMock()
        with (
            patch.object(srv, "_pool", pool),
            patch.object(srv.plugin_config, "set_server_enabled", return_value=True) as persist,
        ):
            result = await srv.mcp_set_enabled(name="live", enabled=False)
        parsed = _json.loads(result)
        assert parsed["status"] == "disabled"
        pool.disconnect_server.assert_called_once()
        persist.assert_called_once_with("live", False)

    @pytest.mark.asyncio
    async def test_enable_persists_true(self, restore_root_logger):
        """Re-enable must clear the persisted ``enabled: false`` a prior
        disable wrote — otherwise the server loads disabled again on the
        next restart (the disable path persists; the enable path must too)."""
        import json as _json

        srv = _import_mcp_server()
        conn = MagicMock()
        conn.config = ServerConfig(name="live", command="echo")
        conn.tools_ok = True
        conn.list_tools.return_value = []
        pool = MagicMock()
        pool.get_server.return_value = conn
        with (
            patch.object(srv, "_pool", pool),
            patch.object(srv.plugin_config, "set_server_enabled", return_value=True) as persist,
        ):
            result = await srv.mcp_set_enabled(name="live", enabled=True)
        parsed = _json.loads(result)
        assert parsed["status"] == "connected"
        persist.assert_called_once_with("live", True)


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
            "__mcp_list_tools",
            "__mcp_call_tool",
            "__check",
        }

    @pytest.mark.asyncio
    async def test_mcp_set_rejects_reserved_builtin_names(self, restore_root_logger):
        """REVIEW C8 — an external server cannot take a built-in plugin name,
        or its tools would collide/misroute in the harness namespace.

        The reserved set is derived from the central plugin contract, so it
        covers every built-in — including sharefile and job-coding, which a
        hand-written list used to miss."""
        import json as _json

        from slife.plugins.spec import mcp_child_reserved_names

        srv = _import_mcp_server()
        reserved = mcp_child_reserved_names()
        assert {"sharefile", "job-coding"} <= reserved
        for name in sorted(reserved):
            result = await getattr(srv, "mcp_set")(name=name, command="echo")
            parsed = _json.loads(result)
            assert parsed.get("status") == "error", name
            assert "reserved" in parsed.get("error", ""), name

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
    """Reconnect notifications: publish on the server's subscription bus.

    The session-set notifier is gone — at the modern era a change event
    reaches clients only through a ``subscriptions/listen`` stream, so the
    wrapper publishes an event and ``ListenHandler`` fans it out.
    """

    @pytest.mark.asyncio
    async def test_pool_is_wired_to_notify(self, restore_root_logger):
        srv = _import_mcp_server()
        # The pool fires on_tools_changed(server_name) → catalog sync + notify.
        assert srv._pool._on_tools_changed is srv._on_tools_changed

    @pytest.mark.asyncio
    async def test_notify_publishes_tools_changed(self, restore_root_logger):
        srv = _import_mcp_server()
        bus = MagicMock()
        bus.publish = AsyncMock()
        with patch.object(srv._notifier, "_bus", bus):
            await srv._notify_tools_changed()

        bus.publish.assert_awaited_once()
        event = bus.publish.await_args.args[0]
        assert type(event).__name__ == "ToolsListChanged"

    @pytest.mark.asyncio
    async def test_publish_without_a_bus_is_a_noop(self, restore_root_logger):
        srv = _import_mcp_server()
        with patch.object(srv._notifier, "_bus", None):
            await srv._notify_tools_changed()  # must not raise

    @pytest.mark.asyncio
    async def test_publish_failure_never_propagates(self, restore_root_logger):
        """A bus fault must not take down the caller (a tool handler)."""
        srv = _import_mcp_server()
        bus = MagicMock()
        bus.publish = AsyncMock(side_effect=RuntimeError("bus gone"))
        with patch.object(srv._notifier, "_bus", bus):
            await srv._notify_tools_changed()  # must not raise

    def test_listen_handler_is_registered_on_import(self, restore_root_logger):
        """fastmcp never registers `subscriptions/listen` — this module does,
        and without it a modern client's change notifications have no path."""
        srv = _import_mcp_server()
        assert "subscriptions/listen" in srv.mcp._mcp_server._request_handlers


class TestMCPListToolsSingleRead:
    """mcp_list_tools — always-live single read (the wrapper owns no catalog).

    The shared host tools.db is fed by the agent's reconcile calling this
    tool on connect; the wrapper itself returns the live MCP ``tools/list``.
    """

    @staticmethod
    async def _list(srv, *, connected=True, live=None, live_raise="", autoload=False,
                    limit=0, tool="mcp_list_tools"):
        """Call a listing tool with a patched pool (no real catalog store).

        ``connected`` here means "a tool list is held"; with none, the tool
        re-reads the server before answering.  ``tool`` picks which of the two
        listings to drive — the capped ``mcp_list_tools`` (the model's) or the
        uncapped ``__mcp_list_tools`` (the host's).
        """
        import contextlib
        import json as _json

        conn = MagicMock()
        conn.config = ServerConfig(name="fs", command="x", auto_load=autoload)
        conn.has_tools = MagicMock(return_value=connected)
        conn.refresh_tools = AsyncMock(return_value=connected)
        conn.error = None if connected else "connect failed"
        pool = MagicMock()
        pool.get_server.return_value = conn
        if live_raise:
            pool.list_all_tools.side_effect = RuntimeError(live_raise)
        else:
            pool.list_all_tools.return_value = live or []

        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(srv, "_pool", pool))
            fn = getattr(srv, tool)
            if tool == "mcp_list_tools":
                raw = await fn(server="fs", limit=limit)
            else:
                raw = await fn(server="fs")
        return _json.loads(raw)

    @staticmethod
    def _live(name, desc=""):
        return {"name": name, "description": desc, "inputSchema": {"type": "object"}}

    @pytest.mark.asyncio
    async def test_connected_lists_live_tools(self, restore_root_logger):
        """The live built-in MCP tools/list is ALWAYS the source now."""
        srv = _import_mcp_server()
        live = [self._live("a", "read a"), self._live("b", "read b")]
        out = await self._list(srv, live=live)

        assert out["status"] == "ok"
        assert out["connected"] is True
        assert out["source"] == "live"
        assert out["tools"] == live
        assert out["tool_count"] == 2
        assert "tools/list" in out["note"]

    @pytest.mark.asyncio
    async def test_autoload_is_live_too(self, restore_root_logger):
        """auto_load servers list live as well — the wrapper no longer keeps a
        catalog branch; the host reconcile decides load semantics."""
        srv = _import_mcp_server()
        live = [self._live("a")]
        out = await self._list(srv, live=live, autoload=True)

        assert out["source"] == "live"
        assert out["tools"] == live

    @pytest.mark.asyncio
    async def test_connected_empty_tools(self, restore_root_logger):
        srv = _import_mcp_server()
        out = await self._list(srv, live=[])
        assert out["connected"] is True
        assert out["tools"] == []
        assert out["tool_count"] == 0

    @pytest.mark.asyncio
    async def test_the_listing_is_capped(self, restore_root_logger):
        """A many-tool server must not spend the model's context on names it
        never asked for: the tail is replaced by the one instruction that
        finds a specific tool, and the real total is still reported."""
        srv = _import_mcp_server()
        live = [self._live(f"op{i}") for i in range(30)]
        out = await self._list(srv, live=live, limit=5)

        assert out["tool_count"] == 30      # the server's real total
        assert len(out["tools"]) == 5       # what the caller was shown
        assert out["truncated"] is True
        assert "tool_search" in out["note"]

    @pytest.mark.asyncio
    async def test_the_cap_defaults_to_the_configured_value(self, restore_root_logger):
        """``mcp.tool_list_limit`` (default 20) is what an unqualified listing
        gets — the model never has to know a number."""
        from slife.plugins.mcp_gateway import config as gateway_config

        srv = _import_mcp_server()
        live = [self._live(f"op{i}") for i in range(30)]
        out = await self._list(srv, live=live)

        assert len(out["tools"]) == gateway_config.DEFAULT_TOOL_LIST_LIMIT
        assert gateway_config.tool_list_limit() == gateway_config.DEFAULT_TOOL_LIST_LIMIT
        assert out["truncated"] is True

    @pytest.mark.asyncio
    async def test_the_internal_twin_is_uncapped(self, restore_root_logger):
        """The host's catalog sync writes a row per tool, so a truncated
        listing would silently drop the rest of the server's tools — it reads
        ``__mcp_list_tools`` instead, which no cap touches."""
        srv = _import_mcp_server()
        live = [self._live(f"op{i}") for i in range(30)]
        out = await self._list(srv, live=live, tool="__mcp_list_tools")

        assert len(out["tools"]) == 30
        assert out["tool_count"] == 30
        assert out["truncated"] is False
        assert "tool_search" not in out["note"]

    @pytest.mark.asyncio
    async def test_the_internal_twin_is_hidden_from_the_model(self, restore_root_logger):
        """``__``-prefixed tools are filtered out of the model's tool set
        (``is_internal_tool``) — that prefix is the whole hiding mechanism."""
        from slife.server_utils import is_internal_tool

        srv = _import_mcp_server()
        names = {t.name for t in await srv.mcp.list_tools()}

        assert "__mcp_list_tools" in names      # registered for the host …
        assert is_internal_tool("__mcp_list_tools")   # … and not for the model
        assert not is_internal_tool("mcp_list_tools")

    @pytest.mark.asyncio
    async def test_a_small_server_is_not_truncated(self, restore_root_logger):
        srv = _import_mcp_server()
        live = [self._live("a"), self._live("b")]
        out = await self._list(srv, live=live)

        assert out["truncated"] is False
        assert len(out["tools"]) == 2

    @pytest.mark.asyncio
    async def test_no_tool_list_returns_note(self, restore_root_logger):
        """No list — after a re-read attempt — is reported as such, with the
        reason, rather than as an empty success."""
        srv = _import_mcp_server()
        out = await self._list(srv, connected=False)
        assert out["connected"] is False
        assert out["tools"] == []
        assert out["tool_count"] == 0
        assert "no tool list" in out["note"]
        assert "connect failed" in out["note"]

    @pytest.mark.asyncio
    async def test_live_read_failure_reports_unavailable(self, restore_root_logger):
        """A live read exception on a connected server ⇒ 'MCP unavailable'."""
        srv = _import_mcp_server()
        out = await self._list(srv, connected=True, live_raise="pool boom")

        assert out["status"] == "error"
        assert "MCP unavailable" in out["error"]
        assert "pool boom" in out["error"]


class TestBootConnectsEveryEnabled:
    """Startup is decided by tools.json5 alone: enabled ⇒ bring it up now.

    Nothing is remembered between sessions — no db, no snapshot — so a server
    that was down when the user quit is retried at the next boot like any
    other, and a disabled one is merely registered (``mcp_list`` must still
    list the same set as the config).

    Bringing it up is the TRANSPORT only (``read_tools=False``): boot spawns
    and stops there, because the tool list is asked for by the host's
    reconcile and boot has no reader waiting for one.
    """

    @pytest.mark.asyncio
    async def test_connects_enabled_and_registers_disabled(self, restore_root_logger):
        srv = _import_mcp_server()
        pool = MagicMock()
        pool.add_server = AsyncMock()
        fake_config = MagicMock()
        fake_config.load_config.return_value = {
            "mcp": {
                "servers": {
                    "up": {"command": "echo"},
                    "off": {"command": "echo", "enabled": False},
                },
            },
        }
        fake_config._servers_dict.side_effect = (
            lambda raw: raw.get("mcp", {}).get("servers", {})
            or raw.get("servers", {})
        )
        # No ``rest-api`` section in this config, and the set must be a real
        # iterable — the boot path asks once for the names placement says are
        # REST APIs.
        fake_config.rest_api_names.return_value = set()
        fake_config.resolve_server_config.side_effect = (
            lambda name, entry, **kw: ServerConfig(
                name=name, command="echo", enabled=entry.get("enabled", True),
            )
        )
        with (
            patch.object(srv, "_pool", pool),
            patch.object(srv, "plugin_config", fake_config),
        ):
            await srv._auto_connect_configured()

        calls = {c[0][0].name: c.kwargs for c in pool.add_server.await_args_list}
        # enabled → transport up, tool list left to the first reader
        assert calls["up"] == {"read_tools": False}
        assert calls["off"] == {"connect": False}      # disabled → register only


class TestNoCatalogAccess:
    """The wrapper must not touch tools.db at all.

    Connection state lives on the catalog's tool rows (written by the HOST),
    and which servers to bring up comes from tools.json5 — so the child has no
    business reading (or writing) the shared catalog.
    """

    def test_wrapper_has_no_db_helpers(self, restore_root_logger):
        srv = _import_mcp_server()
        assert not hasattr(srv, "_eager_set_from_db")
        assert not hasattr(srv, "_mark_server_error")

    def test_wrapper_source_has_no_sqlite_or_tools_db(self, restore_root_logger):
        import inspect

        srv = _import_mcp_server()
        src = inspect.getsource(srv)
        assert "sqlite3" not in src
        assert "get_tools_db_path" not in src


class TestNotifyReachesEveryListenStream:
    """A change event must reach *every* listener, not the last caller.

    The retired mechanism remembered sessions from the request path, so a
    server that finished connecting AFTER the host's last call had nobody to
    notify — the "auto_load tool still needs mcp_tool_load" bug.  A bus
    publish carries no such coupling: listeners subscribe, and the event
    lands on all of them.
    """

    @pytest.mark.asyncio
    async def test_publish_reaches_two_listeners(self, restore_root_logger):
        from mcp.server.subscriptions import InMemorySubscriptionBus

        from slife.server_utils import ToolsChangedNotifier

        bus = InMemorySubscriptionBus()
        seen: list[object] = []
        bus.subscribe(lambda event: seen.append(event))
        bus.subscribe(lambda event: seen.append(event))

        notifier = ToolsChangedNotifier(bus)
        await notifier.flush()

        assert len(seen) == 2
        assert all(type(e).__name__ == "ToolsListChanged" for e in seen)
