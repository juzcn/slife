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


class TestFormatExport:
    """Tests for format_export() — shell export formatting."""

    def test_bash_simple(self):
        from credstore._store import format_export
        result = format_export("MY_KEY", "my-secret", "bash")
        assert result == "export MY_KEY='my-secret'"

    def test_bash_with_single_quote(self):
        from credstore._store import format_export
        result = format_export("KEY", "val'ue", "bash")
        assert result == "export KEY='val'\\''ue'"

    def test_powershell(self):
        from credstore._store import format_export
        result = format_export("MY_KEY", "my-secret", "powershell")
        assert result == "$env:MY_KEY = 'my-secret'"

    def test_powershell_backtick_escape(self):
        from credstore._store import format_export
        result = format_export("KEY", "abc`def", "powershell")
        assert result == "$env:KEY = 'abc``def'"

    def test_cmd(self):
        from credstore._store import format_export
        result = format_export("MY_KEY", "my-secret", "cmd")
        assert result == "set MY_KEY=my-secret"

    def test_auto_windows(self, monkeypatch):
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.delenv("PROMPT", raising=False)  # clear cmd.exe indicator
        monkeypatch.setenv("PSModulePath", "C:\\Modules")  # force PowerShell
        from credstore._store import format_export
        result = format_export("KEY", "val", "auto")
        assert result.startswith("$env:KEY")

    def test_auto_unix(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        from credstore._store import format_export
        result = format_export("KEY", "val", "auto")
        assert result.startswith("export KEY")


class TestFormatUnset:
    """Tests for format_unset() — shell environment variable removal."""

    def test_bash(self):
        from credstore._store import format_unset
        assert format_unset("MY_KEY", "bash") == "unset MY_KEY"

    def test_powershell(self):
        from credstore._store import format_unset
        assert format_unset("MY_KEY", "powershell") == "Remove-Item Env:MY_KEY"

    def test_cmd(self):
        from credstore._store import format_unset
        assert format_unset("MY_KEY", "cmd") == "set MY_KEY="

    def test_auto_windows(self, monkeypatch):
        monkeypatch.setattr("os.name", "nt")
        monkeypatch.delenv("PROMPT", raising=False)
        monkeypatch.setenv("PSModulePath", "C:\\Modules")
        from credstore._store import format_unset
        result = format_unset("KEY", "auto")
        assert result == "Remove-Item Env:KEY"

    def test_auto_unix(self, monkeypatch):
        monkeypatch.setattr("os.name", "posix")
        from credstore._store import format_unset
        result = format_unset("KEY", "auto")
        assert result == "unset KEY"

    def test_unknown_shell_raises(self):
        from credstore._store import format_unset
        with pytest.raises(ValueError):
            format_unset("KEY", "fish")


class TestProfilePersistence:
    """Tests for profile-based persistence (add_to_profile / remove_from_profile)."""

    def test_get_profile_path_powershell(self, monkeypatch):
        monkeypatch.setitem(os.environ, "PROFILE", "C:\\Users\\me\\Documents\\PowerShell\\profile.ps1")
        monkeypatch.setitem(os.environ, "PSModulePath", "C:\\Modules")  # not cmd.exe
        from credstore._shell import get_profile_path
        p = get_profile_path("powershell")
        assert p.name == "Microsoft.PowerShell_profile.ps1" or "profile" in str(p).lower()

    def test_get_profile_path_cmd(self, monkeypatch):
        """Windows uses registry — cmd.exe has no profile file."""
        monkeypatch.setitem(os.environ, "PROMPT", "$P$G")
        from credstore._shell import get_profile_path
        p = get_profile_path("cmd")
        assert p is None  # Windows uses registry, not profile

    def test_get_profile_path_bash(self, monkeypatch):
        monkeypatch.setattr("os.environ", {"HOME": "/home/user"})
        from credstore._shell import get_profile_path
        p = get_profile_path("bash")
        assert p.name == ".bashrc"

    def test_add_to_profile_creates_file(self, tmp_path, monkeypatch):
        """add_to_profile creates profile file and appends inject line."""
        monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
        from credstore._shell import add_to_profile
        assert add_to_profile("MY_KEY", "bash") is True
        content = (tmp_path / ".bashrc").read_text()
        assert "# credstore: MY_KEY" in content
        assert "credstore inject MY_KEY" in content

    def test_add_to_profile_overwrites_existing(self, tmp_path, monkeypatch):
        """Second inject of same key overwrites, doesn't duplicate."""
        profile = tmp_path / ".bashrc"
        profile.write_text("# credstore: MY_KEY\neval old-line\n")
        monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
        from credstore._shell import add_to_profile
        assert add_to_profile("MY_KEY", "bash") is True
        content = profile.read_text()
        assert "old-line" not in content
        assert "credstore inject MY_KEY" in content
        assert content.count("# credstore: MY_KEY") == 1

    def test_remove_from_profile_removes_key(self, tmp_path, monkeypatch):
        profile = tmp_path / ".bashrc"
        profile.write_text("export FOO=bar\n# credstore: MY_KEY\neval old-line\n# credstore: OTHER\neval other\n")
        monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
        from credstore._shell import remove_from_profile
        assert remove_from_profile("MY_KEY", "bash") is True
        content = profile.read_text()
        assert "MY_KEY" not in content
        assert "OTHER" in content

    def test_remove_from_profile_not_found(self, tmp_path, monkeypatch):
        profile = tmp_path / ".bashrc"
        profile.write_text("export FOO=bar\n")
        monkeypatch.setitem(os.environ, "HOME", str(tmp_path))
        from credstore._shell import remove_from_profile
        assert remove_from_profile("NONEXISTENT", "bash") is False

    def test_remove_key_lines_cleans_trailing_blanks(self):
        from credstore._shell import _remove_key_lines
        content = "\n# credstore: X\neval x\n\n\n"
        result = _remove_key_lines(content, "X")
        assert not result.endswith("\n\n")

    def test_powershell_profile_line_format(self):
        from credstore._shell import _make_profile_line
        result = _make_profile_line("MY_KEY", "powershell")
        assert "# credstore: MY_KEY" in result
        assert "Invoke-Expression" in result
        assert "2>$null" in result

    def test_cmd_profile_line_format(self):
        """Windows uses registry — _make_profile_line not used for cmd."""
        from credstore._shell import _make_profile_line
        result = _make_profile_line("MY_KEY", "cmd")
        # Falls through to bash format when not powershell
        assert "eval" in result

    def test_bash_profile_line_format(self):
        from credstore._shell import _make_profile_line
        result = _make_profile_line("MY_KEY", "bash")
        assert "# credstore: MY_KEY" in result
        assert "eval" in result


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
