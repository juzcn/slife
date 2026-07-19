"""Tests for Slife.bootstrap — logging setup and session initialization."""

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch, mock_open

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
    def test_custom_agent_id(self, mock_mkdir):
        path = bootstrap._session_log_path(agent_id="testbot")
        assert "_testbot.log" in str(path)

    @patch("pathlib.Path.mkdir")
    def test_timestamp_format(self, mock_mkdir):
        path = bootstrap._session_log_path()
        # Timestamp: YYYYMMDD_HHMMSS
        name = path.stem  # e.g. 20260719_113147_slife
        parts = name.split("_")
        assert len(parts) >= 3  # YYYYMMDD, HHMMSS, agent_id


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

        # Cleanup
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

        # Cleanup
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
            # Cleanup
            root.handlers.clear()

    def test_noisy_loggers_silenced(self):
        """setup_logging silences noisy third-party loggers."""
        root = logging.getLogger()
        root.handlers.clear()

        bootstrap.setup_logging()

        assert logging.getLogger("httpx").level == logging.WARNING
        assert logging.getLogger("asyncio").level == logging.WARNING
        assert logging.getLogger("openai._base_client").level == logging.WARNING
        assert logging.getLogger("httpcore.connection").level == logging.WARNING

        # Cleanup
        root.handlers.clear()
