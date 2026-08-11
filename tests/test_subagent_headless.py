"""Tests for Slife.subagent.headless — headless JSON-RPC 2.0 mode."""

import pytest; pytestmark = pytest.mark.unit


import json
import sys
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from slife.subagent.headless import _write, _notify, main


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


class TestNotify:
    """Tests for _notify() — JSON-RPC 2.0 notification writer.

    The reply path (inbox on_reply → _reply) writes a result envelope then
    notifies ``worker/complete``, so the parent's task record is closed.
    """

    def test_notify_with_params(self):
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _notify("worker/complete", {"task_id": "task-1"})

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["jsonrpc"] == "2.0"
        assert output["method"] == "worker/complete"
        assert output["params"]["task_id"] == "task-1"
        assert "id" not in output

    def test_notify_no_params(self):
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _notify("shutdown")

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["method"] == "shutdown"
        assert "params" not in output


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
