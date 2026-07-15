"""Tests for slife.logfmt — structured logging, contextvars, timing, stderr."""

import asyncio
import logging
import sys
from unittest.mock import MagicMock, patch

import pytest

from slife.logfmt import (
    init_session_id,
    set_session_id,
    get_session_id,
    request_scope,
    get_request_id,
    SessionFormatter,
    elapsed,
    read_stderr_lines,
    FILE_LOG_FORMAT,
)


# ── Session ID ──────────────────────────────────────────────────────────────


class TestSessionId:
    """Tests for session ID functions."""

    def test_init_generates_hex_string(self):
        sid = init_session_id()
        assert len(sid) == 12
        assert all(c in "0123456789abcdef" for c in sid)

    def test_init_updates_contextvar(self):
        sid = init_session_id()
        assert get_session_id() == sid

    def test_set_session_id(self):
        set_session_id("my-custom-id")
        assert get_session_id() == "my-custom-id"

    def test_get_session_id_uninitialized(self):
        import contextvars
        token = contextvars.ContextVar("_reset", default="").set("")
        # Fresh contextvar without the slife one — get_session_id returns placeholder
        # But we can't reset the actual module-level var without affecting other tests.
        # Instead verify the fallback behavior via direct check:
        # get_session_id returns '' initially if never called before
        # but conftest may have set it. Just verify it's non-empty after set.
        set_session_id("test-123")
        assert get_session_id() == "test-123"
        # Reset to empty
        set_session_id("")
        assert get_session_id() == "--------"

    def test_get_session_id_default(self):
        """Ensure fallback works for unset session."""
        # After setting to empty, fallback placeholder is returned
        set_session_id("")
        assert get_session_id() == "--------"
        # Restore
        init_session_id()


# ── Request ID ──────────────────────────────────────────────────────────────


class TestRequestId:
    """Tests for request_scope and get_request_id."""

    def test_request_scope_generates_id(self):
        with request_scope("test message") as rid:
            assert len(rid) == 8
            assert get_request_id() == rid

    def test_request_scope_restores_previous(self):
        prev = "previous-rid"
        set_session_id(prev)  # Not request, but check context restoration
        with request_scope("outer"):
            outer_rid = get_request_id()
            with request_scope("inner"):
                inner_rid = get_request_id()
                assert inner_rid != outer_rid
            assert get_request_id() == outer_rid

    def test_get_request_id_unset(self):
        # Outside a request_scope, should return placeholder
        assert get_request_id() == "--------"

    def test_request_scope_empty_label(self):
        with request_scope("") as rid:
            assert len(rid) == 8


# ── SessionFormatter ────────────────────────────────────────────────────────


class TestSessionFormatter:
    """Tests for SessionFormatter."""

    def test_format_injects_session_and_request_ids(self):
        init_session_id()
        with request_scope("test"):
            fmt = SessionFormatter("%(asctime)s [s=%(sid)s] [r=%(rid)s] %(message)s")
            record = logging.LogRecord(
                "test", logging.INFO, "path", 42, "hello world", (), None,
            )
            result = fmt.format(record)
            assert "hello world" in result
            assert "[s=" in result
            assert "[r=" in result

    def test_format_fallback_to_dashes(self):
        """When no session/request set, uses '--------'."""
        set_session_id("")
        # Can't easily reset request_id contextvar without proper token access
        fmt = SessionFormatter("%(asctime)s [s=%(sid)s] [r=%(rid)s] %(message)s")
        record = logging.LogRecord(
            "test", logging.INFO, "path", 42, "test message", (), None,
        )
        result = fmt.format(record)
        assert "[s=--------]" in result
        assert "[r=--------]" in result

    def test_format_time_with_milliseconds(self):
        fmt = SessionFormatter()
        record = logging.LogRecord(
            "test", logging.INFO, "path", 42, "test", (), None,
        )
        record.created = 1234567890.123
        record.msecs = 123
        ts = fmt.formatTime(record)
        assert "123" in ts  # milliseconds appended

    def test_format_time_with_custom_datefmt(self):
        fmt = SessionFormatter()
        record = logging.LogRecord(
            "test", logging.INFO, "path", 42, "test", (), None,
        )
        record.created = 1234567890.456
        record.msecs = 456
        ts = fmt.formatTime(record, datefmt="%H:%M:%S")
        assert ts == "23:31:30.456"

    def test_format_time_no_datefmt(self):
        fmt = SessionFormatter()
        record = logging.LogRecord(
            "test", logging.INFO, "path", 42, "test", (), None,
        )
        record.created = 1234567890.789
        record.msecs = 789
        ts = fmt.formatTime(record)
        assert "2009-02-13" in ts
        assert "789" in ts


# ── elapsed context manager ─────────────────────────────────────────────────


class TestElapsed:
    """Tests for elapsed() timing context manager."""

    def test_elapsed_logs_done_message(self):
        logger = logging.getLogger("test_elapsed_1")
        logger.setLevel(logging.DEBUG)
        with patch.object(logger, "log") as mock_log:
            with elapsed("myop", logger, level=logging.DEBUG, server="test"):
                pass
            mock_log.assert_called_once()
            # logger.log(level, "%s_done %s", operation, " ".join(parts))
            fmt_string = mock_log.call_args[0][1]
            operation = mock_log.call_args[0][2]
            extras = mock_log.call_args[0][3]
            assert "%s_done %s" == fmt_string
            assert operation == "myop"
            assert "took_ms=" in extras
            assert "server=test" in extras

    def test_elapsed_no_extra(self):
        logger = logging.getLogger("test_elapsed_2")
        logger.setLevel(logging.DEBUG)
        with patch.object(logger, "log") as mock_log:
            with elapsed("connect", logger, level=logging.DEBUG):
                pass
            mock_log.assert_called_once()
            fmt_string = mock_log.call_args[0][1]
            operation = mock_log.call_args[0][2]
            assert "%s_done %s" == fmt_string
            assert operation == "connect"

    def test_elapsed_on_exception_still_logs(self):
        logger = logging.getLogger("test_elapsed_3")
        logger.setLevel(logging.DEBUG)
        with patch.object(logger, "log") as mock_log:
            try:
                with elapsed("failing_op", logger, level=logging.DEBUG):
                    raise ValueError("boom")
            except ValueError:
                pass
            mock_log.assert_called_once()
            fmt_string = mock_log.call_args[0][1]
            operation = mock_log.call_args[0][2]
            assert "%s_done %s" == fmt_string
            assert operation == "failing_op"


# ── read_stderr_lines ───────────────────────────────────────────────────────


class TestReadStderrLines:
    """Tests for read_stderr_lines async generator."""

    def _make_stderr_mock(self, lines: list):
        """Build a mock process whose stderr.readline returns the given lines."""
        proc = MagicMock()
        proc.stderr = MagicMock()

        async def _readline_side_effect():
            if lines:
                return lines.pop(0)
            return b""

        proc.stderr.readline = _readline_side_effect
        return proc

    @pytest.mark.asyncio
    async def test_no_process_returns_immediately(self):
        lines = [line async for line in read_stderr_lines(None)]
        assert lines == []

    @pytest.mark.asyncio
    async def test_no_stderr_returns_immediately(self):
        proc = MagicMock()
        proc.stderr = None
        result = [line async for line in read_stderr_lines(proc)]
        assert result == []

    @pytest.mark.asyncio
    async def test_reads_lines_until_eof(self):
        proc = self._make_stderr_mock([
            b"error: something went wrong\n",
            b"warning: check config\n",
        ])
        lines = [line async for line in read_stderr_lines(proc)]
        assert lines == ["error: something went wrong", "warning: check config"]

    @pytest.mark.asyncio
    async def test_empty_lines_skipped(self):
        proc = self._make_stderr_mock([
            b"real line\n",
            b"\n",
            b"  \n",
            b"another line\n",
        ])
        lines = [line async for line in read_stderr_lines(proc)]
        assert lines == ["real line", "another line"]

    @pytest.mark.asyncio
    async def test_running_check_stops_early(self):
        running = [True, True, False]
        def check():
            return running.pop(0) if running else False

        proc = self._make_stderr_mock([
            b"line1\n",
            b"line2\n",
            b"line3\n",
        ])
        lines = [line async for line in read_stderr_lines(proc, running_check=check)]
        assert len(lines) == 2

    @pytest.mark.asyncio
    async def test_cancelled_error_handled(self):
        proc = MagicMock()
        proc.stderr = MagicMock()

        async def _raise_cancelled():
            raise asyncio.CancelledError()

        proc.stderr.readline = _raise_cancelled
        lines = [line async for line in read_stderr_lines(proc)]
        assert lines == []

    @pytest.mark.asyncio
    async def test_decode_error_replacement(self):
        proc = self._make_stderr_mock([
            b"valid line\n",
            b"\xff\xfeinvalid utf8\n",
        ])
        lines = [line async for line in read_stderr_lines(proc)]
        assert lines[0] == "valid line"
        assert "invalid utf8" in lines[1]  # errors='replace' handles it
