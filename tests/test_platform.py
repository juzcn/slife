"""Tests for slife.platform — platform-aware shell command generation."""

import sys
import pytest
from unittest.mock import patch

from slife.platform import (
    get_shell_command,
    _run_script_cmd,
    _check_env_cmd,
    _install_cmd,
    IS_WINDOWS,
)


# ── get_shell_command ──────────────────────────────────────────────────


class TestGetShellCommand:
    """Tests for get_shell_command main entry point."""

    def test_no_args_returns_fallback(self):
        result = get_shell_command()
        assert result == "No action specified."

    def test_all_none_returns_fallback(self):
        result = get_shell_command(run_script=None, check_env=None, install=None)
        assert result == "No action specified."

    def test_run_script(self):
        result = get_shell_command(run_script="myscript.py {}")
        assert "python" in result
        assert "myscript.py" in result

    def test_check_env(self):
        result = get_shell_command(check_env="MY_VAR")
        assert "MY_VAR" in result

    def test_install(self):
        result = get_shell_command(install="requests")
        assert "uv pip install requests" in result

    def test_multiple_actions(self):
        result = get_shell_command(
            check_env="PATH",
            install="pytest",
        )
        lines = result.split("\n")
        assert len(lines) == 2
        assert "PATH" in lines[0]
        assert "uv pip install" in lines[1]

    def test_all_three_actions(self):
        result = get_shell_command(
            run_script="s.py {}",
            check_env="X",
            install="pkg",
        )
        lines = result.split("\n")
        assert len(lines) == 3


# ── _run_script_cmd ────────────────────────────────────────────────────


class TestRunScriptCmd:
    """Tests for _run_script_cmd helper."""

    def test_no_json_args(self):
        """Script without JSON args (no braces/brackets)."""
        result = _run_script_cmd("script.py")
        assert "script.py" in result
        assert "python" in result
        assert "{" not in result  # No JSON arg quoting

    def test_with_json_braces(self):
        """Script with JSON args in braces."""
        result = _run_script_cmd('script.py {"key": "value"}')
        assert "script.py" in result
        # On Windows, quotes are escaped; on Unix, single-quoted
        assert "key" in result
        assert "value" in result

    def test_with_json_brackets(self):
        """Script with JSON args in brackets."""
        result = _run_script_cmd("script.py [1, 2, 3]")
        assert "script.py" in result

    def test_empty_string(self):
        result = _run_script_cmd("")
        assert "python" in result

    def test_cmd_normalization(self):
        """Direct command (not script path) works."""
        result = _run_script_cmd("echo hello")
        assert "echo hello" in result


# ── _check_env_cmd ─────────────────────────────────────────────────────


class TestCheckEnvCmd:
    """Tests for _check_env_cmd helper."""

    def test_unix_style(self):
        if not IS_WINDOWS:
            result = _check_env_cmd("MY_VAR")
            assert result == "echo $MY_VAR"

    def test_format_has_env_name(self):
        result = _check_env_cmd("SOME_VAR")
        assert "SOME_VAR" in result


# ── _install_cmd ───────────────────────────────────────────────────────


class TestInstallCmd:
    """Tests for _install_cmd helper."""

    def test_install(self):
        result = _install_cmd("requests")
        assert result == "uv pip install requests"

    def test_install_with_version(self):
        result = _install_cmd("pytest>=7")
        assert result == "uv pip install pytest>=7"


# ── Platform detection ──────────────────────────────────────────────────


class TestPlatformDetection:
    """Tests for IS_WINDOWS flag."""

    def test_is_windows_matches_sys_platform(self):
        """IS_WINDOWS matches sys.platform == 'win32'."""
        assert IS_WINDOWS == (sys.platform == "win32")
