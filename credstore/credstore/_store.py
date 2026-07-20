"""Credential store — system keyring with cryptfile backup.

- get():      system keyring only (fast, no master key)
- list_keys(): system keyring keys only — NEVER returns secret values
- set():      system keyring only (cryptfile backup handled by CLI layer)
- delete():   both stores (cryptfile delete does not need master key)
- reset():    cryptfile → system keyring (explicit recovery, needs master key)

Memory safety: secrets are Python ``str`` objects (immutable).  Callers MUST
``del`` references after use to release them for garbage collection.  Never
batch-load all values into a single collection — compare inline, one at a time.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("credstore")

DEFAULT_SERVICE = "credstore"

_store: CredentialStore | None = None


class CredentialStore:
    """Dual-write credential storage."""

    def __init__(self, service: str = DEFAULT_SERVICE):
        self._service = service

    # ── get: system keyring only ───────────────────────────────

    def get(self, key: str) -> str | None:
        """Retrieve from system keyring only — fast, no master key.

        Prefer ``exists()`` when you only need to know whether a
        credential is stored — it avoids pulling the full secret
        into process memory.
        """
        from credstore._backend import get_system_keyring

        sk = get_system_keyring()
        if sk is not None:
            return sk.get_password(self._service, key)
        return None

    def exists(self, key: str) -> bool:
        """Check whether a credential exists without retrieving its value.

        Returns True/False — NEVER the secret content.
        """
        return self.get(key) is not None

    def list_keys(self) -> list[str]:
        """List credential keys from system keyring — NEVER returns values.

        Returns key names only.  Use ``exists()`` to check individual
        credentials without pulling secrets into memory.
        """
        from credstore._backend import get_system_keyring

        sk = get_system_keyring()
        if sk is None:
            return []

        # Most keyring backends don't support enumeration.
        # On Windows, enumeration is handled by the CLI layer via
        # win32cred.CredEnumerate (see __main__._enumerate_windows).
        # This method provides the safe, value-free interface.
        return []

    # ── set ───────────────────────────────────────────────────

    def set(self, key: str, secret: str) -> None:
        """Store to system keyring.

        Requires master key to have been set (cryptfile exists).
        Cryptfile backup is handled by the CLI layer.
        """
        from credstore._backend import get_system_keyring, has_master_key

        if not has_master_key():
            raise RuntimeError(
                "Master key not set.\n"
                "Run 'credstore set-password' first."
            )

        sk = get_system_keyring()
        if sk is None:
            raise RuntimeError("No system keyring available")
        sk.set_password(self._service, key, secret)
        logger.info("credential_stored key=%s", key)

    # ── delete ─────────────────────────────────────────────────

    def delete(self, key: str) -> bool:
        """Delete from system keyring only.

        Cryptfile cleanup is handled by the CLI layer (needs master key)."""
        from credstore._backend import get_system_keyring

        existed = False

        sk = get_system_keyring()
        if sk is not None:
            try:
                sk.delete_password(self._service, key)
                existed = True
            except Exception:
                pass

        if existed:
            logger.info("credential_deleted key=%s", key)
        return existed

    # ── reset: cryptfile → system keyring ────────────────────

    def reset(self, master_password: str) -> int:
        """Restore all credentials from cryptfile to system keyring.

        Reads every credential from the encrypted cryptfile using
        *master_password*, then writes each one to the system keyring.
        Returns the count of restored credentials.
        """
        from credstore._backend import get_system_keyring, unlocked_cryptfile

        sk = get_system_keyring()
        if sk is None:
            raise RuntimeError("No system keyring available")

        with unlocked_cryptfile(master_password) as cf:
            keys = _read_cryptfile_keys(cf)
            count = 0
            for key in keys:
                try:
                    value = cf.get_password(self._service, key)
                    if value is not None:
                        sk.set_password(self._service, key, value)
                        count += 1
                        logger.info("reset_restored key=%s", key)
                        del value  # release secret after write
                except Exception as exc:
                    logger.warning("reset_skip key=%s err=%s", key, exc)

            logger.info("reset_complete count=%d", count)
            return count

    # ── mask ──────────────────────────────────────────────────

    @staticmethod
    def mask(value: str) -> str:
        """Return a masked representation for CLI display.

        Shows first 4 + last 4 characters for human verification
        in the terminal.  Agent tools must use ``exists()`` instead
        — never expose partial credential data to an LLM.
        """
        if not value:
            return "(empty)"
        if len(value) <= 8:
            return "***"
        return f"{value[:4]}…{value[-4:]}"


# ── helpers ──────────────────────────────────────────────────


def _read_cryptfile_keys(cf) -> list[str]:
    """Read all credential keys from a cryptfile INI file."""
    import configparser
    cfg = configparser.ConfigParser()
    cfg.read(cf.file_path)
    keys = []
    for section in cfg.sections():
        if section.startswith("keyring") or section.startswith("DEFAULT"):
            continue
        if section == DEFAULT_SERVICE:
            from keyrings.cryptfile.escape import unescape
            for raw_key in cfg.options(section):
                # configparser's optionxform lowercases keys.
                # unescape reverses _XX hex encoding → original UTF-8 bytes.
                # Normalize to uppercase to match keyring convention.
                keys.append(unescape(raw_key).upper())
    return keys


# ── module-level API ─────────────────────────────────────────


def init_store(password: str | None = None) -> CredentialStore:
    global _store
    from credstore._backend import init_backend
    init_backend(password=password)
    if _store is None:
        _store = CredentialStore()
    return _store


def _get_store() -> CredentialStore:
    global _store
    if _store is None:
        init_store()
    return _store


def get_credential(key: str) -> str | None:
    """Retrieve a credential from system keyring.

    Callers MUST ``del`` the returned value after use to release the
    secret from memory as soon as possible.  Python strings are
    immutable — only the reference can be cleaned up.

    Prefer ``exists_credential()`` when you only need to check existence.
    """
    return _get_store().get(key)


def exists_credential(key: str) -> bool:
    """Check whether a credential exists WITHOUT retrieving its value.

    Returns True/False — NEVER the secret content.
    Prefer this over ``get_credential()`` when you only need to know
    if a key is stored.
    """
    return _get_store().exists(key)


def list_credential_keys() -> list[str]:
    """List credential keys WITHOUT retrieving any secret values.

    Returns key names only — NEVER the secret content.
    Use this for enumeration; call ``get_credential()`` individually
    for the specific keys you need, and ``del`` the result after use.
    """
    return _get_store().list_keys()


def set_credential(key: str, secret: str) -> None:
    _get_store().set(key, secret)


def delete_credential(key: str) -> bool:
    return _get_store().delete(key)


def reset_credentials(master_password: str) -> int:
    return _get_store().reset(master_password)


def get_backend_name() -> str:
    from credstore._backend import get_active_backend_name
    return get_active_backend_name()


def check_backend() -> dict:
    from credstore._backend import get_backend_info
    return get_backend_info()


# ── shell formatting (re-exported from _shell.py) ────────────

from credstore._shell import format_export, format_unset  # noqa: F401 — backward compat
