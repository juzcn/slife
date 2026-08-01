"""Platform detection helpers.

A single source of truth for platform checks — import from here instead
of duplicating detection logic across modules.
"""

from __future__ import annotations

import os


def is_wsl() -> bool:
    """Detect Windows Subsystem for Linux.

    Checks two indicators: the WSL interop file (present on WSL 1 & 2),
    and the ``/proc/version`` kernel string (fallback).
    """
    if os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
        return True
    try:
        with open("/proc/version", "r", encoding="ascii", errors="replace") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return False
