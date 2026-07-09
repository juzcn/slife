"""Tests for slife.tools.registry — ToolRegistry."""

import pytest

from slife.tools.registry import ToolRegistry
from slife.tools.base import Tool


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
