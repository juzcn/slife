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

    def test_write_result_none_omits_result(self):
        """An empty/silent turn must not corrupt into ``{"result": {}}``."""
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _write(rpc_id=None)

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["jsonrpc"] == "2.0"
        assert "result" not in output

    def test_write_result_empty_string_stays_empty(self):
        """An explicitly-empty reply stays ``"",`` — the parent reads it as
        str(msg.get("result", "")) without seeing the literal "{}"."""
        buf = BytesIO()
        mock_stdout = MagicMock()
        mock_stdout.buffer = buf

        with patch("slife.subagent.headless.sys.stdout", mock_stdout):
            _write(result="", rpc_id="req-empty")

        output = json.loads(buf.getvalue().decode("utf-8"))
        assert output["id"] == "req-empty"
        assert output["result"] == ""

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


class TestRunHeadlessHostFacts:
    """The worker records the same host facts the main agent does.

    Pinned here rather than only in health's own tests because the failure
    mode is a missing CALL: without a recorder at this entry point a worker
    reported 14 components against its parent's 20, and neither report said
    which facts were absent or why.
    """

    class _Stop(Exception):
        """Abort at the service construction — everything before it ran."""

    def _run_to_service(self, monkeypatch, tmp_path, config_json):
        import asyncio

        from slife.config import Config, ModelConfig
        from slife.subagent import headless

        # The preferred channel: the parent writes its config to a temp file
        # and passes the path (the env-var form is the older fallback).
        cfg_file = tmp_path / "inherited.json"
        cfg_file.write_text(json.dumps(config_json), encoding="utf-8")
        monkeypatch.setenv("SLIFE_CONFIG_FILE", str(cfg_file))
        monkeypatch.delenv("SLIFE_CONFIG", raising=False)
        monkeypatch.setattr(
            headless, "setup_server_logging", lambda *a, **k: tmp_path / "sub.log",
        )
        config = Config(
            models=[ModelConfig(
                ref="deepseek/deepseek-flash", provider="deepseek",
                api_model="deepseek-flash", display_name="Flash",
                api_key="sk-x", context_window=1000,
            )],
            active_model_ref="deepseek/deepseek-flash",
            tools=[],
        )
        with patch("slife.config.Config.from_dict", return_value=config), \
             patch(
                 "slife.agent.service.AgentService", side_effect=self._Stop,
             ):
            with pytest.raises(self._Stop):
                asyncio.run(headless.run_headless([]))

    def test_the_worker_records_the_host_facts(self, monkeypatch, tmp_path):
        recorded: list[str] = []
        with patch(
            "slife.health.record_host_facts",
            side_effect=lambda *a, **k: recorded.append(k.get("source", "")),
        ):
            self._run_to_service(monkeypatch, tmp_path, {"agent_name": "slife"})

        # The source says where THIS process got its config: a worker is
        # handed the parent's, it never reads the yaml itself.
        assert recorded == ["inherited from the main agent"]


class TestMain:
    """Tests for main() entry point."""

    def test_main_runs_headless(self):
        with patch("slife.subagent.headless.asyncio.run") as mock_run:
            with patch("slife.subagent.headless.run_headless") as mock_rh:
                main([])
                mock_run.assert_called_once()
                mock_rh.assert_called_once_with([])

    def test_main_forwards_argv(self):
        """main() forwards the FULL argv (program name included) —
        ``parse_cli_config_path`` slices argv[1:] itself, so a stripped
        argv would double-strip a positional config path."""
        with patch("slife.subagent.headless.asyncio.run") as mock_run:
            with patch("slife.subagent.headless.run_headless") as mock_rh:
                main(["prog", "somefile.yaml", "--debug"])
                mock_run.assert_called_once()
                mock_rh.assert_called_once_with(["prog", "somefile.yaml", "--debug"])
