"""Tests for Slife.platform — platform detection and Python script runner."""

import sys
import pytest
from unittest.mock import patch

from slife.platform import (
    build_python_command,
    resolve_command,
    IS_WINDOWS,
    get_os_info,
)
from slife.tools.system import CheckOsInfoTool


# ── build_python_command ───────────────────────────────────────────────


class TestBuildPythonCommand:
    """Tests for build_python_command."""

    def test_no_json_args(self):
        """Script without JSON args (no braces/brackets)."""
        result = build_python_command("script.py")
        assert "script.py" in result
        assert "python" in result
        assert "{" not in result

    def test_with_json_braces(self):
        """Script with JSON args in braces."""
        result = build_python_command('script.py {"key": "value"}')
        assert "script.py" in result
        assert "key" in result
        assert "value" in result

    def test_with_json_brackets(self):
        """Script with JSON args in brackets."""
        result = build_python_command("script.py [1, 2, 3]")
        assert "script.py" in result

    def test_empty_string(self):
        result = build_python_command("")
        assert "python" in result

    def test_cmd_normalization(self):
        """Direct command (not script path) works."""
        result = build_python_command("echo hello")
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


class TestCheckOsInfoTool:
    """Tests for the standalone CheckOsInfoTool."""

    @pytest.mark.asyncio
    async def test_execute_returns_os_name(self):
        """Tool returns JSON with OS name."""
        tool = CheckOsInfoTool()
        result = await tool.execute()
        import json
        data = json.loads(result)
        assert len(data) >= 1
        assert data[0]["key"] == "system"
        assert data[0]["value"] in ("Windows", "Linux", "Darwin")


class TestRunPythonScriptTool:
    """Tests for the standalone RunPythonScriptTool."""

    @pytest.mark.asyncio
    async def test_execute_runs_script(self):
        """Tool executes a simple Python one-liner and returns output."""
        from slife.tools.exec import RunPythonScriptTool
        tool = RunPythonScriptTool()
        result = await tool.execute(script="-c print('hello')")
        assert "hello" in result


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
    def test_windows_unresolvable_falls_back(self, _mock_which):
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
        cmd = build_python_command('myscript.py {"k":[1,2]}')
        assert "myscript.py" in cmd
        assert "{" in cmd

    def test_windows_uses_utf8_flag(self):
        if IS_WINDOWS:
            cmd = build_python_command('script.py {"a":1}')
            assert "-X utf8" in cmd
            assert '\\"a\\":1' in cmd

    def test_non_windows_uses_single_quotes(self):
        if not IS_WINDOWS:
            cmd = build_python_command('script.py {"a":1}')
            assert "'" in cmd

    def test_whitespace_in_script_path(self):
        cmd = build_python_command("  my script.py  ")
        assert "my script.py" in cmd


# ── terminate_process ────────────────────────────────────────────────


class TestTerminateProcess:
    """Tests for terminate_process async function."""

    @pytest.mark.asyncio
    async def test_none_process_noop(self):
        """Terminating None is a no-op."""
        from slife.platform import terminate_process
        await terminate_process(None, label="test")  # type: ignore[arg-type]
        # Should not raise

    @pytest.mark.asyncio
    async def test_already_exited_noop(self):
        """Process with returncode set needs no termination."""
        import asyncio
        from unittest.mock import MagicMock
        from slife.platform import terminate_process

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = 0
        await terminate_process(proc, label="test")
        proc.terminate.assert_not_called()
        proc.kill.assert_not_called()

    @pytest.mark.asyncio
    async def test_closes_stdin(self):
        """Stdin is closed to signal the process."""
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        from slife.platform import terminate_process

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        await terminate_process(proc, label="test")
        proc.stdin.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_lookup_error_swallowed(self):
        """ProcessLookupError (process already gone) is swallowed."""
        import asyncio
        from unittest.mock import MagicMock
        from slife.platform import terminate_process

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        proc.stdin = None
        proc.terminate = MagicMock(side_effect=ProcessLookupError)

        if IS_WINDOWS:
            await terminate_process(proc, label="test")
            # Should not raise

    @pytest.mark.asyncio
    async def test_stdin_close_error_swallowed(self):
        """Errors closing stdin are swallowed gracefully."""
        import asyncio
        from unittest.mock import MagicMock, AsyncMock
        from slife.platform import terminate_process

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        proc.stdin = MagicMock()
        proc.stdin.close = MagicMock(side_effect=OSError("pipe broken"))
        proc.wait = AsyncMock(return_value=0)

        await terminate_process(proc, label="test")
        # Should not raise

    @pytest.mark.asyncio
    async def test_general_exception_swallowed(self):
        """General exceptions during termination are swallowed."""
        import asyncio
        from unittest.mock import MagicMock
        from slife.platform import terminate_process

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        proc.stdin = None
        proc.terminate = MagicMock(side_effect=RuntimeError("unexpected"))

        if IS_WINDOWS:
            await terminate_process(proc, label="test")
            # Should not raise — RuntimeError is caught


# ── resolve_command — Windows-specific ───────────────────────────────


class TestResolveCommandWindows:
    """Windows-specific resolve_command tests."""

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows only")
    @patch("shutil.which")
    def test_finds_exe(self, mock_which):
        mock_which.side_effect = lambda c: (
            r"C:\tools\git.exe" if c in ("git", "git.exe") else None
        )
        result = resolve_command("git")
        assert "git.exe" in result or "git" in result

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows only")
    @patch("shutil.which")
    def test_finds_cmd_fallback(self, mock_which):
        mock_which.side_effect = lambda c: (
            r"C:\tools\npm.cmd" if c in ("npm.cmd",) else None
        )
        result = resolve_command("npm")
        assert "npm" in result.lower()

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows only")
    @patch("shutil.which", return_value=None)
    def test_unresolvable_returns_original(self, _mock_which):
        result = resolve_command("nonexistent_cmd")
        assert result == "nonexistent_cmd"


# ── run_python_script cross-platform ─────────────────────────────────


class TestRunPythonScript:
    """Tests for run_python_script quoting on different platforms."""

    def test_unix_quoting(self):
        """Non-Windows uses single-quote wrapping for JSON args."""
        import sys
        from slife.platform import build_python_command
        with patch("slife.platform.IS_WINDOWS", False):
            # input_str format: "<script_path> <json_args>"
            result = build_python_command('/tmp/script.py {"key": "val"}')
            assert "'" in result
            assert sys.executable in result


# ── terminate_process force-kill ──────────────────────────────────────


class TestTerminateProcessForceKill:
    """Tests for terminate_process force-kill escalation."""

    @pytest.mark.asyncio
    async def test_force_kill_after_timeout(self):
        """After terminate times out, kill is called."""
        import asyncio
        from unittest.mock import MagicMock
        from slife.platform import terminate_process
        mock_proc = MagicMock()
        mock_proc.returncode = None
        # terminate succeeds
        mock_proc.terminate.return_value = None
        # wait raises TimeoutError twice
        mock_proc.wait = MagicMock(side_effect=[asyncio.TimeoutError(), asyncio.TimeoutError()])

        with patch("slife.platform.IS_WINDOWS", True):
            await terminate_process(mock_proc, label="test_kill")

        mock_proc.kill.assert_called()


# ── desktop_notify ────────────────────────────────────────────────────


class TestDesktopNotify:
    """Tests for desktop_notify."""

    @patch("subprocess.run")
    @patch("slife.platform._platform.system", return_value="Windows")
    def test_windows_notification(self, _mock_system, mock_run):
        from slife.platform import desktop_notify
        desktop_notify("Test", "Hello World")
        mock_run.assert_called_once()
        assert "powershell" in mock_run.call_args[0][0][0]

    @patch("subprocess.run")
    @patch("slife.platform._platform.system", return_value="Darwin")
    def test_macos_notification(self, _mock_system, mock_run):
        from slife.platform import desktop_notify
        desktop_notify("Test", "Hello")
        mock_run.assert_called_once()
        assert "osascript" in mock_run.call_args[0][0][0]

    @patch("subprocess.run")
    @patch("slife.platform._platform.system", return_value="Linux")
    def test_linux_notification(self, _mock_system, mock_run):
        from slife.platform import desktop_notify
        desktop_notify("Test", "Hello")
        mock_run.assert_called_once()

    @patch("subprocess.run", side_effect=Exception("notify failed"))
    @patch("slife.platform._platform.system", return_value="Windows")
    def test_notification_exception_swallowed(self, _mock_system, _mock_run):
        from slife.platform import desktop_notify
        # Should not raise
        desktop_notify("Test", "Hello")
