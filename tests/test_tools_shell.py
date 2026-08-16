"""Tests for Slife.tools.shell — shell command execution tool."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import base64
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.tools.exec import ShellTool, _shell_argv, _shell_output_codec


# ── Tool metadata ─────────────────────────────────────────────────────


class TestShellMetadata:
    """Tests for ShellTool class-level attributes."""

    def test_name(self):
        assert ShellTool.name == "execute_shell"

    def test_description(self):
        assert "Run a shell command" in ShellTool.description

    def test_parameters(self):
        params = ShellTool.parameters
        assert params["type"] == "object"
        assert "command" in params["properties"]
        assert "command" in params["required"]


# ── Construction ─────────────────────────────────────────────────────


class TestShellConstruction:
    """Tests for ShellTool.__init__."""

    def test_default_timeout(self):
        tool = ShellTool()
        assert tool.timeout == 30

    def test_custom_timeout(self):
        tool = ShellTool(timeout=60)
        assert tool.timeout == 60


# ── execute ───────────────────────────────────────────────────────────


class TestShellExecute:
    """Tests for ShellTool.execute."""

    @pytest.mark.asyncio
    async def test_successful_command(self):
        """Command runs and returns stdout."""
        tool = ShellTool(timeout=10)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"hello world", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="echo hello")

        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_runs_detected_shell_argv(self):
        """execute spawns the detected shell (not COMSPEC=cmd.exe) — so a
        powershell-detected Windows runs ``powershell …``, and the argv is
        passed through create_subprocess_exec."""
        tool = ShellTool(timeout=10)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"ok", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)) as m:
            await tool.execute(command="Get-Date")

        args = m.call_args.args
        assert args[0] == _shell_argv("Get-Date")[0]  # same shell the prompt claims

    @pytest.mark.asyncio
    async def test_command_with_stderr(self):
        """Command returns combined stdout and stderr."""
        tool = ShellTool(timeout=10)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b"error output"))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="some-command")

        assert "output" in result
        assert "[stderr]" in result
        assert "error output" in result

    @pytest.mark.asyncio
    async def test_command_timeout(self):
        """Command times out."""
        tool = ShellTool(timeout=1)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
        mock_process.returncode = None  # still running when the timeout fires
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="sleep 100")

        assert "timed out" in result
        assert "1s" in result
        mock_process.kill.assert_called_once()
        mock_process.wait.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_empty_output(self):
        """Commands with no output return exit code info."""
        tool = ShellTool(timeout=10)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="true")

        assert "exit code" in result
        assert "no output" in result

    @pytest.mark.asyncio
    async def test_empty_output_with_whitespace(self):
        """Whitespace-only output is treated as empty."""
        tool = ShellTool(timeout=10)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"   \n  ", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="echo")

        assert "exit code" in result

    @pytest.mark.asyncio
    async def test_unicode_decode_errors(self):
        """Non-decodable output is handled with replacement chars."""
        tool = ShellTool(timeout=10)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"\xff\xfeinvalid", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_exec", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="cat binary")

        # Invalid bytes decode with U+FFFD replacement and the trailing valid
        # "invalid" survives — decode must not raise and must return a string.
        assert isinstance(result, str)
        assert "invalid" in result


# ── Shell selection + output codec helpers ────────────────────────────────


class TestShellArgv:
    """Tests for _shell_argv — the tool runs the shell the prompt claims."""

    def test_windows_powershell_uses_encoded_command(self, monkeypatch):
        """Detected powershell → powershell -EncodedCommand (quote-proof)."""
        monkeypatch.setattr("os.name", "nt")
        with patch(
            "slife.platform.detect_current_shell", return_value="powershell",
        ) as mock_detect:
            argv = _shell_argv("Get-Date")
        mock_detect.assert_called_once_with()
        assert argv[0] == "powershell"
        assert "-EncodedCommand" in argv
        encoded = argv[argv.index("-EncodedCommand") + 1]
        script = base64.b64decode(encoded).decode("utf-16-le")
        # ProgressPreference is prepended so PS's module-load progress record
        # doesn't pollute stderr as CLIXML; the user's command follows.
        assert script.endswith("Get-Date")
        assert "SilentlyContinue" in script

    def test_windows_cmd_uses_cmd_c(self, monkeypatch):
        """Detected cmd → cmd /c."""
        monkeypatch.setattr("os.name", "nt")
        with patch(
            "slife.platform.detect_current_shell", return_value="cmd",
        ):
            argv = _shell_argv("dir")
        assert argv == ["cmd", "/c", "dir"]

    def test_posix_uses_shell(self, monkeypatch):
        """POSIX (incl. WSL) → $SHELL -c, same value the prompt reports."""
        monkeypatch.setattr("os.name", "posix")
        monkeypatch.setenv("SHELL", "/bin/bash")
        assert _shell_argv("ls -la") == ["/bin/bash", "-c", "ls -la"]

    def test_posix_falls_back_to_sh(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        monkeypatch.delenv("SHELL", raising=False)
        assert _shell_argv("ls") == ["/bin/sh", "-c", "ls"]


class TestShellOutputCodec:
    """Tests for _shell_output_codec — GBK/cp936 on zh-CN Windows, UTF-8 on POSIX."""

    def test_windows_uses_locale_codec(self, monkeypatch):
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.setattr("locale.getpreferredencoding", lambda _: "cp936")
        assert _shell_output_codec() == "cp936"

    def test_posix_uses_utf8(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        assert _shell_output_codec() == "utf-8"
