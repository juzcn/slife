"""Startup bootstrap — logging setup and session initialization.

Extracted from ``slife/__init__.py`` to keep the package entry point
focused on ``main()``.
"""

import logging
import os
from datetime import datetime
from pathlib import Path

from slife.logfmt import init_session_id, SessionFormatter, FILE_LOG_FORMAT, silence_noisy_loggers

logger = logging.getLogger("slife")

LOG_DIR = Path("logs")


def _session_log_path(agent_name: str | None = None) -> Path:
    """Generate a timestamped log file path for this session.

    Follows the same naming convention as sub-agent logs:
    ``logs/slife_<name>_YYYYMMDD_HHMMSS.log``.
    """
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = agent_name or "main"
    return LOG_DIR / f"slife_{name}_{ts}.log"


def setup_logging(
    agent_name: str | None = None,
    level: int = logging.DEBUG,
) -> tuple[Path, logging.Handler]:
    """Configure logging to both console and file.

    Console: INFO+ during startup (before TUI), WARNING+ during TUI runtime.
    File:    DEBUG+ with timestamps, session/request IDs for troubleshooting.
    Each session writes to a new ``logs/slife_<name>_YYYYMMDD_HHMMSS.log`` file.

    Returns:
        (log_path, console_handler) — caller should raise console to WARNING
        before starting the TUI to prevent display corruption.
    """
    root = logging.getLogger()

    # Dedup: skip if handlers already set up (e.g. tests calling main() repeatedly)
    if root.handlers:
        # Find the first StreamHandler that writes to stderr
        console = next(
            (h for h in root.handlers if isinstance(h, logging.StreamHandler)
             and getattr(h, 'stream', None) is not None),
            None
        )
        if console is not None:
            return _session_log_path(agent_name), console

    root.setLevel(logging.DEBUG)

    # Console handler — INFO during startup, caller raises to WARNING before TUI
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    # File handler — detailed format with session/request IDs, one per session
    log_path = _session_log_path(agent_name)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(SessionFormatter(FILE_LOG_FORMAT))
    root.addHandler(file_handler)

    # Silence noisy third-party HTTP/logging libraries.
    silence_noisy_loggers()

    return log_path, console
