"""Structured logging for mcp_plugin.

Provides session/request correlation (``SessionFormatter``), subprocess
stderr relay helpers, secret sanitization, JSON response envelopes, and
root-logging configuration.  mcp-plugin is a built-in slife plugin, so its
log *directory* resolves like every built-in plugin's — see
:func:`resolve_log_dir`.
"""

import asyncio
import contextvars
import json
import logging
import secrets
from datetime import datetime
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
    "%(asctime)s [%(levelname)-5s] %(name)-32s [s=%(sid)s] [r=%(rid)s] | %(message)s"
)

# Third-party loggers that should be silenced to WARNING to avoid
# flooding the log file with HTTP request/response bodies.
_NOISY_LOGGER_NAMES = (
    "openai._base_client",
    # Same hazard as openai._base_client: dumps the full request body
    # (every tool schema) at DEBUG — a single "Request options" line is
    # hundreds of KB with a large tool registry.  In a subagent that line
    # rides the stderr pipe to the parent and overruns the relay reader.
    "anthropic._base_client",
    "httpcore.connection",
    "httpcore.http11",
    "httpcore.proxy",
    "httpcore._synchronization",
    "httpx",
    # The httpx2/httpcore2 generation the anthropic/openai/mcp SDKs are
    # built on logs under its own namespace — same DEBUG dump hazard.
    "httpcore2.connection",
    "httpcore2.http11",
    "httpcore2.proxy",
    "httpcore2._synchronization",
    "httpx2",
    "asyncio",
    "urllib3",
    "aiosqlite",              # dumps full SQL with messages JSON at DEBUG
    "keyring.backend",        # probes 8 backends at startup (KWallet, SecretService, …)
    "win32ctypes.core.cffi",  # "Loaded cffi backend" — one-shot, not diagnostic
    "credstore",              # "backend already initialized" — noise on every import
)


def silence_noisy_loggers(extra: tuple[str, ...] = ()) -> None:
    """Suppress DEBUG output from common third-party loggers.

    These libraries dump full request/response bodies at DEBUG level,
    making log files unreadable. mcp_plugin's own DEBUG output is sufficient.

    Args:
        extra: Additional logger names to silence (e.g. FastMCP internals).
    """
    for name in (*_NOISY_LOGGER_NAMES, *extra):
        logging.getLogger(name).setLevel(logging.WARNING)

# ── Session ID ──────────────────────────────────────────────────────────


def init_session_id() -> str:
    """Generate and set a session ID. Call once at startup."""
    sid = secrets.token_hex(6)
    _session_id.set(sid)
    return sid


def set_session_id(sid: str) -> None:
    """Adopt an existing session ID (e.g. from an ``SLIFE_SESSION_ID`` env var)."""
    _session_id.set(sid)


def get_session_id() -> str:
    """Return the current session ID, or '--------' if not initialized."""
    return _session_id.get() or "--------"


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
        """Return timestamp with milliseconds, e.g. 10:30:15.123.

        Local time — matches the session log filename (built with
        ``datetime.now()``) so file content and file name stay consistent.
        """
        dt = datetime.fromtimestamp(record.created)
        if datefmt:
            s = dt.strftime(datefmt)
        else:
            s = dt.strftime("%Y-%m-%d %H:%M:%S")
        return f"{s}.{int(record.msecs):03d}"


# ── Stderr relay helpers ─────────────────────────────────────────────

logger = logging.getLogger(__name__)

#: Per-line cap for subprocess stderr relays.  The StreamReader default is
#: 64 KB; a single line beyond the limit makes ``readline()`` raise.  1 MB
#: relays even enormous tracebacks while capping in-memory buffering.
_STDERR_LIMIT = 1024 * 1024

#: Relayed lines are truncated to this many characters.  The relay is
#: diagnostic — the child's own log file keeps the full line.  The cap
#: bounds the per-line cost of ``sanitize_secrets`` on the caller's event
#: loop and keeps multi-hundred-KB dumps out of the session log.
_MAX_RELAYED_CHARS = 16 * 1024


async def _discard_overlong_line(stderr) -> int:
    """Drop the remainder of a line that overran the reader limit.

    ``readline()`` raises ``ValueError`` (``LimitOverrunError``) after
    discarding the buffered head of the over-long line — but the tail is
    still in flight and must be consumed up to and including its newline,
    otherwise the next ``readline()`` returns that tail as if it were a
    fresh line (and the consumer's line accounting silently corrupts).

    Returns the number of discarded tail bytes (lower bound — the head
    size is unknown once ``readline`` cleared its buffer).
    """
    dropped = 0
    while True:
        try:
            rest = await stderr.readline()
        except ValueError:
            # The remainder alone still exceeds the limit — readline raised
            # again after discarding another head-sized chunk; keep going.
            continue
        if not rest:
            break  # EOF inside the over-long line
        dropped += len(rest)
        if rest.endswith(b"\n"):
            break  # the newline terminating the over-long line
    return dropped


async def read_stderr_lines(process, running_check=None):
    """Async generator yielding decoded stderr lines from a subprocess.

    An over-long line (beyond :data:`_STDERR_LIMIT`) is discarded with a
    warning instead of killing the relay: ``readline()`` raises on it, and
    a dead relay orphans the child's stderr pipe — the pipe fills, the
    child blocks on its next log write, and a consumer hangs forever.

    Args:
        process: An ``asyncio.subprocess.Process`` with a ``.stderr`` pipe.
        running_check: Optional callable returning bool — when False, the
                       generator stops.  Pass ``None`` to drain until EOF.

    Yields:
        Decoded, rstripped, non-empty stderr lines.
    """
    if not process or not process.stderr:
        return
    stderr = process.stderr
    # Raise the StreamReader limit (a real StreamReader; no-op on mocks).
    try:
        stderr._limit = max(stderr._limit, _STDERR_LIMIT)  # type: ignore[attr-defined]
    except (AttributeError, TypeError):
        pass
    try:
        while running_check is None or running_check():
            try:
                line = await stderr.readline()
            except ValueError:
                # LimitOverrunError — an over-long line.  Discard its
                # remainder and keep relaying; never die here.
                dropped = await _discard_overlong_line(stderr)
                logger.warning(
                    "stderr_line_overlong_discarded min_bytes=%d", dropped,
                )
                continue
            if not line:
                break
            text = line.decode("utf-8", errors="replace").rstrip()
            if not text:
                continue
            if len(text) > _MAX_RELAYED_CHARS:
                yield (
                    text[:_MAX_RELAYED_CHARS]
                    + f"… [truncated: {len(line)} bytes total]"
                )
            else:
                yield text
    except asyncio.CancelledError:
        pass
    except Exception:
        # The relay must never die silently — a dead stderr relay wedges
        # the child process (see docstring).  Log why it stopped.
        logger.warning("stderr_relay_failed", exc_info=True)


# ── Log directory resolution ──────────────────────────────────────────


def resolve_log_dir() -> Path:
    """Return the log directory for mcp_plugin — the slife data-dir logs.

    Same resolution as every built-in plugin server: ``SLIFE_LOG_DIR`` when
    the host (slife) exported it (the per-session log then lands next to the
    main session log), else ``<data_dir>/logs`` (``~/.slife/logs`` in
    production).  File naming is unchanged (``{ts}_{agent}_{service}.log``) —
    mcp_plugin keeps its own plugin-named log file.
    """
    from slife.logfmt import resolve_log_dir as _resolve_slife_log_dir

    return _resolve_slife_log_dir()


# ── JSON response helpers ─────────────────────────────────────────────


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


# ── Secret sanitization for stderr / log output ──────────────────────
# Single source of truth: slife.logfmt.sanitize_secrets.  An independent
# redaction table here would silently diverge (a regex fixed in one path
# hanging the other); re-export the shared implementation so every
# sanitizing caller — logfmt (tool results) and the gateway stderr relay —
# redacts identically.  ``re`` stays imported for the other uses below.
# Private-alias + redefinition marks this as a deliberate re-export to both
# ruff (F401) and pyright (reportUnusedImport) — ``# noqa`` alone only wins
# against ruff.
from slife.logfmt import sanitize_secrets as _sanitize_secrets

sanitize_secrets = _sanitize_secrets


# ── Shared root-logging setup ──────────────────────────────────────────


def configure_root_logging(
    stderr_level: int = logging.DEBUG,
    stderr_format: logging.Formatter | None = None,
    file_path: Path | None = None,
    file_level: int = logging.DEBUG,
    file_format: logging.Formatter | None = None,
    *,
    clear_existing: bool = False,
) -> logging.Handler:
    """Configure the root logger with stderr and optional file handlers.

    Args:
        stderr_level: Log level for the stderr stream handler.
        stderr_format: Formatter for stderr output.
        file_path: If given, a ``FileHandler`` is added writing to this path.
        file_level: Log level for the file handler.
        file_format: Formatter for the file handler.
        clear_existing: Remove existing root handlers before adding new ones.

    Returns:
        The stderr ``StreamHandler`` (for callers that need a reference).
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    if clear_existing:
        root.handlers.clear()

    if stderr_format is None:
        stderr_format = logging.Formatter("%(message)s")

    stderr_handler = logging.StreamHandler()
    stderr_handler.setLevel(stderr_level)
    stderr_handler.setFormatter(stderr_format)
    root.addHandler(stderr_handler)

    if file_path is not None:
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(file_path, encoding="utf-8")
        fh.setLevel(file_level)
        fh.setFormatter(file_format or SessionFormatter(FILE_LOG_FORMAT))
        root.addHandler(fh)

    silence_noisy_loggers()
    return stderr_handler