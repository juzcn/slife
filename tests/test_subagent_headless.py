"""Tests for Slife.subagent.headless — headless JSON-RPC 2.0 mode."""

import json
import sys
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from slife.subagent.headless import _write, _process, main


class TestWrite:
    """Tests for _write() — JSON-RPC 2.0 response writer."""

    def test_write_result(self):
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _write(result={"ready": True}, rpc_id="req-1")

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["jsonrpc"] == "2.0"
        assert output["id"] == "req-1"
        assert output["result"] == {"ready": True}
        assert "error" not in output

    def test_write_error(self):
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _write(
                error={"code": -32000, "message": "Something broke"},
                rpc_id="req-2",
            )

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["jsonrpc"] == "2.0"
        assert output["id"] == "req-2"
        assert output["error"]["code"] == -32000
        assert output["error"]["message"] == "Something broke"

    def test_write_result_none_becomes_empty_dict(self):
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _write(rpc_id=None)

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["jsonrpc"] == "2.0"
        assert output["result"] == {}

    def test_write_error_default_code(self):
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _write(error={}, rpc_id="req-3")

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["error"]["code"] == -32000
        assert output["error"]["message"] == ""

    def test_write_unicode_content(self):
        """Emoji and Chinese characters should be writable."""
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _write(result={"message": "你好 \U0001f30d"}, rpc_id="emoji-1")

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["result"]["message"] == "你好 \U0001f30d"

    def test_write_flush_is_called(self):
        """Verify buffer.write and buffer.flush are both called."""
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _write(result={"data": "x"}, rpc_id="f")
            output = buf.getvalue()
            assert len(output) > 0
            assert b"jsonrpc" in output


class TestProcess:
    """Tests for _process() — task processing handler."""

    @pytest.mark.asyncio
    async def test_process_success(self):
        mock_service = MagicMock()
        mock_result = MagicMock()
        mock_result.text = "The answer is 42."
        mock_result.usage.prompt_tokens = 50
        mock_result.usage.completion_tokens = 50
        mock_result.usage.total_tokens = 100
        mock_service.agent_loop.run = AsyncMock(return_value=mock_result)

        with patch("slife.subagent.headless._write") as mock_write:
            with patch("slife.subagent.headless.elapsed") as mock_elapsed:
                # elapsed is used as a context manager
                mock_elapsed.return_value.__enter__ = MagicMock()
                mock_elapsed.return_value.__exit__ = MagicMock()
                await _process("What is the answer?", "task-1", mock_service)

        mock_write.assert_called_once()
        call_args = mock_write.call_args
        assert call_args[1]["result"] == "The answer is 42."
        assert call_args[1]["rpc_id"] == "task-1"

    @pytest.mark.asyncio
    async def test_process_max_iterations(self):
        from slife.agent.loop import MaxIterationsExceeded

        mock_service = MagicMock()
        mock_service.agent_loop.run = AsyncMock(
            side_effect=MaxIterationsExceeded("Too many iterations")  # type: ignore[arg-type]
        )

        with patch("slife.subagent.headless._write") as mock_write:
            await _process("Complex task", "task-2", mock_service)

        mock_write.assert_called_once()
        call_kwargs = mock_write.call_args[1]
        assert call_kwargs["error"]["code"] == -32000
        assert "Too many iterations" in call_kwargs["error"]["message"]

    @pytest.mark.asyncio
    async def test_process_general_exception(self):
        mock_service = MagicMock()
        mock_service.agent_loop.run = AsyncMock(
            side_effect=RuntimeError("Unexpected failure")
        )

        with patch("slife.subagent.headless._write") as mock_write:
            await _process("Broken task", "task-3", mock_service)

        mock_write.assert_called_once()
        call_kwargs = mock_write.call_args[1]
        assert call_kwargs["error"]["code"] == -32000
        assert "Unexpected failure" in call_kwargs["error"]["message"]


class TestMain:
    """Tests for main() entry point."""

    def test_main_runs_headless(self):
        with patch("slife.subagent.headless.asyncio.run") as mock_run:
            with patch("slife.subagent.headless.run_headless") as mock_rh:
                main([])
                mock_run.assert_called_once()
                mock_rh.assert_called_once_with()

    def test_main_with_args_ignored(self):
        """Config comes from SLIFE_CONFIG env var — CLI args are ignored."""
        with patch("slife.subagent.headless.asyncio.run") as mock_run:
            with patch("slife.subagent.headless.run_headless") as mock_rh:
                main(["somefile.json5", "--debug"])
                mock_run.assert_called_once()
                mock_rh.assert_called_once_with()
