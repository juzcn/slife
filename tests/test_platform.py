"""Tests for Slife.platform — platform detection and Python script runner."""

import sys
import pytest
from unittest.mock import patch

from slife.platform import (
    run_python_script,
    resolve_command,
    IS_WINDOWS,
    get_os_info,
)


# ── run_python_script ──────────────────────────────────────────────────


class TestRunPythonScript:
    """Tests for run_python_script."""

    def test_no_json_args(self):
        """Script without JSON args (no braces/brackets)."""
        result = run_python_script("script.py")
        assert "script.py" in result
        assert "python" in result
        assert "{" not in result

    def test_with_json_braces(self):
        """Script with JSON args in braces."""
        result = run_python_script('script.py {"key": "value"}')
        assert "script.py" in result
        assert "key" in result
        assert "value" in result

    def test_with_json_brackets(self):
        """Script with JSON args in brackets."""
        result = run_python_script("script.py [1, 2, 3]")
        assert "script.py" in result

    def test_empty_string(self):
        result = run_python_script("")
        assert "python" in result

    def test_cmd_normalization(self):
        """Direct command (not script path) works."""
        result = run_python_script("echo hello")
        assert "echo hello" in result


# ── Platform detection ──────────────────────────────────────────────────


class TestPlatformDetection:
    """Tests for IS_WINDOWS flag and get_os_info."""

    def test_is_windows_matches_sys_platform(self):
        """IS_WINDOWS matches sys.platform == 'win32'."""
        assert IS_WINDOWS == (sys.platform == "win32")

    def test_get_os_info_returns_known_os(self):
        """get_os_info returns one of the expected OS names."""
        os_name = get_os_info()
        assert os_name in ("Windows", "Linux", "macOS", "FreeBSD", "OpenBSD", "NetBSD", "SunOS")

    def test_get_os_info_matches_platform_system(self):
        """get_os_info derives from platform.system()."""
        import platform as _platform
        system = _platform.system()
        os_name = get_os_info()
        if system == "Darwin":
            assert os_name == "macOS"
        elif system == "Windows":
            assert os_name == "Windows"
        elif system == "Linux":
            assert os_name == "Linux"
        else:
            assert os_name == system


class TestGetOsInfoTool:
    """Tests for the standalone GetOsInfoTool."""

    @pytest.mark.asyncio
    async def test_execute_returns_os_name(self):
        """Tool returns a known OS name."""
        from slife.tools.os_info import GetOsInfoTool
        tool = GetOsInfoTool()
        result = await tool.execute()
        assert result in ("Windows", "Linux", "macOS")


class TestRunPythonScriptTool:
    """Tests for the standalone RunPythonScriptTool."""

    @pytest.mark.asyncio
    async def test_execute_returns_command(self):
        """Tool returns a command containing python and the script name."""
        from slife.tools.run_python_script import RunPythonScriptTool
        tool = RunPythonScriptTool()
        result = await tool.execute(script="myscript.py {}")
        assert "python" in result
        assert "myscript.py" in result


# ── resolve_command ─────────────────────────────────────────────────────


class TestResolveCommand:
    """Tests for resolve_command."""

    def test_non_windows_returns_as_is(self):
        if not IS_WINDOWS:
            assert resolve_command("python3") == "python3"
            assert resolve_command("mycmd") == "mycmd"

    def test_windows_with_exe_already(self):
        if IS_WINDOWS:
            result = resolve_command("cmd.exe")
            # Already has .exe, should just use it
            assert "cmd" in result.lower()

    def test_windows_with_cmd_already(self):
        if IS_WINDOWS:
            result = resolve_command("npm.cmd")
            assert "npm" in result.lower()

    @patch("shutil.which", return_value=None)
    def test_windows_unresolvable_falls_back(self, mock_which):
        if IS_WINDOWS:
            result = resolve_command("nonexistent_xyzzy")
            assert result == "nonexistent_xyzzy"


# ── get_os_info — mocked ────────────────────────────────────────────────


class TestGetOsInfoMocked:
    """Tests for get_os_info with mocked platform.system."""

    @patch("platform.system", return_value="Darwin")
    def test_macos_mocked(self, _mock):
        assert get_os_info() == "macOS"

    @patch("platform.system", return_value="Windows")
    def test_windows_mocked(self, _mock):
        assert get_os_info() == "Windows"

    @patch("platform.system", return_value="Linux")
    def test_linux_mocked(self, _mock):
        assert get_os_info() == "Linux"

    @patch("platform.system", return_value="FreeBSD")
    def test_other_fallback_mocked(self, _mock):
        assert get_os_info() == "FreeBSD"


# ── run_python_script — edge cases ──────────────────────────────────────


class TestRunPythonScriptEdgeCases:
    """Edge cases for run_python_script."""

    def test_script_with_braces_first_not_bracket(self):
        """Split happens at the first { even if [ appears later."""
        cmd = run_python_script('myscript.py {"k":[1,2]}')
        assert "myscript.py" in cmd
        assert "{" in cmd

    def test_windows_uses_chcp_and_utf8(self):
        if IS_WINDOWS:
            cmd = run_python_script('script.py {"a":1}')
            assert "chcp 65001" in cmd
            assert "-X utf8" in cmd

    def test_non_windows_uses_single_quotes(self):
        if not IS_WINDOWS:
            cmd = run_python_script('script.py {"a":1}')
            assert "'" in cmd

    def test_whitespace_in_script_path(self):
        cmd = run_python_script("  my script.py  ")
        assert "my script.py" in cmd
