"""Dual-write backend for credstore.

Architecture:
  - System keyring: primary read/write (Windows CredMan / macOS Keychain / SecretService)
  - keyrings.cryptfile: encrypted backup sync (survives OS password changes)

On set(): write to BOTH system keyring + cryptfile.
On get(): try system first → if missing, try cryptfile (auto-restore to system).
On delete(): delete from BOTH.

The cryptfile master password is set via ``credstore set-password``.
Without it, secrets are stored in system keyring only (with a warning).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger("credstore")

# Singleton instances
_system_keyring = None
_cryptfile = None


def get_system_keyring():
    """Get or init the system keyring backend."""
    global _system_keyring
    if _system_keyring is not None:
        return _system_keyring
    _system_keyring = _init_system()
    return _system_keyring


def get_cryptfile():
    """Get the cryptfile backend, or None if not configured."""
    global _cryptfile
    return _cryptfile


def has_master_key() -> bool:
    """Check if master key has been set (cryptfile exists).

    Does NOT attempt to unlock — just verifies the user ran
    ``credstore set-password`` at least once.
    """
    if _cryptfile is not None and hasattr(_cryptfile, "file_path"):
        return __import__("os").path.exists(_cryptfile.file_path)
    return False


def is_cryptfile_ready() -> bool:
    """Check if cryptfile is initialized (master key set)."""
    return has_master_key()


def init_backend(password: str | None = None) -> None:
    """Initialize both backends.  Call once at module load.

    *password* is only used when setting/changing the cryptfile master
    password (from ``credstore set-password``).
    """
    global _system_keyring, _cryptfile

    # Init system keyring (always)
    _system_keyring = _init_system()

    # Init cryptfile (may need password)
    _init_cryptfile(password)

    # Report status
    if is_cryptfile_ready():
        logger.info("backend=dual system=%s cryptfile=%s",
                     type(_system_keyring).__name__ if _system_keyring else "none",
                     "ready")
    elif _cryptfile is not None:
        logger.warning("cryptfile needs master password — run 'credstore set-password'")
    else:
        logger.warning("cryptfile unavailable — secrets in system keyring only")


def reinit_cryptfile(password: str) -> None:
    """Re-initialize cryptfile with a new password (for set-password / change-password)."""
    global _cryptfile
    _init_cryptfile(password)
    if is_cryptfile_ready():
        logger.info("cryptfile reinitialized with new password")


def _init_system():
    """Initialize the system keyring backend."""
    import keyring

    try:
        kr = keyring.get_keyring()
    except Exception as exc:
        logger.debug("system keyring get_keyring failed: %s", exc)
        return None

    from keyring.backends.fail import Keyring as FailKeyring
    if isinstance(kr, FailKeyring):
        logger.debug("system keyring: fail backend (no viable backends)")
        return None

    try:
        kr.get_password("credstore", "__probe__")
    except Exception as exc:
        logger.debug("system keyring probe failed: %s", exc)
        return None

    logger.debug("system keyring: %s", type(kr).__name__)
    keyring.set_keyring(kr)
    return kr


def _init_cryptfile(password: str | None = None):
    """Initialize the cryptfile backend."""
    global _cryptfile

    try:
        from keyrings.cryptfile.cryptfile import CryptFileKeyring
    except ImportError:
        logger.debug("keyrings.cryptfile not installed")
        _cryptfile = None
        return

    try:
        kr = CryptFileKeyring()
        from slife.credstore._config import get_cryptfile_path
        crypt_path = get_cryptfile_path()
        kr.file_path = crypt_path
        _ensure_dir(Path(crypt_path).parent)

        if password:
            kr.keyring_key = password

        _cryptfile = kr
    except Exception as exc:
        logger.debug("cryptfile init failed: %s", exc)
        _cryptfile = None


def _ensure_dir(path: Path) -> None:
    if path.exists():
        return
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


def get_backend_info() -> dict:
    """Return diagnostic info. Triggers lazy init if needed."""
    # Ensure init has run
    init_backend()

    info: dict = {
        "available": _system_keyring is not None,
        "backend": get_active_backend_name(),
        "cryptfile_ready": is_cryptfile_ready(),
    }
    if _cryptfile is not None:
        from slife.credstore._config import get_cryptfile_path
        info["cryptfile_path"] = get_cryptfile_path()
        info["cryptfile_locked"] = getattr(_cryptfile, "_keyring_key", None) is None
    return info


def get_active_backend_name() -> str:
    """Human-readable backend description."""
    if _system_keyring is not None and is_cryptfile_ready():
        return "system keyring + cryptfile (dual-write)"
    elif _system_keyring is not None:
        return "system keyring only (cryptfile not configured)"
    elif _cryptfile is not None:
        return "cryptfile only (system keyring unavailable)"
    return "none"
