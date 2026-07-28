"""Shared mock backend classes for credstore tests.

Importable by both conftest.py and individual test files.
Files named ``_*.py`` are excluded from pytest collection.
"""

from __future__ import annotations


class MockKeyringBackend:
    """Simulates a keyring backend with an optional in-memory dict.

    Parameters
    ----------
    store:
        The backing dict.  When ``prefix`` is set, keys are namespaced as
        ``{prefix}:{username}`` (used when two backends share one dict).
        When ``prefix`` is ``None``, keys are stored as-is.
    prefix:
        Optional namespace prefix for shared-dict mode.
    keyring_key:
        When set, behaves like an unlocked cryptfile (get/set/delete succeed).
        When ``None``, those operations raise ``ValueError`` (simulates
        a locked cryptfile).
    file_path:
        Filesystem path (used by cryptfile-aware checks).
    """

    def __init__(
        self,
        store: dict | None = None,
        *,
        prefix: str | None = None,
        keyring_key: str | None = None,
        file_path: str = "/mock/credentials.crypt",
    ):
        self._store = store if store is not None else {}
        self._prefix = prefix
        self._keyring_key = keyring_key
        self.file_path = file_path

    # -- keyring_key property (cryptfile-compatible) -------------------

    @property
    def keyring_key(self) -> str | None:
        return self._keyring_key

    @keyring_key.setter
    def keyring_key(self, value: str | None) -> None:
        self._keyring_key = value

    @keyring_key.deleter
    def keyring_key(self) -> None:
        self._keyring_key = None

    # -- keyring protocol -----------------------------------------------

    def _make_key(self, username: str) -> str:
        if self._prefix:
            return f"{self._prefix}:{username}"
        return username

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get(self._make_key(username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[self._make_key(username)] = password

    def delete_password(self, service: str, username: str) -> None:
        key = self._make_key(username)
        if key in self._store:
            del self._store[key]
        else:
            raise KeyError(username)


class MockCryptfileBackend(MockKeyringBackend):
    """Mock backend that enforces keyring_key locking (like cryptfile).

    When ``keyring_key`` is ``None``, get/set/delete raise ``ValueError``
    (simulates locked state).  When set to any string, operations work.
    """

    def get_password(self, service: str, username: str) -> str | None:
        if self._keyring_key is None:
            raise ValueError("keyring_key not set")
        return super().get_password(service, username)

    def set_password(self, service: str, username: str, password: str) -> None:
        if self._keyring_key is None:
            raise ValueError("keyring_key not set")
        super().set_password(service, username, password)

    def delete_password(self, service: str, username: str) -> None:
        if self._keyring_key is None:
            raise ValueError("keyring_key not set")
        super().delete_password(service, username)
