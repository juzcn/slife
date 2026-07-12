"""Tests for slife.tools.factory — config-driven tool loading."""

import logging
import pytest

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

    def test_multiple_tools(self):
        """Multiple tools are created from config."""
        registry = create_tools_from_config([
            {"type": "shell", "timeout": 10},
            {"type": "platform"},
        ])
        names = {t.name for t in registry.list_tools()}
        assert names == {"execute_shell", "run_python_script", "get_os_info"}

    def test_unknown_tool_type_warns(self, caplog):
        """Unknown tool type logs a warning and is skipped."""
        with caplog.at_level(logging.WARNING):
            registry = create_tools_from_config([
                {"type": "unknown_tool_xyz"},
            ])
        assert registry.list_tools() == []
        assert "Unknown tool type" in caplog.text

    def test_missing_type_field_warns(self, caplog):
        """Entry without 'type' field logs a warning."""
        with caplog.at_level(logging.WARNING):
            registry = create_tools_from_config([
                {"timeout": 30},
            ])
        assert registry.list_tools() == []
        assert "missing" in caplog.text.lower()

    def test_mixed_valid_and_invalid(self, caplog):
        """Valid tools are created even if some entries are invalid."""
        with caplog.at_level(logging.WARNING):
            registry = create_tools_from_config([
                {"type": "shell"},
                {"type": "bad_type"},
                {},
                {"type": "platform"},
            ])
        names = {t.name for t in registry.list_tools()}
        assert names == {"execute_shell", "run_python_script", "get_os_info"}
        assert "Unknown tool type" in caplog.text
        assert "missing" in caplog.text.lower()


class TestRunPythonScriptTool:
    """Tests for RunPythonScriptTool.execute()."""

    @pytest.mark.asyncio
    async def test_execute_returns_command(self):
        """execute() returns platform command for a script."""
        from slife.tools.run_python_script import RunPythonScriptTool
        tool = RunPythonScriptTool()
        result = await tool.execute(script="script.py {}")
        assert "python" in result
        assert "script.py" in result

    @pytest.mark.asyncio
    async def test_execute_with_json_args(self):
        """execute() handles JSON args."""
        from slife.tools.run_python_script import RunPythonScriptTool
        tool = RunPythonScriptTool()
        result = await tool.execute(script='script.py {"key": "value"}')
        assert "script.py" in result
        assert "key" in result
