"""Tests for the ToolRegistry (slife.tools.registry)."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from slife.tools.base import Tool
from slife.tools.registry import ToolRegistry


# ── Test tool fixtures ────────────────────────────────────────────────


class _MockTool(Tool):
    """Simple mock tool for testing."""

    name = "mock_tool"
    description = "A mock tool for testing"
    parameters = {
        "type": "object",
        "properties": {
            "param1": {"type": "string"},
        },
        "required": ["param1"],
    }

    async def execute(self, param1: str) -> str:
        return f"mock result: {param1}"


class _MockTool2(Tool):
    """Second mock tool for testing."""

    name = "mock_tool2"
    description = "Another mock tool"
    parameters = {"type": "object", "properties": {}}

    async def execute(self, **kwargs) -> str:
        return "result2"


# ══════════════════════════════════════════════════════════════════════


class TestToolRegistryInit:
    """Tests for ToolRegistry.__init__()."""

    def test_empty_registry(self):
        """New registry has no tools."""
        registry = ToolRegistry()
        assert registry.list_tools() == []
        assert registry.to_openai_functions() == []


class TestToolRegistryRegister:
    """Tests for ToolRegistry.register()."""

    def test_register_tool(self):
        """Tool is added to the registry."""
        registry = ToolRegistry()
        tool = _MockTool()
        registry.register(tool)
        assert len(registry.list_tools()) == 1
        assert registry.list_tools()[0] == tool

    def test_register_multiple_tools(self):
        """Multiple tools can be registered."""
        registry = ToolRegistry()
        t1 = _MockTool()
        t2 = _MockTool2()
        registry.register(t1)
        registry.register(t2)
        assert len(registry.list_tools()) == 2

    def test_register_overwrites_by_name(self):
        """Registering a tool with the same name overwrites."""
        registry = ToolRegistry()

        class ToolV1(Tool):
            name = "versioned"
            description = "v1"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return "v1"

        class ToolV2(Tool):
            name = "versioned"
            description = "v2"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return "v2"

        registry.register(ToolV1())
        registry.register(ToolV2())
        assert len(registry.list_tools()) == 1
        assert registry.list_tools()[0].description == "v2"


class TestToolRegistryGet:
    """Tests for ToolRegistry.get()."""

    def test_get_existing_tool(self):
        """get() returns the tool for a known name."""
        registry = ToolRegistry()
        tool = _MockTool()
        registry.register(tool)
        assert registry.get("mock_tool") == tool

    def test_get_unknown_tool_returns_none(self):
        """get() returns None for unknown tool name."""
        registry = ToolRegistry()
        assert registry.get("nonexistent") is None

    def test_get_from_empty_registry(self):
        """get() on empty registry returns None."""
        registry = ToolRegistry()
        assert registry.get("anything") is None


class TestToolRegistryListTools:
    """Tests for ToolRegistry.list_tools()."""

    def test_list_returns_copy(self):
        """list_tools() returns a new list."""
        registry = ToolRegistry()
        t1 = _MockTool()
        registry.register(t1)

        tools = registry.list_tools()
        tools.append(_MockTool2())  # Mutate the returned list
        assert len(registry.list_tools()) == 1  # Registry unchanged

    def test_list_preserves_order(self):
        """Tools are returned in registration order."""
        registry = ToolRegistry()
        t1 = _MockTool()
        t2 = _MockTool2()
        registry.register(t1)
        registry.register(t2)

        tools = registry.list_tools()
        assert tools[0].name == "mock_tool"
        assert tools[1].name == "mock_tool2"


class TestToolRegistryToOpenaiFunctions:
    """Tests for ToolRegistry.to_openai_functions()."""

    def test_empty_registry(self):
        """Empty registry returns empty list."""
        registry = ToolRegistry()
        assert registry.to_openai_functions() == []

    def test_single_tool(self):
        """Single tool converted to OpenAI format."""
        registry = ToolRegistry()
        registry.register(_MockTool())
        result = registry.to_openai_functions()
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "mock_tool"

    def test_multiple_tools(self):
        """All tools are converted."""
        registry = ToolRegistry()
        registry.register(_MockTool())
        registry.register(_MockTool2())
        result = registry.to_openai_functions()
        assert len(result) == 2
        names = {f["function"]["name"] for f in result}
        assert names == {"mock_tool", "mock_tool2"}


class TestToolRegistryExecute:
    """Tests for ToolRegistry.execute()."""

    @pytest.mark.asyncio
    async def test_execute_known_tool(self):
        """Known tool is executed with kwargs."""
        registry = ToolRegistry()
        registry.register(_MockTool())

        result = await registry.execute("mock_tool", param1="hello")
        assert result == "mock result: hello"

    @pytest.mark.asyncio
    async def test_execute_unknown_tool(self):
        """Unknown tool returns error message."""
        registry = ToolRegistry()
        result = await registry.execute("unknown_tool", arg="val")
        assert result.startswith("Error: Unknown tool")
        assert "unknown_tool" in result

    @pytest.mark.asyncio
    async def test_execute_tool_raises_exception(self):
        """Tool execution error is caught and returned as error string."""

        class FailingTool(Tool):
            name = "failer"
            description = "Always fails"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                raise RuntimeError("Something broke")

        registry = ToolRegistry()
        registry.register(FailingTool())

        result = await registry.execute("failer")
        assert "Error" in result
        assert "Something broke" in result

    @pytest.mark.asyncio
    async def test_execute_passes_kwargs(self):
        """execute() passes all kwargs to the tool."""

        class KwargsTool(Tool):
            name = "kwargs_tool"
            description = "Checks kwargs"
            parameters = {"type": "object", "properties": {}}

            async def execute(self, **kwargs) -> str:
                return f"got: {sorted(kwargs.keys())}"

        registry = ToolRegistry()
        registry.register(KwargsTool())

        result = await registry.execute("kwargs_tool", a=1, b=2, c=3)
        assert "a" in result
        assert "b" in result
        assert "c" in result
