"""Tests for Slife.tools.factory — auto-discovery tool loading."""

import pytest; pytestmark = pytest.mark.unit


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
        assert "skill_list" in names
        assert "skill_use" in names
        assert "skill_set" in names
        assert "skill_remove" in names
        assert "config_env_set" in names
        assert "config_env_get" in names
        assert "config_env_remove" in names
        assert "cli_set" in names
        assert "cli_remove" in names
        assert "cli_list" in names

    def test_model_tools_all_discoverable(self):
        """All four model tools auto-discover (regression: 069c954).

        The shared _ModelConfigTool base sets _skip_auto_register = True,
        which is inherited by its subclasses.  Auto-discovery must exclude
        the base itself but still register model_set/model_remove/model_switch;
        otherwise the slife agent can list models but never switch them.
        """
        registry = create_tools_from_config(None)
        names = {t.name for t in registry.list_tools()}
        assert "model_list" in names
        assert "model_set" in names
        assert "model_remove" in names
        assert "model_switch" in names

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
        assert tool.timeout == 45  # type: ignore[attr-defined]

    def test_shell_tool_default_timeout(self):
        """Shell tool uses default timeout when no override given."""
        registry = create_tools_from_config(None)
        tool = registry.get("execute_shell")
        assert tool is not None
        assert tool.timeout == 30  # type: ignore[attr-defined]

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
            {"name": "skill_list", "skills_dir": "custom_skills"},
        ])
        # Overridden tool gets custom dir
        list_tool = registry.get("skill_list")
        assert list_tool is not None
        assert str(list_tool.skills_dir) == "custom_skills"  # type: ignore[attr-defined]
        # Other skill tools use the resolved default (absolute path)
        from slife.paths import get_skills_dir
        skill_tool = registry.get("skill_use")
        assert skill_tool is not None
        assert str(skill_tool.skills_dir) == str(get_skills_dir())  # type: ignore[attr-defined]


class TestRunPythonScriptTool:
    """Tests for RunPythonScriptTool.execute()."""

    @pytest.mark.asyncio
    async def test_execute_runs_script(self):
        """execute() runs a Python one-liner and returns output."""
        from slife.tools.exec import RunPythonScriptTool
        tool = RunPythonScriptTool()
        result = await tool.execute(script="-c print('hello')")
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_execute_missing_script_returns_error(self):
        """execute() returns error info for a failing command."""
        from slife.tools.exec import RunPythonScriptTool
        tool = RunPythonScriptTool()
        result = await tool.execute(script="-c raise SystemExit(1)")
        assert "Error" in result


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

    def test_local_subagent_native_but_mesh_in_plugin(self):
        """Local subagent ops are native (no broker); pure mesh tools are plugin."""
        registry = create_tools_from_config(None, config=None)
        names = {t.name for t in registry.list_tools()}
        # Local subagent worker/lifecycle tools are native and always present.
        assert "spawn_subagent" in names
        assert "list_subagents" in names
        assert "stop_subagent" in names
        assert "subagent_send_task" in names
        assert "subagent_send_task_async" in names
        assert "subagent_get_task_result" in names
        # Pure mesh tools are NOT native — they live in the a2a plugin and
        # only register when the MQTT broker is up.
        assert "a2a_send_task" not in names
        assert "a2a_list_agents" not in names
        assert "a2a_broadcast" not in names
