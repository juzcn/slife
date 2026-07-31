"""KeyutilsBackend — Linux kernel keyring via python-keyutils.

Used on headless Linux (no D-Bus / SecretService) as a fallback system
keyring backed by ``python-keyutils`` (SAS Institute), a maintained
wrapper around the libkeyutils C library.

Keys are stored in the **persistent keyring** (``@p``, per-UID, survives
logouts).  Each credential is a ``"user"`` key with description
``"credstore:<service>/<key>"``.

Dependency: ``keyutils>=0.6`` (Linux-only, installed as optional dep).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Optional

from jaraco.classes import properties
from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError, PasswordSetError

logger = logging.getLogger("credstore.keyutils")


def _is_wsl() -> bool:
    """Detect whether we are running inside Windows Subsystem for Linux (WSL)."""
    if os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop"):
        return True
    try:
        with open("/proc/version", "r", encoding="ascii", errors="replace") as f:
            content = f.read().lower()
            return "microsoft" in content or "wsl" in content
    except (FileNotFoundError, PermissionError, OSError):
        pass
    return False


def _check_linux_keyutils() -> str | None:
    """Verify keyutils is usable.  Returns ``None`` on success, or an
    error message string on failure (for raising ``RuntimeError``)."""
    # WSL uses keyring-wincred, not the kernel keyring
    if _is_wsl():
        return "WSL detected — prefer keyring-wincred"
    try:
        import keyutils  # type: ignore[import-untyped]
        # Probe: can we access the persistent keyring?
        pid = keyutils.request_key(  # type: ignore[attr-defined]
            b"_credstore_probe", keyutils.KEY_SPEC_PERSISTENT_KEYRING,  # type: ignore[attr-defined]
        )
        if pid is not None and pid < 0:
            return "Persistent keyring inaccessible"
    except ImportError:
        # keyutils is Linux-only; on other platforms the import fails
        if sys.platform == "linux":
            return "python-keyutils not installed — run: pip install keyutils"
        return "KeyutilsBackend requires Linux"
    except Exception as exc:
        return f"Persistent keyring unavailable: {exc}"
    return None


class KeyutilsBackend(KeyringBackend):
    """Headless-Linux keyring backend backed by the kernel keyutils.

    **Priority**: 1.5 (above fail 0, below OS-native 5.0 and keyring-wincred 9.0).
    Raises ``RuntimeError`` in ``priority`` when not on non-WSL Linux
    or when ``keyutils`` / persistent keyring is unavailable.
    """

    @properties.classproperty
    def priority(cls) -> float:  # type: ignore[override]
        """Return 1.5 when kernel keyring is usable, raise RuntimeError otherwise."""
        err = _check_linux_keyutils()
        if err is not None:
            raise RuntimeError(err)
        return 1.5

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _desc(service: str, username: str) -> bytes:
        """Build a key description: ``credstore:<service>/<key>``."""
        return f"credstore:{service}/{username}".encode("utf-8")

    # ── get_password ──────────────────────────────────────────────────

    def get_password(self, service: str, username: str) -> Optional[str]:
        """Retrieve a credential from the kernel persistent keyring."""
        import keyutils  # type: ignore[import-untyped]

        desc = self._desc(service, username)
        kid = keyutils.request_key(desc, keyutils.KEY_SPEC_PERSISTENT_KEYRING)
        if kid is None:
            return None

        try:
            value = keyutils.read_key(kid)
        except OSError:
            return None
        try:
            return value.decode("utf-8") if isinstance(value, bytes) else value
        except UnicodeDecodeError:
            logger.debug("keyutils payload is not valid UTF-8")
            return None

    # ── set_password ──────────────────────────────────────────────────

    def set_password(self, service: str, username: str, password: str) -> None:
        """Store a credential in the kernel persistent keyring.

        ``add_key`` creates or updates a key — if a key with the same
        description already exists, the kernel updates its payload.
        """
        import keyutils  # type: ignore[import-untyped]

        desc = self._desc(service, username)
        try:
            kid = keyutils.add_key(
                desc,
                password.encode("utf-8"),
                keyutils.KEY_SPEC_PERSISTENT_KEYRING,
            )
        except OSError as exc:
            raise PasswordSetError(
                f"Cannot store credential '{service}/{username}': {exc}"
            ) from exc

        if kid is None or kid < 0:
            raise PasswordSetError(
                f"Cannot store credential '{service}/{username}' (key_id={kid})"
            )
        logger.debug("keyutils credential_stored key=%s/%s kid=%s",
                     service, username, kid)

    # ── delete_password ───────────────────────────────────────────────

    def delete_password(self, service: str, username: str) -> None:
        """Delete a credential from the kernel persistent keyring.

        Uses ``keyctl_revoke(2)`` via ``keyutils.revoke()``.
        """
        import keyutils  # type: ignore[import-untyped]

        desc = self._desc(service, username)
        kid = keyutils.request_key(desc, keyutils.KEY_SPEC_PERSISTENT_KEYRING)
        if kid is None:
            raise PasswordDeleteError(
                f"Credential '{service}/{username}' not found in kernel keyring"
            )

        try:
            keyutils.revoke(kid)
        except OSError as exc:
            raise PasswordDeleteError(
                f"Cannot delete credential '{service}/{username}': {exc}"
            ) from exc

        logger.debug("keyutils credential_deleted key=%s/%s kid=%s",
                     service, username, kid)
