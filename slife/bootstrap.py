"""Startup bootstrap — logging setup, console restore, and first-run helpers.

Extracted from ``slife/__init__.py`` to keep the package entry point
focused on ``main()``.
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from slife.logfmt import SessionFormatter, FILE_LOG_FORMAT, log_stamp, resolve_log_dir

logger = logging.getLogger("slife")


def _session_log_path(agent_name: str = "slife") -> Path:
    """Generate a timestamped log file path for this session.

    Follows the same naming convention as sub-agent logs:
    ``logs/YYYYMMDD_HHMMSS_<agent_name>.log``.
    """
    log_dir = resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    ts = log_stamp()
    return log_dir / f"{ts}_{agent_name}.log"


def setup_logging(
    agent_name: str = "slife",
    level: int = logging.DEBUG,
) -> tuple[Path, logging.Handler]:
    """Configure logging to a per-session file (logs never reach the terminal).

    Logs are for developers, the TUI is for the user: the console stderr
    handler is set to ``CRITICAL + 1`` so no log record ever prints to the
    terminal — the terminal belongs entirely to the TUI, and user-visible
    status is surfaced there through the business layer (not logging).
    File:    DEBUG+ with timestamps, session/request IDs for troubleshooting.
    Each session writes to a new ``logs/YYYYMMDD_HHMMSS_<agent_name>.log`` file.

    Returns:
        (log_path, console_handler) — console is a silent no-op handler;
        all output goes to the per-session log file.
    """
    from slife.logfmt import configure_root_logging

    root = logging.getLogger()

    # Dedup: skip if handlers already set up (e.g. tests calling main()
    # repeatedly).  Return the EXISTING file handler's path — a fresh
    # _session_log_path() would point the caller at a file nothing writes to
    # (records go to the first session's file).
    if root.handlers:
        console = next(
            (h for h in root.handlers if isinstance(h, logging.StreamHandler)
             and getattr(h, 'stream', None) is not None),
            None
        )
        file_handler = next(
            (h for h in root.handlers if isinstance(h, logging.FileHandler)),
            None,
        )
        if file_handler is not None:
            return Path(file_handler.baseFilename), console or logging.NullHandler()
        if console is not None:
            return _session_log_path(agent_name), console

    log_path = _session_log_path(agent_name)
    file_fmt = SessionFormatter(FILE_LOG_FORMAT)

    console = configure_root_logging(
        # CRITICAL+1: no log record ever reaches the user terminal.  Logs
        # are for developers (the file), the TUI is for the user.
        stderr_level=logging.CRITICAL + 1,
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


# ── Unclean-exit marker ───────────────────────────────────────────────
#
# A hard kill — ``taskkill /F``, Task Manager's End Task, a closed console —
# is TerminateProcess: no ``finally``, no ``atexit``, no log line.  The
# session stops mid-record and everything its teardown would have done
# (restore_windows_console, the plugin shutdowns, the ``session_end``
# summary) silently doesn't happen.  When a tool call ran ``taskkill /T``
# against a pid it had mis-parsed, it took the whole slife tree with it: the
# log ended mid-sentence, the console stayed in Textual's raw mode (garbled
# echoes, Ctrl-C no longer a signal), and both the user and the agent could
# only guess at the cause — and guessed wrong.
#
# Nothing in-process can record that at the time, because it never runs
# again.  So leave a marker behind and read it on the NEXT start: it names
# the pid, the start time and the log, and finding one whose pid is gone
# means that session was killed from outside.  Keyed by pid so two sessions
# in one data dir can't clobber each other, and removed by the teardown so a
# session that ends normally leaves nothing to find.

_SESSION_MARKER_FMT = ".session.{pid}.state"


def _session_marker(log_dir: Path, pid: int) -> Path:
    """The marker file for *pid*, beside the session logs it describes.

    The directory is always the SESSION LOG's own parent, never a fresh
    ``resolve_log_dir()`` lookup: writer and reader must agree on the file by
    construction, and a marker written where nobody looks is silently a
    marker that does not exist.
    """
    return Path(log_dir) / _SESSION_MARKER_FMT.format(pid=pid)


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness probe — ``os.kill(pid, 0)``, the portable one."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError as e:
        # Windows reports a missing pid as a plain OSError (WinError 87 /
        # 1168); any other failure (an epoch/permission error) means the
        # process is there but not ours.
        return getattr(e, "winerror", None) not in (87, 1168)
    return True


def note_session_start(log_path: Path, session_id: str) -> None:
    """Record this session, for the next start to read (see above)."""
    try:
        marker = _session_marker(Path(log_path).parent, os.getpid())
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(
            json.dumps({
                "pid": os.getpid(),
                "session_id": session_id,
                "started": datetime.now().isoformat(timespec="seconds"),
                "log": str(log_path),
            }),
            encoding="utf-8",
        )
    except Exception:  # diagnostics only — never break startup over a marker
        logger.debug("session_marker_write_failed", exc_info=True)


def clear_session_marker(log_dir: Path) -> None:
    """Drop this session's marker: the teardown reached its end."""
    try:
        _session_marker(log_dir, os.getpid()).unlink(missing_ok=True)
    except Exception:
        logger.debug("session_marker_clear_failed", exc_info=True)


def previous_session_killed(log_dir: Path) -> str | None:
    """One line naming the last session that never reached its teardown.

    ``None`` when the previous session ended cleanly (it removed its marker)
    or is still running (its marker is left for its own end).  Reported
    markers are deleted as they are read — the fact has been surfaced, and
    keeping them would re-report the same death at every start.
    """
    try:
        stale = sorted(Path(log_dir).glob(_SESSION_MARKER_FMT.format(pid="*")))
    except Exception:
        return None

    reported: str | None = None
    for marker in stale:
        try:
            info = json.loads(marker.read_text(encoding="utf-8"))
            pid = int(info.get("pid") or 0)
        except Exception:  # unreadable, truncated (killed mid-write), or junk
            marker.unlink(missing_ok=True)
            continue
        if pid and pid != os.getpid() and _pid_alive(pid):
            continue  # that session is live; its marker stays for its own end
        marker.unlink(missing_ok=True)
        reported = (
            f"the last session (pid {pid}, started {info.get('started', '?')}) "
            f"was killed from outside, so it never shut down and its teardown "
            f"never ran. Log: {info.get('log', '?')}"
        )
    return reported


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
