"""Tests for slife.agent.system_prompt."""

from slife.agent.system_prompt import build


class TestBuild:
    def test_renders_template(self):
        assert build().startswith("Your tools come from five sources")

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
        """Prompt mentions anyapi-mcp-server and config_env_set — the
        project-specific conventions the LLM cannot discover from schemas."""
        result = build()
        assert "anyapi-mcp-server" in result
        assert "config_env_set" in result

    def test_five_tool_categories(self):
        """Prompt describes all 5 tool categories and their discovery mechanisms."""
        result = build()
        assert "five sources" in result
        assert "Native functions" in result
        assert "MCP servers" in result
        assert "Skills" in result
        assert "CLI tools" in result
        assert "REST APIs" in result
        # Discovery mechanisms
        assert "list_skills" in result
        assert "use_skill" in result
        assert "cli_list_tools" in result
        assert "cli_add_tool" in result
