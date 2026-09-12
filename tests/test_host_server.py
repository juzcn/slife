"""slife-as-plugin (host server) tests — in-process ToolRegistry → MCP bridge.

Covers the DESIGNER_NOTES §8 "turn slife to a plugin" surface:
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
import inspect

import pytest

from fastmcp import FastMCP
from fastmcp.tools.function_tool import FunctionTool

from slife.tools.registry import ToolRegistry
from slife.mcp.host_server import (
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
    name = "clear_context"
    description = "Resets loaded turns (harness control)."
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
        assert is_exposed(ConfigTool()) is False      # clear_context
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
        assert "clear_context" not in names   # harness control excluded
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
        reg = _registry()  # registers echo/clear_context/_turn_prompt together
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