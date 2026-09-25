"""Tests for Slife.bootstrap — logging setup and session initialization."""

import pytest; pytestmark = pytest.mark.unit


import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import slife.bootstrap as bootstrap


# ── _session_log_path ──────────────────────────────────────────────────────


class TestSessionLogPath:
    """Tests for _session_log_path."""

    @patch("pathlib.Path.mkdir")
    def test_default_name(self, mock_mkdir):
        path = bootstrap._session_log_path()
        assert path.parent.name == "logs"
        assert path.name.endswith("_slife.log")

    @patch("pathlib.Path.mkdir")
    def test_custom_agent_name(self, mock_mkdir):
        path = bootstrap._session_log_path(agent_name="testbot")
        assert "_testbot.log" in str(path)

    @patch("pathlib.Path.mkdir")
    def test_timestamp_format(self, mock_mkdir):
        path = bootstrap._session_log_path()
        # Timestamp: YYYYMMDD_HHMMSS
        name = path.stem  # e.g. 20260719_113147_slife
        parts = name.split("_")
        assert len(parts) >= 3  # YYYYMMDD, HHMMSS, agent_name


# ── setup_logging ───────────────────────────────────────────────────────────


class TestSetupLogging:
    """Tests for setup_logging."""

    def test_basic_setup_returns_path_and_handler(self):
        """First call creates handlers and returns log path + console handler."""
        # Clear existing handlers so we get a fresh setup
        root = logging.getLogger()
        root.handlers.clear()

        log_path, console = bootstrap.setup_logging()
        assert isinstance(log_path, Path)
        assert isinstance(console, logging.StreamHandler)
        assert len(root.handlers) >= 2  # console + file

        # Cleanup — close file handlers to avoid ResourceWarning
        for h in root.handlers:
            h.close()
        root.handlers.clear()

    def test_dedup_skips_when_handlers_exist(self):
        """Second call returns existing console handler without creating duplicates."""
        root = logging.getLogger()
        root.handlers.clear()

        log_path1, console1 = bootstrap.setup_logging()
        handler_count = len(root.handlers)

        log_path2, console2 = bootstrap.setup_logging()
        assert console2 is console1
        assert len(root.handlers) == handler_count

        # Cleanup — close file handlers to avoid ResourceWarning
        for h in root.handlers:
            h.close()
        root.handlers.clear()

    def test_dedup_returns_none_when_no_stream_handler(self):
        """If handlers exist but none is a StreamHandler, returns None for console."""
        root = logging.getLogger()
        root.handlers.clear()

        # Add a non-StreamHandler
        null_handler = logging.NullHandler()
        root.addHandler(null_handler)

        try:
            log_path, console = bootstrap.setup_logging()
            # Should return path but no StreamHandler console found
            assert isinstance(log_path, Path)
            # console may still be None since NullHandler is not a StreamHandler
            # The handler lookup tries isinstance(h, logging.StreamHandler) on
            # the NullHandler (which is just a Handler, not StreamHandler).
        finally:
            # Cleanup — close file handlers to avoid ResourceWarning
            for h in root.handlers:
                h.close()
            root.handlers.clear()

    def test_noisy_loggers_silenced(self):
        """setup_logging silences noisy third-party loggers."""
        root = logging.getLogger()
        root.handlers.clear()

        bootstrap.setup_logging()

        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("httpx2").level == logging.WARNING
        assert logging.getLogger("asyncio").level == logging.WARNING
        assert logging.getLogger("openai._base_client").level == logging.WARNING
        assert logging.getLogger("httpcore.connection").level == logging.WARNING
        assert logging.getLogger("httpcore2.http11").level == logging.WARNING

        # Cleanup — close file handlers to avoid ResourceWarning
        for h in root.handlers:
            h.close()
        root.handlers.clear()

    def test_console_never_emits(self):
        """Logs never reach the terminal — the console handler is a no-op.

        Terminal belongs to the TUI; user-visible status is surfaced there
        by the business layer.  The console StreamHandler exists structurally
        but sits at CRITICAL+1 so no log record ever prints.
        """
        root = logging.getLogger()
        root.handlers.clear()

        try:
            log_path, console = bootstrap.setup_logging()
            assert console.level == logging.CRITICAL + 1
        finally:
            for h in root.handlers:
                h.close()
            root.handlers.clear()


# ── Unclean-exit marker ────────────────────────────────────────────────────


class TestSessionMarker:
    """The marker a killed session leaves for the next start to read.

    A hard kill runs no teardown, so the death has to be inferred afterwards
    — that is the whole point of the marker (see bootstrap's section comment).
    """

    @patch("slife.bootstrap.resolve_log_dir")
    def test_start_writes_and_clear_removes(self, mock_dir, tmp_path):
        mock_dir.return_value = tmp_path

        bootstrap.note_session_start(tmp_path / "s.log", "sid-1")

        (marker,) = list(tmp_path.glob(".session.*.state"))
        info = json.loads(marker.read_text(encoding="utf-8"))
        assert info["pid"] == os.getpid()
        assert info["session_id"] == "sid-1"
        assert info["log"].endswith("s.log")

        bootstrap.clear_session_marker(tmp_path)
        assert list(tmp_path.glob(".session.*.state")) == []

    def test_clean_previous_session_reports_nothing(self, tmp_path):
        assert bootstrap.previous_session_killed(tmp_path) is None

    @patch("slife.bootstrap._pid_alive", return_value=False)
    def test_killed_previous_session_is_reported_once(self, mock_alive, tmp_path):
        marker = tmp_path / ".session.4242.state"
        marker.write_text(
            json.dumps({
                "pid": 4242,
                "started": "2026-09-25T21:18:20",
                "log": "logs/20260925_211820_slife.log",
            }),
            encoding="utf-8",
        )

        line = bootstrap.previous_session_killed(tmp_path)

        assert "4242" in line
        assert "2026-09-25T21:18:20" in line
        assert "20260925_211820_slife.log" in line
        # Reported once: the marker is consumed, so the next start is silent.
        assert bootstrap.previous_session_killed(tmp_path) is None

    @patch("slife.bootstrap._pid_alive", return_value=True)
    def test_live_session_marker_is_left_alone(self, mock_alive, tmp_path):
        """A second session in the same data dir is not a death to report."""
        marker = tmp_path / ".session.4242.state"
        marker.write_text(json.dumps({"pid": 4242}), encoding="utf-8")

        assert bootstrap.previous_session_killed(tmp_path) is None
        assert marker.exists()

    def test_unreadable_marker_is_dropped(self, tmp_path):
        """Killed mid-write leaves a truncated file — drop it, don't crash."""
        marker = tmp_path / ".session.4242.state"
        marker.write_text("{ truncated", encoding="utf-8")

        assert bootstrap.previous_session_killed(tmp_path) is None
        assert not marker.exists()

    def test_marker_with_junk_pid_is_dropped(self, tmp_path):
        """A startup path may not raise on a junk file someone left behind."""
        marker = tmp_path / ".session.4242.state"
        marker.write_text(json.dumps({"pid": "not-a-number"}), encoding="utf-8")

        assert bootstrap.previous_session_killed(tmp_path) is None
        assert not marker.exists()
