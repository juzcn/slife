"""Tests for slife.agent.system_prompt — Jinja2-based builder."""

import sys

from slife.agent.system_prompt import build


class TestBuild:
    """Tests for build()."""

    def test_preserves_base_prompt(self):
        """The base prompt text appears in the rendered output."""
        base = "You are slife, a helpful assistant."
        result = build(base)
        assert base in result

    def test_appends_os_notice(self):
        """The OS notice section is rendered after the base prompt."""
        result = build("Core instructions.")
        assert "SYSTEM ENVIRONMENT" in result

    def test_base_comes_before_notice(self):
        """Base prompt appears before OS notice in output."""
        result = build("AAAA-BASE-MARKER")
        assert result.index("AAAA-BASE-MARKER") < result.index("SYSTEM ENVIRONMENT")

    def test_windows_users_see_cmd_exe(self):
        """On Windows, the notice warns about cmd.exe vs bash."""
        result = build("Base.")
        if sys.platform == "win32":
            assert "cmd.exe" in result
            assert "NOT bash" in result or "NOT ls" in result
        else:
            assert "bash" in result

    def test_empty_base_uses_template_default(self):
        """build('') returns the built-in default prompt from the template."""
        result = build("")
        assert "helpful AI assistant" in result
        assert "SYSTEM ENVIRONMENT" in result
