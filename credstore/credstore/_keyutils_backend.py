"""KeyutilsBackend — Linux kernel keyring via ctypes.

Used on headless Linux (no D-Bus / SecretService) as a fallback system
keyring.  Calls ``add_key`` and ``keyctl`` syscalls directly through libc
— zero Python dependencies beyond stdlib.

Keys are stored in the **persistent keyring** (``@p``, per-UID, survives
logouts).  Each credential is a ``"user"`` key with description
``"credstore:<service>/<key>"``.

Reference: ``man 2 add_key``, ``man 2 keyctl``, ``man 7 keyrings``.
"""

from __future__ import annotations

import ctypes
import logging
import os
import platform
import sys
from typing import Optional

from jaraco.classes import properties
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError, PasswordSetError

logger = logging.getLogger("credstore.keyutils")

# ── libc bindings ──────────────────────────────────────────────────────────

_libc = ctypes.CDLL("libc.so.6", use_errno=True)

# long syscall(long number, ...)
_libc.syscall.argtypes = [
    ctypes.c_long, ctypes.c_long, ctypes.c_ulong,
    ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong,
]
_libc.syscall.restype = ctypes.c_long

# ── syscall / keyctl constants ──────────────────────────────────────────────

# Architecture-specific syscall numbers for add_key(2) and keyctl(2)
# Both are called via syscall() to avoid glibc wrapper availability issues.
_SYS_add_key: int = {
    "x86_64":  248,
    "aarch64": 217,
    "armv7l":  309,
    "i686":    287,
    "ppc64le": 310,
    "s390x":   278,
    "riscv64": 259,
}.get(platform.machine(), 248)

_SYS_KEYCTL: int = {
    "x86_64":  250,
    "aarch64": 219,
    "armv7l":  311,
    "i686":    288,
    "ppc64le": 300,
    "s390x":   279,
    "riscv64": 261,
}.get(platform.machine(), 250)

# keyctl(2) operation codes
KEYCTL_READ       = 11
KEYCTL_SEARCH     = 4
KEYCTL_INVALIDATE = 2

# ENOKEY — "Required key not available" (Linux-specific, not in Windows errno)
_ENOKEY = 126

# special keyring IDs
KEY_SPEC_PERSISTENT_KEYRING = -11

# key type
KEY_TYPE = b"user"

# ── helpers ────────────────────────────────────────────────────────────────

def _is_wsl() -> bool:
    """Detect Windows Subsystem for Linux."""
    if os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
        return True
    try:
        with open("/proc/version", "r", encoding="ascii", errors="replace") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return False


def _check_viable() -> str | None:
    """Verify kernel keyring is usable.  Returns error message or None."""
    if sys.platform != "linux":
        return "KeyutilsBackend requires Linux"
    if _is_wsl():
        return "WSL detected — prefer keyring-wincred"
    # Probe: can we access the persistent keyring?
    result = _libc.syscall(_SYS_KEYCTL, KEYCTL_READ,
                           KEY_SPEC_PERSISTENT_KEYRING, 0, 0, 0)
    if result < 0:
        err = -result
        return f"Persistent keyring unavailable (errno={err}: {os.strerror(err)})"
    return None


def _search(desc: bytes) -> int:
    """Search persistent keyring for a key by description.
    Returns key ID on success, negative errno on failure.
    """
    type_buf = ctypes.create_string_buffer(KEY_TYPE)
    desc_buf = ctypes.create_string_buffer(desc)
    return _libc.syscall(
        _SYS_KEYCTL, KEYCTL_SEARCH,
        KEY_SPEC_PERSISTENT_KEYRING,
        ctypes.addressof(type_buf),
        ctypes.addressof(desc_buf),
        0,
    )


# ── backend ────────────────────────────────────────────────────────────────

class KeyutilsBackend(KeyringBackend):
    """Headless-Linux keyring backend backed by the kernel keyutils.

    **Priority**: 1.5 (above fail 0, below OS-native 5.0 and keyring-wincred 9.0).
    Raises ``RuntimeError`` in ``priority`` when not on non-WSL Linux
    or when persistent keyring support is unavailable.
    """

    @properties.classproperty
    def priority(cls) -> float:  # type: ignore[override]
        err = _check_viable()
        if err is not None:
            raise RuntimeError(err)
        return 1.5

    # ── helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _desc(service: str, username: str) -> bytes:
        return f"credstore:{service}/{username}".encode("utf-8")

    # ── get_password ──────────────────────────────────────────────────

    def get_password(self, service: str, username: str) -> Optional[str]:
        kid = _search(self._desc(service, username))
        if kid < 0:
            return None

        # first call: determine payload size
        size = _libc.syscall(_SYS_KEYCTL, KEYCTL_READ, kid, 0, 0, 0)
        if size <= 0:
            return None

        buf = ctypes.create_string_buffer(size)
        n = _libc.syscall(_SYS_KEYCTL, KEYCTL_READ, kid,
                          ctypes.addressof(buf), size, 0)
        if n <= 0:
            return None
        try:
            return buf.raw[:n].decode("utf-8")
        except UnicodeDecodeError:
            logger.debug("keyutils payload is not valid UTF-8")
            return None

    # ── set_password ──────────────────────────────────────────────────

    def set_password(self, service: str, username: str, password: str) -> None:
        desc = self._desc(service, username)
        payload = password.encode("utf-8")

        # remove existing key with the same description (idempotent)
        old = _search(desc)
        if old >= 0:
            _libc.syscall(_SYS_KEYCTL, KEYCTL_INVALIDATE, old, 0, 0, 0)

        kid = _libc.syscall(
            _SYS_add_key,
            ctypes.addressof(ctypes.create_string_buffer(KEY_TYPE)),
            ctypes.addressof(ctypes.create_string_buffer(desc)),
            ctypes.addressof(ctypes.create_string_buffer(payload)),
            ctypes.c_size_t(len(payload)),
            ctypes.c_int32(KEY_SPEC_PERSISTENT_KEYRING),
        )
        if kid < 0:
            raise PasswordSetError(
                f"Cannot store credential '{service}/{username}' "
                f"(errno={-kid}: {os.strerror(-kid)})"
            )
        logger.debug("keyutils credential_stored key=%s/%s kid=%s",
                     service, username, kid)

    # ── delete_password ───────────────────────────────────────────────

    def delete_password(self, service: str, username: str) -> None:
        kid = _search(self._desc(service, username))
        if kid < 0:
            if -kid == _ENOKEY:
                raise PasswordDeleteError(
                    f"Credential '{service}/{username}' not found in kernel keyring"
                )
            raise PasswordDeleteError(
                f"Cannot search for credential (errno={-kid})"
            )
        result = _libc.syscall(_SYS_KEYCTL, KEYCTL_INVALIDATE, kid, 0, 0, 0)
        if result < 0:
            raise PasswordDeleteError(
                f"Cannot invalidate key {kid} (errno={-result})"
            )
        logger.debug("keyutils credential_deleted key=%s/%s kid=%s",
                     service, username, kid)
