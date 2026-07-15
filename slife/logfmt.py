"""Lightweight structured logging — session/request correlation and timing.

Provides:
  - Session ID shared across slife + slife_mcp (via env var)
  - Request ID for grouping log lines per user message
  - SessionFormatter with millisecond timestamps
  - contextvars-based — async-safe, no global mutation
  - read_stderr_lines — shared async generator for subprocess stderr

Usage:
    from slife.logfmt import init_session_id, request_scope, SessionFormatter

    sid = init_session_id()
    fmt = SessionFormatter("%(asctime)s ... %(sid)s ... %(rid)s ...")

    with request_scope("user: hello"):
        logger.info("something")  # automatically tagged with request id
"""

import asyncio
import contextvars
import logging
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone

# ── Context variables (async-safe) ──────────────────────────────────────

_session_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "session_id", default=""
)
_request_id: contextvars.ContextVar[str] = contextvars.ContextVar(
    "request_id", default=""
)

# Default format for file handlers. Console stays plain for TUI safety.
FILE_LOG_FORMAT = (
    "%(asctime)s [%(levelname)-5s] %(name)-24s [s=%(sid)s] [r=%(rid)s] | %(message)s"
)

# Third-party loggers that should be silenced to WARNING to avoid
# flooding the log file with HTTP request/response bodies.
_NOISY_LOGGER_NAMES = (
    "openai._base_client",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.proxy",
    "httpcore._synchronization",
    "httpx",
    "asyncio",
    "urllib3",
)


def silence_noisy_loggers(extra: tuple[str, ...] = ()) -> None:
    """Suppress DEBUG output from common third-party loggers.

    These libraries dump full request/response bodies at DEBUG level,
    making log files unreadable. slife's own DEBUG output is sufficient.

    Args:
        extra: Additional logger names to silence (e.g. FastMCP internals).
    """
    for name in (*_NOISY_LOGGER_NAMES, *extra):
        logging.getLogger(name).setLevel(logging.WARNING)

# ── Session ID ──────────────────────────────────────────────────────────


def init_session_id() -> str:
    """Generate and set a session ID. Call once at startup.

    Returns a 12-char hex string suitable for display and correlation.
    """
    sid = secrets.token_hex(6)
    _session_id.set(sid)
    return sid


def set_session_id(sid: str) -> None:
    """Adopt an existing session ID (e.g. from SLIFE_SESSION_ID env var)."""
    _session_id.set(sid)


def get_session_id() -> str:
    """Return the current session ID, or '--------' if not initialized."""
    return _session_id.get() or "--------"


# ── Request ID ──────────────────────────────────────────────────────────


@contextmanager
def request_scope(label: str = ""):
    """Set a request ID for all log calls within this block.

    Args:
        label: Optional human-readable label (e.g. user message preview).

    Yields:
        The generated 8-char hex request ID.
    """
    rid = secrets.token_hex(4)
    token = _request_id.set(rid)
    try:
        yield rid
    finally:
        _request_id.reset(token)


def get_request_id() -> str:
    """Return the current request ID, or '--------' if not in a scope."""
    return _request_id.get() or "--------"


# ── Formatter ───────────────────────────────────────────────────────────


class SessionFormatter(logging.Formatter):
    """Formatter that injects session_id and request_id into log records.

    Reads from contextvars — no constructor parameters needed.
    Adds milliseconds to timestamps via formatTime() override.

    The format string must include %(sid)s and %(rid)s placeholders.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.sid = _session_id.get() or "--------"
        record.rid = _request_id.get() or "--------"
        return super().format(record)

    def formatTime(
        self, record: logging.LogRecord, datefmt: str | None = None
    ) -> str:
        """Return timestamp with milliseconds, e.g. 10:30:15.123."""
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc)
        if datefmt:
            s = dt.strftime(datefmt)
        else:
            s = dt.strftime("%Y-%m-%d %H:%M:%S")
        return f"{s}.{int(record.msecs):03d}"


# ── Timing helper ───────────────────────────────────────────────────────


@contextmanager
def elapsed(
    operation: str,
    logger: logging.Logger,
    level: int = logging.DEBUG,
    **extra: object,
):
    """Log elapsed time when exiting the context.

    Logs: "<operation>_done <extra...> took_ms=<N>" at the given level.

    Usage:
        with elapsed("connect", logger, server="filesystem"):
            await conn.connect()
        # Logs: connect_done server=filesystem took_ms=123
    """
    t0 = time.monotonic()
    try:
        yield
    finally:
        ms = (time.monotonic() - t0) * 1000
        parts = [f"{k}={v}" for k, v in extra.items()]
        parts.append(f"took_ms={ms:.0f}")
        logger.log(level, "%s_done %s", operation, " ".join(parts))


# ── Stderr drain helper ───────────────────────────────────────────────


async def read_stderr_lines(process, running_check=None):
    """Async generator yielding decoded stderr lines from a subprocess.

    Used by MCPWrapperProcess, BrokerManager, and SubagentProcess to
    avoid duplicating the readline/decode/running-check loop.

    Args:
        process: An ``asyncio.subprocess.Process`` with a ``.stderr`` pipe.
        running_check: Optional callable returning bool — when False, the
                       generator stops.  Pass ``None`` to drain until EOF.

    Yields:
        Decoded, rstripped, non-empty stderr lines.
    """
    if not process or not process.stderr:
        return
    try:
        while running_check is None or running_check():
            line = await process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                yield text
    except (asyncio.CancelledError, Exception):
        pass
