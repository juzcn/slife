"""Dual-write backend for credstore.

Architecture:
  - System keyring: primary read/write (deterministic per-platform:
    WinVaultKeyring / WslBackend / macOS Keychain / KeyutilsBackend)
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
import subprocess
import sys
from contextlib import contextmanager
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


@contextmanager
def unlocked_cryptfile(password: str):
    """Context manager that temporarily unlocks the cryptfile.

    Sets ``keyring_key`` on entry, deletes it on exit.
    Raises ``RuntimeError`` if the cryptfile backend is not available.

    Usage::

        with unlocked_cryptfile(master_pw) as cf:
            cf.set_password(DEFAULT_SERVICE, key, secret)
    """
    cf = get_cryptfile()
    if cf is None:
        raise RuntimeError("Cryptfile backend not available")
    cf.keyring_key = password
    try:
        yield cf
    finally:
        del cf.keyring_key


def has_master_key() -> bool:
    """Check if master key has been set (cryptfile exists).

    Does NOT attempt to unlock — just verifies the user ran
    ``credstore set-password`` at least once.
    """
    if _cryptfile is not None and hasattr(_cryptfile, "file_path"):
        return os.path.exists(_cryptfile.file_path)
    return False


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
    if has_master_key():
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
    if has_master_key():
        logger.info("cryptfile reinitialized with new password")


from credstore._platform import is_wsl  # noqa: E402


def _init_system():
    """Initialize the system keyring backend.

    Deterministic platform → backend dispatch.  No keyring auto-discovery:
    the mapping below is the single source of truth.  On each platform the
    exact backend is known up front, so behavior never depends on which
    third-party backends happen to be installed or their priority.

    Backend matrix (only these platforms are supported):

      Windows            WinVaultKeyring      (Credential Manager / Vault)
      WSL                WslBackend           (PowerShell bridge → CredMan)
      macOS (GUI)        macOS.Keyring        (login keychain)
      macOS (headless)   macOS.Keyring +      (isolated keychain file via
                           with_properties      CREDSTORE_KEYCHAIN)
      Linux (native)     KeyutilsBackend      (kernel persistent keyring @p)

    Any other platform raises — credstore supports exactly these five.

    If a *supported* platform's backend is unavailable (e.g. Linux where
    keyctl is blocked by seccomp/policy), returns ``None`` so the caller
    continues in **cryptfile-only** mode instead of failing every command.
    The reason is logged; ``kind`` in ``get_backend_info()`` reports why.
    """

    # ── WSL: direct backend, no auto-discovery ──────────────────────
    # The PowerShell bridge shares Windows Credential Manager, so WSL and
    # native Windows credentials are the same store.  A transient probe
    # failure (e.g. PowerShell cold-start) is logged but tolerated — it
    # doesn't mean the backend is broken.
    if is_wsl():
        from credstore._wsl_backend import WslBackend

        kr = WslBackend()
        try:
            kr.get_password("credstore", "__probe__")
        except Exception as exc:
            logger.warning(
                "system keyring probe failed: %s — using WslBackend anyway",
                exc,
            )
        logger.debug("system keyring: WslBackend (direct)")
        _activate_system_keyring(kr)
        return kr

    # ── Platform → backend factory (each may return None) ───────────
    if os.name == "nt":
        kr = _init_windows_backend()
    elif sys.platform == "darwin":
        kr = _init_macos_backend()
    elif sys.platform.startswith("linux"):
        kr = _init_linux_backend()
    else:
        raise RuntimeError(
            f"credstore does not support platform '{sys.platform}' (os.name={os.name}).\n"
            "Supported: Windows, WSL, macOS, Linux. "
            "See https://github.com/juzcn/slife/tree/main/credstore"
        )

    if kr is None:
        logger.warning("system keyring unavailable — cryptfile-only mode")
        return None
    _activate_system_keyring(kr)
    return kr


def _activate_system_keyring(kr) -> None:
    """Register the selected backend so every keyring caller shares it."""
    import keyring

    keyring.set_keyring(kr)


def _init_windows_backend():
    """Windows Credential Manager via keyring's WinVaultKeyring.

    This is the effective primary of the auto-discovery chain on Windows
    (WinVaultKeyring 5.0 > cryptfile 2.5/0.6/0.5), so selecting it
    explicitly is behavior-identical — just deterministic.
    Returns None if construction fails (pywin32 missing, etc.).
    """
    try:
        from keyring.backends.Windows import WinVaultKeyring

        kr = WinVaultKeyring()
    except Exception as exc:
        logger.warning("Windows system keyring unavailable: %s", exc)
        return None
    logger.debug("system keyring: WinVaultKeyring (deterministic)")
    return kr


def _init_macos_backend():
    """macOS Keychain — login keychain, or isolated file on headless.

    Returns None if the Keychain layer is unavailable.
    """
    try:
        from keyring.backends.macOS import Keyring as MacKeyring

        keychain = _resolve_macos_keychain()
        if keychain is not None:
            ensure_macos_keychain(keychain)
        kr = (
            MacKeyring()
            if keychain is None
            else MacKeyring().with_properties(keychain=keychain)
        )
    except Exception as exc:
        logger.warning("macOS system keyring unavailable: %s", exc)
        return None
    logger.debug("system keyring: macOS Keychain (keychain=%s)", keychain or "login")
    return kr


def _init_linux_backend():
    """Linux (incl. headless): kernel persistent keyring.

    Returns None if the kernel keyring is unavailable (e.g. keyctl blocked
    by seccomp or container policy — common on HPC login nodes) so the
    caller falls back to cryptfile-only storage.
    """
    try:
        from credstore._keyutils_backend import KeyutilsBackend

        err = _check_keyutils_viable()
        if err is not None:
            logger.warning("Linux system keyring unavailable: %s", err)
            return None
        kr = KeyutilsBackend()
    except Exception as exc:
        logger.warning("Linux system keyring unavailable: %s", exc)
        return None
    logger.debug("system keyring: KeyutilsBackend (deterministic)")
    return kr


def _check_keyutils_viable() -> str | None:
    """Return an error string if the kernel keyring is unusable, else None."""
    from credstore._keyutils_backend import _check_viable

    return _check_viable()


def _resolve_macos_keychain() -> str | None:
    """Resolve the keychain path for macOS.

    Returns None → use the default (login) keychain.  Returns a path →
    use an isolated keychain file.  The path comes from
    ``CREDSTORE_KEYCHAIN``, falling back to ``~/.credstore/credentials.keychain-db``
    when the file already exists (headless setup via ``security create-keychain``).
    """
    env_path = os.environ.get("CREDSTORE_KEYCHAIN")
    if env_path:
        return os.path.expanduser(env_path)

    default = os.path.join(
        os.path.expanduser("~"), ".credstore", "credentials.keychain-db"
    )
    if os.path.exists(default):
        return default
    return None


def ensure_macos_keychain(keychain: str) -> None:
    """Create an isolated macOS keychain file if it does not exist.

    Headless macOS (CI, servers) has no login-keychain interaction, so
    ``SecItem*`` writes fail with ``errSecInteractionNotAllowed``.  An
    isolated keychain file created with ``security create-keychain`` is
    the supported escape.  The caller passes the *keychain* path that
    ``_resolve_macos_keychain`` already chose.
    """
    if os.path.exists(keychain):
        return
    parent = os.path.dirname(keychain)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    # -h/-U keep it unlocked, non-interactive; -p '' avoids a prompt
    subprocess.run(
        [
            "security", "create-keychain", "-p", "", "-h", "-U", keychain,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


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
        from credstore._config import get_cryptfile_path
        crypt_path = get_cryptfile_path()
        kr.file_path = crypt_path  # type: ignore[assignment]
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
        "cryptfile_ready": has_master_key(),
    }
    if _cryptfile is not None:
        from credstore._config import get_cryptfile_path
        info["cryptfile_path"] = get_cryptfile_path()
        info["cryptfile_locked"] = getattr(_cryptfile, "_keyring_key", None) is None
    return info


def get_active_backend_name() -> str:
    """Human-readable backend description."""
    if _system_keyring is not None and has_master_key():
        return "system keyring + cryptfile (dual-write)"
    elif _system_keyring is not None:
        return "system keyring only (cryptfile not configured)"
    elif _cryptfile is not None:
        return "cryptfile only (system keyring unavailable)"
    return "none"


def read_cryptfile_entry(key: str, master_password: str, service: str = "credstore") -> str | None:
    """Read a single credential from the cryptfile.

    Returns the secret value, or None if not found.
    Raises ``ValueError`` if the master password is wrong.

    Caller is responsible for ``del``-ing the returned secret.
    """
    cf = get_cryptfile()
    if cf is None:
        return None

    with unlocked_cryptfile(master_password) as cf_ctx:
        return cf_ctx.get_password(service, key)
