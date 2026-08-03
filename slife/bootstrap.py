"""Startup bootstrap — logging setup, console restore, and first-run helpers.

Extracted from ``slife/__init__.py`` to keep the package entry point
focused on ``main()``.
"""

import logging
import sys
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


# ── Windows console restore ───────────────────────────────────────────


def restore_windows_console() -> None:
    """Restore the Windows console to a sane default mode.

    Textual sets ``ENABLE_VIRTUAL_TERMINAL_INPUT`` (0x0200) on stdin
    and clears ``ENABLE_PROCESSED_INPUT | ENABLE_LINE_INPUT |
    ENABLE_ECHO_INPUT``.  If ``stop_application_mode()`` is skipped
    the terminal stays in raw mode.  This restores the standard flags.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        STD_INPUT_HANDLE = -10
        SANE_MODE = (
            0x0001   # ENABLE_PROCESSED_INPUT
            | 0x0002   # ENABLE_LINE_INPUT
            | 0x0004   # ENABLE_ECHO_INPUT
            | 0x0008   # ENABLE_WINDOW_INPUT
            | 0x0010   # ENABLE_MOUSE_INPUT
            | 0x0020   # ENABLE_INSERT_MODE
            | 0x0040   # ENABLE_QUICK_EDIT_MODE
            | 0x0080   # ENABLE_EXTENDED_FLAGS
        )
        h = ctypes.windll.kernel32.GetStdHandle(STD_INPUT_HANDLE)
        if h != -1:
            ctypes.windll.kernel32.SetConsoleMode(h, SANE_MODE)
    except Exception:
        pass


# ── Skills seeding ────────────────────────────────────────────────────


def seed_skills(skills_dir: Path) -> None:
    """Copy bundled skills to the data directory on first run.

    Only copies when *skills_dir* does not yet exist, so users can
    edit and add their own skills without fear of overwrites.
    """
    if skills_dir.exists():
        return
    pkg_skills = Path(__file__).resolve().parent / "skills"
    if not pkg_skills.is_dir():
        return
    import shutil
    shutil.copytree(pkg_skills, skills_dir)
    logger.info("skills_seeded from=%s to=%s", pkg_skills, skills_dir)
