"""Structured logging for local-embed.

Standalone minimal setup — local-embed must not import slife.  Writes one
human-readable line per event to stderr (the harness / terminal captures
it) with a stable ``key=value`` suffix so failures are greppable, exactly
like slife's logfmt conventions.  The ``silence_noisy_loggers`` helper
drops the httpcore/httpx/uvicorn access noise so a request stream never
drowns the meaningful events.

File logging: when slife spawns local-embed it exports ``SLIFE_LOG_DIR``
(plus ``SLIFE_AGENT_NAME`` / ``SLIFE_SESSION_ID``), so the per-session log
file lands next to the main session log with the same
``{ts}_{agent}_{service}.log`` naming.  Standalone (no ``SLIFE_LOG_DIR``),
logs go to ``~/.local-embed/logs/``.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

#: Loggers whose DEBUG/INFO chatter would drown the meaningful events.
_NOISY = (
    "httpx",
    "httpcore",
    "uvicorn.access",
    "mcp.server.lowlevel.server",
    "fastmcp",
)

#: File format mirrors slife's FILE_LOG_FORMAT (session/request correlation).
_FILE_LOG_FORMAT = (
    "%(asctime)s [%(levelname)-5s] %(name)-32s [s=%(sid)s] [r=%(rid)s] | %(message)s"
)

#: Module-level session/request ids — adopted from the host when spawned by
#: slife, else generated at first use.
_session_id: str = ""
_request_id: str = ""


def resolve_log_dir() -> Path:
    """Return the directory for local-embed's per-session log file.

    ``SLIFE_LOG_DIR`` when the host (slife) exported it, else the standalone
    default ``~/.local-embed/logs`` (mirrors mcp-plugin).
    """
    override = os.getenv("SLIFE_LOG_DIR")
    if override:
        return Path(override)
    return Path.home() / ".local-embed" / "logs"


class _SessionFormatter(logging.Formatter):
    """Formatter that injects session_id and request_id into log records.

    Reads from module-level state (process-wide, single session).  The
    format string must include %(sid)s and %(rid)s placeholders.
    """

    def format(self, record: logging.LogRecord) -> str:
        record.sid = _session_id or "--------"
        record.rid = _request_id or "--------"
        return super().format(record)


def setup_logging(level: int = logging.INFO, service_name: str = "local-embed") -> None:
    """Configure root logging for the local-embed process (stderr + file).

    - stderr: DEBUG+ with timestamped format (the host's wrapper relays it).
    - file:   ``<log_dir>/{ts}_{agent}_{service_name}.log`` with
      session/request correlation, one per session.
    - Adopts ``SLIFE_SESSION_ID`` / ``SLIFE_AGENT_NAME`` from the parent env.

    Safe to call more than once — replaces handlers, keeps level as the
    more verbose of the current and requested.
    """
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
        try:
            h.close()
        except Exception:
            pass

    _sid = os.environ.get("SLIFE_SESSION_ID", "")
    if _sid:
        global _session_id
        _session_id = _sid

    stderr_handler = logging.StreamHandler()
    stderr_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        )
    )
    root.addHandler(stderr_handler)

    # Per-session file log — slife naming ({ts}_{agent}_{service}.log) so a
    # session's files stay grouped regardless of who spawned the process.
    agent = os.environ.get("SLIFE_AGENT_NAME", "slife")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_dir = resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"{ts}_{agent}_{service_name}.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(_SessionFormatter(_FILE_LOG_FORMAT))
    root.addHandler(file_handler)

    root.setLevel(level)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)


def silence_noisy_loggers() -> None:
    """Raise the log level of noisy library loggers (idempotent)."""
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.WARNING)
