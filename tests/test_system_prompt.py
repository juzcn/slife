"""Tests for Slife.agent.system_prompt."""

from slife.agent.system_prompt import build


class TestBuild:
    def test_starts_with_slife(self):
        assert build().startswith("You are Slife.")

    def test_platform_guidance(self):
        """Prompt tells LLM how to use platform tools."""
        result = build()
        assert "Platform" in result
        assert "get_os_info" in result
        assert "run_python_script" in result

    def test_tool_categories(self):
        """Prompt describes all tool categories under Tools section."""
        result = build()
        assert "Platform" in result
        assert "Configuration" in result
        assert "Tools" in result
        assert "Skills" in result
        assert "CLI" in result
        assert "MCP" in result
        assert "REST APIs" in result

    def test_config_reference(self):
        assert "slife.json5" in build()

    def test_mcp_reference(self):
        """Prompt mentions anyapi-mcp-server and config_env_set."""
        result = build()
        assert "anyapi-mcp-server" in result
        assert "config_env_set" in result

    def test_no_shell_leak(self):
        """Prompt should not leak shell-specific syntax."""
        result = build()
        assert "cmd.exe" not in result
        assert "bash" not in result
