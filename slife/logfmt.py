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
import json
import logging
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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
    "aiosqlite",              # dumps full SQL with messages JSON at DEBUG
    "keyring.backend",        # probes 8 backends at startup (KWallet, SecretService, …)
    "win32ctypes.core.cffi",  # "Loaded cffi backend" — one-shot, not diagnostic
    "credstore",              # "backend already initialized" — noise on every import
    "mcp.client.sse",         # full JSONRPCResponse payloads at DEBUG
    "mcp.server.sse",         # full SessionMessage payloads at DEBUG
    "sse_starlette.sse",      # raw SSE chunk bytes at DEBUG (duplicates above)
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


async def drain_stderr(
    process,
    prefix: str,
    logger: logging.Logger,
    running_check=None,
) -> None:
    """Async task: read and log stderr from *process* with *prefix*.

    A thin convenience wrapper around :func:`read_stderr_lines`.  Every
    non-empty stderr line is logged at DEBUG with the given prefix, e.g.
    ``[subagent:foo]`` or ``[mosquitto]``.

    Args:
        process: ``asyncio.subprocess.Process`` or ``None``.
        prefix: String label to prepend to each line.
        logger: Logger to write to (DEBUG level).
        running_check: Optional ``() -> bool`` to stop draining early.
    """
    async for text in read_stderr_lines(process, running_check):
        logger.debug("[%s] %s", prefix, sanitize_secrets(text))


# ── Log directory resolution ──────────────────────────────────────────


def resolve_log_dir() -> Path:
    """Return the log directory.

    Dev mode: ``./logs/``.  Production: ``~/.slife/logs/``.
    """
    from slife.paths import get_logs_dir
    return get_logs_dir()


# ── JSON response helpers ─────────────────────────────────────────────


# ── Secret sanitization for stderr / log output ──────────────────────

# Patterns that look like API keys or bearer tokens in free-form text.
# These are deliberately conservative — they only match well-known
# prefixes and high-entropy strings that are almost certainly secrets.
_SECRET_PATTERNS: list[re.Pattern] = [
    # OpenAI / Anthropic / common API keys
    re.compile(r"\bsk-(?:ant|agent|proj|svcacct|admin|org)?[A-Za-z0-9_-]{20,}\b"),
    # GitHub personal access tokens
    re.compile(r"\bgh[psu]_[A-Za-z0-9]{20,}\b"),
    # Google OAuth access tokens
    re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}\b"),
    # Generic bearer tokens in Authorization headers
    re.compile(r"(?:Authorization|Bearer)\s+([A-Za-z0-9+/=._-]{20,})", re.IGNORECASE),
    # key=value patterns with secret-looking values
    re.compile(r"(?:api_key|apikey|api-key|secret|token|password)\s*[=:]\s*([^\s]{20,})", re.IGNORECASE),
    # Generic hex-ish tokens (32+ chars) — catches API keys without known prefixes
    re.compile(r"\b[A-Za-z0-9]{32,}\b"),
    # Base64-like blobs (32+ chars with +/=)
    re.compile(r"\b[A-Za-z0-9+/=]{32,}\b"),
]

_MASKED = "<MASKED>"


def sanitize_secrets(text: str) -> str:
    """Mask API key / token patterns from *text*.

    Used for log output and tool-result sanitisation before the text
    reaches the LLM context.  Replaces matched secret substrings with
    ``<MASKED>``.  Idempotent — safe to call on already-masked text.

    >>> sanitize_secrets("Authorization: Bearer sk-ant-api03-abc123...")
    'Authorization: <MASKED>'
    >>> sanitize_secrets("DEEPSEEK_API_KEY=sk-abcdef1234567890abcdef1234567890ab")
    '<MASKED>'
    >>> sanitize_secrets("normal log message")
    'normal log message'
    """
    if not text or not isinstance(text, str):
        return text
    for pat in _SECRET_PATTERNS:
        text = pat.sub(_MASKED, text)
    return text


def ok_json(**extra: object) -> str:
    """Render ``{"status": "ok", ...}`` — the standard success envelope.

    Keys with ``None`` values are omitted.  Output is indented and safe
    for display in TUI tool-result widgets.
    """
    payload: dict = {"status": "ok", **{k: v for k, v in extra.items() if v is not None}}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def error_json(message: str, **extra: object) -> str:
    """Render ``{"status": "error", "error": <message>, ...}``.

    The *message* parameter is required — every error must explain itself.
    Extra keys with ``None`` values are omitted.
    """
    payload: dict = {
        "status": "error",
        "error": message,
        **{k: v for k, v in extra.items() if v is not None},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)
