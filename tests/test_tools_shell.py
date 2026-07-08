"""Tests for ShellTool (slife.tools.shell)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.tools.shell import ShellTool


# ══════════════════════════════════════════════════════════════════════
# Tool Metadata
# ══════════════════════════════════════════════════════════════════════


class TestShellMetadata:
    """Tests for class-level metadata."""

    def test_name(self):
        """Tool name is 'execute_shell'."""
        assert ShellTool.name == "execute_shell"

    def test_description(self):
        """Tool has a non-empty description."""
        assert len(ShellTool.description) > 10
        assert "shell" in ShellTool.description.lower()

    def test_parameters_schema(self):
        """Parameters define a 'command' string parameter."""
        params = ShellTool.parameters
        assert params["type"] == "object"
        assert "command" in params["properties"]
        assert params["properties"]["command"]["type"] == "string"
        assert "command" in params["required"]

    def test_to_openai_function(self):
        """to_openai_function() returns correct format."""
        func = ShellTool.to_openai_function()
        assert func["type"] == "function"
        assert func["function"]["name"] == "execute_shell"


# ══════════════════════════════════════════════════════════════════════
# Initialization
# ══════════════════════════════════════════════════════════════════════


class TestShellInit:
    """Tests for ShellTool.__init__()."""

    def test_default_timeout(self):
        """Default timeout is 30 seconds."""
        tool = ShellTool()
        assert tool.timeout == 30

    def test_custom_timeout(self):
        """Custom timeout is stored."""
        tool = ShellTool(timeout=60)
        assert tool.timeout == 60

    def test_zero_timeout(self):
        """Zero timeout is accepted."""
        tool = ShellTool(timeout=0)
        assert tool.timeout == 0


# ══════════════════════════════════════════════════════════════════════
# execute()
# ══════════════════════════════════════════════════════════════════════


class TestShellExecute:
    """Tests for ShellTool.execute()."""

    @pytest.mark.asyncio
    async def test_successful_command(self):
        """Successful command returns stdout."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"hello world", b""))
            mock_proc.returncode = 0
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=10)
            result = await tool.execute(command="echo hello")

            assert result == "hello world"
            mock_create.assert_called_once()
            # Check the command was passed
            call_args = mock_create.call_args[0][0]
            assert call_args == "echo hello"

    @pytest.mark.asyncio
    async def test_command_with_stderr(self):
        """Stderr output is appended to result."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"stdout text", b"error text"))
            mock_proc.returncode = 1
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=10)
            result = await tool.execute(command="some_command")

            assert "stdout text" in result
            assert "[stderr]" in result
            assert "error text" in result

    @pytest.mark.asyncio
    async def test_command_only_stderr(self):
        """Only stderr output."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b"error occurred"))
            mock_proc.returncode = 1
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=10)
            result = await tool.execute(command="failing_command")

            assert "[stderr]" in result
            assert "error occurred" in result

    @pytest.mark.asyncio
    async def test_command_no_output(self):
        """Command with no stdout or stderr shows exit code."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_proc.returncode = 0
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=10)
            result = await tool.execute(command="true")

            assert "Command completed" in result
            assert "exit code 0" in result
            assert "(no output)" in result

    @pytest.mark.asyncio
    async def test_timeout(self):
        """Command that times out returns error message."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError())
            mock_proc.wait = AsyncMock()
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=1)
            result = await tool.execute(command="sleep 100")

            assert "Error: Command timed out" in result
            assert "1s" in result
            # Process should be killed
            mock_proc.kill.assert_called_once()
            mock_proc.wait.assert_called_once()

    @pytest.mark.asyncio
    async def test_subprocess_pipes_configured(self):
        """Subprocess is created with stdout and stderr pipes."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"out", b""))
            mock_proc.returncode = 0
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=10)
            await tool.execute(command="test")

            call_kwargs = mock_create.call_args
            # Second positional arg should include stdout/stderr config
            assert "stdout" in call_kwargs[1] or len(call_kwargs[0]) > 1

    @pytest.mark.asyncio
    async def test_unicode_output(self):
        """Unicode characters in output are handled."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            # UTF-8 encoded unicode
            mock_proc.communicate = AsyncMock(return_value=("café ♥ résumé".encode("utf-8"), b""))
            mock_proc.returncode = 0
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=10)
            result = await tool.execute(command="echo unicode")

            assert "café ♥ résumé" in result

    @pytest.mark.asyncio
    async def test_decode_errors_replaced(self):
        """Invalid UTF-8 bytes are replaced rather than crashing."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            # Invalid UTF-8 sequence: 0xFF is invalid
            mock_proc.communicate = AsyncMock(return_value=(b"valid\xffinvalid", b""))
            mock_proc.returncode = 0
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=10)
            result = await tool.execute(command="bad_encoding")

            assert "valid" in result
            # The invalid byte should be replaced (by errors="replace")
            # Just verify it didn't crash

    @pytest.mark.asyncio
    async def test_whitespace_only_output(self):
        """Whitespace-only output is still considered no output."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"   \n  \t  ", b""))
            mock_proc.returncode = 0
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=10)
            result = await tool.execute(command="whitespace_only")

            # Whitespace-only result triggers "no output" branch
            assert "Command completed" in result or "exit code" in result

    @pytest.mark.asyncio
    async def test_large_output(self):
        """Large output is handled correctly."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            large_data = b"x" * 100000
            mock_proc.communicate = AsyncMock(return_value=(large_data, b""))
            mock_proc.returncode = 0
            mock_create.return_value = mock_proc

            tool = ShellTool(timeout=30)
            result = await tool.execute(command="generate_large_output")

            assert len(result) >= 100000

    @pytest.mark.asyncio
    async def test_wait_for_with_timeout(self):
        """asyncio.wait_for is called with the configured timeout."""
        with patch("asyncio.create_subprocess_shell") as mock_create:
            mock_proc = MagicMock()
            mock_proc.communicate = AsyncMock(return_value=(b"ok", b""))
            mock_proc.returncode = 0
            mock_create.return_value = mock_proc

            with patch("asyncio.wait_for") as mock_wait_for:
                mock_wait_for.return_value = (b"ok", b"")
                tool = ShellTool(timeout=25)
                await tool.execute(command="test")

                mock_wait_for.assert_called_once()
                # Should have timeout=25
                assert mock_wait_for.call_args[1]["timeout"] == 25
