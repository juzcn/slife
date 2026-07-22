"""Shared utilities for Slife MCP server entry points.

Provides consistent logging setup across all child-process servers:
slife-mcp, slife-memory, slife-wechat, and slife-subagent.
"""

import json
import logging
import os
import socket
import sys
from datetime import datetime
from pathlib import Path

from slife.logfmt import (
    SessionFormatter,
    FILE_LOG_FORMAT,
    resolve_log_dir,
    set_session_id,
    silence_noisy_loggers,
)

logger = logging.getLogger(__name__)

# FastMCP-specific loggers that should also be silenced.
_FASTMCP_NOISE = ("mcp.server.lowlevel.server", "fastmcp")


# ── Logging setup / shutdown ────────────────────────────────────────────


def setup_server_logging(
    service_name: str,
    log_dir: Path | None = None,
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
    if log_dir is None:
        log_dir = resolve_log_dir()

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

    log_dir.mkdir(parents=True, exist_ok=True)
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


# ── Port binding ──────────────────────────────────────────────────────


def bind_free_port(host: str = "127.0.0.1") -> tuple[socket.socket, int]:
    """Bind a socket to *host*:0 and return ``(socket, port)``.

    The OS assigns a free port.  The returned socket is pre-bound and
    can be passed directly to FastMCP via ``sockets=[sock]`` — no race
    between port discovery and server startup.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, 0))
    port = sock.getsockname()[1]
    return sock, port


def signal_port(port: int) -> None:
    """Write the port to stdout as a JSON line and close stdout.

    The parent ``MCPWrapperProcess`` reads this line to discover the
    dynamically-assigned port before connecting via Streamable HTTP.
    """
    line = json.dumps({"port": port}, ensure_ascii=False)
    sys.stdout.buffer.write((line + "\n").encode("utf-8"))
    sys.stdout.buffer.flush()
    sys.stdout.close()


# ── JSON response helpers (re-exported from logfmt) ────────────────────
# These were moved to slife.logfmt for better cohesion.
# Kept here for backward compatibility.

from slife.logfmt import ok_json, error_json  # noqa: F401
