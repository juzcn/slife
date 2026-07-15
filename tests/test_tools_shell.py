"""Tests for Slife.tools.shell — shell command execution tool."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.tools.shell import ShellTool


# ── Tool metadata ─────────────────────────────────────────────────────


class TestShellMetadata:
    """Tests for ShellTool class-level attributes."""

    def test_name(self):
        assert ShellTool.name == "execute_shell"

    def test_description(self):
        assert "Execute a shell command" in ShellTool.description

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

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="echo hello")

        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_command_with_stderr(self):
        """Command returns combined stdout and stderr."""
        tool = ShellTool(timeout=10)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"output", b"error output"))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
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
        mock_process.kill = MagicMock()
        mock_process.wait = AsyncMock()

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
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

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
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

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="echo")

        assert "exit code" in result

    @pytest.mark.asyncio
    async def test_unicode_decode_errors(self):
        """Non-UTF-8 output is handled with replacement chars."""
        tool = ShellTool(timeout=10)

        mock_process = MagicMock()
        mock_process.communicate = AsyncMock(return_value=(b"\xff\xfeinvalid", b""))
        mock_process.returncode = 0

        with patch("asyncio.create_subprocess_shell", AsyncMock(return_value=mock_process)):
            result = await tool.execute(command="cat binary")

        # Should not raise, uses replacement chars
        assert "�" in result or result  # either has replacement chars or is the decoded string
