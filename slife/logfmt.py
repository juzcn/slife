"""Lightweight structured logging — session/request correlation and timing.

Provides:
  - Session ID shared across slife + plugins (via env var)
  - Request ID for grouping log lines per user message
  - SessionFormatter with millisecond timestamps
  - contextvars-based — async-safe, no global mutation
  - read_stderr_lines — shared async generator for subprocess stderr

Log message convention: ``event_name key1=value1 key2=value2 …``

  - Event name: snake_case, past-tense for completions (``tool_done``),
    present-tense for state (``mcp_connected``).
  - Keys: no spaces around ``=``.  Values with embedded spaces or special
    characters are ``%s``-formatted.
  - Timing: use :func:`elapsed` context manager — it appends
    ``took_ms=<N>`` automatically when the block exits.
  - Errors: ``logger.exception("event_failed key=value …")`` so the
    traceback is captured alongside structured context.
  - Level guide: ``debug`` for per-request detail, ``info`` for lifecycle
    milestones, ``warning`` for recoverable problems, ``error`` for
    hard failures (use ``exception()`` to include the traceback).

Usage:
    from slife.logfmt import init_session_id, request_scope, SessionFormatter

    sid = init_session_id()
    fmt = SessionFormatter("%(asctime)s … [s=%(sid)s] [r=%(rid)s] …")

    with request_scope("user: hello"):
        logger.info("something")  # automatically tagged with request id
"""

import asyncio
import contextvars
import json
import logging
import re
import secrets
import time
from contextlib import contextmanager
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
    # Same hazard from the httpx2/httpcore2 generation the anthropic /
    # openai / mcp SDKs are built on — they log under their own namespace.
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

logger = logging.getLogger(__name__)

#: Per-line cap for subprocess stderr relays.  The StreamReader default is
#: 64 KB; a single line beyond the limit makes ``readline()`` raise.  1 MB
#: relays even enormous tracebacks while capping in-memory buffering.
_STDERR_LIMIT = 1024 * 1024

#: Relayed lines are truncated to this many characters.  The relay is
#: diagnostic — the child's own log file keeps the full line.  The cap
#: bounds the per-line cost of ``sanitize_secrets`` on the parent's event
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

    Used by MCPWrapperProcess, BrokerManager, and SubagentProcess to
    avoid duplicating the readline/decode/running-check loop.

    An over-long line (beyond :data:`_STDERR_LIMIT`) is discarded with a
    warning instead of killing the relay: ``readline()`` raises on it, and
    a dead relay orphans the child's stderr pipe — the pipe fills, the
    child blocks on its next log write, and a subagent hangs mid-task with
    its task stuck "pending" forever.

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

    ``SLIFE_LOG_DIR`` when set (the main process exports it for plugin
    children); else dev mode ``./logs/``, production ``~/.slife/logs/``.
    """
    from slife.paths import get_logs_dir
    return get_logs_dir()


# ── JSON response helpers ─────────────────────────────────────────────


# ── Secret sanitization for stderr / log output ──────────────────────

# Credential patterns — well-known prefixes + key=value pairs.
# No generic heuristics; secrets belong in the credential store.
_SECRET_PATTERNS: list[re.Pattern] = [
    # ── Well-known AI / cloud provider prefixes ─────────────────────
    # OpenAI / Anthropic / DeepSeek / Azure
    re.compile(r"\bsk-(?:ant|agent|proj|svcacct|admin|or|org)?[A-Za-z0-9_-]{20,}\b"),
    # Stripe secret/restricted keys (underscore form: sk_live_…, sk_test_…)
    re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{10,}\b"),
    # Groq
    re.compile(r"\bgsk_[A-Za-z0-9]{20,}\b"),
    # HuggingFace
    re.compile(r"\bhf_[A-Za-z0-9]{20,}\b"),
    # Replicate
    re.compile(r"\br8_[A-Za-z0-9]{20,}\b"),
    # Perplexity
    re.compile(r"\bpplx-[A-Za-z0-9]{20,}\b"),
    # xAI / Grok
    re.compile(r"\bxai-[A-Za-z0-9]{20,}\b"),
    # Google AI / Gemini (AIzaSy...)
    re.compile(r"\bAIza[A-Za-z0-9_-]{30,}\b"),
    # Fireworks AI
    re.compile(r"\bfw_[A-Za-z0-9]{20,}\b"),
    # NVIDIA
    re.compile(r"\bnvapi-[A-Za-z0-9_-]{20,}\b"),
    # Baidu Qianfan (bce-v3/ALTAK-...)
    re.compile(r"\bbce-v3/ALTAK-[A-Za-z0-9/_-]{20,}\b"),
    # ── Generic service tokens ──────────────────────────────────────
    # GitHub classic PATs (ghp_/ghs_/ghu_) and fine-grained PATs (github_pat_)
    re.compile(r"\bgh[psu]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    # Google OAuth
    re.compile(r"\bya29\.[A-Za-z0-9._-]{20,}\b"),
    # PyPI
    re.compile(r"\bpypi-[A-Za-z0-9._-]{20,}\b"),
    # ── Header / key=value patterns ─────────────────────────────────
    # Authorization: Bearer|Basic|Token <credential> (and bare Bearer/Token).
    # The old pattern required the keyword directly before the token, so
    # "Authorization: Basic dXNlcjpwYXNz" and "Authorization: Token …"
    # slipped through unmasked.  The value must contain a digit or one of
    # +/ = . _ - — "Token consumption" (prose, e.g. a tool description) is
    # not a credential, but "Bearer sk-ant-api03-…" / "Token abc123…" still
    # are.  Lower floor (8) is fine here — the header context is specific.
    re.compile(
        r"(?:Authorization\s*:\s*)?(?:Basic|Bearer|Token)\s+"
        r"((?=[A-Za-z0-9+/=._-]*[0-9+/=._-])[A-Za-z0-9+/=._-]{8,})",
        re.IGNORECASE,
    ),
    # key=value pairs — whole words (api_key, token, password, …) and
    # compound names (AWS_SECRET_ACCESS_KEY, STRIPE_SECRET_KEY,
    # aws_access_key_id).  Value floor 6 chars so short secrets mask too.
    # The value class excludes quotes/braces/brackets so JSON like
    # {"api_key": "sk-…"} masks just the value instead of swallowing the
    # closing braces and corrupting the line.
    # The key-name sandwiches bound their repeats ({0,64}, not *): with an
    # unbounded "[A-Za-z0-9_]*secret…" the engine backtracks O(n) per start
    # position on lines without a match — quadratic overall, which froze the
    # parent's event loop for minutes on a 300 KB relayed stderr line.  Key
    # names are short; 64 chars on each side is generous.
    re.compile(
        r"(?:api[_-]?key|apikey|token|password|auth[_-]?token|"
        r"[A-Za-z0-9_]{0,64}secret[A-Za-z0-9_]{0,64}|"
        r"[A-Za-z0-9_]{0,64}access[_-]?key[A-Za-z0-9_]{0,64})\s*[=:]\s*"
        r"([^\s\"'{}()\[\];,]{6,})",
        re.IGNORECASE,
    ),
]

_MASKED = "<MASKED>"

# Connection-string credentials: scheme://user:password@host — mask just the
# password, keeping the rest of the URL readable.  The password class excludes
# `/` so "https://host:8080/user@domain" (a port + path, NOT credentials) is
# no longer corrupted by swallowing "8080/user".
_URL_CREDENTIAL_PATTERN = re.compile(r"(://[^/\s@]*:)[^@\s/]+(@)")


def sanitize_secrets(text: str) -> str:
    """Mask credentials from *text*.

    Catches well-known API key prefixes (``sk-``, ``ghp_``, ``ya29.``,
    ``pypi-``), ``Authorization: Bearer`` tokens, and key=value pairs
    with credential-like names (``api_key``, ``secret``, ``token``,
    ``password``, ``auth_token``).

    No generic hex/blob heuristics — secrets belong in the credential store.

    >>> sanitize_secrets("Authorization: Bearer sk-ant-api03-abc123...")
    'Authorization: <MASKED>'
    >>> sanitize_secrets("DEEPSEEK_API_KEY=sk-abc123...")
    '<MASKED>'
    """
    if not text or not isinstance(text, str):
        return text

    for pat in _SECRET_PATTERNS:
        text = pat.sub(_MASKED, text)

    # Mask credentials embedded in connection-string URLs (scheme://user:pass@host).
    text = _URL_CREDENTIAL_PATTERN.sub(r"\1" + _MASKED + r"\2", text)

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

    Used by both the main harness (:func:`slife.bootstrap.setup_logging`)
    and plugin servers (:func:`slife.server_utils.setup_server_logging`).

    Logs never reach the user terminal: the main harness runs its stderr
    handler at ``CRITICAL + 1`` (a no-op) so the terminal belongs entirely
    to the TUI; plugin/subagent paths run it at DEBUG because their stderr
    is a diagnostic pipe to the parent, not a user terminal.

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
