"""Tests for slife.tools.factory — config-driven tool loading."""

import pytest
import warnings

from slife.tools.factory import create_tools_from_config


class TestCreateToolsFromConfig:
    """Tests for create_tools_from_config."""

    def test_empty_config(self):
        """Empty tool entries list returns empty registry."""
        registry = create_tools_from_config([])
        assert registry.list_tools() == []

    def test_shell_tool(self):
        """Shell tool is created from config."""
        registry = create_tools_from_config([
            {"type": "shell", "timeout": 45},
        ])
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "execute_shell"
        assert tools[0].timeout == 45

    def test_shell_tool_default_timeout(self):
        """Shell tool uses default timeout when not specified."""
        registry = create_tools_from_config([
            {"type": "shell"},
        ])
        tool = registry.list_tools()[0]
        assert tool.timeout == 30

    def test_serper_tool(self):
        """Serper tool is created from config."""
        registry = create_tools_from_config([
            {"type": "serper", "api_key": "test-api-key"},
        ])
        tools = registry.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "web_search"
        assert tools[0].api_key == "test-api-key"

    def test_multiple_tools(self):
        """Multiple tools are created from config."""
        registry = create_tools_from_config([
            {"type": "shell", "timeout": 10},
            {"type": "serper", "api_key": "key"},
        ])
        names = {t.name for t in registry.list_tools()}
        assert names == {"execute_shell", "web_search"}

    def test_unknown_tool_type_warns(self):
        """Unknown tool type logs a warning and is skipped."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            registry = create_tools_from_config([
                {"type": "unknown_tool_xyz"},
            ])
            assert len(w) == 1
            assert "Unknown tool type" in str(w[0].message)
        assert registry.list_tools() == []

    def test_missing_type_field_warns(self):
        """Entry without 'type' field logs a warning."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            registry = create_tools_from_config([
                {"timeout": 30},
            ])
            assert len(w) == 1
            assert "missing" in str(w[0].message).lower()
        assert registry.list_tools() == []

    def test_mixed_valid_and_invalid(self):
        """Valid tools are created even if some entries are invalid."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            registry = create_tools_from_config([
                {"type": "shell"},
                {"type": "bad_type"},
                {},
                {"type": "serper", "api_key": "k"},
            ])
            # Two warnings for bad_type and missing type
            assert len(w) == 2

        names = {t.name for t in registry.list_tools()}
        assert names == {"execute_shell", "web_search"}
