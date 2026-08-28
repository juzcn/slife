"""Tests for slife/server_utils.py — shared server logging setup."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import logging
import os
import socket
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

import pytest

import slife.server_utils as server_utils
from slife.server_utils import (
    create_plugin_server,
    run_plugin_server,
    setup_server_logging,
    shutdown_server_logging,
)


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def temp_log_dir(tmp_path: Path) -> Path:
    return tmp_path / "logs"


@pytest.fixture(autouse=True)
def restore_root_logger():
    """Save and restore root logger state after every test."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)


# ── helpers ───────────────────────────────────────────────────────────


def _run_with_mocks(log_dir, session_id=""):
    """Call setup_server_logging with FileHandler mocked.

    Returns (log_path, mock_file_handler).
    """
    mock_handler = MagicMock()
    mock_fh_cls = MagicMock(return_value=mock_handler)

    env = {"SLIFE_SESSION_ID": session_id} if session_id else {}
    with patch.dict(os.environ, env, clear=True):
        with patch("slife.server_utils.logging.FileHandler", mock_fh_cls):
            log_path = setup_server_logging("test_svc", log_dir=log_dir)

    return log_path, mock_handler


# ── setup_server_logging ─────────────────────────────────────────────


class TestSetupServerLogging:
    def test_creates_log_dir_and_returns_path(self, temp_log_dir):
        """Log directory is created and a path with the service name is returned."""
        log_path, _ = _run_with_mocks(temp_log_dir)

        assert log_path.parent == temp_log_dir
        assert temp_log_dir.exists()
        assert "test_svc" in log_path.name
        assert log_path.suffix == ".log"

    def test_sets_root_logger_to_debug(self, tmp_path):
        """Root logger level is set to DEBUG."""
        _run_with_mocks(tmp_path / "logs")
        assert logging.getLogger().level == logging.DEBUG

    def test_adds_two_handlers(self, tmp_path):
        """Both a StreamHandler (stderr) and a FileHandler are added."""
        _run_with_mocks(tmp_path / "logs")
        handlers = logging.getLogger().handlers
        assert len(handlers) == 2
        assert isinstance(handlers[0], logging.StreamHandler)

    def test_session_id_from_env(self, tmp_path):
        """When SLIFE_SESSION_ID is set, it propagates to logfmt."""
        with patch("slife.server_utils.set_session_id") as mock_set:
            mock_handler = MagicMock()
            mock_fh_cls = MagicMock(return_value=mock_handler)
            with patch.dict(os.environ, {"SLIFE_SESSION_ID": "abc123"}):
                with patch("slife.server_utils.logging.FileHandler", mock_fh_cls):
                    setup_server_logging("svc", log_dir=tmp_path / "logs")
            mock_set.assert_called_once_with("abc123")

    def test_no_session_id_skips_set(self, tmp_path):
        """When SLIFE_SESSION_ID is empty, set_session_id is not called."""
        with patch("slife.server_utils.set_session_id") as mock_set:
            _, _ = _run_with_mocks(tmp_path / "logs", session_id="")
        mock_set.assert_not_called()

    def test_log_dir_from_env_when_not_passed(self, tmp_path):
        """Without an explicit log_dir, SLIFE_LOG_DIR is used — the env
        contract the main process exports for plugin children."""
        mock_handler = MagicMock()
        mock_fh_cls = MagicMock(return_value=mock_handler)
        with patch.dict(os.environ, {"SLIFE_LOG_DIR": str(tmp_path / "env_logs")}, clear=True):
            with patch("slife.server_utils.logging.FileHandler", mock_fh_cls):
                log_path = setup_server_logging("svc")
        assert log_path.parent == tmp_path / "env_logs"

    def test_file_handler_uses_session_formatter(self, tmp_path):
        """The file handler is configured with SessionFormatter."""
        from slife.logfmt import SessionFormatter

        _, mock_handler = _run_with_mocks(tmp_path / "logs")

        assert mock_handler.setFormatter.called
        formatter = mock_handler.setFormatter.call_args[0][0]
        assert isinstance(formatter, SessionFormatter)

    def test_file_handler_is_debug_level(self, tmp_path):
        """File handler is set to DEBUG level."""
        _, mock_handler = _run_with_mocks(tmp_path / "logs")
        mock_handler.setLevel.assert_called_with(logging.DEBUG)

    def test_silences_noisy_loggers(self, tmp_path):
        """Noisy third-party and FastMCP loggers are silenced."""
        with patch("slife.server_utils.silence_noisy_loggers") as mock_silence:
            _, _ = _run_with_mocks(tmp_path / "logs")

        mock_silence.assert_called_once()
        args = mock_silence.call_args[1]
        assert "extra" in args
        assert "mcp.server.lowlevel.server" in args["extra"]
        assert "fastmcp" in args["extra"]

    def test_log_filename_includes_timestamp_and_service(self, temp_log_dir):
        """Log follows pattern: YYYYMMDD_HHMMSS_servicename.log."""
        log_path, _ = _run_with_mocks(temp_log_dir)

        name = log_path.name
        assert name.endswith(".log")
        assert "_test_svc" in name
        # First two underscore-separated segments: date(8) + time(6)
        prefix_parts = name.split("_")
        assert len(prefix_parts[0]) == 8  # YYYYMMDD
        assert len(prefix_parts[1]) == 6  # HHMMSS

    def test_clears_existing_handlers(self, tmp_path):
        """Existing root logger handlers are removed before adding new ones."""
        root = logging.getLogger()
        existing = logging.StreamHandler()
        root.addHandler(existing)

        _, _ = _run_with_mocks(tmp_path / "logs")

        assert existing not in root.handlers

    def test_stderr_handler_formatter(self, tmp_path):
        """The stderr handler uses a standard Formatter."""
        _, _ = _run_with_mocks(tmp_path / "logs")

        handlers = logging.getLogger().handlers
        stderr_handler = handlers[0]
        assert isinstance(stderr_handler.formatter, logging.Formatter)


# ── shutdown_server_logging ─────────────────────────────────────────────


class TestShutdownServerLogging:
    """Tests for shutdown_server_logging()."""

    def test_noop_when_no_handlers(self):
        """Safe to call when no handlers are set up."""
        root = logging.getLogger()
        root.handlers.clear()
        # Should not raise
        shutdown_server_logging()

    def test_closes_and_removes_all_handlers(self, tmp_path):
        """All handlers are flushed, closed, and removed."""
        _, _ = _run_with_mocks(tmp_path / "logs")
        root = logging.getLogger()
        assert len(root.handlers) > 0

        shutdown_server_logging()
        assert len(root.handlers) == 0

    def test_handler_close_error_swallowed(self):
        """Exceptions during handler.close() are caught, not propagated."""
        root = logging.getLogger()
        root.handlers.clear()
        bad_handler = MagicMock()
        bad_handler.close.side_effect = OSError("file locked")
        bad_handler.flush = MagicMock()
        root.addHandler(bad_handler)

        # Should not raise
        shutdown_server_logging()
        assert len(root.handlers) == 0

    def test_extra_logger_names_cleaned(self):
        """Child loggers with their own handlers are also cleaned."""
        child = logging.getLogger("test_extra_cleanup")
        child.handlers.clear()
        child.addHandler(logging.StreamHandler())

        shutdown_server_logging(extra_logger_names=("test_extra_cleanup",))
        assert len(child.handlers) == 0


# ── Plugin loading contract: signal AFTER the app is ready ──────────────


class TestPluginReadySignal:
    """The port signal means *ready to serve MCP* — emitted only after the
    server's lifespan completes (slow init such as ngrok / MQTT), so the
    parent's first ``initialize`` always lands on a ready server."""

    def test_wrapped_lifespan_signals_after_inner_lifespan(self):
        """_ready_callback fires after the inner lifespan (slow init) enters,
        before the app serves."""
        order = []

        @asynccontextmanager
        async def inner(app):
            order.append("inner-enter")
            yield
            order.append("inner-exit")

        with patch("slife.server_utils._ready_callback",
                   lambda: order.append("signal")):
            created, _, _ = create_plugin_server(
                "slife-test", "test", lifespan=inner,
            )

            async def _serve():
                async with created._lifespan(object()):
                    order.append("serving")

            asyncio.run(_serve())

        assert order == ["inner-enter", "signal", "serving", "inner-exit"]

    def test_wrapped_lifespan_signals_without_inner(self):
        """A plugin with no lifespan still signals (no slow work to wait for)."""
        order = []

        with patch("slife.server_utils._ready_callback",
                   lambda: order.append("signal")):
            created, _, _ = create_plugin_server("slife-test", "test")

            async def _serve():
                async with created._lifespan(object()):
                    order.append("serving")

            asyncio.run(_serve())

        assert order == ["signal", "serving"]

    def test_run_plugin_server_signals_port_via_ready_callback(self):
        """run_plugin_server wires _ready_callback to signal_port(port); the
        callback is cleared after the server shuts down."""
        mock_server = MagicMock()
        captured = {}

        def fake_run(**kwargs):
            captured["cb"] = server_utils._ready_callback

        mock_server.run.side_effect = fake_run
        with patch("slife.server_utils.signal_port") as mock_signal:
            run_plugin_server(mock_server, port=4321)
            captured["cb"]()
            mock_signal.assert_called_once_with(4321)
        assert server_utils._ready_callback is None

    def test_run_plugin_server_sockets_branch_signals_socket_port(self):
        """A pre-bound socket (memfiles) derives the port from the socket."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        mock_server = MagicMock()
        captured = {}
        mock_server.run.side_effect = lambda **kw: captured.update(
            cb=server_utils._ready_callback,
        )
        try:
            with patch("slife.server_utils.signal_port") as mock_signal:
                run_plugin_server(mock_server, sockets=[sock])
                captured["cb"]()
                mock_signal.assert_called_once_with(port)
        finally:
            sock.close()
        assert server_utils._ready_callback is None
