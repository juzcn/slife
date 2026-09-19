"""Tests for Slife.platform — platform detection and Python script runner."""

import pytest; pytestmark = pytest.mark.unit


import sys
import pytest
from unittest.mock import patch

from slife.platform import (
    build_python_command,
    resolve_command,
    IS_WINDOWS,
    get_os_info,
    detect_current_shell,
    _taskkill_tree_sync,
    assign_to_job_object,
    terminate_process,
    terminate_process_sync,
    _windows_job,
)


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

    @pytest.fixture(autouse=True)
    def _no_real_taskkill(self):
        """On Windows these ladders shell out to ``taskkill`` — never from a
        test.  The patch is autouse so every case here stays hermetic; the
        tree-kill behaviour itself is asserted in :class:`TestTreeKill`."""
        with patch("slife.platform._taskkill_tree_sync") as mock:
            yield mock

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
        proc.stdin = None  # required by _close_pipe_transports in finally
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
        # Called twice: once to signal EOF before terminate,
        # once in _close_pipe_transports for resource cleanup.
        assert proc.stdin.close.call_count == 2

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


# ── tree kill & kill-on-close job (the orphaned-child guarantee) ─────


class TestTreeKill:
    """The terminate ladders take the whole tree, not just the direct child.

    A single-process kill is what left a plugin's own children — the sharefile
    tunnel's cloudflared, the external MCP servers the gateway runs — alive
    after the owner that could have reached them was gone.
    """

    def test_taskkill_argv_kills_the_tree(self):
        """``/T`` is the whole point: the children die with the child."""
        with patch("slife.platform._subprocess.run") as run:
            _taskkill_tree_sync(4242, "sharefile")
        assert run.call_args[0][0] == ["taskkill", "/F", "/T", "/PID", "4242"]

    def test_missing_taskkill_is_not_a_failure(self):
        """A missing taskkill / already-dead pid never propagates."""
        with patch("slife.platform._subprocess.run", side_effect=OSError("gone")):
            _taskkill_tree_sync(4242)  # must not raise

    def test_wedged_taskkill_is_bounded(self):
        """A hung taskkill is bounded by its timeout and swallowed."""
        import subprocess as _sp

        with patch("slife.platform._subprocess.run",
                   side_effect=_sp.TimeoutExpired("taskkill", 1)):
            _taskkill_tree_sync(4242)  # must not raise

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only branch")
    def test_sync_ladder_kills_the_tree(self):
        """The Ctrl+C ``finally`` path goes through taskkill, not terminate()."""
        import asyncio
        from unittest.mock import MagicMock

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        proc.pid = 4242
        with patch("slife.platform._taskkill_tree_sync") as tk:
            terminate_process_sync(proc, label="sharefile")
        tk.assert_called_once_with(4242, "sharefile")
        proc.terminate.assert_not_called()

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows-only branch")
    @pytest.mark.asyncio
    async def test_async_ladder_kills_the_tree(self):
        """The wrapper's stop path goes through taskkill, not terminate()."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        proc = MagicMock(spec=asyncio.subprocess.Process)
        proc.returncode = None
        proc.stdin = None
        proc.pid = 4242
        proc.wait = AsyncMock(return_value=0)
        with patch("slife.platform._taskkill_tree_sync") as tk:
            await terminate_process(proc, label="mcp_wrapper")
        tk.assert_called_once_with(4242, "mcp_wrapper")
        proc.terminate.assert_not_called()


class TestKillOnCloseJob:
    """The Windows job that covers a hard-killed parent.

    taskkill can only run while slife is alive to run it.  A kill-on-close job
    is the kernel's answer for the paths that run no Python at all: Task
    Manager, ``TerminateProcess``, a crash.
    """

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows only")
    def test_job_is_created_with_kill_on_close(self):
        """A created job proves the struct layout was accepted — a wrong
        JOBOBJECT_EXTENDED_LIMIT_INFORMATION fails SetInformationJobObject,
        which returns None here instead of an unusable handle."""
        assert _windows_job() is not None

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows only")
    def test_real_child_is_assigned(self):
        """A live child joins the job (its own children inherit it)."""
        import subprocess as _sp

        proc = _sp.Popen(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            stdout=_sp.DEVNULL, stderr=_sp.DEVNULL,
        )
        try:
            assert assign_to_job_object(proc.pid, label="test-child") is True
        finally:
            proc.kill()
            proc.wait(timeout=10)

    @pytest.mark.skipif(not IS_WINDOWS, reason="Windows only")
    def test_unopenable_pid_is_not_a_failure(self):
        """A pid that cannot be opened returns False rather than raising."""
        assert assign_to_job_object(99_999_999) is False

    def test_non_windows_is_a_noop(self):
        """POSIX has no job object: False, no exception, nothing created."""
        with patch("slife.platform.IS_WINDOWS", False):
            assert _windows_job() is None
            assert assign_to_job_object(1234) is False


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


# ── detect_current_shell ────────────────────────────────────────────────


class TestDetectCurrentShell:
    """Tests for detect_current_shell()."""

    def test_windows_powershell(self, monkeypatch):
        if IS_WINDOWS:
            # PowerShell-launched session: no PROMPT (cmd.exe sets it), but
            # PSModulePath is present.
            monkeypatch.delenv("PROMPT", raising=False)
            monkeypatch.setenv("PSModulePath", r"C:\Modules")
            assert detect_current_shell() == "powershell"

    def test_windows_cmd_launched_with_powershell_installed(self, monkeypatch):
        """Regression: cmd.exe sets PROMPT in the env; a machine with
        PowerShell installed always has PSModulePath — the latter alone must
        not misclassify a cmd.exe session as PowerShell."""
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.setenv("PROMPT", "$P$G")
        monkeypatch.setenv("PSModulePath", r"C:\Modules")
        assert detect_current_shell() == "cmd"

    def test_windows_cmd_fallback(self, monkeypatch):
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.delenv("PSModulePath", raising=False)
        monkeypatch.delenv("PROMPT", raising=False)
        assert detect_current_shell() == "cmd"

    def test_posix_from_env(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        monkeypatch.setenv("SHELL", "/bin/zsh")
        assert detect_current_shell() == "/bin/zsh"

    def test_posix_default(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        monkeypatch.delenv("SHELL", raising=False)
        assert detect_current_shell() == "sh"
