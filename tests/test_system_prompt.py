"""Tests for slife.agent.system_prompt."""

from slife.agent.system_prompt import build


class TestBuild:
    def test_renders_template(self):
        assert build().startswith("Use list_skills")

    def test_no_platform_leak(self):
        result = build()
        assert "Windows" not in result
        assert "cmd.exe" not in result
        assert "bash" not in result

    def test_no_tool_names(self):
        result = build()
        assert "execute_shell" not in result
        assert "get_shell_command" not in result

    def test_config_reference(self):
        assert "slife.json5" in build()
        assert "env:" in build()

    def test_mcp_reference(self):
        """Prompt mentions MCP management tools — project-specific knowledge."""
        result = build()
        assert "mcp_add_server" in result
        assert "mcp_list_tools" in result
        assert "mcp_call_tool" in result
