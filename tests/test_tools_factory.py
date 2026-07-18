"""Tests for Slife.tools.factory — auto-discovery tool loading."""

import pytest

from slife.tools.factory import create_tools_from_config


class TestCreateToolsFromConfig:
    """Tests for create_tools_from_config with auto-discovery."""

    def test_auto_discovery_no_overrides(self):
        """All tools auto-discovered when no overrides given."""
        registry = create_tools_from_config(None)
        names = {t.name for t in registry.list_tools()}
        assert "execute_shell" in names
        assert "run_python_script" in names
        assert "get_os_info" in names
        assert "list_skills" in names
        assert "use_skill" in names
        assert "add_skill" in names
        assert "remove_skill" in names
        assert "config_env_set" in names
        assert "config_secret_register" in names
        assert "config_env_get" in names
        assert "config_env_remove" in names
        assert "cli_add_tool" in names
        assert "cli_remove_tool" in names
        assert "cli_list_tools" in names

    def test_empty_list_same_as_none(self):
        """Empty overrides list == all tools discovered."""
        registry = create_tools_from_config([])
        names = {t.name for t in registry.list_tools()}
        assert "execute_shell" in names

    def test_shell_tool_timeout_override(self):
        """Override matched by tool name."""
        registry = create_tools_from_config([
            {"name": "execute_shell", "timeout": 45},
        ])
        tool = registry.get("execute_shell")
        assert tool is not None
        assert tool.timeout == 45

    def test_shell_tool_default_timeout(self):
        """Shell tool uses default timeout when no override given."""
        registry = create_tools_from_config(None)
        tool = registry.get("execute_shell")
        assert tool.timeout == 30

    def test_disable_tool(self):
        """Tool can be disabled with enabled: false."""
        registry = create_tools_from_config([
            {"name": "execute_shell", "enabled": False},
        ])
        assert registry.get("execute_shell") is None
        assert registry.get("run_python_script") is not None

    def test_skill_tool_custom_skills_dir(self):
        """Each skill tool matched individually by name."""
        registry = create_tools_from_config([
            {"name": "list_skills", "skills_dir": "custom_skills"},
        ])
        # Overridden tool gets custom dir
        assert str(registry.get("list_skills").skills_dir) == "custom_skills"
        # Other skill tools still use default
        assert str(registry.get("use_skill").skills_dir) == "skills"


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


class TestCreateToolsOverrideEdgeCases:
    """Edge cases for create_tools_from_config overrides."""

    def test_override_entry_without_name_logs_warning(self, caplog):
        """Override entries without a 'name' key log a warning."""
        registry = create_tools_from_config([
            {"timeout": 60},  # No name!
        ])
        # Tool should still be discovered normally
        assert registry.get("execute_shell") is not None
        # Warning should be logged
        assert any("tool_override_no_name" in r.message for r in caplog.records)

    def test_a2a_tools_skipped_when_no_a2a_config(self):
        """A2A tools with requires_a2a=True are skipped when A2A is disabled."""
        registry = create_tools_from_config(None, config=None)
        names = {t.name for t in registry.list_tools()}
        # Only a2a_list_agents has requires_a2a=True
        assert "a2a_list_agents" not in names
        # Other A2A tools don't require A2A and are always registered
        assert "a2a_send_task" in names
