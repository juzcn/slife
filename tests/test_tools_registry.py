"""Tests for Slife.tools.registry — ToolRegistry."""

import pytest

from slife.tools.registry import ToolRegistry, get_registry, set_registry
from slife.tools.base import Tool


class TestGlobalRegistry:
    """Tests for module-level get_registry / set_registry."""

    def test_get_registry_returns_none_initially(self):
        """Before any set_registry call, get_registry returns None (or whatever the
        module default is — we don't set it, so it may already be None)."""
        # The _current_registry module var may already be set by other tests.
        # We store the original, set it to None, test, then restore it.
        import slife.tools.registry as _mod
        original = _mod._current_registry
        try:
            _mod._current_registry = None
            assert get_registry() is None
        finally:
            _mod._current_registry = original

    def test_set_and_get_registry_round_trip(self):
        """set_registry() followed by get_registry() returns the same object."""
        import slife.tools.registry as _mod
        original = _mod._current_registry
        try:
            registry = ToolRegistry()
            set_registry(registry)
            assert get_registry() is registry
        finally:
            _mod._current_registry = original

    def test_set_registry_overwrites_previous(self):
        """Calling set_registry twice overwrites the previous value."""
        import slife.tools.registry as _mod
        original = _mod._current_registry
        try:
            r1 = ToolRegistry()
            r2 = ToolRegistry()
            set_registry(r1)
            assert get_registry() is r1
            set_registry(r2)
            assert get_registry() is r2
        finally:
            _mod._current_registry = original


class TestToolRegistry:
    """Tests for ToolRegistry."""

    def test_empty_registry(self, empty_registry):
        """New registry has no tools."""
        assert empty_registry.list_tools() == []

    def test_register_and_get(self, echo_tool):
        registry = ToolRegistry()
        registry.register(echo_tool)
        assert registry.get("echo") is echo_tool

    def test_get_missing_returns_none(self, empty_registry):
        assert empty_registry.get("nonexistent") is None

    def test_list_tools(self, tool_registry):
        tools = tool_registry.list_tools()
        names = {t.name for t in tools}
        assert names == {"echo", "failer"}

    def test_to_openai_functions(self, tool_registry):
        fns = tool_registry.to_openai_functions()
        assert len(fns) == 2
        names = [f["function"]["name"] for f in fns]
        assert "echo" in names
        assert "failer" in names

    @pytest.mark.asyncio
    async def test_execute_known_tool(self, tool_registry):
        result = await tool_registry.execute("echo", message="hello")
        assert "Echo: hello" in result

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self, empty_registry):
        result = await empty_registry.execute("nonexistent")
        assert result.startswith("Error: Unknown tool")
        assert "nonexistent" in result

    @pytest.mark.asyncio
    async def test_execute_with_error(self, tool_registry):
        result = await tool_registry.execute("failer", reason="test failure")
        assert "Error executing failer" in result
        assert "Intentional failure" in result

    @pytest.mark.asyncio
    async def test_execute_is_async(self, tool_registry):
        """Execute is async and can be awaited."""
        result = await tool_registry.execute("echo", message="test")
        assert result == "Echo: test"

    @pytest.mark.asyncio
    async def test_register_overwrites(self, echo_tool):
        """Registering a tool with the same name overwrites."""
        registry = ToolRegistry()
        registry.register(echo_tool)

        class NewEcho(Tool):
            name = "echo"
            description = "New echo"
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "New!"

        registry.register(NewEcho())
        assert len(registry.list_tools()) == 1
        result = await registry.execute("echo")
        assert "New!" in result

    def test_unregister_existing(self, tool_registry):
        """Unregister returns True and removes the tool."""
        # Clone since tool_registry is session-scoped
        registry = ToolRegistry()
        registry.register(tool_registry.get("echo"))
        assert registry.unregister("echo") is True
        assert registry.get("echo") is None

    def test_unregister_missing(self, empty_registry):
        """Unregister returns False for non-existent tools."""
        assert empty_registry.unregister("nonexistent") is False

    def test_unregister_by_prefix(self, tool_registry):
        """Unregister by prefix removes matching tools and returns count."""
        registry = ToolRegistry()
        registry.register(tool_registry.get("echo"))
        registry.register(tool_registry.get("failer"))
        # Both don't share a prefix — this should remove 0
        count = registry.unregister_by_prefix("nonexistent_prefix_")
        assert count == 0
        assert len(registry.list_tools()) == 2

    def test_unregister_by_prefix_multiple(self):
        """Multiple tools with matching prefix are all removed."""
        registry = ToolRegistry()

        class ToolA(Tool):
            name = "mcp__tool_a"
            description = "A"
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "a"

        class ToolB(Tool):
            name = "mcp__tool_b"
            description = "B"
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "b"

        class ToolC(Tool):
            name = "other_tool"
            description = "C"
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "c"

        registry.register(ToolA())
        registry.register(ToolB())
        registry.register(ToolC())

        count = registry.unregister_by_prefix("mcp__")
        assert count == 2
        assert registry.get("mcp__tool_a") is None
        assert registry.get("mcp__tool_b") is None
        assert registry.get("other_tool") is not None

    @pytest.mark.asyncio
    async def test_execute_positional_only(self, tool_registry):
        """The tool_name parameter is positional-only — kwargs don't collide."""
        result = await tool_registry.execute("echo", message="positional_test")
        assert result == "Echo: positional_test"
