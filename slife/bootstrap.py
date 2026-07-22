"""Startup bootstrap — logging setup and session initialization.

Extracted from ``slife/__init__.py`` to keep the package entry point
focused on ``main()``.
"""

import logging
from datetime import datetime
from pathlib import Path

from slife.logfmt import SessionFormatter, FILE_LOG_FORMAT, resolve_log_dir

logger = logging.getLogger("slife")


def _session_log_path(agent_id: str = "slife") -> Path:
    """Generate a timestamped log file path for this session.

    Follows the same naming convention as sub-agent logs:
    ``logs/YYYYMMDD_HHMMSS_<agent_id>.log``.
    """
    log_dir = resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return log_dir / f"{ts}_{agent_id}.log"


def setup_logging(
    agent_id: str = "slife",
    level: int = logging.DEBUG,
) -> tuple[Path, logging.Handler]:
    """Configure logging to both console and file.

    Console: WARNING+ only — keeps the terminal clean before Textual's
             alternate screen activates and during TUI runtime.
    File:    DEBUG+ with timestamps, session/request IDs for troubleshooting.
    Each session writes to a new ``logs/YYYYMMDD_HHMMSS_<agent_id>.log`` file.

    Returns:
        (log_path, console_handler) — console is already at WARNING;
        detailed output goes to the per-session log file.
    """
    from slife.logfmt import configure_root_logging

    root = logging.getLogger()

    # Dedup: skip if handlers already set up (e.g. tests calling main() repeatedly)
    if root.handlers:
        console = next(
            (h for h in root.handlers if isinstance(h, logging.StreamHandler)
             and getattr(h, 'stream', None) is not None),
            None
        )
        if console is not None:
            return _session_log_path(agent_id), console

    log_path = _session_log_path(agent_id)
    file_fmt = SessionFormatter(FILE_LOG_FORMAT)

    console = configure_root_logging(
        stderr_level=logging.WARNING,
        file_path=log_path,
        file_level=level,
        file_format=file_fmt,
    )

    return log_path, console
