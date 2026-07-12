"""Tests for slife.platform — platform detection and Python script runner."""

import sys
import pytest

from slife.platform import (
    run_python_script,
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
