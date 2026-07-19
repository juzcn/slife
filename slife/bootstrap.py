"""Startup bootstrap — logging setup and session initialization.

Extracted from ``slife/__init__.py`` to keep the package entry point
focused on ``main()``.
"""

import logging
from datetime import datetime
from pathlib import Path

from slife.logfmt import SessionFormatter, FILE_LOG_FORMAT, silence_noisy_loggers

logger = logging.getLogger("slife")

LOG_DIR = Path("logs")


def _session_log_path(agent_id: str = "slife") -> Path:
    """Generate a timestamped log file path for this session.

    Follows the same naming convention as sub-agent logs:
    ``logs/YYYYMMDD_HHMMSS_slife_<agent_id>.log``.
    """
    LOG_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return LOG_DIR / f"{ts}_slife_{agent_id}.log"


def setup_logging(
    agent_id: str = "slife",
    level: int = logging.DEBUG,
) -> tuple[Path, logging.Handler]:
    """Configure logging to both console and file.

    Console: WARNING+ only — keeps the terminal clean before Textual's
             alternate screen activates and during TUI runtime.
    File:    DEBUG+ with timestamps, session/request IDs for troubleshooting.
    Each session writes to a new ``logs/YYYYMMDD_HHMMSS_slife_<agent_id>.log`` file.

    Returns:
        (log_path, console_handler) — console is already at WARNING;
        detailed output goes to the per-session log file.
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
            return _session_log_path(agent_id), console

    root.setLevel(logging.DEBUG)

    # Console handler — WARNING from the start to prevent terminal flash
    # before Textual's alternate screen takes over.  Any log output on
    # stderr between startup and TUI init is briefly visible to the user
    # and disappears when the alternate screen activates, creating a
    # jarring "flash" of text.
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING)
    console.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(console)

    # File handler — detailed format with session/request IDs, one per session
    log_path = _session_log_path(agent_id)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(level)
    file_handler.setFormatter(SessionFormatter(FILE_LOG_FORMAT))
    root.addHandler(file_handler)

    # Silence noisy third-party HTTP/logging libraries.
    silence_noisy_loggers()

    return log_path, console
