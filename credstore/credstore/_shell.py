"""Shell formatting and environment persistence utilities.

Shell formatting: ``format_export`` / ``format_unset`` convert
key/value pairs into shell export/unset statements for ``eval``
consumption.  Pure string formatting — no keyring or I/O.

Profile persistence: ``get_profile_path``, ``add_to_profile``, and
``remove_from_profile`` manage shell startup files for persistent
environment variable injection on Unix.  On Windows, ``_setx`` /
``_setx_delete`` write directly to the registry (HKCU\\Environment).

The ``persist_key`` / ``unpersist_key`` wrappers dispatch to the
correct backend per ``os.name``.

Memory safety: ``format_export`` receives the secret as a parameter.
The caller MUST ``del`` the value after calling — Python strings are
immutable, so only the reference can be cleaned up.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

__all__ = [
    "format_export",
    "format_unset",
    "resolve_shell",
    "get_profile_path",
    "is_persisted",
    "add_to_profile",
    "remove_from_profile",
    "persist_key",
    "unpersist_key",
]


def resolve_shell(shell: str = "auto") -> str:
    """Resolve 'auto' to platform-appropriate shell, validate choices.

    On Windows, detects PowerShell vs cmd.exe by checking env vars.
    On Unix, returns 'bash'.

    >>> resolve_shell("bash")
    'bash'
    >>> resolve_shell("auto")  # platform-dependent
    """
    if shell != "auto":
        if shell not in ("bash", "powershell", "cmd"):
            raise ValueError(f"Unknown shell: {shell}")
        return shell
    return _detect_shell()


def _detect_shell() -> str:
    """Auto-detect the running shell.

    On Windows, cmd.exe often inherits PSModulePath from a PowerShell
    parent (e.g. VS Code terminal, Windows Terminal).  Check cmd.exe
    indicators first; PowerShell has unique env vars that cmd.exe does
    NOT set natively.
    """
    if os.name != "nt":
        return "bash"

    # cmd.exe has a PROMPT env var (default: $P$G); PowerShell uses a function
    prompt = os.environ.get("PROMPT", "")
    if prompt:
        return "cmd"

    # PowerShell has PSModulePath set natively (cmd.exe only has it if leaked)
    if os.environ.get("PSModulePath"):
        return "powershell"

    comspec = os.environ.get("COMSPEC", "")
    if comspec.lower().endswith("cmd.exe"):
        return "cmd"

    return "powershell"


def format_export(key: str, value: str, shell: str = "auto") -> str:
    """Format a (key, value) pair as a shell export statement.

    Returns a string suitable for ``eval`` in the target shell.
    The caller MUST ``del`` the *value* after calling — this function
    receives the secret as a parameter and has no control over the
    caller's reference.

    >>> format_export("MY_KEY", "sk-abc123", "bash")
    "export MY_KEY='sk-abc123'"
    >>> format_export("MY_KEY", "abc`def", "powershell")
    "$env:MY_KEY = 'abc``def'"
    """
    fmt = resolve_shell(shell)
    if fmt == "powershell":
        escaped = value.replace("`", "``")
        return f"$env:{key} = '{escaped}'"
    elif fmt == "cmd":
        return f"set {key}={value}"
    else:  # bash / zsh
        escaped = value.replace("'", "'\\''")
        return f"export {key}='{escaped}'"


def format_unset(key: str, shell: str = "auto") -> str:
    """Format an environment variable removal for the target shell.

    Does NOT touch the keyring — purely a formatting function.
    Safe to call without any secret in memory.

    >>> format_unset("MY_KEY", "bash")
    "unset MY_KEY"
    >>> format_unset("MY_KEY", "powershell")
    "Remove-Item Env:MY_KEY"
    """
    fmt = resolve_shell(shell)
    if fmt == "powershell":
        return f"Remove-Item Env:{key}"
    elif fmt == "cmd":
        return f"set {key}="
    else:  # bash / zsh
        return f"unset {key}"


# ── profile persistence ───────────────────────────────────────

_COMMENT_MARKER = "credstore"


def get_profile_path(shell: str = "auto") -> Path | None:
    """Return the shell profile file path, or None if unsupported.

    Windows uses registry (setx) — no profile file needed.
    Powershell   → $PROFILE
    bash / zsh   → ~/.bashrc
    """
    fmt = resolve_shell(shell)
    if fmt == "powershell":
        raw = os.environ.get("PROFILE", "")
        if raw:
            return Path(raw)
        docs = os.environ.get("USERPROFILE", ".")
        return Path(docs) / "Documents" / "WindowsPowerShell" / "Microsoft.PowerShell_profile.ps1"
    elif fmt == "cmd":
        return None  # Windows uses registry, not profile file
    else:  # bash / zsh
        home = os.environ.get("HOME", ".")
        return Path(home) / ".bashrc"


def is_persisted(key: str, shell: str = "auto") -> bool:
    """Check whether *key* is already persisted in the shell profile."""
    profile = get_profile_path(shell)
    if profile is None or not profile.exists():
        return False
    content = profile.read_text(encoding="utf-8")
    return _find_key_line(content, key) >= 0


def add_to_profile(key: str, shell: str = "auto") -> bool:
    """Ensure an inject line for *key* exists in the shell profile.

    If a previous entry for *key* exists, it is replaced (overwritten).
    Creates the profile file if it doesn't exist.
    Always returns True — the key is persisted after this call.
    """
    profile = get_profile_path(shell)
    if profile is None:
        return False

    fmt = resolve_shell(shell)
    content = _read_or_empty(profile)

    # Remove any existing entry for this key
    content = _remove_key_lines(content, key)

    # Append new entry
    line = _make_profile_line(key, fmt)
    content = content.rstrip("\n") + "\n" + line + "\n"

    _write_profile(profile, content)
    return True


def remove_from_profile(key: str, shell: str = "auto") -> bool:
    """Remove the inject line for *key* from the shell profile.

    Returns True if a line was removed, False if *key* wasn't found.
    The marker comment ``# credstore: KEY`` is used to identify lines.
    """
    profile = get_profile_path(shell)
    if profile is None or not profile.exists():
        return False

    content = profile.read_text(encoding="utf-8")
    new_content = _remove_key_lines(content, key)
    if new_content == content:
        return False  # nothing changed

    _write_profile(profile, new_content)
    return True


# ── internal helpers ──────────────────────────────────────────


def _read_or_empty(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


def _write_profile(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_profile_line(key: str, fmt: str) -> str:
    """Build the profile line(s) that inject a key at shell startup.

    Windows uses registry (setx) — only Unix/PowerShell use profile files.
    The comment line ``# credstore: KEY`` is the marker for removal.
    """
    if fmt == "powershell":
        comment = f"# {_COMMENT_MARKER}: {key}"
        inject = f'Invoke-Expression (credstore inject {key} 2>$null)'
    else:  # bash / zsh
        comment = f"# {_COMMENT_MARKER}: {key}"
        inject = f'eval "$(credstore inject {key} 2>/dev/null)"'
    return f"{comment}\n{inject}"


def _find_key_line(content: str, key: str) -> int:
    """Return line index of the credstore marker for *key*, or -1."""
    marker = f"# {_COMMENT_MARKER}: {key}"
    lines = content.split("\n")
    for i, line in enumerate(lines):
        if line.strip() == marker:
            return i
    return -1


def _remove_key_lines(content: str, key: str) -> str:
    """Remove marker comment + following inject line for *key* from content.

    Returns the content unchanged if *key* is not found.
    Cleans up trailing blank lines left after removal.
    """
    idx = _find_key_line(content, key)
    if idx < 0:
        return content

    lines = content.split("\n")
    # Remove the comment line
    del lines[idx]
    # Remove the inject line that follows (if it's not a comment/blank)
    if idx < len(lines) and lines[idx].strip() and not lines[idx].strip().startswith("#"):
        del lines[idx]

    # Trim trailing blank lines from the end
    while lines and lines[-1].strip() == "":
        lines.pop()

    return "\n".join(lines) + ("\n" if lines else "")


# ── Windows registry persistence ──────────────────────────────────


def _setx(key: str, value: str) -> None:
    """Write to HKCU\\Environment directly — no command-line leak."""
    import ctypes
    import winreg

    key_handle = winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
    )
    winreg.SetValueEx(key_handle, key, 0, winreg.REG_EXPAND_SZ, value)
    winreg.CloseKey(key_handle)
    _broadcast_environment_change()


def _setx_delete(key: str) -> None:
    """Delete a value from HKCU\\Environment directly."""
    import ctypes
    import winreg

    try:
        key_handle = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0,
            winreg.KEY_SET_VALUE,
        )
        winreg.DeleteValue(key_handle, key)
        winreg.CloseKey(key_handle)
        _broadcast_environment_change()
    except FileNotFoundError:
        pass


def _broadcast_environment_change() -> None:
    """Notify running processes that HKCU\\Environment changed.

    Uses ``SendMessageTimeoutW`` with ``SMTO_ABORTIFHUNG`` so a hung
    top-level window cannot stall the broadcast (and thus the ``inject``
    or ``uninject`` command).  Falls back to a fire-and-forget
    ``SendNotifyMessageW`` if the timeout API is unavailable.
    """
    import ctypes

    HWND_BROADCAST = 0xFFFF
    WM_SETTINGCHANGE = 0x001A
    SMTO_ABORTIFHUNG = 0x0002
    ENV = "Environment"

    user32 = ctypes.windll.user32

    # Prefer SendMessageTimeoutW — aborts on hung windows after 2 s
    try:
        result = ctypes.c_ulong()
        user32.SendMessageTimeoutW(
            HWND_BROADCAST,
            WM_SETTINGCHANGE,
            0,
            ENV,
            SMTO_ABORTIFHUNG,
            2000,  # 2-second timeout per window
            ctypes.byref(result),
        )
    except Exception:
        # Fallback: async fire-and-forget (no hang risk, but some
        # processes may not see the change until restart)
        user32.SendNotifyMessageW(HWND_BROADCAST, WM_SETTINGCHANGE, 0, ENV)


def persist_key(key: str, value: str, shell: str = "auto") -> None:
    """Persist a credential to system environment.

    On Windows: writes to registry (HKCU\\Environment).
    On Unix: appends to shell profile file.
    """
    if os.name == "nt":
        _setx(key, value)
    else:
        add_to_profile(key, shell)


def unpersist_key(key: str, shell: str = "auto") -> None:
    """Remove a credential from system environment.

    On Windows: deletes from registry (HKCU\\Environment).
    On Unix: removes from shell profile file.
    """
    if os.name == "nt":
        _setx_delete(key)
    else:
        remove_from_profile(key, shell)
