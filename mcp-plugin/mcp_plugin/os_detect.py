"""OS-level path detection for MCP allowed-paths injection.

The philosophy: trust the LLM, use OS file permissions as the safety net.
Instead of hard-coding restricted paths in MCP server configs, we detect
what the OS user can access and expose everything — the OS itself enforces
read/write/execute permissions on every file access.

Public functions:
    get_os_accessible_paths() -> list[str]
    is_windows() -> bool
    is_wsl() -> bool
    is_macos() -> bool
    is_linux() -> bool
"""

import os
import platform
import sys


def is_wsl() -> bool:
    """Check if running under Windows Subsystem for Linux.

    Reads ``/proc/version`` and looks for the "microsoft" or "wsl"
    markers that the WSL kernel inserts.

    Returns:
        True when running inside WSL, False otherwise (native Linux,
        macOS, native Windows, or when ``/proc/version`` is unreadable).
    """
    try:
        with open("/proc/version", "r") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except (FileNotFoundError, PermissionError, OSError):
        return False


def is_windows() -> bool:
    """Check if running on Windows — native or WSL.

    Returns True for both native Windows (``platform.system() == "Windows"``)
    and WSL (Linux kernel on a Windows host, detected via ``is_wsl()``).

    Returns:
        True on any Windows host, False on macOS or native Linux.
    """
    return platform.system() == "Windows" or is_wsl()


def is_macos() -> bool:
    """Check if running on macOS.

    Returns:
        True when ``platform.system()`` returns ``"Darwin"``.
    """
    return platform.system() == "Darwin"


def is_linux() -> bool:
    """Check if running on native Linux (non-WSL).

    Returns True only for native Linux — WSL is excluded because it runs
    on a Windows host.  Use ``is_windows()`` when you want the host OS.

    Returns:
        True on native Linux, False on WSL / macOS / Windows.
    """
    return platform.system() == "Linux" and not is_wsl()


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
