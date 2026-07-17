"""Tests for Slife.mcp.process — MCP wrapper process lifecycle."""

import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from slife.mcp.process import MCPWrapperProcess


class TestMCPWrapperProcessInit:
    """Tests for MCPWrapperProcess.__init__."""

    def test_default_command_is_sys_executable(self):
        wp = MCPWrapperProcess()
        assert wp._command == sys.executable

    def test_default_args_use_server_module(self):
        wp = MCPWrapperProcess()
        assert wp._args == ["-m", "slife.plugins.mcp.server"]

    def test_custom_command(self):
        wp = MCPWrapperProcess(command="/usr/bin/python3")
        assert wp._command == "/usr/bin/python3"

    def test_custom_args(self):
        wp = MCPWrapperProcess(args=["-c", "print('hi')"])
        assert wp._args == ["-c", "print('hi')"]

    def test_custom_server_module(self):
        wp = MCPWrapperProcess(server_module="custom.server")
        assert wp._args == ["-m", "custom.server"]

    def test_custom_command_overrides_server_module(self):
        wp = MCPWrapperProcess(command="python3", server_module="custom.server")
        assert wp._command == "python3"
        assert wp._args == ["-m", "custom.server"]

    def test_initial_state_not_running(self):
        wp = MCPWrapperProcess()
        assert wp._running is False
        assert wp._process is None

    def test_custom_args_overrides_server_module(self):
        wp = MCPWrapperProcess(
            args=["-m", "other.module"], server_module="custom.server",
        )
        assert wp._args == ["-m", "other.module"]


class TestMCPWrapperProcessProperties:
    """Tests for MCPWrapperProcess properties."""

    def test_is_running_false_initially(self):
        wp = MCPWrapperProcess()
        assert wp.is_running is False

    def test_is_running_true_when_set(self):
        wp = MCPWrapperProcess()
        wp._running = True
        wp._process = MagicMock(spec=asyncio.subprocess.Process)
        assert wp.is_running is True

    def test_is_running_false_when_running_but_no_process(self):
        wp = MCPWrapperProcess()
        wp._running = True
        wp._process = None
        assert wp.is_running is False

    def test_pid_none_when_no_process(self):
        wp = MCPWrapperProcess()
        assert wp.pid is None

    def test_pid_returns_process_pid(self):
        wp = MCPWrapperProcess()
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.pid = 12345
        wp._process = mock_proc
        assert wp.pid == 12345


class TestMCPWrapperProcessStart:
    """Tests for MCPWrapperProcess.start()."""

    @pytest.mark.asyncio
    async def test_already_running_logs_warning(self):
        wp = MCPWrapperProcess()
        wp._running = True
        wp._process = MagicMock(spec=asyncio.subprocess.Process)
        wp._process.pid = 999

        with patch("slife.mcp.process.logger") as mock_logger:
            await wp.start()
            mock_logger.warning.assert_called_once()
            assert "wrapper_already_running" in mock_logger.warning.call_args[0][0]

    @pytest.mark.asyncio
    async def test_start_creates_subprocess(self):
        wp = MCPWrapperProcess()
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.pid = 100
        mock_proc.stderr = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            with patch("slife.mcp.process.logger"):
                with patch("slife.mcp.process.asyncio.create_task"):
                    await wp.start()

            assert wp._running is True
            assert wp._process is mock_proc

    @pytest.mark.asyncio
    async def test_start_passes_env_vars(self):
        wp = MCPWrapperProcess()
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.pid = 100
        mock_proc.stderr = MagicMock()

        with patch("asyncio.create_subprocess_exec") as mock_create:
            mock_create.return_value = mock_proc
            with patch("slife.mcp.process.logger"):
                with patch("slife.mcp.process.get_session_id",
                           return_value="test-sid-1234"):
                    with patch("slife.mcp.process.asyncio.create_task"):
                        await wp.start()

            call_kwargs = mock_create.call_args[1]
            assert "env" in call_kwargs
            assert call_kwargs["env"]["SLIFE_SESSION_ID"] == "test-sid-1234"

    @pytest.mark.asyncio
    async def test_start_file_not_found_error(self):
        wp = MCPWrapperProcess()
        with patch("asyncio.create_subprocess_exec",
                   side_effect=FileNotFoundError("not found")):
            with patch("slife.mcp.process.logger"):
                with pytest.raises(FileNotFoundError):
                    await wp.start()
                assert wp._running is False

    @pytest.mark.asyncio
    async def test_start_general_exception(self):
        wp = MCPWrapperProcess()
        with patch("asyncio.create_subprocess_exec",
                   side_effect=OSError("something broke")):
            with patch("slife.mcp.process.logger"):
                with pytest.raises(OSError):
                    await wp.start()
                assert wp._running is False


class TestMCPWrapperProcessCreateClient:
    """Tests for MCPWrapperProcess.create_client()."""

    @pytest.mark.asyncio
    async def test_raises_when_not_running(self):
        wp = MCPWrapperProcess()
        with pytest.raises(RuntimeError, match="not running"):
            await wp.create_client()

    @pytest.mark.asyncio
    async def test_raises_when_process_exited(self):
        wp = MCPWrapperProcess()
        wp._running = True
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.pid = 42
        mock_proc.returncode = 1  # Exited with error
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"crash info\n")
        wp._process = mock_proc

        with pytest.raises(RuntimeError, match="MCP child process.*exited"):
            await wp.create_client()

    @pytest.mark.asyncio
    async def test_creates_client_successfully(self):
        wp = MCPWrapperProcess()
        wp._running = True
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.pid = 42
        mock_proc.returncode = None  # Still running
        mock_proc.stdout = MagicMock()
        mock_proc.stdin = MagicMock()
        wp._process = mock_proc

        # MCPClient is imported inside create_client() via:
        #   from slife.mcp.client import MCPClient
        with patch("slife.mcp.client.MCPClient") as MockClient:
            mock_client = MagicMock()
            mock_client.connect_streams = AsyncMock()
            MockClient.return_value = mock_client

            result = await wp.create_client()
            assert result is mock_client
            mock_client.connect_streams.assert_awaited_once_with(
                read_stream=mock_proc.stdout,
                write_stream=mock_proc.stdin,
            )

    @pytest.mark.asyncio
    async def test_stderr_read_error_is_handled(self):
        wp = MCPWrapperProcess()
        wp._running = True
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.pid = 42
        mock_proc.returncode = 1
        mock_proc.stderr = MagicMock()
        mock_proc.stderr.read = AsyncMock(side_effect=Exception("read failed"))
        wp._process = mock_proc

        with pytest.raises(RuntimeError):
            await wp.create_client()


class TestMCPWrapperProcessStop:
    """Tests for MCPWrapperProcess.stop()."""

    @pytest.mark.asyncio
    async def test_stop_not_running_noop(self):
        wp = MCPWrapperProcess()
        with patch("slife.mcp.process.terminate_process") as mock_term:
            await wp.stop()
            mock_term.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_no_process_noop(self):
        wp = MCPWrapperProcess()
        wp._running = False
        wp._process = None
        with patch("slife.mcp.process.terminate_process") as mock_term:
            await wp.stop()
            mock_term.assert_not_called()

    @pytest.mark.asyncio
    async def test_stop_calls_terminate(self):
        wp = MCPWrapperProcess()
        wp._running = True
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.pid = 1234
        wp._process = mock_proc

        with patch("slife.mcp.process.terminate_process") as mock_term:
            mock_term.return_value = AsyncMock()()
            await wp.stop()

            mock_term.assert_called_once_with(
                mock_proc, graceful_timeout=5.0, label="mcp_wrapper",
            )
            assert wp._running is False
            assert wp._process is None


class TestMCPWrapperProcessLogStderr:
    """Tests for MCPWrapperProcess._log_stderr()."""

    @pytest.mark.asyncio
    async def test_log_stderr_relays_non_banner_lines(self):
        """Regular stderr lines are relayed as debug log messages."""
        wp = MCPWrapperProcess()
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.stderr = MagicMock()
        wp._process = mock_proc

        # read_stderr_lines is imported inside _log_stderr via:
        #   from slife.logfmt import read_stderr_lines
        with patch("slife.logfmt.read_stderr_lines") as mock_read:
            async def _gen():
                yield "error: something went wrong"

            mock_read.return_value = _gen()
            with patch("slife.mcp.process.logger") as mock_logger:
                await wp._log_stderr()
                mock_logger.debug.assert_called()

    @pytest.mark.asyncio
    async def test_log_stderr_suppresses_banners(self):
        wp = MCPWrapperProcess()
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.stderr = MagicMock()
        wp._process = mock_proc

        with patch("slife.logfmt.read_stderr_lines") as mock_read:
            async def _gen():
                yield "╭── FastMCP ──────"
                yield "│    Serving on http://localhost:8000     │"
                yield "╰─────────"

            mock_read.return_value = _gen()
            with patch("slife.mcp.process.logger") as mock_logger:
                await wp._log_stderr()
                mock_logger.debug.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_stderr_suppresses_subprocess_log_lines(self):
        wp = MCPWrapperProcess()
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.stderr = MagicMock()
        wp._process = mock_proc

        with patch("slife.logfmt.read_stderr_lines") as mock_read:
            async def _gen():
                yield "12:34:56 [INFO] slife_mcp some log message"
                yield "12:34:57 [WARNING] slife_mcp another log"

            mock_read.return_value = _gen()
            with patch("slife.mcp.process.logger") as mock_logger:
                await wp._log_stderr()
                mock_logger.debug.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_stderr_relays_traceback_lines(self):
        """Traceback lines that don't match the logger pattern should be relayed."""
        wp = MCPWrapperProcess()
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.stderr = MagicMock()
        wp._process = mock_proc

        with patch("slife.logfmt.read_stderr_lines") as mock_read:
            async def _gen():
                yield '  File "/some/path.py", line 42, in <module>'
                yield '    raise ValueError("boom")'
                yield "ValueError: boom"

            mock_read.return_value = _gen()
            with patch("slife.mcp.process.logger") as mock_logger:
                await wp._log_stderr()
                assert mock_logger.debug.call_count == 3

    @pytest.mark.asyncio
    async def test_log_stderr_empty_lines_skipped(self):
        wp = MCPWrapperProcess()
        mock_proc = MagicMock(spec=asyncio.subprocess.Process)
        mock_proc.stderr = MagicMock()
        wp._process = mock_proc

        with patch("slife.logfmt.read_stderr_lines") as mock_read:
            async def _gen():
                yield ""
                yield "  "

            mock_read.return_value = _gen()
            with patch("slife.mcp.process.logger") as mock_logger:
                await wp._log_stderr()
                mock_logger.debug.assert_not_called()
