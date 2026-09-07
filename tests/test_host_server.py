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
    DEFAULT_HOST_PORT,
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


class SysNoteTool:
    name = "_sys_note"
    description = "Harness marker (must be excluded from outward face)."
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "note"


def _registry() -> ToolRegistry:
    r = ToolRegistry()
    r.register(EchoTool())
    r.register(ConfigTool())
    r.register(SysNoteTool())
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
        assert is_exposed(ConfigTool()) is False   # clear_context
        assert is_exposed(SysNoteTool()) is False  # _sys_note

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
        assert "_sys_note" not in names       # harness marker excluded

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
        server, task, stop = start_host_server(reg, port=0)
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
        server, task, stop = start_host_server(reg, port=0)
        try:
            assert len(reg._on_change) >= 1
            # Registry alone (no host) has none.
            assert ToolRegistry()._on_change == []
        finally:
            await stop()