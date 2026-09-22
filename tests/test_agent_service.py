"""Tests for Slife.agent.service — AgentService lifecycle and message processing."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import json as _json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.agent.service import (
    AgentService,
    _extract_turn_annotation,
    _short_reason,
    compact_tool_results,
)
from slife.agent.inbox import MemorySaveError
from slife.ui.i18n import t
from slife.agent.plugins import PluginStartStatus
from slife.agent.llm_client import TokenUsage
from slife.a2a.identity import HUMAN, WECHAT


# ── AgentService initialisation ─────────────────────────────────────────────


class TestAgentServiceInit:
    """Tests for AgentService.__init__."""

    def test_basic_initialization(self, sample_config):
        config = sample_config
        service = AgentService(config)

        assert service.config is config
        assert service.llm_client is not None
        assert service.agent_loop is not None
        assert service.tool_registry is not None
        assert service.message_history is not None
        assert isinstance(service.session_usage, TokenUsage)

    def test_initial_mcp_state(self, sample_config):
        config = sample_config
        service = AgentService(config)

        assert service._plugins["mcp-gateway"].client is None
        assert service._plugins["mcp-gateway"].process is None
        assert service.mcp_enabled is False

    def test_initial_a2a_state(self, sample_config):
        config = sample_config
        service = AgentService(config)

        assert service._plugins["a2a"].process is None
        assert service.a2a_enabled is False


class TestAgentServiceProperties:
    """Tests for AgentService properties."""

    def test_model_display_name(self, sample_config):
        service = AgentService(sample_config)
        assert service.model_display_name == "deepseek/deepseek-v4-flash"

    def test_thinking_enabled(self, sample_config):
        config = sample_config
        service = AgentService(config)
        assert service.thinking_enabled is False

    def test_subagent_manager_none_initially(self, sample_config):
        service = AgentService(sample_config)
        assert service.subagent_manager is None


class TestAgentServiceClear:
    """Tests for AgentService.clear()."""

    def test_clear_resets_usage(self, sample_config):
        service = AgentService(sample_config)
        service.session_usage = TokenUsage(
            prompt_tokens=500, completion_tokens=300, total_tokens=800,
        )

        service.clear()

        assert service.session_usage.total_tokens == 0

    def test_clear_preserves_system_prompt(self, sample_config):
        service = AgentService(sample_config)
        initial_count = len(service.message_history.messages)
        # System prompt should be present
        assert initial_count >= 1

        service.clear()

        # clear() preserves the system prompt
        assert len(service.message_history.messages) == 1
        assert service.message_history.messages[0]["role"] == "system"


# ── AgentService MCP lifecycle ──────────────────────────────────────────────


class TestAgentServiceMCPEnrichment:
    """The retained MCP enrichment adapter — mcp-gateway self-hosts config,
    auto-connect and reconnection; this is the harness-side glue that wires
    the wrapper client and harvests the external servers' tools."""

    @pytest.mark.asyncio
    async def test_wire_mcp_glue_wires_handler_and_harvests(self, sample_config):
        """_wire_mcp_glue re-points the tool context, wires the
        tools/list_changed handler, and re-runs the proxy reconcile."""
        service = AgentService(sample_config)
        client = AsyncMock()
        client.is_connected = True
        service._plugins["mcp-gateway"].client = client

        with patch.object(service, "_sync_mcp_proxies", AsyncMock()) as mock_sync:
            await service._wire_mcp_glue()

        assert service._tool_ctx.mcp_client is client
        # Bound methods are recreated on access, so compare __self__/__func__.
        assert client.on_notification is not None
        assert client.on_notification.__self__ is service
        assert client.on_notification.__func__ is AgentService._on_mcp_tools_changed
        mock_sync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_tools_changed_triggers_reharvest(self, sample_config):
        """A tools/list_changed notification from the wrapper re-syncs proxies."""
        service = AgentService(sample_config)
        service._plugins["mcp-gateway"].client = AsyncMock()
        with patch.object(service, "_sync_mcp_proxies", AsyncMock()) as mock_sync:
            await service._on_mcp_tools_changed("notifications/tools/list_changed", {})
        mock_sync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_on_tools_changed_ignores_other_methods(self, sample_config):
        """Only tools/list_changed triggers a reconcile."""
        service = AgentService(sample_config)
        service._plugins["mcp-gateway"].client = AsyncMock()
        with patch.object(service, "_sync_mcp_proxies", AsyncMock()) as mock_sync:
            await service._on_mcp_tools_changed("notifications/initialized", {})
        mock_sync.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_proxies_registers_every_enabled_server(self, sample_config):
        """_sync_mcp_proxies registers proxies for EVERY enabled server.

        ``auto_load`` is not a registration gate: it decides what the catalog
        seeds as loaded, not whether an execution instance exists.  A DISABLED
        server is not touched at all: mirroring it means reading its tool list,
        and reading is connecting (``enabled: false`` = "stays configured but
        is not connected").
        """
        service = AgentService(sample_config)
        client = AsyncMock()
        client.is_connected = True
        client.call_tool = AsyncMock(return_value=_json.dumps([
            {"name": "autol", "enabled": True, "auto_load": True},
            {"name": "ondemand", "enabled": True, "auto_load": False},
            {"auto_load": True, "enabled": True},          # no name at all
            {"name": "disabled", "enabled": False, "auto_load": True},
        ]))
        service._plugins["mcp-gateway"].client = client

        with patch.object(
            service, "_discover_and_register_external_tools", AsyncMock(),
        ) as mock_reg:
            await service._sync_mcp_proxies()

        client.call_tool.assert_any_await("__mcp_list")
        assert {c.kwargs["server_name"] for c in mock_reg.await_args_list} == {
            "autol", "ondemand",   # auto_load no longer decides
        }
        # Nothing asked for the disabled server's tool list — asking is what
        # would spawn it.
        assert not any(
            c.args and c.args[0] == "__mcp_list_tools"
            and c.args[1].get("server") == "disabled"
            for c in client.call_tool.await_args_list
        )

    @staticmethod
    def _fake_gateway(tools_server: str, *, enabled: bool = True,
                      listed: bool = True, reachable: bool = True,
                      spawn_settled: bool = True):
        """A gateway client whose __mcp_list holds one server, whose tool list
        holds one tool.  ``tools_server`` is stamped onto the tool dict because
        the registering path (``create_proxy_tools``) reads it, exactly as the
        wrapper's own ``__mcp_list_tools`` payload supplies it.

        The two "no list yet" shapes are separate facts, because the host acts
        on them differently: ``listed=False, reachable=True`` is a server whose
        transport is up but which has not answered a ``tools/list`` yet (a slow
        REST proxy at startup — still starting, waited for), while
        ``reachable=False`` is one that never came up at all (settled: it is
        unavailable and nothing waits on it).
        """
        client = AsyncMock()
        client.is_connected = True

        async def fake_call_tool(name, arguments=None):
            if name == "__mcp_list":
                return _json.dumps([
                    {"name": tools_server, "enabled": enabled, "auto_load": False},
                ])
            if name == "__check":
                return _json.dumps({
                    "servers": [
                        {"name": tools_server, "tools_ok": listed,
                         "reachable": reachable},
                    ],
                    "spawn_settled": spawn_settled,
                })
            if name in ("mcp_list_tools", "__mcp_list_tools"):
                return _json.dumps({
                    "server": tools_server, "connected": listed,
                    "tools": [] if not listed else [
                        {"server": tools_server, "name": "search",
                         "description": "Search stuff",
                         "inputSchema": {"type": "object",
                                         "properties": {"q": {"type": "string"}}}},
                    ],
                    "tool_count": 0 if not listed else 1,
                })
            raise AssertionError(f"unexpected tool call: {name} {arguments}")

        client.call_tool = fake_call_tool
        return client

    async def _sync_with_catalog(self, sample_config, tmp_path, server: str, *,
                                 enabled: bool = True, activity=None,
                                 listed: bool = True, reachable: bool = True,
                                 spawn_settled: bool = True, seed=None):
        """Run one reconcile against a real catalog; return (service, store).

        *activity* is registered BEFORE that pass, so a test can observe the
        genuinely-first reconcile rather than a later one.  *listed* says
        whether the fake server has answered a ``tools/list`` yet; *reachable*
        whether its transport ever came up; *spawn_settled* whether the boot
        pass that brings servers up has finished.  *seed* is an async callable
        handed the store before the op window opens, for the rows the real
        startup writes ahead of its first pass (the boot seed, the skill/cli
        mirror) — written outside the window so they are not this pass's delta.
        """
        from slife.tools.catalog import CatalogStore
        from slife.tools.catalog_service import ToolCatalogService

        store = CatalogStore(tmp_path / "tools.db")
        await store.open()
        if seed is not None:
            await seed(store)
        # The op window, armed where the process arms it: with the catalog,
        # before its first write.  ``_init_catalog`` does this in the real
        # startup; this helper builds the catalog by hand, so without it the
        # pass would report against a window nobody opened.
        store.begin_ops()
        service = AgentService(sample_config)
        service._catalog = ToolCatalogService(store, write_owner=True)
        service._catalog_semantic = None
        service._plugins["mcp-gateway"].client = self._fake_gateway(
            server, enabled=enabled, listed=listed, reachable=reachable,
            spawn_settled=spawn_settled,
        )
        if activity is not None:
            service.on_activity(activity)
        # The reconcile also purges catalog servers that left tools.yaml — pin
        # the gateway config view to the mocked pool so this test's server
        # counts as configured (no TOOLS_FILE isolation here, so the real repo
        # config would otherwise read as the truth).
        #
        # The skill/cli mirror is stubbed for the same reason: it reads the
        # real tools.yaml and skills dir, and those rows would ride along in
        # the pass's op delta (which these tests assert on).
        with patch(
            "slife.plugins.mcp_gateway.config.servers", return_value={server: {}},
        ), patch.object(
            AgentService, "_refresh_local_rows_if_changed", AsyncMock(),
        ):
            await service._sync_mcp_proxies()
        return service, store

    @pytest.mark.asyncio
    async def test_sync_proxies_registers_on_demand_server_proxies(
        self, sample_config, tmp_path,
    ):
        """An ENABLED on-demand server gets proxies at reconcile time, not just
        catalog rows.

        The registry is the execution pool: a call needs an instance, so a row
        loaded in a previous session — whose ``load_status`` survives the
        restart while its proxy does not — has no route until the reconcile
        re-registers it.  (What fixes that is an instance, not a load state:
        see ``test_tools_catalog_injection`` for how a call is refused.)
        """
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "ondemand",
        )
        try:
            names = {t.name for t in service.tool_registry.list_tools()}
            assert "ondemand__search" in names

            # Registered is not injected: the row still lands unloaded, and
            # only load_status puts a tool into the turn snapshot.
            row = await store.get_tool("ondemand__search")
            assert row is not None
            assert row["category"] == "mcp"
            assert row["load_status"] == "unloaded"
            assert row["schema"]

            # ... so the restart case works: a row loaded last session is
            # immediately executable after the next reconcile.  (Re-pin the
            # config view — the purge compares against it, and without the
            # patch this second reconcile reads the real repo config and drops
            # "ondemand"'s rows as unconfigured.)
            await store.set_load_status("ondemand__search", "loaded", bump=True)
            with patch(
                "slife.plugins.mcp_gateway.config.servers",
                return_value={"ondemand": {}},
            ):
                await service._sync_mcp_proxies()
            assert "ondemand__search" in {
                t.name for t in service.tool_registry.list_tools()
            }
            assert (await store.get_tool("ondemand__search"))["load_status"] == "loaded"
        finally:
            # An unclosed aiosqlite connection keeps its thread alive and
            # hangs pytest at exit — close on the failure path too.
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_reports_the_tool_set_once_then_only_on_change(
        self, sample_config, tmp_path,
    ):
        """The user is waiting to know when tools are callable, so the pass
        that converges reports with its duration — and it is the ONLY pass that
        ever reports: later ones ride the gateway's tools/list_changed cadence,
        so a line each would be a heartbeat rather than news."""
        # AsyncMock, not a lambda: _notify_activity AWAITS the callback, and a
        # sync callable's None return is swallowed as a failure.  Registered
        # before the helper's pass so this observes the FIRST reconcile.
        events = AsyncMock()
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "svc", activity=events,
        )
        try:
            assert events.await_count == 1
            assert events.await_args.args[0] == "tools_synced"
            kw = events.await_args.kwargs
            # ``total`` is what is USABLE, read off the catalog: in this
            # helper's world that is the one row the server's mirror wrote.
            # (Not the registry — the service was constructed with builtins
            # this hand-built catalog never seeded, and the registry-less
            # families could never appear in it at all: see
            # test_total_counts_the_registry_less_families_too.)
            assert kw["total"] == 1
            # The delta is what the startup WROTE to the catalog, not what the
            # registry went from-to: this helper's startup is one pass over a
            # cold db, so that is exactly the one row the server's mirror
            # inserted — never the whole registry, which a name-set diff would
            # report as "added" on every restart.
            assert kw["added"] == 1
            assert kw["updated"] == 0 and kw["removed"] == 0
            assert kw["error"] == ""
            assert isinstance(kw["seconds"], float)

            await service._sync_mcp_proxies()   # unchanged re-run
            assert events.await_count == 1      # ...and stays quiet
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_the_op_delta_spans_every_pass_not_just_the_reporting_one(
        self, sample_config, tmp_path,
    ):
        """The line reports the whole startup, so an earlier pass's rows count.

        A cold start converges on a LATE pass: a server is "still starting"
        until its ``tools/list`` lands, so the slowest one arrives last and the
        pass that finally reports is the pass that mirrored it.  Arming the op
        window per pass therefore reported one source out of a whole catalog —
        a real cold boot read ``新增 1239`` against the 1586 rows it had just
        written, 1239 being exactly the github REST source whose listing
        happened to be the one that converged the pass.
        """
        from slife.tools.catalog import CatalogStore
        from slife.tools.catalog_service import ToolCatalogService

        listed = {"a": True, "b": False}   # "b" answers only on the later pass

        client = AsyncMock()
        client.is_connected = True

        async def call_tool(name, arguments=None):
            if name == "__mcp_list":
                return _json.dumps([
                    {"name": "a", "enabled": True, "auto_load": False},
                    {"name": "b", "enabled": True, "auto_load": False},
                ])
            if name == "__check":
                return _json.dumps({
                    "servers": [
                        {"name": "a", "tools_ok": True, "reachable": True},
                        {"name": "b", "tools_ok": listed["b"],
                         "reachable": True},
                    ],
                    "spawn_settled": True,
                })
            if name in ("mcp_list_tools", "__mcp_list_tools"):
                server = (arguments or {}).get("server", "")
                ok = listed.get(server, False)
                return _json.dumps({
                    "server": server, "connected": ok,
                    "tools": [] if not ok else [
                        {"server": server, "name": "search",
                         "description": "Search stuff",
                         "inputSchema": {"type": "object",
                                         "properties": {"q": {"type": "string"}}}},
                    ],
                    "tool_count": 1 if ok else 0,
                })
            raise AssertionError(f"unexpected tool call: {name} {arguments}")

        client.call_tool = call_tool
        store = CatalogStore(tmp_path / "tools.db")
        await store.open()
        store.begin_ops()          # where _init_catalog arms it
        service = AgentService(sample_config)
        service._catalog = ToolCatalogService(store, write_owner=True)
        service._catalog_semantic = None
        events = AsyncMock()
        service.on_activity(events)
        service._plugins["mcp-gateway"].client = client
        try:
            with patch(
                "slife.plugins.mcp_gateway.config.servers",
                return_value={"a": {}, "b": {}},
            ), patch.object(
                AgentService, "_refresh_local_rows_if_changed", AsyncMock(),
            ):
                # Pass 1: "a" mirrors, "b" is still starting — so silence.
                await service._sync_mcp_proxies()
                assert events.await_count == 0
                # Pass 2: "b" arrives and converges the set, and THIS pass is
                # the one that reports.  Both servers' rows are the startup's.
                listed["b"] = True
                await service._sync_mcp_proxies()
        finally:
            await store.close()
        assert events.await_count == 1
        assert events.await_args.kwargs["added"] == 2

    @pytest.mark.asyncio
    async def test_total_counts_the_registry_less_families_too(
        self, sample_config, tmp_path,
    ):
        """``total`` is what is USABLE — and a skill row is usable.

        ``FUNCTION_CATEGORIES`` splits on the LOAD STATE, not on callability: a
        skill / cli row is searchable, switchable and usable, it simply has no
        state to flip — and no instance either (a skill IS its SKILL.md text),
        which is exactly why a registry count cannot see it.  Counting the
        registry promised "N 个工具可用" while reporting instances, so the line
        read 1575 against a catalog it had just been written 1586 rows into.
        """
        async def seed(store):
            await store.reconcile([{
                "name": "browser-use", "description": "Drive a browser",
                "category": "skill", "source_id": "skill",
                "schema": "# browser-use\n", "status": "enabled",
            }])

        events = AsyncMock()
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "svc", activity=events, seed=seed,
        )
        try:
            # Usable, and out of the registry by construction: there is no
            # instance to register, so only a catalog count can see it.
            assert "browser-use" not in {
                t.name for t in service.tool_registry.list_tools()
            }
            # It and the server's one mirrored tool; the run above counted 1.
            assert events.await_args.kwargs["total"] == 2
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_stays_silent_when_a_later_pass_moves_tools(
        self, sample_config, tmp_path,
    ):
        """One line per process.  A later pass that really does move tools —
        the server left the config, so its proxies are unregistered and its
        rows purged — still says nothing: that line belongs to startup, and a
        session that keeps talking about its tool set is a heartbeat."""
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "svc",
        )
        try:
            events = AsyncMock()
            service.on_activity(events)
            await service._sync_mcp_proxies()   # already reported by the helper
            events.reset_mock()

            client = service._plugins["mcp-gateway"].client
            original = client.call_tool

            async def shrunk(name, arguments=None):
                if name == "__mcp_list":
                    return _json.dumps([])
                return await original(name, arguments)

            client.call_tool = shrunk
            await service._sync_mcp_proxies()

            events.assert_not_awaited()
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_waits_for_a_server_that_has_not_listed_yet(
        self, sample_config, tmp_path,
    ):
        """A configured server that is up but has not answered ``tools/list``
        is STILL STARTING, not absent — the startup line waits for it.

        Announcing a half-arrived set would both lie and turn the pass that
        finally carries the rest into a phantom change ("新增 1239" on every
        boot, for a REST proxy that takes tens of seconds to install itself).
        """
        events = AsyncMock()
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "slow", activity=events, listed=False,
        )
        try:
            events.assert_not_awaited()     # still starting — silence

            # The server finishes listing; its tools/list_changed wakes the
            # next pass, which is the one that reports.
            service._plugins["mcp-gateway"].client = self._fake_gateway("slow")
            with patch(
                "slife.plugins.mcp_gateway.config.servers",
                return_value={"slow": {}},
            ), patch.object(
                AgentService, "_refresh_local_rows_if_changed", AsyncMock(),
            ):
                await service._sync_mcp_proxies()

            assert events.await_count == 1
            kw = events.await_args.kwargs
            assert kw["error"] == ""
            assert "slow__search" in {
                t.name for t in service.tool_registry.list_tools()
            }
            # The late arrival is what this pass WROTE (the row it mirrored),
            # not "the rest of the tool set appeared out of nowhere".
            assert kw["added"] == 1
            assert kw["updated"] == 0 and kw["removed"] == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_does_not_wait_for_a_server_that_never_came_up(
        self, sample_config, tmp_path,
    ):
        """A server whose transport never came up is SETTLED, not pending: its
        tools are marked ``unavailable`` and the line reports what the set has.

        Waiting on it would hold the startup line for a server that is simply
        down — and its recovery needs no reservation, because the gateway's
        armed retry re-syncs it (silently: this process has already reported).
        """
        events = AsyncMock()
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "dead", activity=events,
            listed=False, reachable=False,
        )
        try:
            assert events.await_count == 1                     # no waiting
            kw = events.await_args.kwargs
            assert kw["error"] == ""
            assert kw["added"] == 0 and kw["removed"] == 0     # nothing mirrored
            assert "dead__search" not in {
                t.name for t in service.tool_registry.list_tools()
            }
            # It is configured, so its (absent) tools are not a removal either.
            row = await store.get_tool("dead__search")
            assert row is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_waits_for_the_spawn_pass_to_finish(
        self, sample_config, tmp_path,
    ):
        """The sync does not start judging until the boot pass is over.

        A server with no transport is one of two things — a spawn still in
        flight, or one that failed — and only ``spawn_settled`` tells them
        apart.  So while the pass is running, nothing is a verdict and the line
        stays away; the moment it settles, the same server is ``unavailable``
        and the line goes out.
        """
        events = AsyncMock()
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "booting", activity=events,
            listed=False, reachable=False, spawn_settled=False,
        )
        try:
            events.assert_not_awaited()     # unjudged — the spawn may still win

            # ...and the boot pass finishes without that server coming up.
            service._plugins["mcp-gateway"].client = self._fake_gateway(
                "booting", listed=False, reachable=False,
            )
            with patch(
                "slife.plugins.mcp_gateway.config.servers",
                return_value={"booting": {}},
            ), patch.object(
                AgentService, "_refresh_local_rows_if_changed", AsyncMock(),
            ):
                await service._sync_mcp_proxies()

            assert events.await_count == 1
            assert events.await_args.kwargs["error"] == ""
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_reports_a_failure_rather_than_going_silent(
        self, sample_config, tmp_path,
    ):
        """Silence has to keep meaning 'still syncing' — a pass that blew up
        must say so, or a dead sync reads exactly like a slow one."""
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "svc",
        )
        try:
            events = []
            service.on_activity(lambda kind, **kw: events.append(kw))
            with patch.object(
                service, "_mark_server_connectivity",
                AsyncMock(side_effect=RuntimeError("boom")),
            ):
                with pytest.raises(RuntimeError):
                    await service._sync_mcp_proxies()

            assert len(events) == 1
            assert events[0]["error"] == "boom"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_proxies_never_asks_a_disabled_server(self, sample_config):
        """Nothing reads a disabled server's tool list — reading is connecting.

        The rows-only mirror of a disabled server (a882ed8) spawns every
        switched-off server at boot just to fill a catalog from it.
        """
        service = AgentService(sample_config)
        asked: list[tuple] = []

        async def fake_call_tool(name, arguments=None):
            asked.append((name, (arguments or {}).get("server")))
            if name == "__mcp_list":
                return _json.dumps([
                    {"name": "live", "enabled": True},
                    {"name": "off", "enabled": False},
                ])
            if name == "__check":
                return _json.dumps({"servers": [], "spawn_settled": True})
            raise AssertionError(f"unexpected tool call: {name} {arguments}")

        client = AsyncMock()
        client.is_connected = True
        client.call_tool = AsyncMock(side_effect=fake_call_tool)
        service._plugins["mcp-gateway"].client = client

        with patch.object(
            service, "_discover_and_register_external_tools", AsyncMock(),
        ):
            await service._sync_mcp_proxies()

        assert ("__mcp_list", None) in asked       # the pass did run
        assert ("__mcp_list_tools", "off") not in asked

    @pytest.mark.asyncio
    async def test_sync_proxies_leaves_a_disabled_server_alone(
        self, sample_config, tmp_path,
    ):
        """A disabled server is not mirrored, so a fresh catalog has no rows
        for it — the rows a *previously enabled* one keeps are the catalog's,
        not the reconcile's (see the test below)."""
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "off", enabled=False,
        )
        try:
            assert "off__search" not in {
                t.name for t in service.tool_registry.list_tools()
            }
            assert await store.get_tool("off__search") is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_proxies_keeps_the_rows_of_a_server_just_disabled(
        self, sample_config, tmp_path,
    ):
        """Disabling a server keeps the rows it mirrored while enabled.

        They are the model's map of the server (``tool_search`` still finds
        them, reporting ``disabled``); only the execution route and the process
        go away.  Nothing re-reads the server to keep that true.
        """
        service, store = await self._sync_with_catalog(
            sample_config, tmp_path, "off", enabled=True,
        )
        try:
            assert await store.get_tool("off__search") is not None
            # The config switches it off; the next reconcile must leave the row
            # alone.  The helper's patches are re-applied because this pass
            # purges the sources tools.yaml does not name.
            service._plugins["mcp-gateway"].client = self._fake_gateway(
                "off", enabled=False,
            )
            with patch(
                "slife.plugins.mcp_gateway.config.servers",
                return_value={"off": {}},
            ), patch.object(
                AgentService, "_refresh_local_rows_if_changed", AsyncMock(),
            ):
                await service._sync_mcp_proxies()

            row = await store.get_tool("off__search")
            assert row is not None
            assert row["category"] == "mcp"
            assert await store.get_effective("off__search") == "disabled"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_sync_proxies_noop_when_disconnected(self, sample_config):
        """A disconnected / absent client means nothing to reconcile."""
        service = AgentService(sample_config)
        client = AsyncMock()
        client.is_connected = False
        service._plugins["mcp-gateway"].client = client
        with patch.object(service, "_discover_and_register_external_tools", AsyncMock()) as mock_reg:
            await service._sync_mcp_proxies()
        mock_reg.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_sync_proxies_reconcile_keeps_disabled_loaded_proxy(self, sample_config):
        """A configured server's on-demand-loaded proxy is KEPT even when its
        server is disabled — the catalog's effective-status join hides it
        (disable ≠ remove; remove is the only unregister path)."""
        from slife.mcp.tool_adapter import create_proxy_tools

        service = AgentService(sample_config)
        client = AsyncMock()
        client.is_connected = True

        async def fake_call_tool(name, arguments=None):
            if name == "__mcp_list":
                return _json.dumps([{"name": "foo", "enabled": False, "auto_load": False}])
            raise AssertionError(f"unexpected tool call: {name} {arguments}")

        client.call_tool = fake_call_tool
        service._plugins["mcp-gateway"].client = client
        proxy = create_proxy_tools(client, [
            {"server": "foo", "name": "t1", "description": "",
             "inputSchema": {"type": "object", "properties": {}}},
        ])[0]
        service.tool_registry.register(proxy)

        await service._sync_mcp_proxies()

        names = {t.name for t in service.tool_registry.list_tools()}
        assert "foo__t1" in names

    @pytest.mark.asyncio
    async def test_sync_proxies_reconcile_keeps_enabled_loaded_proxy(self, sample_config):
        """An enabled on-demand-loaded proxy survives the reconcile."""
        from slife.mcp.tool_adapter import create_proxy_tools

        service = AgentService(sample_config)
        client = AsyncMock()
        client.is_connected = True

        async def fake_call_tool(name, arguments=None):
            if name == "__mcp_list":
                return _json.dumps([{"name": "foo", "enabled": True, "auto_load": False}])
            raise AssertionError(f"unexpected tool call: {name} {arguments}")

        client.call_tool = fake_call_tool
        service._plugins["mcp-gateway"].client = client
        proxy = create_proxy_tools(client, [
            {"server": "foo", "name": "t1", "description": "",
             "inputSchema": {"type": "object", "properties": {}}},
        ])[0]
        service.tool_registry.register(proxy)

        await service._sync_mcp_proxies()

        names = {t.name for t in service.tool_registry.list_tools()}
        assert "foo__t1" in names

    @pytest.mark.asyncio
    async def test_sync_proxies_reconcile_drops_removed_server_proxy(self, sample_config):
        """A proxy whose server left the CONFIG is unregistered — removal is
        the only unregister path (mcp_remove)."""
        from slife.mcp.tool_adapter import create_proxy_tools

        service = AgentService(sample_config)
        client = AsyncMock()
        client.is_connected = True

        async def fake_call_tool(name, arguments=None):
            if name == "__mcp_list":
                return _json.dumps([])  # "foo" no longer configured
            raise AssertionError(f"unexpected tool call: {name} {arguments}")

        client.call_tool = fake_call_tool
        service._plugins["mcp-gateway"].client = client
        proxy = create_proxy_tools(client, [
            {"server": "foo", "name": "t1", "description": "",
             "inputSchema": {"type": "object", "properties": {}}},
        ])[0]
        service.tool_registry.register(proxy)

        await service._sync_mcp_proxies()

        names = {t.name for t in service.tool_registry.list_tools()}
        assert "foo__t1" not in names


class TestAgentServiceMCPDiscovery:
    """External-server tool discovery — idempotent full-diff registration."""

    @pytest.fixture(autouse=True)
    def _clean_health(self):
        """The health store is module-global — keep it clean around these
        tests so a recovery record from one test can't leak into another."""
        from slife.health import clear
        clear()
        yield
        clear()

    def _client_with(self, status_servers, tools_by_server):
        client = AsyncMock()
        client.is_connected = True

        async def fake_call_tool(name, arguments=None):
            if name == "__check":
                return _json.dumps(status_servers, ensure_ascii=False)
            if name in ("mcp_list_tools", "__mcp_list_tools"):
                return _json.dumps(
                    {"tools": tools_by_server.get(arguments.get("server"), [])},
                    ensure_ascii=False,
                )
            raise AssertionError(f"unexpected tool call: {name}")

        client.call_tool = fake_call_tool
        return client

    @staticmethod
    def _tool(server, name):
        return {
            "server": server,
            "name": name,
            "description": "",
            "inputSchema": {"type": "object", "properties": {}},
        }

    @pytest.mark.asyncio
    async def test_discover_empty_tools_leaves_registry_untouched(self, sample_config):
        service = AgentService(sample_config)
        service._plugins["mcp-gateway"].client = self._client_with(
            status_servers=[], tools_by_server={},
        )
        service.tool_registry.register(SimpleNamespace(name="foo__keep"))

        await service._discover_and_register_external_tools("foo")

        names = {t.name for t in service.tool_registry.list_tools()}
        assert "foo__keep" in names  # no flicker on an empty / not-ready list

    @pytest.mark.asyncio
    async def test_discover_diff_unregisters_dropped_tools(self, sample_config):
        service = AgentService(sample_config)
        service._plugins["mcp-gateway"].client = self._client_with(
            status_servers=[],
            tools_by_server={"foo": [self._tool("foo", "t1")]},
        )
        service.tool_registry.register(SimpleNamespace(name="foo__gone"))

        await service._discover_and_register_external_tools("foo")

        names = {t.name for t in service.tool_registry.list_tools()}
        assert "foo__t1" in names
        assert "foo__gone" not in names

    @pytest.mark.asyncio
    async def test_discover_records_ok_health_replace(self, sample_config):
        """Successful tool registration supersedes a stale startup warning —
        ``mcp_servers/ok`` with replace=True, so the health store itself
        reflects the recovery, not just the live check_mcp diff."""
        from slife.health import record
        record(
            "mcp_servers", "warning",
            key="foo", value="connect_pending",
            hint="enabled but not yet connected; retrying in background.",
        )
        service = AgentService(sample_config)
        service._plugins["mcp-gateway"].client = self._client_with(
            status_servers=[],
            tools_by_server={"foo": [self._tool("foo", "t1")]},
        )

        await service._discover_and_register_external_tools("foo")

        from slife.health import get_report
        recs = [e for e in get_report() if e.get("key") == "foo"]
        assert len(recs) == 1  # the warning was replaced, not appended
        assert recs[0]["level"] == "ok"
        assert recs[0]["value"] == "tools registered"

    @pytest.mark.asyncio
    async def test_discover_with_no_tools_leaves_health_untouched(self, sample_config):
        """An empty / not-ready tool list is NOT a recovery — the stale
        warning must survive (no flicker in either registry or health)."""
        from slife.health import record
        record(
            "mcp_servers", "warning",
            key="foo", value="connect_pending",
            hint="enabled but not yet connected; retrying in background.",
        )
        service = AgentService(sample_config)
        service._plugins["mcp-gateway"].client = self._client_with(
            status_servers=[], tools_by_server={},
        )
        service.tool_registry.register(SimpleNamespace(name="foo__keep"))

        await service._discover_and_register_external_tools("foo")

        from slife.health import get_report
        recs = [e for e in get_report() if e.get("key") == "foo"]
        assert len(recs) == 1
        assert recs[0]["level"] == "warning"


class TestAgentServicePluginRescan:
    """Runtime tool-set resync for plugins that mutate their own tools
    (job-coding registers/removes job tools on the fly) — the generic
    ``_rescan_plugin_tools`` diff, mirroring the external-server diff."""

    @staticmethod
    def _tool(name):
        return {
            "server": "job-coding",
            "name": name,
            "description": "",
            "inputSchema": {"type": "object", "properties": {}},
        }

    def _client_withtools(self, names):
        client = AsyncMock()
        client.is_connected = True
        client.list_tools = AsyncMock(
            return_value=[self._tool(n) for n in names]
        )
        return client

    def _service_with(self, config, names, registered=None):
        from slife.agent.plugins import PluginLifecycle

        service = AgentService(config)
        lifecycle = PluginLifecycle("job-coding", service)
        lifecycle.client = self._client_withtools(names)
        lifecycle.registered_tools = set(registered or ())
        service._plugins["job-coding"] = lifecycle
        return service

    @pytest.mark.asyncio
    async def test_rescan_registers_new_tools(self, sample_config):
        service = self._service_with(sample_config, names=["translate", "shout"])
        names = {t.name for t in service.tool_registry.list_tools()}
        assert "translate" not in names

        await service._rescan_plugin_tools("job-coding")

        names = {t.name for t in service.tool_registry.list_tools()}
        assert "translate" in names
        assert "shout" in names
        assert service._plugins["job-coding"].registered_tools == {"translate", "shout"}

    @pytest.mark.asyncio
    async def test_rescan_unregisters_dropped_tools(self, sample_config):
        # State is consistent before the rescan: both tools registered + the
        # recorded set match; the plugin no longer reports "gone".
        service = self._service_with(
            sample_config, names=["translate"], registered={"translate", "gone"},
        )
        service.tool_registry.register(SimpleNamespace(name="translate"))
        service.tool_registry.register(SimpleNamespace(name="gone"))

        await service._rescan_plugin_tools("job-coding")

        names = {t.name for t in service.tool_registry.list_tools()}
        assert "translate" in names
        assert "gone" not in names

    @pytest.mark.asyncio
    async def test_rescan_filters_internal_tools(self, sample_config):
        service = self._service_with(sample_config, names=["__check", "translate"])
        await service._rescan_plugin_tools("job-coding")
        names = {t.name for t in service.tool_registry.list_tools()}
        assert "translate" in names
        assert "__check" not in names

    @pytest.mark.asyncio
    async def test_rescan_flag_stays_quiet_when_unconnected(self, sample_config):
        client = AsyncMock()
        client.is_connected = False
        service = self._service_with(sample_config, names=[])
        service._plugins["job-coding"].client = client
        service.tool_registry.register(SimpleNamespace(name="keep"))
        await service._rescan_plugin_tools("job-coding")
        names = {t.name for t in service.tool_registry.list_tools()}
        assert "keep" in names


# ── Subagent HTTP connect (manifest sharing) ───────────────────────────────


class TestAgentServiceConnectPluginHttp:
    """The generic subagent connect path — connect + register + per-plugin
    glue + tools/list_changed wiring, for ANY plugin (no hardcoded subset)."""

    @staticmethod
    def _service_with(config, plugin_name):
        from slife.agent.plugins import PluginLifecycle

        service = AgentService(config)
        lifecycle = PluginLifecycle(plugin_name, service)
        client = AsyncMock()
        client.is_connected = True
        client.list_tools = AsyncMock(return_value=[
            {"server": plugin_name, "name": "my_tool",
             "description": "", "inputSchema": {"type": "object", "properties": {}}},
        ])
        lifecycle.client = client
        service._plugins[plugin_name] = lifecycle
        return service, client

    @pytest.mark.asyncio
    async def test_generic_connect_registers_tools_and_glue(self, sample_config):
        """A shared plugin's tools land in the registry and the glue re-points
        its health-check client — media has no bespoke wrapper anymore."""
        service, _ = self._service_with(sample_config, "media")
        with patch.object(
            service._plugins["media"].__class__, "connect_http", AsyncMock(),
        ):
            await service.connect_plugin_http("media", 12345)
        assert any(t.name == "my_tool" for t in service.tool_registry.list_tools())
        assert service._tool_ctx.media_client is service._plugins["media"].client

    @pytest.mark.asyncio
    async def test_mcp_uses_reconcile_handler(self, sample_config):
        """The gateway shares the wrapper: on_notification →
        _on_mcp_tools_changed; initial connect also re-syncs external proxies
        (chosen by spec.gateway, not a name branch)."""
        service, client = self._service_with(sample_config, "mcp-gateway")
        with patch.object(
            service._plugins["mcp-gateway"].__class__, "connect_http", AsyncMock(),
        ), patch.object(service, "_sync_mcp_proxies", AsyncMock()) as mock_sync:
            await service.connect_plugin_http("mcp-gateway", 12345)
        assert client.on_notification.__self__ is service
        assert client.on_notification.__func__ is AgentService._on_mcp_tools_changed
        mock_sync.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gateway_connect_also_repoints_the_ctx_client(self, sample_config):
        """The gateway declares a ctx_field like every other plugin, so the
        worker must get ``mcp_client`` too — not only the proxy reconcile.

        The branch was an ``elif``, so the gateway skipped the re-point and a
        worker kept ``mcp_client`` None while holding a perfectly good
        connection.  system_health then reported "client not connected" for a
        gateway the worker had just connected, while the parent — reading the
        same live state through its own client — reported OK.  The docstring
        always said "also"; the branch said otherwise.
        """
        service, client = self._service_with(sample_config, "mcp-gateway")
        with patch.object(
            service._plugins["mcp-gateway"].__class__, "connect_http", AsyncMock(),
        ), patch.object(service, "_sync_mcp_proxies", AsyncMock()):
            await service.connect_plugin_http("mcp-gateway", 12345)

        assert service._tool_ctx.mcp_client is client

    @pytest.mark.asyncio
    async def test_generic_connect_wires_list_changed_handler(self, sample_config):
        """Non-mcp plugins wire the generic rescan handler (job-coding's
        dynamic tools stay live under a subagent)."""
        service, client = self._service_with(sample_config, "job-coding")
        with patch.object(
            service._plugins["job-coding"].__class__, "connect_http", AsyncMock(),
        ):
            await service.connect_plugin_http("job-coding", 12345)
        assert client.on_notification is not None
        # The generic handler is the _handler closure (async func) — wiring
        # is present when set; the rescan itself is covered by _rescan_plugin_tools.
        assert callable(client.on_notification)

    @pytest.mark.asyncio
    async def test_reconnect_unregisters_dropped_tools(self, sample_config):
        """B4 regression: reconnecting to a plugin that no longer advertises a
        tool must unregister the vanished tool — otherwise it lingers bound to
        the old, disconnected client (the main-agent watchdog does this diff;
        the subagent HTTP path must too)."""
        from slife.agent.plugins import PluginLifecycle

        service = AgentService(sample_config)
        lifecycle = PluginLifecycle("media", service)
        client = AsyncMock()
        client.is_connected = True
        client.list_tools = AsyncMock(return_value=[
            {"server": "media", "name": "keep",
             "description": "", "inputSchema": {"type": "object", "properties": {}}},
            {"server": "media", "name": "drop",
             "description": "", "inputSchema": {"type": "object", "properties": {}}},
        ])
        lifecycle.client = client
        service._plugins["media"] = lifecycle

        with patch.object(lifecycle.__class__, "connect_http", AsyncMock()):
            await service.connect_plugin_http("media", 12345)
        assert {t.name for t in service.tool_registry.list_tools()} >= {"keep", "drop"}

        # The plugin restarted and dropped "drop".
        client2 = AsyncMock()
        client2.is_connected = True
        client2.list_tools = AsyncMock(return_value=[
            {"server": "media", "name": "keep",
             "description": "", "inputSchema": {"type": "object", "properties": {}}},
        ])
        lifecycle.client = client2
        with patch.object(lifecycle.__class__, "connect_http", AsyncMock()):
            await service.connect_plugin_http("media", 12346)

        names = {t.name for t in service.tool_registry.list_tools()}
        assert "keep" in names
        assert "drop" not in names


# ── AgentService memory ─────────────────────────────────────────────────────


class TestAgentServiceMemory:
    """Tests for memory-related methods."""

    def test_memory_not_enabled_initially(self, sample_config):
        service = AgentService(sample_config)
        assert service.memdb_enabled is False

    @pytest.mark.asyncio
    async def test_start_memdb_branch_wires_client(self, sample_config):
        """The uniform start path spawns memdb via the generic engine and
        exposes the client (its spec's ctx_field) for the embeddings_*
        hot-reload tools — no per-plugin branch."""
        config = sample_config
        service = AgentService(config)
        service.config.memdb_config = MagicMock()
        mock_client = MagicMock()

        with patch.object(
            service, "_spawn_plugin_generic", AsyncMock(return_value=True)
        ) as mock_spawn, \
             patch.object(service, "_arm_watchdog", MagicMock()):
            service._plugins["memdb"].client = mock_client
            result = await service._start_plugin_server_impl(
                "memdb", "slife.plugins.memdb.server",
            )

        mock_spawn.assert_called_once_with("memdb", "slife.plugins.memdb.server")
        assert result == PluginStartStatus.STARTED
        assert service._tool_ctx.memdb_client is mock_client

    @pytest.mark.asyncio
    async def test_save_to_memory_no_turn_content_noop(self, sample_config):
        """No turn content in the history → nothing to persist → no raise,
        even with memdb disconnected (the extraction returns None first)."""
        service = AgentService(sample_config)  # memdb not connected
        await service.save_to_memory(user_message="test", token_count=100)

    @pytest.mark.asyncio
    async def test_save_to_memory_memdb_down_raises(self, sample_config):
        """A4 regression: a completed turn whose memdb write cannot happen
        (client not connected) must raise MemorySaveError — never a silent
        drop.  The inbox surfaces it like an LLM API error."""
        service = AgentService(sample_config)  # memdb not connected
        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        with pytest.raises(MemorySaveError):
            await service.save_to_memory(
                user_message="hi", token_count=10, history=conv,
            )

        # Not the DB-hard-stop path: no frozen inbox / memory-broken flag.
        assert service.inbox._frozen is False
        assert service._memory_broken is False

    @pytest.mark.asyncio
    async def test_save_to_memory_no_user_message(self, sample_config):
        service = AgentService(sample_config)
        # Should not raise with no user_message
        await service.save_to_memory()

    @pytest.mark.asyncio
    async def test_save_to_memory_passes_created_at(self, sample_config):
        """The turn-start timestamp captured at display time flows to the
        __memory_save_turn tool as created_at (→ diary), keeping restore
        aligned with the live TUI."""
        from datetime import datetime

        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        ts = datetime(2026, 8, 12, 14, 32, 9).astimezone()
        await service.save_to_memory(
            user_message="hi", token_count=10,
            history=conv, channel="human", created_at=ts,
        )

        mock_client.call_tool.assert_awaited_once()
        tool_name, args = mock_client.call_tool.await_args.args
        assert tool_name == "__memory_save_turn"
        assert args["created_at"].startswith("2026-08-12T14:32:09")
        # completed_at is captured after _ensure_turn_consistent — an ISO
        # timestamp from this run (not the threaded created_at).
        assert args["completed_at"].startswith(
            datetime.now().astimezone().strftime("%Y-%m-%d")
        )

    @pytest.mark.asyncio
    async def test_save_to_memory_no_created_at_omits_key(self, sample_config):
        """Without a threaded timestamp the tool is called without created_at."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        await service.save_to_memory(
            user_message="hi", token_count=10, history=conv,
        )
        tool_name, args = mock_client.call_tool.await_args.args
        assert tool_name == "__memory_save_turn"
        assert "created_at" not in args

    @pytest.mark.asyncio
    async def test_save_to_memory_matches_sanitized_user_message(self, sample_config):
        """A user message containing an API key is sanitized on store, but the
        turn must still be saved — the backscan compares sanitized forms, so
        the assistant reply is persisted (not an empty turn)."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        secret = "sk-" + "a" * 24  # matches sanitize_secrets' sk- pattern
        conv = service.message_history
        conv.add_user_message(f"my key is {secret}")  # sanitized on store
        conv.add_assistant_message("got it")

        await service.save_to_memory(
            user_message=f"my key is {secret}", history=conv,
        )

        mock_client.call_tool.assert_awaited_once()
        tool_name, args = mock_client.call_tool.await_args.args
        assert tool_name == "__memory_save_turn"
        assert args["messages"]  # not an empty turn

    @pytest.mark.asyncio
    async def test_save_to_memory_skips_when_user_message_absent(self, sample_config):
        """When the user message is no longer in the history (rolled back
        on a content-policy error), nothing is saved — no empty diary row."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        # Empty history — no matching user message to anchor the turn.
        await service.save_to_memory(user_message="hi", token_count=10)

        mock_client.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_to_memory_fatal_error_freezes_inbox(self, sample_config):
        """A persistent memory-save failure (plugin returns {"error": ...})
        must NOT be silent — it sets memory-broken, freezes the inbox, and
        fires the on_memory_broken callback (TUI red banner)."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            return_value='{"error": "database is locked"}',
        )
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        surfaced: list[str] = []
        service.on_memory_broken(surfaced.append)

        await service.save_to_memory(
            user_message="hi", token_count=10, history=conv,
        )

        assert service._memory_broken is True
        assert "database is locked" in service._memory_error
        assert surfaced == ["database is locked"]
        # Inbox frozen — new turns are dropped, not run without memory.
        assert service.inbox._frozen is True
        # Log-only text — English, like every other log line.
        assert "memory save failed" in service.inbox._frozen_reason

    @pytest.mark.asyncio
    async def test_save_to_memory_unparsable_response_raises(self, sample_config):
        """A channel response that is neither a save ack nor an error object
        (non-JSON text, or JSON that isn't an object) must NOT be silently
        swallowed — memory writes are mandatory, so the save raises
        MemorySaveError (the inbox reports it like an LLM API error).  Not the
        DB-hard-stop path: no freeze, no memory-broken flag."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            return_value="<html>502 Bad Gateway</html>",
        )
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        with pytest.raises(MemorySaveError):
            await service.save_to_memory(
                user_message="hi", token_count=10, history=conv,
            )

        assert service.inbox._frozen is False
        assert service._memory_broken is False
        user_msg = next(m for m in conv.messages if m.get("role") == "user")
        assert user_msg["content"] == "hi"

    @pytest.mark.asyncio
    async def test_save_to_memory_timeout_raises(self, sample_config):
        """A 10s timeout on the save call raises MemorySaveError — the row may
        or may not be written server-side, so the user is told it's
        unconfirmed, not silently skipped.  Not the DB-hard-stop: no freeze,
        no memory-broken flag."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(side_effect=asyncio.TimeoutError())
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        with pytest.raises(MemorySaveError):
            await service.save_to_memory(
                user_message="hi", token_count=10, history=conv,
            )

        assert service.inbox._frozen is False
        assert service._memory_broken is False

    @pytest.mark.asyncio
    async def test_save_to_memory_channel_error_raises(self, sample_config):
        """A raised call_tool (transient MCP/channel failure) raises
        MemorySaveError instead of being silently logged or warn-only.  Not
        the DB-hard-stop: no freeze, no memory-broken flag."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            side_effect=RuntimeError("channel dropped"),
        )
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        with pytest.raises(MemorySaveError):
            await service.save_to_memory(
                user_message="hi", token_count=10, history=conv,
            )

        assert service.inbox._frozen is False
        assert service._memory_broken is False

    @pytest.mark.asyncio
    async def test_save_to_memory_compacts_oversized_tool_result(self, sample_config):
        """An oversized tool result is compacted to a head+tail digest in the
        DIARY copy, while the live history keeps the full output (the
        model reasoned over it this turn)."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        big = "y" * 50000
        conv = service.message_history
        conv.add_user_message("read the big file")
        conv.add_assistant_message(
            "", tool_calls=[{"id": "call_1", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}],
        )
        conv.add_tool_result("call_1", big)
        conv.add_assistant_message("the file is huge.")

        await service.save_to_memory(user_message="read the big file", history=conv)

        tool_name, args = mock_client.call_tool.await_args.args
        assert tool_name == "__memory_save_turn"
        # The persisted turn carries the digest, not the full blob.
        persisted_tool = next(m for m in args["messages"] if m.get("role") == "tool")
        assert len(persisted_tool["content"]) < 9000
        assert "[compacted at save: original 50000 chars" in persisted_tool["content"]
        assert "by re-running read_file" in persisted_tool["content"]
        # Live history is untouched — the model still has the full result.
        live_tool = next(m for m in conv.messages if m.get("role") == "tool")
        assert len(live_tool["content"]) == 50000

    @pytest.mark.asyncio
    async def test_save_to_memory_small_tool_result_untouched(self, sample_config):
        """Results within the memory budget are persisted as-is."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        small = "z" * 100
        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message(
            "", tool_calls=[{"id": "call_1", "type": "function",
                             "function": {"name": "check", "arguments": "{}"}}],
        )
        conv.add_tool_result("call_1", small)
        conv.add_assistant_message("checked.")

        await service.save_to_memory(user_message="hi", history=conv)
        _, args = mock_client.call_tool.await_args.args
        persisted_tool = next(m for m in args["messages"] if m.get("role") == "tool")
        assert persisted_tool["content"] == small

    @pytest.mark.asyncio
    async def test_save_strips_runtime_trim_marker(self, sample_config):
        """The trim note is runtime-only — a note on the live
        history (from a prior trim) must not reach the diary."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            return_value=_json.dumps({"turn_id": 7, "status": "saved"}),
        )
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("previous reply")
        conv.append_trim_marker(3)  # a trim happened earlier in the session

        await service.save_to_memory(user_message="hi", history=conv)

        # The live history still carries the note...
        assert "oldest turns have been removed from context" in conv.messages[-1]["content"]
        # ...but the persisted turn is clean.
        _, args = mock_client.call_tool.await_args.args
        assert all(
            "[INFO: " not in (m.get("content") or "")
            for m in args["messages"]
        )

    @pytest.mark.asyncio
    async def test_saved_turn_annotated_with_footnote(self, sample_config):
        """After a successful save, the turn's user message gets the inline
        `[INFO: {"turn_id": N, …}]` footnote so the next LLM call can
        reference it by rowid (and turn_summarize need not race
        latest_rowid)."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            return_value=_json.dumps({"turn_id": 42, "status": "saved"}),
        )
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        await service.save_to_memory(user_message="hi", history=conv)

        content = conv.messages[1]["content"]  # [0] is the system prompt
        assert content.startswith('hi [INFO: {"turn_id": 42')
        assert content.endswith("]")
        # Runtime-only invariant: the footnote is appended AFTER the DB write,
        # so the stored user_message stays the clean original.
        _, args = mock_client.call_tool.await_args.args
        assert args["user_message"] == "hi"
        assert "[INFO:" not in args["user_message"]

    @pytest.mark.asyncio
    async def test_heartbeat_turn_not_annotated(self, sample_config):
        """Heartbeat turns (synthetic triggers) never get a footnote."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            return_value=_json.dumps({"turn_id": 42, "status": "saved"}),
        )
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("[Heartbeat] click.  Reply per your contract.")
        conv.add_assistant_message(".")

        await service.save_to_memory(
            user_message="[Heartbeat] click.  Reply per your contract.",
            history=conv,
        )

        assert conv.messages[1]["content"] == (  # [0] is the system prompt
            "[Heartbeat] click.  Reply per your contract."
        )

    @pytest.mark.asyncio
    async def test_no_rowid_no_footnote(self, sample_config):
        """No rowid in the response (e.g. a timeout whose row may still be
        written server-side) → the message stays unannotated."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        await service.save_to_memory(user_message="hi", history=conv)

        assert conv.messages[1]["content"] == "hi"  # [0] is the system prompt

    @pytest.mark.asyncio
    async def test_save_rides_captured_current_turn_annotation(self, sample_config):
        """A rowid-less turn_summarize called mid-turn rides the save:
        its summary/tags land on the new row (no latest_rowid race)."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            return_value=_json.dumps({"turn_id": 42, "status": "saved"}),
        )
        service._plugins["memdb"].client = mock_client

        conv = service.message_history
        conv.add_user_message("hi")
        conv.add_assistant_message(
            "", tool_calls=[{
                "id": "c1", "type": "function",
                "function": {
                    "name": "turn_summarize",
                    "arguments": '{"summary": "switched model", "tags": "model,vision"}',
                },
            }],
        )
        conv.add_tool_result("c1", '{"status": "captured"}')
        conv.add_assistant_message("done")

        await service.save_to_memory(user_message="hi", history=conv)

        _, args = mock_client.call_tool.await_args.args
        assert args["summary"] == "switched model"
        assert args["tags"] == "model,vision"

    @pytest.mark.asyncio
    async def test_stop_memdb_noop_when_disabled(self, sample_config):
        service = AgentService(sample_config)
        await service.stop_plugin("memdb")  # Should not raise


class TestCompactToolResults:
    """Direct tests for the save-side compaction helper."""

    def test_compacts_oversized_result_to_head_tail(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "run_python_script", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "A" * 1000 + "B" * 1000},
        ]
        n = compact_tool_results(messages, budget_chars=200)
        assert n == 1
        content = messages[1]["content"]
        assert len(content) < 400  # head 100 + marker + tail 100
        assert content.startswith("A" * 100)
        assert content.endswith("B" * 100)
        assert "[compacted at save: original 2000 chars" in content
        assert "by re-running run_python_script" in content

    def test_leaves_small_results_untouched(self):
        messages = [{"role": "tool", "tool_call_id": "c1", "content": "small"}]
        n = compact_tool_results(messages, budget_chars=8000)
        assert n == 0
        assert messages[0]["content"] == "small"

    def test_zero_budget_is_noop(self):
        messages = [{"role": "tool", "tool_call_id": "c1", "content": "x" * 50000}]
        n = compact_tool_results(messages, budget_chars=0)
        assert n == 0
        assert len(messages[0]["content"]) == 50000

    def test_does_not_mutate_input_dicts(self):
        big = "x" * 50000
        original = {"role": "tool", "tool_call_id": "c1", "content": big}
        messages = [original]
        n = compact_tool_results(messages, budget_chars=100)
        assert n == 1
        # the list slot is swapped for a copy — the caller's dict is untouched
        assert original["content"] == big
        assert messages[0] is not original

    def test_marker_omits_tool_name_when_unknown(self):
        messages = [{"role": "tool", "tool_call_id": "orphan", "content": "x" * 50000}]
        n = compact_tool_results(messages, budget_chars=100)
        assert n == 1
        assert "by re-running the tool" in messages[0]["content"]


class TestExtractTurnAnnotation:
    """_extract_turn_annotation — rowid-less turn_summarize calls."""

    @staticmethod
    def _call(args_json: str):
        return [{
            "role": "assistant",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "turn_summarize",
                             "arguments": args_json},
            }],
        }]

    def test_rowidless_captures_summary_and_tags(self):
        msgs = self._call('{"summary": "switched model", "tags": "model,vision"}')
        assert _extract_turn_annotation(msgs) == ("switched model", "model,vision")

    def test_explicit_rowid_is_ignored(self):
        # The tool already wrote it — the save must not duplicate.
        msgs = self._call('{"rowid": 3, "summary": "x"}')
        assert _extract_turn_annotation(msgs) == (None, None)

    def test_prefix_suffixed_name_still_matches(self):
        """A legacy ``{server}__turn_summarize`` call (split("__")[-1]) still
        matches — the matcher is suffix-based, so the prefixed historical form
        keeps working even though built-ins now register bare."""
        msgs = [{
            "role": "assistant",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "memdb__turn_summarize",
                             "arguments": '{"tags": "a,b"}'},
            }],
        }]
        assert _extract_turn_annotation(msgs) == (None, "a,b")

    def test_unrelated_tools_ignored(self):
        msgs = [{
            "role": "assistant",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "read_file", "arguments": "{}"},
            }],
        }]
        assert _extract_turn_annotation(msgs) == (None, None)


# ── AgentService A2A ────────────────────────────────────────────────────────


class TestAgentServiceA2A:
    """Tests for A2A lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_a2a_disabled_noop(self, sample_config):
        service = AgentService(sample_config)
        result = await service.start_plugin_server("a2a", "slife.plugins.a2a.server")
        assert result is PluginStartStatus.SKIPPED
        assert service._plugins["a2a"].process is None

    @pytest.mark.asyncio
    async def test_start_a2a_broker_unreachable_skips(self, sample_config):
        """A2A enabled but no broker on the port → SKIPPED, not a failure.

        This is the "mosquitto 未启动" case: the plugin is expected to be
        skipped (A2A disabled at runtime), never reported as a crash.
        """
        from slife.a2a.config import A2AConfig
        service = AgentService(sample_config)
        service.config.a2a_config = A2AConfig(
            enabled=True, broker_host="localhost", broker_port=1883,
        )
        with patch(
            "slife.a2a.broker.probe_broker", AsyncMock(return_value=False),
        ):
            result = await service.start_plugin_server(
                "a2a", "slife.plugins.a2a.server",
            )
        assert result is PluginStartStatus.SKIPPED
        assert service._plugins["a2a"].process is None
        assert service.config.a2a_config.enabled is False  # downgraded

    @pytest.mark.asyncio
    async def test_stop_a2a_noop_when_disabled(self, sample_config):
        service = AgentService(sample_config)
        await service.stop_plugin("a2a")  # Should not raise

    @pytest.mark.asyncio
    async def test_a2a_poll_prepends_task_id(self, sample_config):
        """Inbound a2a tasks surface the [A2A:…] marker to the LLM, so the
        receiver knows the peer and the task_id it is responding to."""
        import json as _json

        service = AgentService(sample_config)
        mock_a2a = MagicMock()
        mock_a2a.is_connected = True
        calls = [0]
        completed_calls = []

        async def mock_call_tool(name, arguments=None):
            if name == "__a2a_drain_incoming":
                calls[0] += 1
                if calls[0] == 1:
                    return _json.dumps({
                        "tasks": [{
                            "source": "Jack", "content": "do X",
                            "correlation_id": "cid-1",
                        }],
                        "presence": [], "cancellations": [],
                        "task_completions": [],
                    })
                service._plugins["a2a"].client = None  # end the loop
                return _json.dumps({
                    "tasks": [], "presence": [],
                    "cancellations": [], "task_completions": [],
                })
            if name == "a2a_set_task_done":
                completed_calls.append(arguments or {})
            return "{}"

        mock_a2a.call_tool = mock_call_tool
        service._plugins["a2a"].client = mock_a2a

        posted = []
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock(side_effect=lambda msg: posted.append(msg))
        mock_inbox.cancel_correlation = MagicMock()
        service.inbox = mock_inbox

        await service._a2a_poll_loop(interval=0.001)

        assert len(posted) == 1
        assert posted[0].content == (
            '[A2A:{"from": "Jack", "type": "task_request", "task_id": "cid-1"}] '
            "do X"
        )
        assert posted[0].correlation_id == "cid-1"
        # Completion is the model's explicit job (a task may take many turns):
        # no harness auto-dispatch rides the message; the model answers with
        # a2a_send_message(message_type="task_response", task_id=…).
        assert posted[0].on_reply is None
        # The wire kind rides metadata so the mid-turn injector can frame it.
        assert posted[0].metadata.get("a2a_kind") == "task"

    @pytest.mark.asyncio
    async def test_a2a_poll_broadcast_event_has_no_task_id(self, sample_config):
        """A fire-and-forget broadcast event is passive: the marker names only
        the sender (no id, no completion) — informational input, not a task."""
        import json as _json

        service = AgentService(sample_config)
        mock_a2a = MagicMock()
        mock_a2a.is_connected = True
        calls = [0]

        async def mock_call_tool(name, _):
            if name == "__a2a_drain_incoming":
                calls[0] += 1
                if calls[0] == 1:
                    return _json.dumps({
                        "tasks": [],
                        "events": [{
                            "source": "Jack", "content": "all hands on deck",
                        }],
                        "presence": [], "cancellations": [],
                        "task_completions": [],
                    })
                service._plugins["a2a"].client = None  # end the loop
                return _json.dumps({
                    "tasks": [], "events": [], "presence": [],
                    "cancellations": [], "task_completions": [],
                })
            return "{}"

        mock_a2a.call_tool = mock_call_tool
        service._plugins["a2a"].client = mock_a2a

        posted = []
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock(side_effect=lambda msg: posted.append(msg))
        mock_inbox.cancel_correlation = MagicMock()
        service.inbox = mock_inbox

        await service._a2a_poll_loop(interval=0.001)

        assert len(posted) == 1
        assert posted[0].content == '[A2A:{"from": "Jack", "type": "broadcast"}] all hands on deck'
        assert posted[0].on_reply is None
        assert posted[0].metadata.get("a2a_kind") == "event"

    @pytest.mark.asyncio
    async def test_a2a_poll_frames_completion(self, sample_config):
        """Auto-pushed completions are always task frames; a cancelled
        completion still pushes even with an empty result."""
        import json as _json

        service = AgentService(sample_config)
        mock_a2a = MagicMock()
        mock_a2a.is_connected = True
        calls = [0]

        async def mock_call_tool(name, _):
            if name == "__a2a_drain_incoming":
                calls[0] += 1
                if calls[0] == 1:
                    return _json.dumps({
                        "tasks": [], "presence": [], "cancellations": [],
                        "task_completions": [
                            {"corr_id": "c-task", "result": "the answer",
                             "cancelled": False, "peer": "peer-1"},
                            {"corr_id": "c-cancel", "result": "",
                             "cancelled": True, "peer": "peer-3"},
                            {"corr_id": "c-empty", "result": "",
                             "cancelled": False, "peer": "peer-4"},
                        ],
                    })
                service._plugins["a2a"].client = None  # end the loop
                return _json.dumps({
                    "tasks": [], "presence": [],
                    "cancellations": [], "task_completions": [],
                })
            return "{}"

        mock_a2a.call_tool = mock_call_tool
        service._plugins["a2a"].client = mock_a2a

        posted = []
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock(side_effect=lambda msg: posted.append(msg))
        mock_inbox.cancel_correlation = MagicMock()
        service.inbox = mock_inbox

        await service._a2a_poll_loop(interval=0.001)

        contents = [m.content for m in posted]
        assert len(contents) == 2
        assert (
            '[A2A:{"from": "peer-1", "type": "task_response", "task_id": "c-task"}] '
            "Peer **peer-1** completed async task (ID: `c-task`):\n\nthe answer"
        ) in contents
        assert (
            '[A2A:{"from": "peer-3", "type": "task_response", "task_id": "c-cancel"}] '
            "Peer **peer-3** cancelled async task (ID: `c-cancel`):\n\n"
        ) in contents
        # A completed task with an empty result carries no information — dropped.
        assert "c-empty" not in "".join(contents)


# ── AgentService subagent ───────────────────────────────────────────────────


class TestAgentServiceSubagent:
    """Tests for subagent lifecycle."""

    @pytest.mark.asyncio
    async def test_start_subagent_always_creates_manager(self, sample_config):
        """Subagent is always enabled — start_subagent always creates a manager."""
        service = AgentService(sample_config)
        await service.start_subagent()
        assert service._subagent_manager is not None
        assert service._subagent_manager.count == 0

    @pytest.mark.asyncio
    async def test_stop_subagent_noop_when_disabled(self, sample_config):
        service = AgentService(sample_config)
        await service.stop_subagent()  # Should not raise

    @pytest.mark.asyncio
    async def test_subagent_done_rewords_schedule_workers(self, sample_config):
        """A worker that ran a scheduled task is reported by its run record —
        "report saved" only when the run was actually confirmed, otherwise the
        run is settled failed and reported honestly (never a false success);
        plain subagents keep the detail."""
        from slife.agent.schedules import _SCHEDULE_WORKERS

        def make_client(latest, pending):
            marked: dict = {}

            async def fake_call_tool(name, arguments=None):
                arguments = arguments or {}
                if name == "__scheduled_task_by_name":
                    return '{"id": 1, "name": "daily_report"}'
                if name == "__scheduled_runs_list":
                    runs = pending if arguments.get("status") == "pending" else latest
                    return _json.dumps({"runs": runs})
                if name == "__scheduled_mark_run_failed":
                    marked.setdefault("calls", []).append(arguments)
                    return "{}"
                return "{}"

            client = AsyncMock()
            client.call_tool = fake_call_tool
            return client, marked

        service = AgentService(sample_config)
        await service.start_subagent()
        cb = service._subagent_manager.on_task_complete
        assert cb is not None
        service.inbox.post = AsyncMock()

        _SCHEDULE_WORKERS.add("daily_report")
        try:
            # Confirmed run → the completion is announced as saved.
            due = "2026-08-25T09:00:00"
            client, marked = make_client(
                [{"status": "ran", "due_at": due}], [],
            )
            service._tool_ctx.memfiles_client = client
            await cb("daily_report", "t-1", "result text")
            content = service.inbox.post.call_args.args[0].content
            assert "Subagent" not in content
            assert "daily_report" in content
            assert "completed — report saved" in content
            assert marked == {}  # confirmed run → nothing settled

            # Unconfirmed run → never claim saved; settle the run failed so it
            # is backfillable and say so.
            client2, marked2 = make_client(
                [{"status": "pending", "due_at": due}],
                [{"status": "pending", "due_at": due}],
            )
            service._tool_ctx.memfiles_client = client2
            await cb("daily_report", "t-2", "result text")
            content = service.inbox.post.call_args.args[0].content
            assert "report saved" not in content
            assert "report was not saved" in content
            assert "failed" in content
            assert marked2["calls"] == [
                {"task_id": 1, "due_at": due,
                 "error": "worker finished without confirming the run"},
            ]

            await cb("researcher", "t-3", "the result")
            content2 = service.inbox.post.call_args.args[0].content
            # The auto-push carries the [Subagent:…] marker naming the worker
            # and task id, so the LLM can attribute the pushed result.
            assert content2.startswith(
                '[Subagent:{"subagent_name": "researcher", "task_id": "t-3"}] '
            )
            assert "Subagent **researcher**" in content2
            assert "the result" in content2
        finally:
            _SCHEDULE_WORKERS.discard("daily_report")


# ── AgentService callbacks ─────────────────────────────────────────────────


class TestAgentServiceCallbacks:
    """Tests for the activity callback channel (the TUI's activity feed)."""

    @pytest.mark.asyncio
    async def test_on_activity_register_and_fire(self, sample_config):
        service = AgentService(sample_config)
        cb = AsyncMock()
        service.on_activity(cb)

        await service._notify_activity("test_event", data="hello")

        cb.assert_called_once_with("test_event", data="hello")

    @pytest.mark.asyncio
    async def test_callback_error_is_swallowed(self, sample_config):
        service = AgentService(sample_config)
        bad_cb = AsyncMock(side_effect=Exception("broken"))
        good_cb = AsyncMock()
        service.on_activity(bad_cb)
        service.on_activity(good_cb)

        await service._notify_activity("event")

        good_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_inbox_handler_factory_when_no_inbox(self, sample_config):
        service = AgentService(sample_config)
        # Should not raise — inbox is None
        service.set_inbox_handler_factory(lambda: None)


# ── AgentService process_message ────────────────────────────────────────────


class TestAgentServiceProcessMessage:
    """Tests for process_message."""

    @pytest.mark.asyncio
    async def test_process_message_unified_queue(self, sample_config):
        """Always routes through inbox — handler is attached to the message."""
        from slife.a2a.identity import HUMAN

        service = AgentService(sample_config)

        # inbox is always created in __init__
        assert service.inbox is not None

        # Set up inbox mock
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        handler = MagicMock()
        result = await service.process_message("hello", None, handler)

        # Should post to inbox
        mock_inbox.post.assert_called_once()

        # The message should carry the handler
        call_args = mock_inbox.post.call_args[0]
        msg = call_args[0]
        assert msg.handler is handler
        assert msg.content == "hello"
        assert msg.source == HUMAN

        # Returns placeholder
        assert result.text == ""


# ── AgentService stop_memdb ────────────────────────────────────────────────


class TestAgentServiceStopMemory:
    """Tests for the uniform stop_plugin('memdb')."""

    @pytest.mark.asyncio
    async def test_stop_memdb_with_active_client(self, sample_config):
        service = AgentService(sample_config)
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()
        service._plugins["memdb"].client = mock_client

        await service.stop_plugin("memdb")

        mock_client.disconnect.assert_called_once()
        assert service._plugins["memdb"].client is None

    @pytest.mark.asyncio
    async def test_stop_memdb_with_process(self, sample_config):
        service = AgentService(sample_config)
        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        service._plugins["memdb"].process = mock_process  # pyright: ignore[reportAttributeAccessIssue]

        await service.stop_plugin("memdb")

        mock_process.stop.assert_called_once()
        assert service._plugins["memdb"].process is None

    @pytest.mark.asyncio
    async def test_stop_memdb_handles_errors(self, sample_config):
        service = AgentService(sample_config)
        mock_client = MagicMock()
        mock_client.is_connected = True
        # call_tool raises — disconnect should still be attempted
        mock_client.call_tool = AsyncMock(side_effect=Exception("boom"))
        mock_client.disconnect = AsyncMock()
        service._plugins["memdb"].client = mock_client

        await service.stop_plugin("memdb")

        mock_client.disconnect.assert_called_once()


# ── Inbox: always-active unified message queue ───────────────────────────────


class TestAgentServiceInbox:
    """Tests for the always-active unified inbox."""

    def test_inbox_always_created(self, sample_config):
        """Inbox is created in __init__ — not conditional on A2A."""
        service = AgentService(sample_config)
        assert service.inbox is not None

    def test_inbox_has_correct_wiring(self, sample_config):
        """Inbox is wired with agent_loop, histories, and on_turn_complete."""
        service = AgentService(sample_config)
        inbox = service.inbox

        assert inbox._agent_loop is service.agent_loop
        # _on_activity is a bound method — use equality not identity
        assert inbox._on_activity.__func__ is service._notify_activity.__func__  # type: ignore[union-attr]
        assert inbox._on_turn_complete.__func__ is service.save_to_memory.__func__  # type: ignore[union-attr]
        # HUMAN history is pre-seeded from service.message_history
        assert inbox._histories._by_source.get(HUMAN) is service.message_history

    @pytest.mark.asyncio
    async def test_start_inbox_creates_background_task(self, sample_config):
        """start_inbox launches inbox.run() as a background task."""
        service = AgentService(sample_config)

        # Replace inbox.run with a mock so we don't actually start the loop
        mock_run = AsyncMock()
        service.inbox._runner_task = None  # ensure clean state
        with patch.object(service.inbox, "run", mock_run):
            await service.start_inbox()

        assert service._inbox_task is not None
        # start_inbox opened the shared catalog — close it so the aiosqlite
        # worker thread doesn't keep the pytest process alive past the end.
        await service.close_catalog()

    @pytest.mark.asyncio
    async def test_stop_inbox_cancels_task(self, sample_config):
        """stop_inbox cancels the background task and waits for it."""
        service = AgentService(sample_config)

        # Create a real cancellable task
        async def _fake_run():
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise

        service._inbox_task = asyncio.create_task(_fake_run())
        await asyncio.sleep(0)  # let it start

        await service.stop_inbox()

        assert service._inbox_task is None

    @pytest.mark.asyncio
    async def test_stop_inbox_noop_when_not_started(self, sample_config):
        """stop_inbox is safe when inbox was never started."""
        service = AgentService(sample_config)
        service._inbox_task = None
        await service.stop_inbox()  # Should not raise

    @pytest.mark.asyncio
    async def test_process_message_routes_through_inbox(self, sample_config):
        """process_message enqueues via inbox with handler on the message."""
        service = AgentService(sample_config)

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        handler = MagicMock()
        result = await service.process_message("test msg", None, handler)

        mock_inbox.post.assert_called_once()
        msg = mock_inbox.post.call_args[0][0]
        assert msg.source == HUMAN
        assert msg.content == "test msg"
        assert msg.handler is handler
        assert result.text == ""  # placeholder


# ── WeChat lifecycle ─────────────────────────────────────────────────────────


class TestAgentServiceWeChat:
    """Tests for WeChat plugin lifecycle and message processing."""

    def test_wechat_not_enabled_initially(self, sample_config):
        """WeChat client is None until the plugin is started."""
        service = AgentService(sample_config)
        assert service.wechat_enabled is False
        assert service._plugins["wechat"].client is None

    @pytest.mark.asyncio
    async def test_stop_wechat_noop_when_disabled(self, sample_config):
        """stop_plugin('wechat') is safe when WeChat was never started."""
        service = AgentService(sample_config)
        await service.stop_plugin("wechat")  # Should not raise

    @pytest.mark.asyncio
    async def test_start_wechat_with_mocked_internals(self, sample_config):
        """The uniform engine starts wechat through the generic path (its spec
        gate + after-ready glue), registers tools, and schedules the poll."""
        service = AgentService(sample_config)

        # WeChat must be enabled in config for the gate to pass.
        mock_wechat_cfg = MagicMock()
        mock_wechat_cfg.enabled = True
        service.config.wechat_config = mock_wechat_cfg

        # _spawn_plugin_generic is mocked — wire up a mock client for the
        # check_status call and poll loop.
        mock_wechat_client = MagicMock()
        mock_wechat_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["wechat"].client = mock_wechat_client  # pyright: ignore[reportAttributeAccessIssue]

        with patch.object(
            service, "_spawn_plugin_generic", AsyncMock(return_value=True)
        ) as mock_spawn, \
             patch.object(service, "_arm_watchdog", MagicMock()), \
             patch.object(service, "_wechat_restore_session", AsyncMock()), \
             patch.object(service, "_wechat_poll_loop", AsyncMock()):
            result = await service.start_plugin_server(
                "wechat", "slife.plugins.wechat.server",
            )

            mock_spawn.assert_called_once_with(
                "wechat", "slife.plugins.wechat.server",
            )
            assert result is PluginStartStatus.STARTED
            # Poll loop scheduled as a background task by the after-ready glue.
            poll_task = service._plugins["wechat"].poll_task
            assert poll_task is not None and not poll_task.done()

    @pytest.mark.asyncio
    async def test_stop_wechat_cancels_poll_and_disconnects(self, sample_config):
        """stop_plugin('wechat') stops the poll loop and disconnects the client."""
        service = AgentService(sample_config)

        # Set up a fake poll task
        async def _fake_poll():
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise

        service._plugins["wechat"].poll_task = asyncio.create_task(_fake_poll())

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()
        service._plugins["wechat"].client = mock_client

        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        service._plugins["wechat"].process = mock_process  # pyright: ignore[reportAttributeAccessIssue]

        await service.stop_plugin("wechat")

        # Poll task cancelled and cleaned up
        assert service._plugins["wechat"].poll_task is None
        # Client disconnected
        mock_client.disconnect.assert_called_once()
        assert service._plugins["wechat"].client is None
        # Process stopped
        mock_process.stop.assert_called_once()
        assert service._plugins["wechat"].process is None

    @pytest.mark.asyncio
    async def test_wechat_poll_posts_to_inbox(self, sample_config):
        """The poll loop fetches messages and posts AgentMessages to inbox."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "__wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    return _json.dumps({"messages": [{
                        "to_user_id": "wx_user_123",
                        "context_token": "ctx_abc",
                        "text": "你好",
                    }]})
                # Disconnect after first poll to exit the loop cleanly
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        # Message posted to inbox
        mock_inbox.post.assert_called_once()
        msg = mock_inbox.post.call_args[0][0]
        assert msg.source == WECHAT
        assert msg.content == (
            '[Wechat:{"peer_wechat_id": "wx_user_123", '
            '"context_token": "ctx_abc"}] 你好'
        )
        assert msg.metadata["channel"] == "wechat"
        assert msg.on_reply is None

    @pytest.mark.asyncio
    async def test_wechat_poll_announces_login_transitions_only(self, sample_config):
        """The drain reports the session alongside the messages, so login
        state is diffed for free.  Announced on transition — including the
        first drain — and silent while the state holds."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        # ok → ok (no repeat) → not_logged_in → stop
        states = ["ok", "ok", "not_logged_in"]
        calls = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "__wechat_drain_incoming":
                i = calls[0]
                calls[0] += 1
                if i >= len(states):
                    # Stop the loop.  No status here: reporting "ok" would be
                    # a real transition back to logged-in, and the poll would
                    # rightly announce it.
                    mock_wc.is_connected = False
                    return _json.dumps({"messages": []})
                return _json.dumps({"messages": [], "status": states[i]})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        events = AsyncMock()
        service.on_activity(events)
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        seen = [c.kwargs["logged_in"] for c in events.await_args_list
                if c.args[0] == "wechat_status"]
        assert seen == [True, False]   # the repeat "ok" is not re-announced

    @pytest.mark.asyncio
    async def test_wechat_poll_never_logged_in_stays_silent(self, sample_config):
        """A session that was never logged in is the startup default, not a
        transition — announcing it would print a fake ⚠ on every start.  The
        same rule A2A presence uses for a cold retained offline card."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True
        states = ["not_logged_in", "not_logged_in", "ok"]
        calls = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "__wechat_drain_incoming":
                i = calls[0]
                calls[0] += 1
                if i >= len(states):
                    mock_wc.is_connected = False
                    return _json.dumps({"messages": []})
                return _json.dumps({"messages": [], "status": states[i]})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        events = AsyncMock()
        service.on_activity(events)
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        # The two "not_logged_in" drains are silent; only the login is news.
        seen = [c.kwargs["logged_in"] for c in events.await_args_list
                if c.args[0] == "wechat_status"]
        assert seen == [True]

    @pytest.mark.asyncio
    async def test_wechat_poll_error_is_not_a_logout(self, sample_config):
        """A drain that RAISES must not read as a logged-out session."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True
        calls = [0]

        async def mock_call_tool(tool_name, _):
            calls[0] += 1
            if calls[0] >= 2:
                mock_wc.is_connected = False
            raise RuntimeError("drain blew up")

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        events = AsyncMock()
        service.on_activity(events)
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        assert service._wechat_logged_in is None
        assert [c for c in events.await_args_list
                if c.args[0] == "wechat_status"] == []

    @pytest.mark.asyncio
    async def test_wechat_poll_skips_empty_text(self, sample_config):
        """Messages with empty text are not posted to inbox."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "__wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    return _json.dumps({"messages": [
                        {"to_user_id": "wx_1", "context_token": "c1", "text": "   "},
                        {"to_user_id": "wx_2", "context_token": "c2", "text": "real"},
                    ]})
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        # Only the non-empty message is posted, with the channel marker.
        assert mock_inbox.post.call_count == 1
        msg = mock_inbox.post.call_args[0][0]
        assert msg.content == (
            '[Wechat:{"peer_wechat_id": "wx_2", "context_token": "c2"}] real'
        )

    @pytest.mark.asyncio
    async def test_wechat_message_has_no_auto_dispatch(self, sample_config):
        """WeChat messages carry no on_reply — nothing auto-sends back.

        The model is the only sender: it replies via the LLM-visible
        wechat_send_message tool.  A harness auto-dispatch would be an
        invisible side effect the model cannot see or coordinate with.
        """
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "__wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    return _json.dumps({"messages": [{
                        "to_user_id": "wx_123",
                        "context_token": "ctx_xyz",
                        "text": "帮我查一下天气",
                    }]})
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        msg = mock_inbox.post.call_args[0][0]
        assert msg.source == WECHAT
        assert msg.content == (
            '[Wechat:{"peer_wechat_id": "wx_123", '
            '"context_token": "ctx_xyz"}] 帮我查一下天气'
        )
        # No on_reply → the assistant's final text is NOT routed back to
        # WeChat; the model must send its reply via wechat_send_message.
        assert msg.on_reply is None

    @pytest.mark.asyncio
    async def test_wechat_typing_sent_on_arrival(self, sample_config):
        """send_typing(status=1) is called when a message arrives."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "__wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    return _json.dumps({"messages": [{
                        "to_user_id": "wx_1",
                        "context_token": "ctx_1",
                        "text": "hello",
                    }]})
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = AsyncMock(side_effect=mock_call_tool)
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        # After refactor: typing is managed server-side by the plugin.
        # The harness only calls wechat_drain_incoming; the plugin internally
        # starts the typing keep-alive. Verify message arrived at inbox instead.
        mock_inbox.post.assert_called_once()
        msg = mock_inbox.post.call_args[0][0]
        assert msg.content == (
            '[Wechat:{"peer_wechat_id": "wx_1", "context_token": "ctx_1"}] hello'
        )
        assert msg.on_reply is None

    @pytest.mark.asyncio
    async def test_wechat_poll_error_handling(self, sample_config):
        """Poll errors are caught and do not crash the loop."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "__wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    raise Exception("network error")
                # Second call succeeds but disconnects
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        # Should not raise
        await service._wechat_poll_loop(interval=0.001)

        # Error on first poll, second poll should still run
        assert call_count[0] == 2


# ── Direct model switching (Ctrl+S, no LLM needed) ─────────────────────


def _two_model_config():
    """A Config with two models, first active."""
    from slife.config import Config, ModelConfig

    return Config(
        models=[
            ModelConfig(ref="deepseek/dsf", provider="deepseek", api_model="dsf", display_name="DSF", api_key="k"),
            ModelConfig(ref="openai/gpt", provider="openai", api_model="gpt", display_name="GPT", api_key="k"),
        ],
        active_model_ref="deepseek/dsf",
        tools=[],
    )


class TestSwitchModel:
    def test_switch_updates_runtime(self):
        service = AgentService(_two_model_config())
        msg = service.switch_model("openai/gpt")
        assert service.config.active_model_ref == "openai/gpt"
        assert service.config.active_model.display_name == "GPT"
        assert service.llm_client is not None
        assert "GPT" in msg

    def test_switch_persists_active_model(self, tmp_path):
        from slife.tools._config_io import read_config, write_config

        config = _two_model_config()
        path = tmp_path / "slife.yaml"
        config._path = path
        write_config(path, {"models": {"providers": {}}, "active_model": "deepseek/dsf"})
        service = AgentService(config)

        service.switch_model("openai/gpt")

        raw = read_config(path)
        assert raw["active_model"] == "openai/gpt"

    def test_switch_unknown_ref_raises(self):
        service = AgentService(_two_model_config())
        with pytest.raises(ValueError, match="Unknown model ref"):
            service.switch_model("nope/x")

    def test_switch_no_config_path_still_switches(self):
        """In-memory switch works even without a writable config file."""
        service = AgentService(_two_model_config())
        service.config._path = None
        msg = service.switch_model("openai/gpt")
        assert service.config.active_model_ref == "openai/gpt"
        assert "GPT" in msg


class TestAnnotateSavedTurn:
    """_annotate_saved_turn carries two different things: the LLM-facing
    footnote, and the structural id the trim needs.  Only the first is
    suppressed for autonomous turns."""

    @staticmethod
    def _history(user_text: str):
        from slife.agent.message_history import MessageHistory
        conv = MessageHistory(system_prompt="SYS")
        conv.add_user_message(user_text)
        conv.add_assistant_message("reply")
        return conv

    def test_footnote_and_structural_id(self, sample_config):
        from datetime import datetime
        from slife.agent.service import AgentService

        srv = AgentService(sample_config)
        conv = self._history("hello")
        now = datetime(2026, 8, 12, 14, 3, 0)

        srv._annotate_saved_turn(conv, 1, 27, now, now)

        assert conv.messages[1]["_turn_id"] == 27
        assert '[INFO: {"turn_id": 27' in conv.messages[1]["content"]

    def test_autonomous_turn_gets_the_id_but_no_footnote(self, sample_config):
        """A heartbeat/schedule turn is in context like any other — the trim
        must be able to drop it — but its synthetic trigger carries no
        LLM-facing footnote."""
        from datetime import datetime
        from slife.agent.service import AgentService

        srv = AgentService(sample_config)
        conv = self._history("[Heartbeat] click. Reply per your contract.")
        now = datetime(2026, 8, 12, 14, 3, 0)

        srv._annotate_saved_turn(conv, 1, 31, now, now)

        assert conv.messages[1]["_turn_id"] == 31
        assert "INFO" not in conv.messages[1]["content"]

    def test_no_rowid_stamps_nothing(self, sample_config):
        from datetime import datetime
        from slife.agent.service import AgentService

        srv = AgentService(sample_config)
        conv = self._history("hello")

        srv._annotate_saved_turn(conv, 1, None, None, datetime(2026, 8, 12))

        assert "_turn_id" not in conv.messages[1]


class TestGetRecentTurns:
    """Restore fetch: the persisted live-context id list drives it — the
    turns on the list come back verbatim, in list order, and nothing else
    does."""

    def _make_db(self, tmp_path, n, context_turns=None):
        import sqlite3

        db = tmp_path / "test.db"
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE diary (user_message TEXT, messages TEXT, summary TEXT, "
            "tags TEXT, channel TEXT, "
            "created_at TEXT, completed_at TEXT, "
            "who_helped TEXT, what_model TEXT, token_count INT, context_tokens INT)"
        )
        con.execute(
            "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        con.execute(
            "CREATE TABLE turn_channel (turn_id INTEGER PRIMARY KEY, "
            "data TEXT NOT NULL DEFAULT '{}')"
        )
        for i in range(1, n + 1):
            con.execute(
                "INSERT INTO diary (user_message, messages, channel, created_at, token_count) "
                "VALUES (?, ?, 'human', ?, ?)",
                (
                    f"msg {i}",
                    _json.dumps([{"role": "assistant", "content": "x" * 90}]),
                    f"2026-08-12T{i:02d}:00:00+08:00",
                    100 + i,
                ),
            )
        if context_turns is not None:
            con.execute(
                "INSERT INTO diary_meta (key, value) VALUES ('context_turns', ?)",
                (_json.dumps(context_turns),),
            )
        con.commit()
        con.close()
        return db

    @pytest.mark.asyncio
    async def test_listed_turns_restored_whole(
        self, sample_config, tmp_path, monkeypatch
    ):
        """No ceiling re-slicing: every turn on the list is restored
        verbatim — the exit-time context, not a budgeted slice."""
        from slife.agent.service import AgentService

        db = self._make_db(tmp_path, 5, context_turns=[1, 2, 3, 4, 5])
        srv = AgentService(sample_config)
        srv.config.active_model.context_window = 1000
        srv.config.context_ceiling = 0.8
        monkeypatch.setattr(srv, "_get_memory_db_path", lambda: db)

        turns, skipped, budget = await srv.get_recent_turns()

        ids = [t["rowid"] for t in turns]
        assert ids == [1, 2, 3, 4, 5], "the whole exit-time context comes back"
        assert skipped == 0
        assert budget == 0, "no ceiling budget — restore is verbatim"

    @pytest.mark.asyncio
    async def test_restore_returns_only_listed_turns(
        self, sample_config, tmp_path, monkeypatch
    ):
        """Turns dropped from the list by the internal trim (or by an empty
        recall selection) do not come back — the diary keeps them, the context
        does not."""
        from slife.agent.service import AgentService

        db = self._make_db(tmp_path, 8, context_turns=[5, 6, 7, 8])
        srv = AgentService(sample_config)
        srv.config.active_model.context_window = 1000000
        srv.config.context_ceiling = 0.8
        monkeypatch.setattr(srv, "_get_memory_db_path", lambda: db)

        turns, skipped, budget = await srv.get_recent_turns()

        ids = [t["rowid"] for t in turns]
        assert ids == [5, 6, 7, 8], "only the listed turns are restored"
        assert skipped == 0
        assert budget == 0

    @pytest.mark.asyncio
    async def test_list_bounds_the_read_not_the_diary(
        self, sample_config, tmp_path, monkeypatch
    ):
        """The list is its own bound: a large diary with a short list
        restores only the list."""
        from slife.agent.service import AgentService

        db = self._make_db(tmp_path, 20, context_turns=[18, 19, 20])
        srv = AgentService(sample_config)
        srv.config.active_model.context_window = 1000
        srv.config.context_ceiling = 0.8
        monkeypatch.setattr(srv, "_get_memory_db_path", lambda: db)

        turns, _skipped, _budget = await srv.get_recent_turns()

        assert [t["rowid"] for t in turns] == [18, 19, 20]

    @pytest.mark.asyncio
    async def test_non_contiguous_list_order_is_authoritative(
        self, sample_config, tmp_path, monkeypatch
    ):
        """The point of the list: a non-contiguous set restores exactly
        those turns, in the list's order — never re-sorted by rowid."""
        from slife.agent.service import AgentService

        db = self._make_db(tmp_path, 9, context_turns=[7, 2, 9])
        srv = AgentService(sample_config)
        monkeypatch.setattr(srv, "_get_memory_db_path", lambda: db)

        turns, _skipped, _budget = await srv.get_recent_turns()

        assert [t["rowid"] for t in turns] == [7, 2, 9]

    @pytest.mark.asyncio
    async def test_absent_list_restores_nothing(
        self, sample_config, tmp_path, monkeypatch
    ):
        """A DB with turns but no list restores an empty context.

        This is the deliberate break from the old scalar boundary, where an
        absent row meant ``0`` = "replay everything"."""
        from slife.agent.service import AgentService

        db = self._make_db(tmp_path, 5)  # no context_turns key
        srv = AgentService(sample_config)
        monkeypatch.setattr(srv, "_get_memory_db_path", lambda: db)

        turns, _skipped, _budget = await srv.get_recent_turns()

        assert turns == []

    @pytest.mark.asyncio
    async def test_broken_db_raises_memory_error(
        self, sample_config, tmp_path, monkeypatch,
    ):
        """A present-but-broken memory DB raises MemoryDatabaseError —
        restore treats it as fatal (startup abort) instead of silently
        returning [] and starting a memory-less session."""
        import sqlite3
        from slife.agent.service import AgentService, MemoryDatabaseError

        # Old-schema DB — missing the `context_tokens` column the store SELECTs.
        db = tmp_path / "old.db"
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE diary (user_message TEXT, messages TEXT, summary TEXT, "
            "tags TEXT, channel TEXT, created_at TEXT, "
            "who_helped TEXT, what_model TEXT, token_count INT)"
        )
        con.execute(
            "INSERT INTO diary (user_message, messages, channel, created_at, token_count) "
            "VALUES ('hi', '[]', 'human', '2026-08-12T00:00:00+08:00', 100)"
        )
        con.execute(
            "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        con.execute(
            "INSERT INTO diary_meta (key, value) VALUES ('context_turns', '[1]')"
        )
        con.commit()
        con.close()

        srv = AgentService(sample_config)
        monkeypatch.setattr(srv, "_get_memory_db_path", lambda: db)
        with pytest.raises(MemoryDatabaseError):
            await srv.get_recent_turns()


# ── Plugin spawn cancellation cleanup ─────────────────────────────────────


class TestSpawnPluginCancellationCleanup:
    """A cancelled spawn (app-level required-plugin timeout) must stop the
    child process and reset the lifecycle — otherwise the child is orphaned."""

    @pytest.mark.asyncio
    async def test_cancelled_spawn_stops_child_and_resets(self, sample_config):
        service = AgentService(sample_config)
        fake_process = AsyncMock()
        client = fake_process.create_client.return_value
        # list_tools hangs forever → the app's asyncio.timeout cancels the
        # task mid-await; _spawn_plugin_generic must still clean up.
        client.list_tools.side_effect = asyncio.CancelledError()

        with patch(
            "slife.plugins.mcp_gateway.process.MCPWrapperProcess", return_value=fake_process,
        ):
            with pytest.raises(asyncio.CancelledError):
                await service._spawn_plugin_generic(
                    "memdb", "slife.plugins.memdb.server",
                )

        fake_process.stop.assert_awaited()
        assert service._plugins["memdb"].client is None
        assert service._plugins["memdb"].process is None
        assert service._plugins["memdb"].port == 0


class TestSpawnPluginListToolsRetry:
    """A list_tools timeout (stuck SSE session from the plugin-load race)
    must reconnect with a fresh session and retry once — the plugin is
    serving by then, so the load self-heals instead of failing."""

    @pytest.mark.asyncio
    async def test_timeout_retries_with_fresh_session(self, sample_config):
        service = AgentService(sample_config)
        fake_process = AsyncMock()
        fake_process.port = 12345

        first_client = AsyncMock()
        first_client.list_tools.side_effect = TimeoutError("list_tools timed out")
        second_client = AsyncMock()
        second_client.list_tools.return_value = [{
            "name": "turn_recall", "description": "d",
            "inputSchema": {"type": "object", "properties": {}},
        }]
        second_client.call_tool = AsyncMock(
            # Readiness handshake runs at the end of the spawn.
            return_value='{"ready": true, "detail": "store ok"}',
        )
        fake_process.create_client.side_effect = [first_client, second_client]

        with patch(
            "slife.plugins.mcp_gateway.process.MCPWrapperProcess", return_value=fake_process,
        ):
            started = await service._spawn_plugin_generic(
                "memdb", "slife.plugins.memdb.server",
            )

        assert started is True
        first_client.disconnect.assert_awaited()
        assert fake_process.create_client.call_count == 2
        assert service._plugins["memdb"].client is second_client
        assert service._plugins["memdb"].process is fake_process

    @pytest.mark.asyncio
    async def test_second_timeout_still_fails(self, sample_config):
        """If the retry also times out, the spawn fails (fatal for memdb)."""
        service = AgentService(sample_config)
        fake_process = AsyncMock()

        first_client = AsyncMock()
        first_client.list_tools.side_effect = TimeoutError("list_tools timed out")
        second_client = AsyncMock()
        second_client.list_tools.side_effect = TimeoutError("list_tools timed out")
        fake_process.create_client.side_effect = [first_client, second_client]

        with patch(
            "slife.plugins.mcp_gateway.process.MCPWrapperProcess", return_value=fake_process,
        ):
            with pytest.raises(TimeoutError):
                await service._spawn_plugin_generic(
                    "memdb", "slife.plugins.memdb.server",
                )

        first_client.disconnect.assert_awaited()
        fake_process.stop.assert_awaited()


# ── Sharefile tunnel readiness watch ─────────────────────────────────────


class TestSharefileTunnelWatch:
    """Tests for the harness-owned tunnel readiness watch (main process
    probes __check after the sharefile plugin loads; the plugin
    never talks to the TUI)."""

    @pytest.mark.asyncio
    async def test_surfaces_when_failed(self, sample_config):
        """A terminal 'failed' state fires the on_tunnel_down callback once."""
        service = AgentService(sample_config)
        cb = MagicMock()
        service.on_tunnel_down(cb)
        client = AsyncMock()
        client.call_tool.return_value = _json.dumps({
            "active": False, "state": "failed", "url": "",
            "reason": "File sharing tunnel unavailable.",
        })

        await service._check_sharefile_tunnel(client)

        cb.assert_called_once()
        client.call_tool.assert_called_once_with("__check")

    @pytest.mark.asyncio
    async def test_silent_when_active(self, sample_config):
        """A live tunnel fires nothing."""
        service = AgentService(sample_config)
        cb = MagicMock()
        service.on_tunnel_down(cb)
        client = AsyncMock()
        client.call_tool.return_value = _json.dumps({
            "active": True, "state": "active", "url": "https://x.ngrok-free.dev",
        })

        await service._check_sharefile_tunnel(client)

        cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_waits_while_starting_then_surfaces_failed(self, sample_config):
        """A still-starting attempt is not misread as down — the watch follows
        it to the terminal failed state and surfaces once."""
        service = AgentService(sample_config)
        cb = MagicMock()
        service.on_tunnel_down(cb)
        client = AsyncMock()
        client.call_tool.side_effect = [
            _json.dumps({"active": False, "state": "starting", "url": ""}),
            _json.dumps({"active": False, "state": "failed", "url": "",
                         "hint": "already online"}),
        ]

        with patch("asyncio.sleep", new=AsyncMock()):
            await service._check_sharefile_tunnel(client)

        cb.assert_called_once()
        assert client.call_tool.call_count == 2

    @pytest.mark.asyncio
    async def test_probe_error_is_silent(self, sample_config):
        """If the plugin is unreachable mid-watch, nothing is surfaced."""
        service = AgentService(sample_config)
        cb = MagicMock()
        service.on_tunnel_down(cb)
        client = AsyncMock()
        client.call_tool.side_effect = RuntimeError("client gone")

        await service._check_sharefile_tunnel(client)

        cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_without_terminal_state_is_silent(self, sample_config):
        """Never reaching a terminal state within the bounded window stays
        silent rather than guessing."""
        service = AgentService(sample_config)
        cb = MagicMock()
        service.on_tunnel_down(cb)
        client = AsyncMock()
        client.call_tool.return_value = _json.dumps({
            "active": False, "state": "starting", "url": "",
        })

        with patch("asyncio.sleep", new=AsyncMock()), \
             patch("slife.timeouts.timeouts.ready.tunnel_settle", 0.0):
            await service._check_sharefile_tunnel(client)

        cb.assert_not_called()

    @pytest.mark.asyncio
    async def test_warning_names_the_provider_and_its_reason(self, sample_config):
        """A deterministic, provider-specific cause reaches the user.

        The tunnel is down for a reason the harness already knows ("cloudflared
        is not installed"), so the warning must say so rather than sending the
        user to system_health for it.
        """
        service = AgentService(sample_config)
        cb = MagicMock()
        service.on_tunnel_down(cb)
        client = AsyncMock()
        client.call_tool.return_value = _json.dumps({
            "active": False, "state": "failed", "url": "",
            "provider": "cloudflare",
            "reason": "cloudflared not found ('cloudflared'). A Cloudflare Quick "
                      "Tunnel needs the cloudflared binary — install it from "
                      "https://developers.cloudflare.com/downloads/",
        })

        await service._check_sharefile_tunnel(client)

        message = cb.call_args[0][0]
        # One short line: provider + cause, the reason's own terminator not
        # doubled, and no install URL (that belongs in the log) nor the
        # restated consequence + system_health pointer.  Asserted through the
        # localizer so it holds in either language.
        assert message == t(
            "tunnel_down",
            provider="cloudflare",
            reason="cloudflared not found ('cloudflared')",
        )

    @pytest.mark.asyncio
    async def test_warning_without_a_reason_points_at_system_health(self, sample_config):
        """Nothing to say beyond "down" — then the pointer earns its place."""
        service = AgentService(sample_config)
        cb = MagicMock()
        service.on_tunnel_down(cb)
        client = AsyncMock()
        client.call_tool.return_value = _json.dumps({
            "active": False, "state": "failed", "url": "",
            "provider": "ngrok", "reason": "",
        })

        await service._check_sharefile_tunnel(client)

        message = cb.call_args[0][0]
        assert "ngrok" in message
        assert "system_health" in message


class TestShortReason:
    """_short_reason condenses a provider's log-grade reason to one line."""

    def test_empty_stays_empty(self):
        assert _short_reason("") == ""

    def test_whitespace_and_newlines_collapse(self):
        assert _short_reason("line one\n   line two") == "line one line two"

    def test_first_sentence_wins(self):
        assert _short_reason("boom. and then a long tail") == "boom."

    def test_long_single_sentence_is_truncated(self):
        out = _short_reason("x" * 400)
        assert len(out) <= 140
        assert out.endswith("…")

    def test_sentence_past_the_limit_is_truncated_not_split(self):
        out = _short_reason("y" * 200 + ". tail")
        assert len(out) <= 140
        assert out.endswith("…")


class TestReloadActiveModelContextUsage:
    """A model switch must be a no-op on context-usage state.

    context_tokens_for always reports the last API call's real
    prompt + completion tokens (or, on a freshly restored session, the exit-time
    occupancy restore_session primed into _last_usage).  Clearing either
    on a switch made the first _turn_prompt after a restart-with-model-
    restore (cc-switch restoring the recorded active model before the
    first turn) report "Context usage: 0" even though the exit context
    WAS restored — so neither _usage_by_history nor _last_usage is
    touched."""

    def _service_with_two_models(self, sample_model_config, thinking_model_config):
        from slife.config import Config
        return AgentService(Config(
            models=[sample_model_config, thinking_model_config],
            active_model_ref="deepseek/deepseek-v4-flash",
            tools=[],
        ))

    def test_restored_session_keeps_context(self, sample_model_config, thinking_model_config):
        service = self._service_with_two_models(sample_model_config, thinking_model_config)
        # restore_session primes _last_usage with the previous turn's persisted
        # context_tokens and marks the history as freshly restored.
        service.agent_loop._last_usage = TokenUsage(
            prompt_tokens=102400, total_tokens=102400,
        )
        service.agent_loop._just_restored_history = id(service.message_history)

        service.reload_active_model("deepseek/deepseek-v4-pro")

        # The exit-time occupancy survives the switch — the first _turn_prompt
        # and the status bar report it instead of 0.
        assert service.agent_loop._last_usage.prompt_tokens == 102400
        assert service.current_context_tokens == 102400

    def test_mid_session_switch_preserves_usage(self, sample_model_config, thinking_model_config):
        service = self._service_with_two_models(sample_model_config, thinking_model_config)
        # A live reading from the last API call (per-history cache) plus a
        # restore-time fallback — neither is invalidated by switching.
        service.agent_loop._usage_by_history[id(service.message_history)] = TokenUsage(
            prompt_tokens=50000, total_tokens=50500,
        )
        service.agent_loop._last_usage = TokenUsage(
            prompt_tokens=102400, total_tokens=102400,
        )

        service.reload_active_model("deepseek/deepseek-v4-pro")

        assert service.agent_loop._usage_by_history.get(
            id(service.message_history)
        ).prompt_tokens == 50000
        assert service.agent_loop._last_usage.prompt_tokens == 102400
        # …and current_context_tokens still prefers the last real API call.
        assert service.current_context_tokens == 50000

    def test_fresh_session_still_zero(self, sample_model_config, thinking_model_config):
        service = self._service_with_two_models(sample_model_config, thinking_model_config)
        # Genuinely fresh start: no API call, no restore → 0, even after a switch.
        service.reload_active_model("deepseek/deepseek-v4-pro")
        assert service.current_context_tokens == 0


class TestReloadActiveModelHealthFact:
    """A live switch re-records the health report's model line.

    ``system_health``'s ``model`` entry is the only place the model describes
    itself (the system prompt carries no model name), and it comes from a
    startup record — which named the model the session began with until the
    switch superseded it.

    The two models are built HERE rather than taken from the shared
    ``sample_model_config`` fixture: that one is session-scoped and other
    tests mutate its ``context_window``, so its value depends on test
    order."""

    @staticmethod
    def _model(ref, *, thinking, vision, context_window=131072):
        from slife.config import ModelConfig
        return ModelConfig(
            ref=ref, provider="deepseek", api_model=ref.split("/", 1)[1],
            display_name=ref, api_key="sk-x",
            base_url="https://api.deepseek.com", api="openai-completions",
            supports_vision=vision, thinking_enabled=thinking,
            context_window=context_window,
        )

    def test_switch_supersedes_the_startup_record(self):
        from slife.config import Config
        from slife.health import clear, get_report, record_active_model

        clear()
        service = AgentService(Config(
            models=[
                self._model("deepseek/deepseek-v4-flash", thinking=False, vision=False),
                self._model("deepseek/deepseek-v4-pro", thinking=True, vision=True),
            ],
            active_model_ref="deepseek/deepseek-v4-flash",
            tools=[],
        ))
        # Exactly what slife/__init__ records at startup.
        record_active_model(service.config.active_model)
        assert get_report()[-1]["value"] == (
            "deepseek/deepseek-v4-flash (thinking=off, vision=off, ctx 131072)"
        )

        # The pair differs in both capability flags, so a switch that forgot
        # to re-record is caught by either one.
        service.reload_active_model("deepseek/deepseek-v4-pro")

        models = [e for e in get_report() if e["component"] == "model"]
        assert len(models) == 1  # superseded, not appended
        assert models[0]["value"] == (
            "deepseek/deepseek-v4-pro (thinking=on, vision=on, ctx 131072)"
        )
        assert models[0]["level"] == "ok"
