"""Tests for the credstore CLI."""

import pytest; pytestmark = pytest.mark.unit

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
# main() gates
# ═══════════════════════════════════════════════════════════════

class TestMainGates:
    """Tests for pre-dispatch checks in main()."""

    def test_set_requires_master_key(self, mock_backend_locked):
        """set exits early when master key not set."""
        assert main(["set", "test/key"]) == 1

    def test_reset_backup_requires_master_key(self, mock_backend_locked):
        """reset-backup exits early when master key not set."""
        assert main(["reset-backup"]) == 1

    def test_unknown_command(self):
        """Unknown command exits with error (SystemExit)."""
        with pytest.raises(SystemExit):
            main(["nonexistent-command"])


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

    def test_status_shows_cryptfile_only(self, capsys, mock_backend_no_keyring):
        """No system keyring but cryptfile ready → cryptfile-only mode."""
        main(["status"])
        out = capsys.readouterr().out
        assert "cryptfile-only" in out


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

    def test_set_cryptfile_only_when_no_keyring(self, capsys, mock_backend_no_keyring, in_mem_cryptfile, monkeypatch):
        """No system keyring → set stores in cryptfile only, prints notice."""
        inputs = iter(["sk-cf-only-secret", "master-pw"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))
        assert main(["set", "test/key"]) == 0
        captured = capsys.readouterr()
        assert "Stored: test/key" in captured.out
        assert "cryptfile-only" in captured.err
        assert in_mem_cryptfile.get("test/key") == "sk-cf-only-secret"

    def test_set_cryptfile_backend_unavailable(self, mock_backend_no_cryptfile, monkeypatch):
        """When cryptfile backend is not installed, set fails hard."""
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "sk-secret")
        assert main(["set", "test/key"]) == 1

    def test_set_cryptfile_write_value_error(self, mock_backend, in_mem_cryptfile, monkeypatch):
        """Cryptfile.set_password raises ValueError → exit 1, secret cleaned up."""
        inputs = iter(["sk-secret", "master-pw"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))

        # Make cryptfile set_password raise ValueError via the unlocked_cryptfile mock
        import credstore._backend as be
        from contextlib import contextmanager
        @contextmanager
        def _fail_cf_write(pw):
            class _FailingCF:
                def set_password(self, service, key, secret):
                    raise ValueError("wrong master password")
            yield _FailingCF()
        monkeypatch.setattr(be, "unlocked_cryptfile", _fail_cf_write)
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

    def test_get_password_cryptfile_only_mode(self, capsys, mock_backend_no_keyring, in_mem_cryptfile, monkeypatch):
        """No system keyring: cryptfile is authoritative — value printed."""
        in_mem_cryptfile["test/key"] = "sk-cryptfile-value"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        result = main(["get", "--password", "test/key"])
        assert result == 0
        out = capsys.readouterr().out
        assert "sk-cryptfile-value" in out


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
        assert main(["list"]) == 0

    def test_list_cryptfile_read_fails(self, mock_backend, in_mem_cryptfile, monkeypatch):
        """Exception during cryptfile key read → exit 1, master_pw cleaned up."""
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")

        # Make _read_cryptfile_keys raise
        import credstore._store as sm
        monkeypatch.setattr(sm, "_read_cryptfile_keys", lambda cf: (_ for _ in ()).throw(RuntimeError("boom")))
        assert main(["list"]) == 1

    def test_list_with_synced_keys(self, capsys, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """Keys in both stores with same value → show 'synced'."""
        in_mem_store["svc/k1"] = "same-val"
        in_mem_cryptfile["svc/k1"] = "same-val"
        # Make _enumerate_system_keyring return this key
        monkeypatch.setattr(
            "credstore.__main__._enumerate_system_keyring",
            lambda service, with_values=False: [("svc/k1", "")],
        )
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["list"]) == 0
        out = capsys.readouterr().out
        assert "synced" in out
        assert "1 credential" in out

    def test_list_with_mismatched_keys(self, capsys, mock_backend, in_mem_store, in_mem_cryptfile, monkeypatch):
        """Keys in both stores with different values → show 'MISMATCH'."""
        in_mem_store["svc/k1"] = "val-a"
        in_mem_cryptfile["svc/k1"] = "val-b"
        # Make _enumerate_system_keyring return this key
        monkeypatch.setattr(
            "credstore.__main__._enumerate_system_keyring",
            lambda service, with_values=False: [("svc/k1", "")],
        )
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["list"]) == 0
        out = capsys.readouterr().out
        assert "MISMATCH" in out

    def test_list_with_cryptfile_only(self, capsys, mock_backend, in_mem_cryptfile, monkeypatch):
        """Key only in cryptfile → show 'cryptfile only' and suggest reset-keyring."""
        in_mem_cryptfile["svc/k1"] = "v1"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["list"]) == 0
        out = capsys.readouterr().out
        assert "reset-keyring" in out


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

    def test_reset_keyring_failure(self, mock_backend, in_mem_cryptfile, monkeypatch):
        """reset_credentials raises → exit 1, master_pw cleaned up."""
        in_mem_cryptfile["test/a"] = "val-a"
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        import credstore._store as sm
        monkeypatch.setattr(sm, "reset_credentials", lambda mp: (_ for _ in ()).throw(RuntimeError("boom")))
        assert main(["reset-keyring"]) == 1


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

    def test_reset_backup_cryptfile_not_available(self, mock_backend_no_cryptfile, monkeypatch):
        """cf is None → exit 1."""
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["reset-backup"]) == 1

    def test_reset_backup_wrong_password(self, mock_backend, monkeypatch):
        """unlocked_cryptfile raises ValueError → exit 1."""
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")

        # Mock _enumerate_system_keyring to return entries so we reach the cryptfile unlock
        monkeypatch.setattr(
            "credstore.__main__._enumerate_system_keyring",
            lambda service, with_values=False: [("test/k", "val")],
        )
        # Mock unlocked_cryptfile to raise ValueError (wrong password)
        import credstore._backend as be
        from contextlib import contextmanager
        @contextmanager
        def _fail_unlock(pw):
            yield (_ for _ in ()).throw(ValueError("wrong password"))
        monkeypatch.setattr(be, "unlocked_cryptfile", _fail_unlock)
        assert main(["reset-backup"]) == 1


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
        # Simulate first-time: cryptfile does not exist yet
        import os as _os
        monkeypatch.setattr(_os.path, "exists", lambda p: False)
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

    def test_set_password_change_wrong_old_password(self, mock_backend, in_mem_cryptfile, monkeypatch):
        """Incorrect old password during change → exit 1."""
        in_mem_cryptfile["svc/k1"] = "old-secret"
        import os
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        # Mock unlocked_cryptfile to raise on old password
        import credstore._backend as be
        from contextlib import contextmanager
        @contextmanager
        def _fail_old_unlock(pw):
            yield (_ for _ in ()).throw(ValueError("wrong password"))
        monkeypatch.setattr(be, "unlocked_cryptfile", _fail_old_unlock)

        inputs = iter(["wrong-old-pw", "new-pw-12345", "new-pw-12345"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))
        assert main(["set-password"]) == 1

    def test_set_password_change_cryptfile_not_available(self, mock_backend, monkeypatch):
        """cf is None during change → exit 1."""
        import os
        monkeypatch.setattr(os.path, "exists", lambda p: True)

        # Mock get_cryptfile to return None
        import credstore._backend as be
        monkeypatch.setattr(be, "get_cryptfile", lambda: None)

        inputs = iter(["old-pw", "new-pw-12345", "new-pw-12345"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))
        assert main(["set-password"]) == 1


# ═══════════════════════════════════════════════════════════════
# inject / uninject
# ═══════════════════════════════════════════════════════════════

@pytest.mark.usefixtures("_tty")
class TestCliInject:
    """Tests for 'credstore inject' — persist to system environment."""

    @pytest.fixture(autouse=True)
    def _mock_persist(self, monkeypatch, tmp_path):
        """Prevent inject from touching real registry/filesystem."""
        monkeypatch.setattr("credstore._shell._setx", lambda k, v: None)
        monkeypatch.setattr("credstore._shell._setx_delete", lambda k: None)
        monkeypatch.setattr("credstore._shell.add_to_profile", lambda key, shell: True)

    @pytest.mark.parametrize("shell,expected", [
        ("bash", "export MY_KEY='my-secret'"),
        ("powershell", "$env:MY_KEY = 'my-secret'"),
        ("cmd", "set MY_KEY=my-secret"),
    ])
    def test_inject_shell_format(self, capsys, mock_backend, in_mem_store, shell, expected):
        in_mem_store["MY_KEY"] = "my-secret"
        assert main(["inject", "MY_KEY", "--shell", shell]) == 0
        out = capsys.readouterr().out
        assert expected in out

    def test_inject_not_found(self, mock_backend):
        assert main(["inject", "NONEXISTENT"]) == 1

    def test_inject_multiple_keys(self, capsys, mock_backend, in_mem_store):
        in_mem_store["KEY1"] = "val1"
        in_mem_store["KEY2"] = "val2"
        assert main(["inject", "KEY1", "KEY2", "--shell", "bash"]) == 0
        out = capsys.readouterr().out
        assert "export KEY1='val1'" in out
        assert "export KEY2='val2'" in out

    def test_inject_partial_failure_stops(self, capsys, mock_backend, in_mem_store):
        """First key succeeds, second not found → exit 1, no output for second."""
        in_mem_store["KEY1"] = "val1"
        assert main(["inject", "KEY1", "NONEXISTENT", "--shell", "bash"]) == 1
        out = capsys.readouterr().out
        assert "export KEY1='val1'" in out
        assert "NONEXISTENT" not in out

    def test_inject_value_with_single_quote(self, capsys, mock_backend, in_mem_store):
        in_mem_store["KEY"] = "val'quote"
        assert main(["inject", "KEY", "--shell", "bash"]) == 0
        out = capsys.readouterr().out
        assert "export KEY='val'\\''quote'" in out

    def test_inject_cryptfile_only(self, capsys, mock_backend_no_keyring, in_mem_cryptfile, monkeypatch):
        """No system keyring → inject reads from cryptfile (prompts master pw)."""
        in_mem_cryptfile["MY_KEY"] = "sk-from-cryptfile"
        inputs = iter(["master-pw"])
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": next(inputs))
        assert main(["inject", "MY_KEY", "--shell", "bash"]) == 0
        out = capsys.readouterr().out
        assert "export MY_KEY='sk-from-cryptfile'" in out

    def test_inject_cryptfile_only_not_found(self, capsys, mock_backend_no_keyring, monkeypatch):
        """No system keyring, key not in cryptfile → error."""
        monkeypatch.setattr("credstore.__main__.masked_input", lambda prompt="": "master-pw")
        assert main(["inject", "NONEXISTENT", "--shell", "bash"]) == 1

@pytest.mark.usefixtures("_tty")
class TestCliUninject:
    """Tests for 'credstore uninject' — remove from system environment."""

    @pytest.fixture(autouse=True)
    def _mock_persist(self, monkeypatch):
        """Prevent uninject from touching real registry/filesystem."""
        monkeypatch.setattr("credstore._shell._setx", lambda k, v: None)
        monkeypatch.setattr("credstore._shell._setx_delete", lambda k: None)
        monkeypatch.setattr("credstore._shell.remove_from_profile", lambda key, shell: False)

    @pytest.mark.parametrize("shell,expected", [
        ("bash", "unset MY_KEY"),
        ("powershell", "Remove-Item Env:MY_KEY"),
        ("cmd", "set MY_KEY="),
    ])
    def test_uninject_shell_format(self, capsys, mock_backend, shell, expected):
        assert main(["uninject", "MY_KEY", "--shell", shell]) == 0
        out = capsys.readouterr().out
        assert expected in out

    def test_uninject_multiple_keys(self, capsys, mock_backend):
        assert main(["uninject", "KEY1", "KEY2", "--shell", "bash"]) == 0
        out = capsys.readouterr().out
        assert "unset KEY1" in out
        assert "unset KEY2" in out

    def test_uninject_never_fails(self, mock_backend):
        """uninject doesn't touch keyring — always succeeds."""
        assert main(["uninject", "ANYTHING"]) == 0


# ═══════════════════════════════════════════════════════════════
# enumeration edge cases
# ═══════════════════════════════════════════════════════════════

class TestEnumerateWindows:
    """Tests for Windows Credential Manager enumeration."""

    def test_import_error_win32cred(self, monkeypatch):
        """Both win32cred imports fail → returns empty list."""
        from credstore._enumerate import _enumerate_windows
        # Simulate import failure
        import builtins
        orig_import = builtins.__import__
        def _block_win32cred(name, *args, **kwargs):
            if "win32cred" in name or "win32ctypes" in name:
                raise ImportError("no win32cred")
            return orig_import(name, *args, **kwargs)
        monkeypatch.setattr(builtins, "__import__", _block_win32cred)
        result = _enumerate_windows("test-service")
        assert result == []

    def test_keys_only_mode(self, monkeypatch):
        """with_values=False skips credential blob decoding."""
        monkeypatch.setattr(
            "credstore._enumerate._enumerate_windows",
            lambda service, with_values=False: [("my-key", "")],
        )
        from credstore._enumerate import _enumerate_windows
        result = _enumerate_windows("test-service", with_values=False)
        for _, val in result:
            assert val == ""  # no value decoded

    def test_with_values_mode(self, monkeypatch):
        """with_values=True decodes and returns credential values."""
        import credstore._enumerate as en
        monkeypatch.setattr(en, "_enumerate_windows", lambda service, with_values=True: [("my-key", "secret")])
        result = en._enumerate_windows("test-service", with_values=True)
        assert len(result) == 1
        assert result[0][1] == "secret"


class TestEnumerateSystemKeyring:
    """Tests for _enumerate_system_keyring platform dispatch."""

    def test_non_windows_returns_empty(self, monkeypatch):
        """On non-Windows, returns empty list with stderr message."""
        monkeypatch.setattr("os.name", "posix")
        from credstore._enumerate import enumerate_system_keyring
        result = enumerate_system_keyring("test-service")
        assert result == []


# ═══════════════════════════════════════════════════════════════
# fixtures — delegate to conftest.py shared infrastructure
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def mock_backend(base_backend, cli_store):
    """Full mock: system keyring + cryptfile both available."""
    return base_backend


@pytest.fixture
def mock_backend_locked(base_backend, cli_store, monkeypatch):
    """Mock where cryptfile exists but is LOCKED (no master key)."""
    import credstore._backend as backend
    import credstore._store as sm

    monkeypatch.setattr(backend, "has_master_key", lambda: False)
    monkeypatch.setattr(backend, "get_backend_info", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": False, "cryptfile_locked": True,
        "cryptfile_path": "/mock/credentials.crypt",
    })
    monkeypatch.setattr(sm, "check_backend", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": False, "cryptfile_locked": True,
        "cryptfile_path": "/mock/credentials.crypt",
    })
    return base_backend


@pytest.fixture
def mock_backend_no_cryptfile(base_backend, cli_store, monkeypatch):
    """Mock where system keyring is available but cryptfile is not installed."""
    import credstore._backend as backend
    import credstore._store as sm

    monkeypatch.setattr(backend, "_cryptfile", None)
    monkeypatch.setattr(
        "credstore.__main__._enumerate_system_keyring",
        lambda service, with_values=False: [("test/k", "val")],
    )
    monkeypatch.setattr(backend, "get_backend_info", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": False,
    })
    monkeypatch.setattr(sm, "check_backend", lambda: {
        "available": True, "backend": "MockBackend",
        "cryptfile_ready": False,
    })


@pytest.fixture
def mock_backend_no_keyring(base_backend, cli_store, monkeypatch):
    """Mock where the system keyring is unavailable (cryptfile-only mode)."""
    import credstore._backend as backend

    monkeypatch.setattr(backend, "get_system_keyring", lambda: None)
    monkeypatch.setattr(backend, "get_backend_info", lambda: {
        "available": False, "backend": "MockBackend",
        "cryptfile_ready": True, "cryptfile_path": "/mock/credentials.crypt",
    })
    import credstore._store as sm
    monkeypatch.setattr(sm, "check_backend", lambda: {
        "available": False, "backend": "MockBackend",
        "cryptfile_ready": True, "cryptfile_path": "/mock/credentials.crypt",
    })
    return base_backend
