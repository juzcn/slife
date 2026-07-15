"""Shared utilities for Slife MCP server entry points.

Used by slife_mcp.server and slife_memory.server to avoid duplicating
URL parsing, config reading, and logging setup across services.
"""

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from slife.logfmt import (
    SessionFormatter,
    FILE_LOG_FORMAT,
    set_session_id,
    silence_noisy_loggers,
)

logger = logging.getLogger(__name__)

# FastMCP-specific loggers that should also be silenced.
_FASTMCP_NOISE = ("mcp.server.lowlevel.server", "fastmcp")


def parse_url(url: str) -> tuple[str, int]:
    """Parse host and port from a wrapper URL like ``http://127.0.0.1:9876/mcp``."""
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 9876
    return host, port


def _nested_get(raw: dict, key_path: str) -> dict | None:
    """Walk a dotted key path into a nested dict. Returns the leaf dict or None."""
    parts = key_path.split(".")
    current: dict = raw
    for part in parts:
        current = current.get(part, {})
        if not isinstance(current, dict):
            return None
    return current


def read_host_port_from_config(
    config_path: str,
    config_key: str = "memory",
    default_port: int = 9877,
) -> tuple[str, int] | None:
    """Read a URL from a JSON5 config and return (host, port).

    ``config_key`` can be a simple key (``"memory"`` → ``raw["memory"]["url"]``)
    or a dotted path (``"mcp.wrapper"`` → ``raw["mcp"]["wrapper"]["url"]``).

    Returns ``None`` if the config is missing or incomplete.
    """
    try:
        import json5
        raw = json5.loads(Path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.error("config_not_found path=%s", config_path)
        return None
    except (ValueError, OSError) as e:
        logger.error("config_parse_error path=%s err=%s", config_path, e)
        return None

    section = _nested_get(raw, config_key)
    if section is None or not isinstance(section, dict):
        logger.error(
            "%s section not found in %s", config_key, config_path,
        )
        return None

    url = section.get("url", f"http://127.0.0.1:{default_port}/mcp")
    host, port = parse_url(str(url))
    logger.info("config_read section=%s host=%s port=%d", config_key, host, port)
    return host, port


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
