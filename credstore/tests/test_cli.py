"""Tests for the credstore CLI."""

import sys
from io import StringIO
from unittest.mock import MagicMock

import pytest
from credstore.__main__ import main


# ── shared fixtures ──────────────────────────────────────────────


@pytest.fixture
def _tty(monkeypatch):
    """Make sys.stdin.isatty() return True (simulates interactive terminal)."""
    mock_stdin = MagicMock()
    mock_stdin.isatty.return_value = True
    monkeypatch.setattr(sys, "stdin", mock_stdin)


# ═══════════════════════════════════════════════════════════════
# TTY guards — every interactive command rejects non-tty stdin
# ═══════════════════════════════════════════════════════════════

class TestTtyGuards:
    """Non-interactive stdin → hard error for all interactive commands."""

    INTERACTIVE_COMMANDS = [
        ["set-password"],
        ["set", "test/key"],
        ["get", "test/key"],
        ["get", "--password", "test/key"],
        ["delete", "test/key"],
        ["list"],
        ["reset-keyring"],
        ["reset-backup"],
    ]

    @pytest.mark.parametrize("argv", INTERACTIVE_COMMANDS)
    def test_non_tty_rejected(self, argv, mock_backend, monkeypatch):
        """Every interactive command rejects non-tty stdin."""
        monkeypatch.setattr(sys, "stdin", StringIO(""))  # not a tty
        # sys.stdin.isatty() returns False because we replaced stdin
        # but StringIO has isatty() → False natively
        assert main(argv) == 1

    def test_status_allows_non_tty(self, mock_backend, monkeypatch):
        """status is the only command that works without a tty."""
        monkeypatch.setattr(sys, "stdin", StringIO(""))
        assert main(["status"]) == 0


# ═══════════════════════════════════════════════════════════════
# status
# ═══════════════════════════════════════════════════════════════

class TestCliStatus:
    def test_status_returns_zero(self, mock_backend):
        assert main(["status"]) == 0

    def test_status_shows_ready(self, capsys, mock_backend):
        main(["status"])
        out = capsys.readouterr().out
        assert "Backend:" in out
        assert "Available:" in out

    def test_status_shows_cryptfile_locked(self, capsys, mock_backend_locked):
        main(["status"])
        out = capsys.readouterr().out
        assert "LOCKED" in out


# ═══════════════════════════════════════════════════════════════
# set
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliSet:
    def test_set_empty_secret(self, mock_backend, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "")
        assert main(["set", "test/key"]) == 1

    def test_set_success_atomic_dual_write(self, capsys, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """set writes to cryptfile first, then keyring. Both succeed."""
        # Two calls: first for secret, second for master password
        inputs = iter(["sk-test-secret-123", "master-pw"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))
        assert main(["set", "test/key"]) == 0
        out = capsys.readouterr().out
        assert "Stored:" in out
        # Both stores have the value
        assert in_mem_store.get("test/key") == "sk-test-secret-123"
        assert in_mem_cryptfile.get("test/key") == "sk-test-secret-123"

    def test_set_rollback_on_keyring_failure(self, capsys, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """If keyring write fails after cryptfile write, rollback cryptfile."""
        inputs = iter(["sk-secret", "master-pw"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))

        # Make set_credential fail — must patch BOTH sm and the credstore
        # package reference (imported during _cmd_set's `import credstore`)
        def _fail(*args, **kwargs):
            raise RuntimeError("keyring failure")

        import credstore._store as sm
        monkeypatch.setattr(sm, "set_credential", _fail)
        monkeypatch.setattr("credstore.set_credential", _fail)

        assert main(["set", "test/key"]) == 1
        # Credential should NOT be in either store
        assert "test/key" not in in_mem_cryptfile
        assert "test/key" not in in_mem_store

    def test_set_cryptfile_backend_unavailable(self, mock_backend_no_cryptfile, monkeypatch):
        """When cryptfile backend is not installed, set fails hard."""
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "sk-secret")
        assert main(["set", "test/key"]) == 1


# ═══════════════════════════════════════════════════════════════
# get (default mode — keyring only, masked)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliGet:
    def test_get_not_found(self, mock_backend):
        assert main(["get", "nonexistent/key"]) == 1

    def test_get_found_shows_masked(self, capsys, mock_backend, in_mem_store):
        in_mem_store["service/key"] = "sk-secret-test-value-long"
        assert main(["get", "service/key"]) == 0
        out = capsys.readouterr().out
        assert "sk-s…long" in out
        assert "sk-secret-test-value-long" not in out


# ═══════════════════════════════════════════════════════════════
# get --password (dual-query, plaintext, consistency check)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliGetPassword:
    def test_dual_query_match(self, capsys, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """Both stores have the same value → plaintext output."""
        in_mem_store["test/key"] = "sk-match-value"
        in_mem_cryptfile["test/key"] = "sk-match-value"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["get", "--password", "test/key"]) == 0
        out = capsys.readouterr().out
        assert "sk-match-value" in out

    def test_dual_query_not_found_either(self, mock_backend, in_mem_cryptfile, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["get", "--password", "nonexistent"]) == 1

    def test_dual_query_only_in_cryptfile(self, mock_backend, in_mem_cryptfile, monkeypatch):
        """Key in cryptfile but not keyring → error, suggests reset-keyring."""
        in_mem_cryptfile["test/key"] = "sk-cf-only"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        result = main(["get", "--password", "test/key"])
        assert result == 1

    def test_dual_query_only_in_keyring(self, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """Key in keyring but not cryptfile → error, suggests reset-backup."""
        in_mem_store["test/key"] = "sk-kr-only"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["get", "--password", "test/key"]) == 1

    def test_dual_query_mismatch(self, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """Both have the key but values differ → error."""
        in_mem_store["test/key"] = "sk-value-a"
        in_mem_cryptfile["test/key"] = "sk-value-b"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["get", "--password", "test/key"]) == 1

    def test_dual_query_short_flag(self, capsys, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """-p short flag works same as --password."""
        in_mem_store["test/key"] = "sk-value"
        in_mem_cryptfile["test/key"] = "sk-value"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["get", "-p", "test/key"]) == 0

    def test_dual_query_empty_password_rejected(self, mock_backend, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "")
        assert main(["get", "--password", "test/key"]) == 1


# ═══════════════════════════════════════════════════════════════
# delete
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliDelete:
    def test_delete_not_found(self, mock_backend, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "test-master-pw")
        assert main(["delete", "nonexistent"]) == 1

    def test_delete_found(self, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "test-master-pw")
        in_mem_store["test/key"] = "secret"
        in_mem_cryptfile["test/key"] = "secret"
        assert main(["delete", "test/key"]) == 0
        assert "test/key" not in in_mem_store


# ═══════════════════════════════════════════════════════════════
# list
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliList:
    def test_list_empty(self, capsys, mock_backend, in_mem_cryptfile, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["list"]) == 0
        out = capsys.readouterr().out
        assert "No credentials" in out

    def test_list_populated(self, capsys, mock_backend, in_mem_cryptfile, monkeypatch):
        in_mem_cryptfile["svc/key1"] = "v1"
        in_mem_cryptfile["svc/key2"] = "v2"
        in_mem_cryptfile["svc/alpha"] = "v3"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["list"]) == 0
        out = capsys.readouterr().out
        assert "3 credential(s)" in out
        # Sorted alphabetically
        assert "svc/alpha" in out
        assert "svc/key1" in out
        assert "svc/key2" in out
        # Secrets are NOT shown
        assert "v1" not in out
        assert "v2" not in out

    def test_list_empty_password_rejected(self, mock_backend, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "")
        assert main(["list"]) == 1

    def test_list_cryptfile_unavailable(self, mock_backend_no_cryptfile, monkeypatch):
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["list"]) == 1


# ═══════════════════════════════════════════════════════════════
# reset-keyring
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliResetKeyring:
    def test_reset_keyring_success(self, capsys, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        in_mem_cryptfile["test/a"] = "val-a"
        in_mem_cryptfile["test/b"] = "val-b"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["reset-keyring"]) == 0
        out = capsys.readouterr().out
        assert "Restored 2" in out
        assert in_mem_store["test/a"] == "val-a"
        assert in_mem_store["test/b"] == "val-b"


# ═══════════════════════════════════════════════════════════════
# reset-backup
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliResetBackup:
    def test_reset_backup_no_credentials(self, capsys, mock_backend, monkeypatch):
        """No credentials in keyring → inform and exit 0."""
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["reset-backup"]) == 0
        out = capsys.readouterr().out
        assert "No credentials" in out


# ═══════════════════════════════════════════════════════════════
# set-password (first-time & change)
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliSetPassword:
    def test_set_password_first_time(self, capsys, mock_backend, in_mem_cryptfile, monkeypatch):
        """First time: create cryptfile with new password."""
        # Two calls: new password + confirm
        inputs = iter(["new-password-123", "new-password-123"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))
        assert main(["set-password"]) == 0
        out = capsys.readouterr().out
        assert "Master password set" in out

    def test_set_password_mismatch(self, mock_backend, monkeypatch):
        """Password and confirm don't match."""
        inputs = iter(["pw-12345678", "pw-different"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))
        assert main(["set-password"]) == 1

    def test_set_password_too_short(self, mock_backend, monkeypatch):
        """Password must be at least 8 chars."""
        inputs = iter(["short", "short"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))
        assert main(["set-password"]) == 1

    def test_set_password_change(self, capsys, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """Changing password: old data preserved, re-encrypted."""
        in_mem_cryptfile["svc/k1"] = "old-secret-1"
        in_mem_cryptfile["svc/k2"] = "old-secret-2"

        # Mock os.path.exists to make set-password think cryptfile already exists
        import os
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        # Three calls: old password, new password, confirm
        inputs = iter(["old-password", "new-password-123", "new-password-123"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))

        assert main(["set-password"]) == 0
        out = capsys.readouterr().out
        assert "Master password changed" in out
        assert "2" in out  # 2 credentials re-encrypted


# ═══════════════════════════════════════════════════════════════
# fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def in_mem_store():
    """In-memory dict for system keyring."""
    return {}


@pytest.fixture
def in_mem_cryptfile():
    """In-memory dict for cryptfile."""
    return {}


class _MockSystemKeyring:
    """Simulates a system keyring backend (no encryption)."""

    def __init__(self, store: dict):
        self._store = store

    def get_password(self, service, username):
        return self._store.get(username)

    def set_password(self, service, username, password):
        self._store[username] = password

    def delete_password(self, service, username):
        if username not in self._store:
            raise KeyError(username)
        self._store.pop(username, None)


class _MockCryptfile:
    """Simulates a keyrings.cryptfile backend."""

    def __init__(self, store: dict):
        self._store = store
        self.file_path = "/mock/credentials.crypt"
        self._keyring_key = None

    @property
    def keyring_key(self):
        return self._keyring_key

    @keyring_key.setter
    def keyring_key(self, value):
        self._keyring_key = value

    @keyring_key.deleter
    def keyring_key(self):
        self._keyring_key = None

    def get_password(self, service, username):
        if self._keyring_key is None:
            raise ValueError("keyring_key not set")
        return self._store.get(username)

    def set_password(self, service, username, password):
        if self._keyring_key is None:
            raise ValueError("keyring_key not set")
        self._store[username] = password

    def delete_password(self, service, username):
        if self._keyring_key is None:
            raise ValueError("keyring_key not set")
        if username not in self._store:
            raise KeyError(username)
        self._store.pop(username, None)


@pytest.fixture
def mock_backend(monkeypatch, in_mem_store, in_mem_cryptfile):
    """Full mock: system keyring + cryptfile both available."""
    import credstore._backend as backend
    import credstore._store as sm

    monkeypatch.setattr(backend, "init_backend", lambda **kw: None)
    monkeypatch.setattr(backend, "has_master_key", lambda: True)
    monkeypatch.setattr(backend, "get_active_backend_name", lambda: "MockBackend")
    monkeypatch.setattr(backend, "get_backend_info", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": True, "cryptfile_path": "/mock/credentials.crypt",
    })

    # Mock get_cryptfile_path so tests don't depend on CWD state
    import credstore._config as cfg
    monkeypatch.setattr(cfg, "get_cryptfile_path", lambda: "/mock/credentials.crypt")

    # System keyring mock
    sk = _MockSystemKeyring(in_mem_store)
    monkeypatch.setattr(backend, "get_system_keyring", lambda: sk)
    monkeypatch.setattr(backend, "_system_keyring", sk)

    # Cryptfile mock
    cf = _MockCryptfile(in_mem_cryptfile)
    monkeypatch.setattr(backend, "get_cryptfile", lambda: cf)
    monkeypatch.setattr(backend, "_cryptfile", cf)

    # Store mock
    monkeypatch.setattr(sm, "init_store", lambda **kw: None)
    store = sm.CredentialStore()
    store.get = in_mem_store.get
    store.set = in_mem_store.__setitem__
    store.delete = lambda key: in_mem_store.pop(key, None) is not None
    monkeypatch.setattr(sm, "_store", store)
    monkeypatch.setattr(sm, "_get_store", lambda: store)
    monkeypatch.setattr(sm, "get_credential", in_mem_store.get)
    monkeypatch.setattr(sm, "set_credential", in_mem_store.__setitem__)
    monkeypatch.setattr(sm, "delete_credential", lambda k: in_mem_store.pop(k, None) is not None)
    # Also patch the credstore package-level references
    import credstore
    monkeypatch.setattr(credstore, "get_credential", in_mem_store.get)
    monkeypatch.setattr(credstore, "set_credential", in_mem_store.__setitem__)
    monkeypatch.setattr(credstore, "delete_credential", lambda k: in_mem_store.pop(k, None) is not None)
    monkeypatch.setattr(sm, "get_backend_name", lambda: "MockBackend")
    monkeypatch.setattr(sm, "check_backend", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": True, "cryptfile_path": "/mock/credentials.crypt",
    })

    # Mock _read_cryptfile_keys (defined in _store.py, used by both CLI and store)
    _cf_keys = lambda cf: list(in_mem_cryptfile.keys())
    monkeypatch.setattr("credstore._store._read_cryptfile_keys", _cf_keys)

    # Mock _enumerate_system_keyring — never touch real Credential Manager
    monkeypatch.setattr("credstore.__main__._enumerate_system_keyring", lambda service: [])

    return cf


@pytest.fixture
def mock_backend_locked(monkeypatch, in_mem_store, in_mem_cryptfile):
    """Mock where cryptfile exists but is LOCKED."""
    import credstore._backend as backend
    import credstore._store as sm

    monkeypatch.setattr(backend, "init_backend", lambda **kw: None)
    monkeypatch.setattr(backend, "has_master_key", lambda: False)
    monkeypatch.setattr(backend, "get_system_keyring", lambda: _MockSystemKeyring(in_mem_store))
    monkeypatch.setattr(backend, "get_active_backend_name", lambda: "MockBackend")
    monkeypatch.setattr(backend, "get_backend_info", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": False, "cryptfile_locked": True,
        "cryptfile_path": "/mock/credentials.crypt",
    })
    monkeypatch.setattr(sm, "init_store", lambda **kw: None)
    monkeypatch.setattr(sm, "get_backend_name", lambda: "MockBackend")
    monkeypatch.setattr(sm, "check_backend", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": False, "cryptfile_locked": True,
        "cryptfile_path": "/mock/credentials.crypt",
    })
    store = sm.CredentialStore()
    store.get = in_mem_store.get
    monkeypatch.setattr(sm, "_store", store)
    monkeypatch.setattr(sm, "_get_store", lambda: store)
    monkeypatch.setattr(sm, "get_credential", in_mem_store.get)


@pytest.fixture
def mock_backend_no_cryptfile(monkeypatch, in_mem_store):
    """Mock where system keyring is available but cryptfile is not installed."""
    import credstore._backend as backend
    import credstore._store as sm

    monkeypatch.setattr(backend, "init_backend", lambda **kw: None)
    monkeypatch.setattr(backend, "has_master_key", lambda: True)
    monkeypatch.setattr(backend, "get_system_keyring", lambda: _MockSystemKeyring(in_mem_store))
    monkeypatch.setattr(backend, "get_cryptfile", lambda: None)
    monkeypatch.setattr(backend, "_cryptfile", None)
    monkeypatch.setattr(backend, "get_active_backend_name", lambda: "MockBackend")
    monkeypatch.setattr(backend, "get_backend_info", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": False,
    })
    monkeypatch.setattr(sm, "init_store", lambda **kw: None)
    store = sm.CredentialStore()
    store.get = in_mem_store.get
    store.set = in_mem_store.__setitem__
    store.delete = lambda key: in_mem_store.pop(key, None) is not None
    monkeypatch.setattr(sm, "_store", store)
    monkeypatch.setattr(sm, "_get_store", lambda: store)
    monkeypatch.setattr(sm, "get_credential", in_mem_store.get)
    monkeypatch.setattr(sm, "set_credential", in_mem_store.__setitem__)
    monkeypatch.setattr(sm, "delete_credential", lambda k: in_mem_store.pop(k, None) is not None)
    import credstore
    monkeypatch.setattr(credstore, "get_credential", in_mem_store.get)
    monkeypatch.setattr(credstore, "set_credential", in_mem_store.__setitem__)
    monkeypatch.setattr(credstore, "delete_credential", lambda k: in_mem_store.pop(k, None) is not None)
    monkeypatch.setattr(sm, "get_backend_name", lambda: "MockBackend")
    monkeypatch.setattr(sm, "check_backend", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": False,
    })
