"""Shared utilities for Slife MCP server entry points.

Provides consistent logging setup across all child-process servers:
slife-mcp, slife-memory, slife-wechat, and slife-subagent.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from slife.logfmt import (
    SessionFormatter,
    FILE_LOG_FORMAT,
    set_session_id,
    silence_noisy_loggers,
)

logger = logging.getLogger(__name__)

# FastMCP-specific loggers that should also be silenced.
_FASTMCP_NOISE = ("mcp.server.lowlevel.server", "fastmcp")


# ── Logging setup / shutdown ────────────────────────────────────────────


def setup_server_logging(
    service_name: str,
    log_dir: Path = Path("logs"),
) -> Path:
    """Configure shared logging for a server process (stderr + file).

    - Adopts ``SLIFE_SESSION_ID`` and ``SLIFE_AGENT_ID`` from the parent env.
    - stderr: DEBUG+ with plain formatter (parent adds session/request context).
    - File:    DEBUG+ with ``SessionFormatter``, one per session.
    - File name includes *agent_id* to avoid conflicts when multiple agents
      run in the same directory (e.g. ``logs/..._slife_mcp.log``).
    - Silences httpx/httpcore/openai/asyncio and FastMCP noise.

    Returns the log file path.
    """
    _sid = os.environ.get("SLIFE_SESSION_ID", "")
    if _sid:
        set_session_id(_sid)

    _agent_id = os.environ.get("SLIFE_AGENT_ID", "slife")

    _stderr_fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    _root = logging.getLogger()
    _root.setLevel(logging.DEBUG)

    # Remove existing handlers to avoid duplicates on module reload
    _root.handlers.clear()

    _stderr = logging.StreamHandler(sys.stderr)
    _stderr.setLevel(logging.DEBUG)
    _stderr.setFormatter(_stderr_fmt)
    _root.addHandler(_stderr)

    log_dir.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"{ts}_{_agent_id}_{service_name}.log"
    _file = logging.FileHandler(log_path, encoding="utf-8")
    _file.setLevel(logging.DEBUG)
    _file.setFormatter(SessionFormatter(FILE_LOG_FORMAT))
    _root.addHandler(_file)

    silence_noisy_loggers(extra=_FASTMCP_NOISE)

    return log_path


def shutdown_server_logging(extra_logger_names: tuple[str, ...] = ()) -> None:
    """Close and remove all root handlers, releasing Windows file locks.

    Call this before process exit to ensure the log file can be rotated
    or inspected by the parent process.  Idempotent — safe to call even
    if ``setup_server_logging`` was never called.
    """
    _root = logging.getLogger()
    for handler in list(_root.handlers):
        try:
            handler.flush()
            handler.close()
        except Exception:
            pass
    _root.handlers.clear()

    # Also silence any named loggers whose handlers weren't on root
    for name in extra_logger_names:
        child = logging.getLogger(name)
        for handler in list(child.handlers):
            try:
                handler.flush()
                handler.close()
            except Exception:
                pass
        child.handlers.clear()


# ── JSON response helpers ──────────────────────────────────────────────


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
