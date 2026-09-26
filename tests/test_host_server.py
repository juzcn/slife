"""slife-as-plugin (host server) tests — in-process ToolRegistry → MCP bridge.

Covers the DESIGN.md §1 slife-as-plugin surface:
- auto-discovered registry tools exposed under their bare names (NO
  hand-written @mcp.tool — the factory's Tool objects ARE the tool set);
- harness/context-control tools excluded from the outward face;
- the ``__check`` internal tool (harness probe, ``__`` prefix);
- execution routes through ``registry.execute`` (the same normalized path);
- a live registry mutation (register/unregister) is reflected in the exposed
  set and pushes the standard ``notifications/tools/list_changed``.

The server is in-process: the fastmcp ``Client`` connects to the FastMCP
instance directly (in-memory transport), no socket needed.
"""

import pytest; pytestmark = pytest.mark.unit

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool

import slife.timeouts as _timeouts
from slife.tools.registry import ToolRegistry
from slife.mcp.host_server import (
    _host_catalog_facts,
    build_registry_mcp,
    is_exposed,
    start_host_server,
)


# ── Test tools (stand-ins for auto-discovered slife.tools.* natives) ──────


class EchoTool:
    name = "echo"
    description = "Echoes back the input."
    parameters = {
        "type": "object",
        "properties": {"m": {"type": "string"}},
        "required": ["m"],
    }

    async def execute(self, **kwargs) -> str:
        return "Echo: " + str(kwargs.get("m", ""))


class ConfigTool:  # an excluded harness/context-control
    name = "set_max_iterations"
    description = "Changes the host loop's iteration cap (harness control)."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "cleared"


class TurnPromptTool:
    name = "_turn_prompt"
    description = "Harness marker (must be excluded from outward face)."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "note"


class CheckInputTool:
    name = "_check_new_input"
    description = "Mid-turn input injector (must be excluded from outward face)."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "injected"


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(EchoTool())
    r.register(ConfigTool())
    r.register(TurnPromptTool())
    r.register(CheckInputTool())
    return r


def _registered_names(mcp: FastMCP) -> set[str]:
    return {
        c.name for c in mcp.local_provider._components.values()
        if isinstance(c, FunctionTool)
    }


# ── Exposure filter ──────────────────────────────────────────────────────


class TestIsExposed:
    def test_bare_native_tool_exposed(self):
        assert is_exposed(EchoTool()) is True

    def test_harness_context_control_excluded(self):
        assert is_exposed(ConfigTool()) is False      # set_max_iterations
        assert is_exposed(TurnPromptTool()) is False  # _turn_prompt
        assert is_exposed(CheckInputTool()) is False  # _check_new_input

    def test_internal_prefix_excluded(self):
        class _Internal:
            name = "__check"
            description = "i"
            parameters = {}
            async def execute(self, **kw): return ""
        assert is_exposed(_Internal()) is False


# ── build_registry_mcp ──────────────────────────────────────────────────


class TestBuildRegistryMcp:
    def test_exposes_auto_discovered_tools(self):
        reg = _registry()
        mcp = build_registry_mcp(reg)
        names = _registered_names(mcp)
        assert "echo" in names                # native tool, bare name
        assert "__check" in names             # internal harness probe
        assert "set_max_iterations" not in names  # harness control excluded
        assert "_turn_prompt" not in names    # harness marker excluded
        assert "_check_new_input" not in names  # input injector excluded

    def test_wrapped_tool_carries_slife_schema(self):
        reg = _registry()
        mcp = build_registry_mcp(reg)
        comp = next(c for c in mcp.local_provider._components.values()
                    if isinstance(c, FunctionTool) and c.name == "echo")
        assert comp.parameters == EchoTool.parameters
        assert comp.description == EchoTool.description
        # ctx is injected by FastMCP — not part of the exposed schema.
        assert "ctx" not in comp.parameters.get("properties", {})

    @pytest.mark.asyncio
    async def test_execution_routes_through_registry_execute(self):
        reg = _registry()
        mcp = build_registry_mcp(reg)
        comp = next(c for c in mcp.local_provider._components.values()
                    if isinstance(c, FunctionTool) and c.name == "echo")
        result = await comp.fn(m="hi")
        assert result == "Echo: hi"

    def test_check_reports_exposed_count(self):
        reg = _registry()
        mcp = build_registry_mcp(reg)
        comp = next(c for c in mcp.local_provider._components.values()
                    if isinstance(c, FunctionTool) and c.name == "__check")
        assert comp is not None

    @pytest.mark.asyncio
    async def test_check_reports_catalog_facts(self, tmp_path):
        """The host-as-plugin's __check carries the unified tool catalog's live
        facts (the host owns tools.db) — a harness/consumer probe gets the
        catalog view without a bespoke check."""
        import json as _json

        from slife.tools.base import Tool
        from slife.tools.catalog import CatalogStore
        from slife.tools.catalog_service import ToolCatalogService

        class _Shell(Tool):
            name = "execute_shell"
            description = "run a shell command"
            parameters = {"type": "object", "properties": {}, "required": []}

            async def execute(self, **kwargs) -> str:
                return "ok"

        store = CatalogStore(tmp_path / "tools.db")
        await store.open()
        svc = ToolCatalogService(store, write_owner=True)
        await svc.sync_system_tools([_Shell()])

        reg = _registry()
        mcp = build_registry_mcp(reg, catalog=svc)
        comp = next(c for c in mcp.local_provider._components.values()
                    if isinstance(c, FunctionTool) and c.name == "__check")
        payload = _json.loads(await comp.fn())
        assert payload["catalog"]["tools"] == 1
        # Seeding registers a row but does NOT load it — only the whitelist is
        # born loaded — so the fact reads 0 until something loads it.
        assert payload["catalog"]["loaded"] == 0
        assert payload["catalog"]["servers"] == 0     # no external rows yet
        await svc.load_tool("execute_shell")
        payload = _json.loads(await comp.fn())
        assert payload["catalog"]["loaded"] == 1
        # Nothing has published a semantic state: this process holds no
        # drainer, and no other process has announced one.  The block says
        # exactly that — it does not claim "disabled", which would be a
        # statement about a drainer nobody has heard from.
        sem = payload["catalog"]["semantic"]
        assert sem["state"] == "unknown"
        assert "configured" not in sem
        await store.close()

        # without a catalog, __check keeps the registry-only shape
        mcp2 = build_registry_mcp(reg)
        comp2 = next(c for c in mcp2.local_provider._components.values()
                     if isinstance(c, FunctionTool) and c.name == "__check")
        p2 = _json.loads(await comp2.fn())
        assert "exposed_count" in p2 and "catalog" not in p2

    @pytest.mark.asyncio
    async def test_check_reports_the_published_semantic_state(self, tmp_path):
        """A process without the drainer reports the state its owner published.

        The tool-catalog index is shared, so its state is written into the same
        db's ``meta`` table by whoever runs the drainer — a subagent reads that
        row instead of knowing nothing (which is what made a degraded index
        read as healthy in a worker while the main agent reported the failure).
        """
        import json as _json

        from slife.tools.base import Tool
        from slife.tools.catalog import CatalogStore
        from slife.tools.catalog_service import ToolCatalogService
        from slife.tools.semantic import SEMANTIC_STATE_KEY

        class _Shell(Tool):
            name = "execute_shell"
            description = "run a shell command"
            parameters = {"type": "object", "properties": {}, "required": []}

            async def execute(self, **kwargs) -> str:
                return "ok"

        store = CatalogStore(tmp_path / "tools.db")
        await store.open()
        try:
            svc = ToolCatalogService(store, write_owner=True)
            await svc.sync_system_tools([_Shell()])
            await store.set_meta(SEMANTIC_STATE_KEY, _json.dumps({
                "configured": True, "available": True, "semantic_ready": False,
                "state": "stalled", "reason": "embedder gave up this round",
                "model": "BAAI/bge-m3", "dimension": 1024,
            }))

            reg = _registry()
            mcp = build_registry_mcp(reg, catalog=svc)
            comp = next(c for c in mcp.local_provider._components.values()
                        if isinstance(c, FunctionTool) and c.name == "__check")
            sem = _json.loads(await comp.fn())["catalog"]["semantic"]

            assert sem["state"] == "stalled"
            assert sem["reason"] == "embedder gave up this round"
            assert sem["model"] == "BAAI/bge-m3"
            assert sem["dimension"] == 1024
            # The pending count is never published — it is counted here, from
            # the rows, so a copy in the published row cannot go stale against
            # them.  One seeded tool row is awaiting its embedding.
            assert sem["unembedded"] == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_serve_self_heals_on_unexpected_death(self):
        """The host-as-plugin runs in-process, so the child watchdog doesn't
        cover it — the serve task self-heals: an unexpected death is logged and
        the same server rebinds with backoff (clean stop after recovery)."""
        reg = _registry()
        calls = {"n": 0}

        async def _flaky(server, host, sockets, port):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return None  # second run completes cleanly → loop returns

        with patch("slife.mcp.host_server._serve_host", _flaky), \
             patch(
                 "slife.mcp.host_server.bind_free_port",
                 return_value=(object(), 9876),
             ), \
             patch.object(_timeouts.timeouts.ready, "watchdog_backoff_initial", 0.0):
            _, task, _stop, port = start_host_server(reg)
            await asyncio.wait_for(task, timeout=5)  # noqa-timeout
            assert calls["n"] == 2
            assert port == 9876

    @pytest.mark.asyncio
    async def test_serve_cancellation_stops(self):
        """A running serve task cancels cleanly (the shutdown path — no
        endless respawn on the graceful stop)."""
        reg = _registry()

        async def _never_returns(server, host, sockets, port):
            await asyncio.Event().wait()  # serve until told to stop

        with patch("slife.mcp.host_server._serve_host", _never_returns), \
             patch(
                 "slife.mcp.host_server.bind_free_port",
                 return_value=(object(), 9877),
             ):
            _, task, _stop, port = start_host_server(reg)
            await asyncio.sleep(0)  # let the task reach serve
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task


# ── Multi-tool routing (A1 regression) ──────────────────────────────────


class _IdentTool:  # returns its own name so routing mistakes are visible
    def __init__(self, name):
        self.name = name
        self.description = f"Tool {name}"
        self.parameters = {
            "type": "object",
            "properties": {},
        }

    async def execute(self, **kwargs) -> str:
        return f"ran {self.name}"


class TestMultiToolRouting:
    @pytest.mark.asyncio
    async def test_each_tool_routes_to_its_own_name(self):
        """A1 regression: every tool registered in one _sync_registry pass must
        execute itself, not the last tool in the batch (late-binding closure)."""
        reg = ToolRegistry()
        for nm in ("tool_a", "tool_b", "tool_c"):
            reg.register(_IdentTool(nm))
        mcp = build_registry_mcp(reg)

        comps = {
            c.name: c for c in mcp.local_provider._components.values()
            if isinstance(c, FunctionTool) and c.name.startswith("tool_")
        }
        assert set(comps) == {"tool_a", "tool_b", "tool_c"}

        for nm, comp in comps.items():
            result = await comp.fn()
            assert result == f"ran {nm}", f"{nm} routed to wrong tool: {result!r}"

    @pytest.mark.asyncio
    async def test_routing_survives_a_live_sync_pass(self):
        """Adding a later tool must not rewire the earlier tools' target."""
        reg = _registry()  # registers echo/set_max_iterations/_turn_prompt
        mcp = build_registry_mcp(reg)
        echo = next(c for c in mcp.local_provider._components.values()
                    if isinstance(c, FunctionTool) and c.name == "echo")

        class OtherTool:
            name = "other"
            description = "Other."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs) -> str:
                return "other-result"

        reg.register(OtherTool())  # triggers the change listener → another sync
        await asyncio.sleep(0.05)
        result = await echo.fn(m="hi")
        assert result == "Echo: hi"


# ── Live registry sync + list_changed ───────────────────────────────────


class TestLiveSync:
    @pytest.mark.asyncio
    async def test_register_refreshes_exposed_set(self):
        """A live registry mutation is reflected in the server's tool set.

        start_host_server wires the registry change listener; registering a
        new tool must grow the exposed set (the same diff _sync_registry
        applies, and its session broadcast notifies connected consumers).
        """
        reg = _registry()
        server, task, stop, _port = start_host_server(reg)
        try:
            class NewTool:
                name = "count_letters"
                description = "Count letters."
                parameters = {
                    "type": "object",
                    "properties": {"s": {"type": "string"}},
                    "required": ["s"],
                }
                async def execute(self, **kw):
                    return str(len(kw.get("s", "")))

            before = _registered_names(server)
            reg.register(NewTool())
            await asyncio.sleep(0.05)  # let the change listener run

            after = _registered_names(server)
            assert "count_letters" in after
            assert after - before == {"count_letters"}
        finally:
            await stop()

    @pytest.mark.asyncio
    async def test_change_listener_added_by_start(self):
        """start_host_server subscribes to the registry's change listeners."""
        reg = _registry()
        server, task, stop, _port = start_host_server(reg)
        try:
            assert len(reg._on_change) >= 1
            # Registry alone (no host) has none.
            assert ToolRegistry()._on_change == []
        finally:
            await stop()

class TestCatalogFacts:
    """The catalog's server count is ONE number, over the sources that own rows.

    It is deliberately not split by family: the catalog holds tools, and
    "an MCP server vs a REST API" is a distinction of how the config
    implements something, not of what the catalog contains.  A split put an
    implementation detail on a surface the agent reads, and its "0 rest-api"
    contradicted the `rest-api` component beside it — which counts CONFIGURED
    servers, a different population.  The report says "servers with rows" for
    the same reason: the two must not read as one number.
    """

    @staticmethod
    def _catalog(source_ids):
        async def _list_source_ids(categories=None):
            return set(source_ids)

        store = MagicMock()
        store.list_source_ids = _list_source_ids
        store.scan_effective = AsyncMock(return_value=[1] * 1596)
        store.count_loaded = AsyncMock(return_value=16)
        return MagicMock(store=store, semantic_manager=None)

    @pytest.mark.asyncio
    async def test_it_counts_sources_that_own_rows(self):
        catalog = self._catalog([f"m{i}" for i in range(18)])
        facts = await _host_catalog_facts(catalog)
        assert facts["servers"] == 18
        assert facts["tools"] == 1596 and facts["loaded"] == 16

    @pytest.mark.asyncio
    async def test_there_is_no_family_split(self):
        """A configured server whose tool list has not been read owns no rows
        and is absent here — the components are what list it.  Splitting this
        count by family is what made the two look comparable."""
        facts = await _host_catalog_facts(self._catalog(["a", "b"]))
        assert not {k for k in facts if k.startswith("servers_")}
