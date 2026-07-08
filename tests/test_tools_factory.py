"""Tests for tool factory (slife.tools.factory)."""

import warnings

import pytest

from slife.tools.base import Tool
from slife.tools.factory import create_tools_from_config
from slife.tools.registry import ToolRegistry


class TestCreateToolsFromConfig:
    """Tests for create_tools_from_config()."""

    def test_returns_tool_registry(self):
        """Returns a ToolRegistry instance."""
        registry = create_tools_from_config([])
        assert isinstance(registry, ToolRegistry)

    def test_create_shell_tool(self):
        """Shell tool is created from config entry."""
        entries = [{"type": "shell", "timeout": 45}]
        registry = create_tools_from_config(entries)
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "execute_shell"
        assert tools[0].timeout == 45

    def test_create_serper_tool(self):
        """Serper tool is created from config entry."""
        entries = [{"type": "serper", "api_key": "test-key-123"}]
        registry = create_tools_from_config(entries)
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "web_search"
        assert tools[0].api_key == "test-key-123"

    def test_create_multiple_tools(self):
        """Multiple tools are created from multiple entries."""
        entries = [
            {"type": "shell", "timeout": 30},
            {"type": "serper", "api_key": "key"},
        ]
        registry = create_tools_from_config(entries)
        tools = registry.list_tools()
        assert len(tools) == 2
        names = {t.name for t in tools}
        assert names == {"execute_shell", "web_search"}

    def test_shell_tool_default_timeout(self):
        """Shell tool uses default timeout when not specified."""
        entries = [{"type": "shell"}]
        registry = create_tools_from_config(entries)
        tool = registry.list_tools()[0]
        assert tool.timeout == 30  # default

    def test_empty_entries(self):
        """Empty entries list returns empty registry."""
        registry = create_tools_from_config([])
        assert registry.list_tools() == []

    def test_missing_type_field(self):
        """Entry without 'type' field triggers a warning and is skipped."""
        with pytest.warns(UserWarning, match="missing 'type'"):
            registry = create_tools_from_config([{"timeout": 30}])
        assert registry.list_tools() == []

    def test_unknown_tool_type(self):
        """Unknown tool type triggers a warning and is skipped."""
        with pytest.warns(UserWarning, match="Unknown tool type"):
            registry = create_tools_from_config([
                {"type": "nonexistent_tool_type_xyz", "arg": 1}
            ])
        assert registry.list_tools() == []

    def test_mixed_valid_and_invalid_entries(self):
        """Valid entries still work alongside invalid ones."""
        with pytest.warns(UserWarning) as record:
            registry = create_tools_from_config([
                {"type": "shell", "timeout": 10},
                {"type": "unknown_type"},
                {"type": "serper", "api_key": "k"},
                {},  # missing type
            ])
        assert len(registry.list_tools()) == 2
        # Two warnings: one for unknown type, one for missing type
        assert len(record) == 2

    def test_registry_is_usable(self):
        """Returned registry can execute tools."""
        entries = [{"type": "shell", "timeout": 10}]
        registry = create_tools_from_config(entries)
        assert registry.get("execute_shell") is not None
        assert registry.to_openai_functions()[0]["function"]["name"] == "execute_shell"

    def test_all_registered_builders_work(self):
        """Ensure all entries in _TOOL_BUILDERS produce valid tools."""
        from slife.tools.factory import _TOOL_BUILDERS

        for tool_type in _TOOL_BUILDERS:
            if tool_type == "serper":
                cfg = {"type": "serper", "api_key": "test"}
            elif tool_type == "shell":
                cfg = {"type": "shell", "timeout": 5}
            else:
                continue

            entries = [cfg]
            registry = create_tools_from_config(entries)
            tools = registry.list_tools()
            assert len(tools) == 1, f"Builder for '{tool_type}' failed"
            assert isinstance(tools[0], Tool)
