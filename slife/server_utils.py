"""Shared utilities for Slife MCP server entry points."""

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


def setup_server_logging(
    service_name: str,
    log_dir: Path = Path("logs"),
) -> Path:
    """Configure shared logging for a server process (stderr + file).

    - Adopts ``SLIFE_SESSION_ID`` from the parent environment.
    - stderr: DEBUG+ with plain formatter (parent adds session/request context).
    - File:    DEBUG+ with ``SessionFormatter``, one per session.
    - Silences httpx/httpcore/openai/asyncio and FastMCP noise.

    Returns the log file path.
    """
    _sid = os.environ.get("SLIFE_SESSION_ID", "")
    if _sid:
        set_session_id(_sid)

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
    log_path = log_dir / f"{ts}_{service_name}.log"
    _file = logging.FileHandler(log_path, encoding="utf-8")
    _file.setLevel(logging.DEBUG)
    _file.setFormatter(SessionFormatter(FILE_LOG_FORMAT))
    _root.addHandler(_file)

    silence_noisy_loggers(extra=_FASTMCP_NOISE)

    return log_path
