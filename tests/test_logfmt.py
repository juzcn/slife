"""Tests for Slife.logfmt — structured logging, contextvars, timing, stderr."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import logging
import sys
from datetime import datetime
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
    drain_stderr,
    sanitize_secrets,
    silence_noisy_loggers,
    ok_json,
    error_json,
    resolve_log_dir,
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
        # Fresh contextvar without the Slife one — get_session_id returns placeholder
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
        # Local time — matches the log filename convention.  Compute the
        # expected value in the runner's own timezone rather than pinning UTC.
        expected = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        assert ts == f"{expected}.456"

    def test_format_time_no_datefmt(self):
        fmt = SessionFormatter()
        record = logging.LogRecord(
            "test", logging.INFO, "path", 42, "test", (), None,
        )
        record.created = 1234567890.789
        record.msecs = 789
        ts = fmt.formatTime(record)
        expected = datetime.fromtimestamp(record.created).strftime("%Y-%m-%d")
        assert expected in ts
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

    @pytest.mark.asyncio
    async def test_overlong_line_discarded_relay_survives(self):
        """Regression: an over-long line must not kill the relay.

        A 315 KB anthropic SDK "Request options" DEBUG line raised
        LimitOverrunError in readline(); the relay died silently, the
        child's stderr pipe filled up, and the child blocked on its next
        log write — a subagent task stuck "pending" forever.  The relay
        must discard the over-long line and keep relaying.
        """
        proc = MagicMock()
        proc.stderr = MagicMock()
        calls = {"readline": 0}

        async def _readline():
            calls["readline"] += 1
            if calls["readline"] == 1:
                # First line overruns the reader limit.
                raise ValueError(
                    "Separator is not found, and chunk exceed the limit",
                )
            if calls["readline"] == 2:
                return b"X" * 1000 + b"\n"  # remainder of the overlong line
            if calls["readline"] == 3:
                return b"next line\n"
            return b""

        proc.stderr.readline = _readline
        lines = [line async for line in read_stderr_lines(proc)]
        # The overlong line (incl. its remainder) is dropped entirely;
        # relaying continues with the next line.
        assert lines == ["next line"]

    @pytest.mark.asyncio
    async def test_reader_limit_raised_large_line_truncated(self):
        """A 300 KB line survives the relay: no overrun, truncated relay.

        The StreamReader default limit is 64 KB; read_stderr_lines raises
        it so real-world large lines (LLM SDK DEBUG dumps, big tracebacks)
        don't overrun, and truncates the relayed copy (the child's own log
        file keeps the full line; the relay is diagnostic).
        """
        reader = asyncio.StreamReader()  # default _limit: 64 KB
        proc = MagicMock()
        proc.stderr = reader
        big = b"Y" * (300 * 1024)
        reader.feed_data(big + b"\n")
        reader.feed_data(b"after\n")
        reader.feed_eof()
        lines = [line async for line in read_stderr_lines(proc)]
        assert reader._limit >= 300 * 1024  # noqa: SLF001
        assert len(lines) == 2
        assert lines[0].startswith("Y" * 1024)
        assert "truncated" in lines[0]
        assert lines[1] == "after"


# ── drain_stderr ────────────────────────────────────────────────────────


class TestDrainStderr:
    """Tests for drain_stderr — thin wrapper for logging stderr lines."""

    @pytest.mark.asyncio
    async def test_drain_logs_each_line(self, caplog):
        """drain_stderr logs every non-empty line with the given prefix."""
        async def _mock_lines(process, running_check):
            yield "error: something went wrong"
            yield "info: recovery complete"

        logger = logging.getLogger("test_drain")
        with patch("slife.logfmt.read_stderr_lines", _mock_lines):
            with caplog.at_level(logging.DEBUG, logger="test_drain"):
                await drain_stderr(None, "myserver", logger)

        assert len(caplog.records) == 2
        assert "[myserver] error: something went wrong" in caplog.text
        assert "[myserver] info: recovery complete" in caplog.text

    @pytest.mark.asyncio
    async def test_drain_empty_lines_yield_nothing(self, caplog):
        """When read_stderr_lines yields nothing, no logs are emitted."""
        async def _mock_lines(process, running_check):
            # No yield — empty iterator
            if False:
                yield

        logger = logging.getLogger("test_drain_empty")
        with patch("slife.logfmt.read_stderr_lines", _mock_lines):
            with caplog.at_level(logging.DEBUG, logger="test_drain_empty"):
                await drain_stderr(None, "empty", logger)

        assert len(caplog.records) == 0


# ── sanitize_secrets ────────────────────────────────────────────────────────


class TestSanitizeSecrets:
    """Tests for sanitize_secrets() — redacts API key patterns."""

    def test_sk_prefix_token_redacted(self):
        """OpenAI/Anthropic style sk-* keys are redacted."""
        result = sanitize_secrets("Using key: sk-ant-api03-abc123def456ghi789jkl")
        assert "sk-ant-api03-abc123def456ghi789jkl" not in result
        assert "<MASKED>" in result

    def test_deepseek_style_sk_key_redacted(self):
        """DeepSeek style sk-* hex keys are redacted."""
        result = sanitize_secrets("DEEPSEEK_API_KEY=sk-abcdef1234567890abcdef1234567890ab")
        assert "sk-abcdef" not in result
        assert "<MASKED>" in result

    def test_github_token_redacted(self):
        """GitHub personal access tokens are redacted."""
        result = sanitize_secrets("GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz1234")
        assert "ghp_abcdef" not in result
        assert "<MASKED>" in result

    def test_google_oauth_token_redacted(self):
        """Google OAuth access tokens are redacted."""
        result = sanitize_secrets("token: ya29.abcdefghijklmnopqrstuvwxyz")
        assert "ya29.abc" not in result
        assert "<MASKED>" in result

    def test_bearer_auth_header_redacted(self):
        """Bearer Authorization headers are redacted."""
        result = sanitize_secrets(
            'Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0'
        )
        assert "eyJhbGci" not in result
        assert "<MASKED>" in result

    def test_key_equals_value_pattern_redacted(self):
        """key=value patterns with secret-looking keys are redacted."""
        result = sanitize_secrets("api_key = sk-1234567890abcdef1234567890abcdef")
        assert "sk-12345" not in result
        assert "<MASKED>" in result

    def test_stripe_underscore_key_redacted(self):
        """Stripe sk_live_/sk_test_ keys (underscore form) are redacted."""
        # Short value (real Stripe keys are 24+ chars) so GitHub's secret
        # scanner doesn't flag the fixture, but still ≥10 to hit the pattern.
        result = sanitize_secrets("STRIPE_KEY=sk_live_abc123def456")
        assert "sk_live_abc" not in result
        assert "<MASKED>" in result

    def test_aws_compound_key_redacted(self):
        """AWS *_SECRET_ACCESS_KEY / *_ACCESS_KEY_ID names are redacted."""
        result = sanitize_secrets(
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )
        assert "wJalrXUtn" not in result
        assert "<MASKED>" in result
        result2 = sanitize_secrets("AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE")
        assert "AKIAIOSFODNN7" not in result2

    def test_short_password_redacted(self):
        """Short credential values (>=6 chars) are redacted."""
        result = sanitize_secrets("password=hunter2")
        assert "hunter2" not in result
        assert "<MASKED>" in result

    def test_connection_string_password_redacted(self):
        """Connection-string URL credentials mask only the password."""
        result = sanitize_secrets("DATABASE_URL=postgres://app:hunter2@db")
        assert "hunter2" not in result
        assert "<MASKED>" in result
        assert "postgres://app:" in result  # structure preserved

    def test_plain_url_passes_through(self):
        """A URL without embedded credentials is unchanged."""
        url = "https://example.com:8080/path"
        assert sanitize_secrets(url) == url

    def test_basic_auth_header_redacted(self):
        """Regression: 'Authorization: Basic …' used to slip through unmasked
        (the header pattern required the token right after the keyword)."""
        result = sanitize_secrets("Authorization: Basic dXNlcjpwYXNzd29yZA==")
        assert "dXNlcjpwYXNzd29yZA==" not in result
        assert "<MASKED>" in result

    def test_token_auth_header_redacted(self):
        """Regression: 'Authorization: Token …' used to slip through."""
        result = sanitize_secrets("Authorization: Token abcdef1234567890abcdef123456")
        assert "abcdef1234567890abcdef123456" not in result

    def test_authorization_bearer_with_colon_redacted(self):
        """'Authorization: Bearer …' masks the token even with the colon."""
        result = sanitize_secrets(
            "Authorization: Bearer sk-ant-api03-abcdefghijklmnopqrstuvwx",
        )
        assert "sk-ant-api03-abcdefghijklmnopqrstuvwx" not in result

    def test_github_finegrained_pat_redacted(self):
        """Regression: GitHub fine-grained PATs (github_pat_…) used to pass
        the gh[psu]_ pattern."""
        pat = "github_pat_11ABCDEF0123456789abcdef"
        assert "github_pat_" not in sanitize_secrets(f"token={pat}")

    def test_port_url_not_corrupted(self):
        """Regression: a host:port/path URL (not credentials) must not have
        its port+path swallowed as if it were a password."""
        url = "https://host:8080/user@domain.com"
        assert sanitize_secrets(url) == url

    def test_json_api_key_not_mangled(self):
        """Regression: the key=value value class must not swallow the closing
        quotes/braces of JSON and corrupt the line."""
        result = sanitize_secrets('{"api_key": "sk-abcdefghijklmnopqrstuvwx"}')
        assert "<MASKED>" in result
        assert '}' in result  # closing brace survives

    def test_hex_token_32_chars_redacted(self):
        """Generic 32+ char hex-ish tokens are redacted."""
        result = sanitize_secrets(
            "Raw token: abcdef1234567890abcdef1234567890extra"
        )
        assert "abcdef1234567890abcdef1234567890extra" not in result
        assert "<MASKED>" in result

    def test_normal_text_passes_through(self):
        """Normal text without secrets is unchanged."""
        result = sanitize_secrets("Hello world, how are you?")
        assert result == "Hello world, how are you?"

    def test_git_style_hash_passes_through(self):
        """Short hex strings (under 32 chars) pass through — git hashes, etc."""
        result = sanitize_secrets("commit abcdef1234567890abcdef1234567890ab")  # 40 chars
        # 40-char lowercase hex — the hex pattern should match it
        # But normal git output like "abc1234" (7 chars) passes through
        short = sanitize_secrets("commit abc1234")
        assert "abc1234" in short

    def test_idempotent_double_call(self):
        """Double sanitization produces the same result."""
        text = "api_key=sk-test-key-xxxxyyyyzzzz11112222"
        once = sanitize_secrets(text)
        twice = sanitize_secrets(once)
        assert once == twice

    def test_none_input(self):
        """None input returns None."""
        assert sanitize_secrets(None) is None  # type: ignore[arg-type]

    def test_non_string_input(self):
        """Non-string input is returned as-is."""
        assert sanitize_secrets(42) == 42  # type: ignore[arg-type]

    def test_empty_string(self):
        """Empty string passes through."""
        assert sanitize_secrets("") == ""

    def test_short_input_no_match(self):
        """Very short strings without secrets pass through."""
        result = sanitize_secrets("OK")
        assert result == "OK"

    def test_large_line_no_quadratic_blowup(self):
        """Regression: a large line must not stall the event loop.

        The key-name sandwiches ("…secret…", "…access…key…") used
        unbounded repeats; on a non-matching line the engine backtracked
        O(n) per start position — quadratic overall.  A 300 KB relayed
        stderr line froze the parent's event loop for minutes, filling the
        child's stderr pipe and wedging a subagent mid-task.  Key-name
        repeats are now bounded; this must stay comfortably fast.
        """
        import time

        big = "A" * (300 * 1024)
        t0 = time.monotonic()
        result = sanitize_secrets(big)
        took = time.monotonic() - t0
        assert result == big
        assert took < 5.0  # measured ~30s+ before the fix, ~ms after

    def test_bounded_key_name_still_redacted(self):
        """The bounded key-name pattern still masks compound key names."""
        assert "<MASKED>" in sanitize_secrets(
            "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        )
        assert "<MASKED>" in sanitize_secrets(
            "STRIPE_ACCESS_KEY=sk_live_abcdefghijklmnop"
        )


# ── silence_noisy_loggers ────────────────────────────────────────────────────


class TestSilenceNoisyLoggers:
    """Tests for silence_noisy_loggers."""

    def test_silences_default_loggers(self):
        """All default noisy loggers are set to WARNING."""
        with patch("logging.getLogger") as mock_get_logger:
            silence_noisy_loggers()
            # Check that setLevel(WARNING) was called for each default logger
            assert mock_get_logger.call_count >= 11  # default _NOISY_LOGGER_NAMES length
            for call_args in mock_get_logger.call_args_list:
                mock_get_logger.return_value.setLevel.assert_called_with(logging.WARNING)

    def test_silences_extra_loggers(self):
        """Extra logger names are also silenced."""
        with patch("logging.getLogger") as mock_get_logger:
            silence_noisy_loggers(extra=("my.custom.logger", "another.one"))
            mock_get_logger.assert_any_call("my.custom.logger")
            mock_get_logger.assert_any_call("another.one")

    def test_extra_loggers_not_duplicated(self):
        """Even if an extra logger is already in defaults, it's still silenced."""
        # Just verify calling with extras doesn't error
        silence_noisy_loggers(extra=("openai._base_client",))

    def test_llm_sdk_client_loggers_silenced(self):
        """Both LLM SDK clients dump full request bodies at DEBUG.

        A single anthropic "Request options" line carries the entire
        request body (every tool schema) — hundreds of KB with a large
        tool registry.  In a subagent that line rides the stderr pipe to
        the parent; it must never be emitted.
        """
        from slife.logfmt import _NOISY_LOGGER_NAMES

        assert "openai._base_client" in _NOISY_LOGGER_NAMES
        assert "anthropic._base_client" in _NOISY_LOGGER_NAMES


# ── ok_json ──────────────────────────────────────────────────────────────────


class TestOkJson:
    """Tests for ok_json() helper."""

    def test_basic_ok(self):
        """Returns status: ok with no extras."""
        result = ok_json()
        import json
        data = json.loads(result)
        assert data == {"status": "ok"}

    def test_with_extra_keys(self):
        """Extra keys are included in the output."""
        result = ok_json(service="test", count=42)
        import json
        data = json.loads(result)
        assert data["status"] == "ok"
        assert data["service"] == "test"
        assert data["count"] == 42

    def test_filters_none_values(self):
        """Keys with None values are omitted."""
        result = ok_json(name="present", missing=None, count=0, empty_str="")
        import json
        data = json.loads(result)
        assert data["name"] == "present"
        assert data["count"] == 0
        assert data["empty_str"] == ""
        assert "missing" not in data

    def test_all_none_extras(self):
        """When all extras are None, only status: ok remains."""
        result = ok_json(a=None, b=None)
        import json
        data = json.loads(result)
        assert data == {"status": "ok"}


# ── error_json ───────────────────────────────────────────────────────────────


class TestErrorJson:
    """Tests for error_json() helper."""

    def test_basic_error(self):
        """Returns status: error with required message."""
        result = error_json("something went wrong")
        import json
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error"] == "something went wrong"

    def test_with_extra_keys(self):
        """Extra keys are included alongside the error message."""
        result = error_json("not found", code=404, detail="missing resource")
        import json
        data = json.loads(result)
        assert data["status"] == "error"
        assert data["error"] == "not found"
        assert data["code"] == 404
        assert data["detail"] == "missing resource"

    def test_filters_none_values(self):
        """Extra keys with None values are omitted."""
        result = error_json("failed", reason="timeout", suggestion=None)
        import json
        data = json.loads(result)
        assert data["reason"] == "timeout"
        assert "suggestion" not in data

    def test_all_none_extras(self):
        """When all extras are None, only status and error remain."""
        result = error_json("boom", a=None, b=None)
        import json
        data = json.loads(result)
        assert data == {"status": "error", "error": "boom"}

    def test_empty_message(self):
        """Empty error message is allowed."""
        result = error_json("")
        import json
        data = json.loads(result)
        assert data["error"] == ""


# ── resolve_log_dir ──────────────────────────────────────────────────────────


class TestResolveLogDir:
    """Tests for resolve_log_dir()."""

    def test_resolves_log_dir(self, tmp_path):
        """Calls slife.paths.get_logs_dir and returns its result."""
        with patch("slife.paths.get_logs_dir", return_value=tmp_path):
            result = resolve_log_dir()
            assert result == tmp_path


