"""OS-level path detection for MCP allowed-paths injection.

The philosophy: trust the LLM, use OS file permissions as the safety net.
Instead of hard-coding restricted paths in MCP server configs, we detect
what the OS user can access and expose everything — the OS itself enforces
read/write/execute permissions on every file access.

Single public function:
    get_os_accessible_paths() -> list[str]
"""

import os
import sys


def get_os_accessible_paths() -> list[str]:
    """Return paths the OS user can access, for use as MCP ``--allow-path`` args.

    Windows:
        Iterates drive letters A-Z, returns all existing drive roots
        (e.g. ``["C:\\\\", "D:\\\\"]``).  Each drive root covers every
        file the user can access on that volume.

    Linux / macOS:
        Returns ``["/"]`` — the root filesystem.  The MCP server will
        attempt any path; OS file permissions (owner/group/mode) block
        access where the user lacks rights.  Protected paths like
        ``/root/`` or ``/etc/shadow`` are naturally denied by the kernel.

    Returns:
        List of absolute path strings suitable for ``--allow-path``.
    """
    if sys.platform == "win32":
        return _windows_drive_roots()
    return ["/"]


def _windows_drive_roots() -> list[str]:
    """Return all existing drive roots on Windows (e.g. ``["C:\\\\", "D:\\\\"]``).

    Uses ``os.path.exists`` on each candidate drive letter — fast, no
    external dependencies, and correctly excludes empty optical/floppy
    drives (they exist as devices but report False for path existence).
    """
    drives: list[str] = []
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(root)
    return drives
