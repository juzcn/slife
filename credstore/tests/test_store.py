"""Tests for credential store operations."""

import pytest
from credstore._store import CredentialStore


class TestCredentialStore:
    def test_set_and_get(self, dual_store):
        store, data = dual_store
        store.set("test-key", "secret-value")
        assert store.get("test-key") == "secret-value"
        assert "system:test-key" in data

    def test_get_not_found(self, dual_store):
        store, _ = dual_store
        assert store.get("nonexistent") is None

    def test_delete_existing(self, dual_store):
        store, data = dual_store
        store.set("test-key", "secret-value")
        assert store.delete("test-key") is True
        assert store.get("test-key") is None
        assert "system:test-key" not in data

    def test_delete_not_found(self, dual_store):
        store, _ = dual_store
        assert store.delete("nonexistent") is False

    def test_overwrite(self, dual_store):
        store, _ = dual_store
        store.set("test-key", "first")
        store.set("test-key", "second")
        assert store.get("test-key") == "second"

    def test_keys_with_slashes(self, dual_store):
        store, _ = dual_store
        store.set("slife/provider/deepseek", "sk-key")
        assert store.get("slife/provider/deepseek") == "sk-key"

    def test_reset_restores_to_system(self, dual_store, monkeypatch):
        """reset() reads from cryptfile and writes to system keyring."""
        store, data = dual_store
        # Put values ONLY in cryptfile (simulates system keyring data loss)
        data["cryptfile:key1"] = "val1"
        data["cryptfile:key2"] = "val2"

        # Mock _read_cryptfile_keys to return key names (without prefix)
        monkeypatch.setattr(
            "credstore._store._read_cryptfile_keys",
            lambda cf: [k.split(":", 1)[1] for k in data if k.startswith("cryptfile:")]
        )

        # get() should NOT find them (system only)
        assert store.get("key1") is None
        # reset() reads from cryptfile mock, writes to system mock
        count = store.reset("mock-password")
        assert count == 2
        assert data["system:key1"] == "val1"
        assert data["system:key2"] == "val2"

    def test_get_system_only(self, dual_store):
        """get() only reads from system keyring, not cryptfile."""
        store, data = dual_store
        data["cryptfile:only-cf"] = "from-cf"
        assert store.get("only-cf") is None  # Not in system, not returned


class TestMask:
    def test_normal_value(self):
        masked = CredentialStore.mask("sk-5f55bd925bc84372917a77e282bdb722")
        assert masked == "sk-5…b722"

    def test_short_value(self):
        assert CredentialStore.mask("short") == "***"

    def test_8_char_value(self):
        assert CredentialStore.mask("12345678") == "***"

    def test_9_char_value(self):
        assert CredentialStore.mask("123456789") == "1234…6789"

    def test_empty_value(self):
        assert CredentialStore.mask("") == "(empty)"


class TestListKeys:
    def test_empty(self, dual_store):
        store, _ = dual_store
        assert store.list_keys() == []


# ── fixtures ───────────────────────────────────────────────────


class _MockBackend:
    """Simulates a keyring backend with in-memory dict."""

    def __init__(self, store: dict, prefix: str):
        self._store = store
        self._prefix = prefix

    def get_password(self, service, username):
        return self._store.get(f"{self._prefix}:{username}")

    def set_password(self, service, username, password):
        self._store[f"{self._prefix}:{username}"] = password

    def delete_password(self, service, username):
        key = f"{self._prefix}:{username}"
        if key in self._store:
            del self._store[key]
        else:
            from keyring.errors import PasswordDeleteError
            raise PasswordDeleteError(f"Not found: {username}")


class _FakeCryptfile(_MockBackend):
    _keyring_key = "mock-password"  # Simulates unlocked cryptfile (private attr)
    file_path = "/tmp/mock.cfg"


@pytest.fixture
def dual_store(monkeypatch):
    """A CredentialStore with mocked dual-write backends."""
    data = {}

    system = _MockBackend(data, "system")
    cryptfile = _FakeCryptfile(data, "cryptfile")

    import credstore._backend as backend
    monkeypatch.setattr(backend, "get_system_keyring", lambda: system)
    monkeypatch.setattr(backend, "get_cryptfile", lambda: cryptfile)
    monkeypatch.setattr(backend, "has_master_key", lambda: True)
    monkeypatch.setattr(backend, "_system_keyring", system)
    monkeypatch.setattr(backend, "_cryptfile", cryptfile)
    monkeypatch.setattr(backend, "init_backend", lambda **kw: None)

    import credstore._store as sm
    store = CredentialStore()
    monkeypatch.setattr(sm, "_store", store)
    monkeypatch.setattr(sm, "_get_store", lambda: store)

    return store, data
