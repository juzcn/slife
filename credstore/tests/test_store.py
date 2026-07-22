"""Tests for credential store operations."""

import os

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


class TestCredentialStoreEdgeCases:
    """Tests for error paths and edge cases in CredentialStore."""

    def test_get_without_keyring(self, monkeypatch):
        """get() returns None when system keyring is unavailable."""
        store = CredentialStore()
        import credstore._backend as be
        monkeypatch.setattr(be, "get_system_keyring", lambda: None)
        assert store.get("any-key") is None

    def test_exists_returns_false_for_missing(self, dual_store):
        store, _data = dual_store
        assert store.exists("nonexistent-key") is False

    def test_exists_returns_true_for_stored(self, dual_store):
        store, _data = dual_store
        store.set("exists-test", "secret")
        assert store.exists("exists-test") is True

    def test_list_keys_returns_list(self, dual_store):
        store, _data = dual_store
        result = store.list_keys()
        assert isinstance(result, list)

    def test_list_keys_without_keyring(self, monkeypatch):
        """list_keys() returns empty list when keyring unavailable."""
        store = CredentialStore()
        import credstore._backend as be
        monkeypatch.setattr(be, "get_system_keyring", lambda: None)
        assert store.list_keys() == []

    def test_set_requires_master_key(self, dual_store, monkeypatch):
        """set() raises RuntimeError when master key not set."""
        store, _data = dual_store
        import credstore._backend as be
        monkeypatch.setattr(be, "has_master_key", lambda: False)
        with pytest.raises(RuntimeError, match="Master key not set"):
            store.set("key", "secret")

    def test_set_requires_system_keyring(self, dual_store, monkeypatch):
        """set() raises RuntimeError when system keyring unavailable."""
        store, _data = dual_store
        import credstore._backend as be
        monkeypatch.setattr(be, "get_system_keyring", lambda: None)
        with pytest.raises(RuntimeError, match="No system keyring"):
            store.set("key", "secret")

    def test_reset_no_keyring(self, monkeypatch):
        """reset() raises RuntimeError when system keyring unavailable."""
        store = CredentialStore()
        import credstore._backend as be
        monkeypatch.setattr(be, "get_system_keyring", lambda: None)
        with pytest.raises(RuntimeError, match="No system keyring"):
            store.reset("master-pw")

    def test_reset_skip_on_get_error(self, dual_store, monkeypatch, tmp_path):
        """reset() skips keys that fail during get_password."""
        store, data = dual_store
        import credstore._backend as be
        from credstore._store import DEFAULT_SERVICE
        from contextlib import contextmanager

        # Write a real cryptfile INI so _read_cryptfile_keys can parse it
        cf_path = tmp_path / "test.crypt"
        cf_path.write_text(
            f"[{DEFAULT_SERVICE}]\n"
            "good-key = good-val\n"
            "bad-key = bad-val\n"
        )

        @contextmanager
        def _unlock_with_bad_key(pw):
            class _BadCF:
                file_path = str(cf_path)
                def get_password(self, service, key):
                    if key == "BAD-KEY":  # _read_cryptfile_keys uppercases keys
                        raise RuntimeError("decrypt error")
                    return data.get(key)
            yield _BadCF()

        monkeypatch.setattr(be, "unlocked_cryptfile", _unlock_with_bad_key)
        data["GOOD-KEY"] = "good-val"
        data["BAD-KEY"] = "bad-val"
        count = store.reset("pw")
        assert count == 1  # only good-key restored


class TestStoreModuleApi:
    """Tests for the module-level convenience functions."""

    def test_init_store_with_password(self, monkeypatch):
        """init_store(password=...) passes password to init_backend."""
        from credstore._store import init_store, _store
        import credstore._backend as be
        monkeypatch.setattr(be, "init_backend", lambda password=None: None)
        # Reset singleton
        import credstore._store as sm
        monkeypatch.setattr(sm, "_store", None)
        monkeypatch.setattr(be, "has_master_key", lambda: True)
        store = init_store(password="test-pw")
        assert isinstance(store, CredentialStore)

    def test_get_store_lazy_init(self, monkeypatch):
        """_get_store() lazy-inits when _store is None."""
        import credstore._store as sm
        monkeypatch.setattr(sm, "_store", None)
        import credstore._backend as be
        monkeypatch.setattr(be, "init_backend", lambda password=None: None)
        monkeypatch.setattr(be, "has_master_key", lambda: True)
        store = sm._get_store()
        assert isinstance(store, CredentialStore)

    def test_list_credential_keys_module(self, monkeypatch):
        """list_credential_keys() returns list without values."""
        from credstore._store import list_credential_keys
        result = list_credential_keys()
        assert isinstance(result, list)

    def test_get_credential_returns_none(self, monkeypatch):
        """get_credential returns None for missing key."""
        import credstore._store as sm
        monkeypatch.setattr(sm, "_store", None)
        import credstore._backend as be
        monkeypatch.setattr(be, "init_backend", lambda password=None: None)
        monkeypatch.setattr(be, "get_system_keyring", lambda: None)
        monkeypatch.setattr(be, "has_master_key", lambda: True)
        assert sm.get_credential("nonexistent") is None

    def test_read_cryptfile_keys_parses_ini(self, tmp_path):
        """_read_cryptfile_keys parses cryptfile INI format."""
        from credstore._store import _read_cryptfile_keys, DEFAULT_SERVICE
        ini_content = (
            "[DEFAULT]\n"
            "keyring = cryptfile\n"
            f"[{DEFAULT_SERVICE}]\n"
            "test_key = escaped-value\n"
        )
        cf_path = tmp_path / "test.crypt"
        cf_path.write_text(ini_content)

        class _MockCF:
            file_path = str(cf_path)
        keys = _read_cryptfile_keys(_MockCF())
        # keyrings.cryptfile escape module normalises keys
        assert isinstance(keys, list)

    def test_read_cryptfile_keys_skips_other_sections(self, tmp_path):
        """_read_cryptfile_keys ignores non-credstore sections."""
        from credstore._store import _read_cryptfile_keys, DEFAULT_SERVICE
        ini_content = (
            "[keyring]\n"
            "something = ignored\n"
            "[other-app]\n"
            "their_key = their-val\n"
            f"[{DEFAULT_SERVICE}]\n"
            "my_key = my-val\n"
        )
        cf_path = tmp_path / "test2.crypt"
        cf_path.write_text(ini_content)

        class _MockCF:
            file_path = str(cf_path)
        keys = _read_cryptfile_keys(_MockCF())
        # Only DEFAULT_SERVICE keys are returned
        assert len(keys) == 1

    def test_set_credential_module_wrapper(self, dual_store, monkeypatch):
        """set_credential() delegates to store.set()."""
        from credstore._store import set_credential, _store
        store, data = dual_store
        monkeypatch.setattr("credstore._store._store", store)
        set_credential("my-key", "my-secret")
        # Mock uses prefix: service:key
        assert data.get("system:my-key") == "my-secret"

    def test_delete_credential_module_wrapper(self, dual_store, monkeypatch):
        """delete_credential() delegates to store.delete()."""
        from credstore._store import delete_credential, _store
        store, data = dual_store
        data["system:my-key"] = "my-secret"
        monkeypatch.setattr("credstore._store._store", store)
        assert delete_credential("my-key") is True
        assert "system:my-key" not in data

    def test_exists_credential_module_wrapper(self, monkeypatch):
        """exists_credential() returns bool without retrieving value."""
        from credstore._store import exists_credential, _store, CredentialStore
        store = CredentialStore()
        monkeypatch.setattr("credstore._store._store", store)
        import credstore._backend as be
        monkeypatch.setattr(be, "get_system_keyring", lambda: None)
        monkeypatch.setattr(be, "has_master_key", lambda: True)
        # No system keyring → all gets return None → exists is False
        assert exists_credential("anything") is False

    def test_get_backend_name_module(self, monkeypatch):
        """get_backend_name() returns a string."""
        import credstore._backend as be
        monkeypatch.setattr(be, "get_active_backend_name", lambda: "test-backend")
        from credstore._store import get_backend_name
        assert get_backend_name() == "test-backend"

    def test_check_backend_module(self, monkeypatch):
        """check_backend() returns a dict."""
        import credstore._backend as be
        monkeypatch.setattr(be, "get_backend_info", lambda: {"available": True})
        from credstore._store import check_backend
        result = check_backend()
        assert result["available"] is True


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
