"""Credential store — system keyring with cryptfile backup.

- get():    system keyring only (fast, no master key)
- set():    system keyring only (cryptfile backup handled by CLI layer)
- delete(): both stores (cryptfile delete does not need master key)
- reset():  cryptfile → system keyring (explicit recovery, needs master key)
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
        from slife.credstore._backend import get_system_keyring

        sk = get_system_keyring()
        if sk is not None:
            return sk.get_password(self._service, key)
        return None

    def exists(self, key: str) -> bool:
        """Check whether a credential exists without retrieving its value.

        Returns True/False — NEVER the secret content.
        """
        return self.get(key) is not None

    # ── set ───────────────────────────────────────────────────

    def set(self, key: str, secret: str) -> None:
        """Store to system keyring.

        Requires master key to have been set (cryptfile exists).
        Cryptfile backup is handled by the CLI layer.
        """
        from slife.credstore._backend import get_system_keyring, has_master_key

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
        from slife.credstore._backend import get_system_keyring

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
        from slife.credstore._backend import get_system_keyring, get_cryptfile

        sk = get_system_keyring()
        if sk is None:
            raise RuntimeError("No system keyring available")

        cf = get_cryptfile()
        if cf is None:
            raise RuntimeError("Cryptfile backend not available")

        # Unlock cryptfile with provided master password
        cf.keyring_key = master_password
        try:
            keys = _read_cryptfile_keys(cf)
            count = 0
            for key in keys:
                try:
                    value = cf.get_password(self._service, key)
                    if value is not None:
                        sk.set_password(self._service, key, value)
                        count += 1
                        logger.info("reset_restored key=%s", key)
                except Exception as exc:
                    logger.warning("reset_skip key=%s err=%s", key, exc)

            logger.info("reset_complete count=%d", count)
            return count
        finally:
            del cf.keyring_key

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
            keys.extend(cfg.options(section))
    return keys


# ── module-level API ─────────────────────────────────────────


def init_store(password: str | None = None) -> CredentialStore:
    global _store
    from slife.credstore._backend import init_backend
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
    return _get_store().get(key)


def exists_credential(key: str) -> bool:
    """Check whether a credential exists WITHOUT retrieving its value.

    Returns True/False — NEVER the secret content.
    Prefer this over ``get_credential()`` when you only need to know
    if a key is stored.
    """
    return _get_store().exists(key)



def set_credential(key: str, secret: str) -> None:
    _get_store().set(key, secret)


def delete_credential(key: str) -> bool:
    return _get_store().delete(key)


def reset_credentials(master_password: str) -> int:
    return _get_store().reset(master_password)


def get_backend_name() -> str:
    from slife.credstore._backend import get_active_backend_name
    return get_active_backend_name()


def check_backend() -> dict:
    from slife.credstore._backend import get_backend_info
    return get_backend_info()
