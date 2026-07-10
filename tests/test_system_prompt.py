"""Tests for slife.agent.system_prompt."""

from slife.agent.system_prompt import build


class TestBuild:
    def test_renders_template(self):
        assert build() == "You are a tool use agent."

    def test_no_platform_leak(self):
        result = build()
        assert "Windows" not in result
        assert "cmd.exe" not in result
        assert "bash" not in result

    def test_no_tool_names(self):
        result = build()
        assert "execute_shell" not in result
        assert "list_skills" not in result

    def test_no_config_reference(self):
        assert "slife.json5" not in build()
