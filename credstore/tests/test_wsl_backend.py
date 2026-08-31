"""Tests for credstore._wsl_backend — WSL system-keyring bridge."""
from __future__ import annotations

import base64
import subprocess

import pytest


from types import SimpleNamespace


def _fake_result(out="", err="", rc=0):
    return SimpleNamespace(returncode=rc, stdout=out, stderr=err)


def _b64(password: str) -> str:
    return base64.b64encode(password.encode("utf-16-le")).decode("ascii")


# ═══════════════════════════════════════════════════════════════════════
# WslBackend.priority
# ═══════════════════════════════════════════════════════════════════════

class TestWslBackendPriority:

    def test_returns_9_5_on_wsl(self, monkeypatch):
        monkeypatch.setattr("credstore._wsl_backend.is_wsl", lambda: True)
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr(
            "credstore._wsl_backend.subprocess.run",
            lambda cmd, **kw: _fake_result(out="ok"),
        )
        from credstore._wsl_backend import WslBackend
        assert WslBackend.priority == 9.5

    def test_raises_on_non_linux(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Windows")
        from credstore._wsl_backend import WslBackend
        with pytest.raises(RuntimeError, match="requires Linux"):
            _ = WslBackend.priority

    def test_raises_on_non_wsl_linux(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("credstore._wsl_backend.is_wsl", lambda: False)
        from credstore._wsl_backend import WslBackend
        with pytest.raises(RuntimeError, match="requires WSL"):
            _ = WslBackend.priority

    def test_raises_when_powershell_unavailable(self, monkeypatch):
        """powershell.exe missing → RuntimeError on first operation.

        The priority property only checks platform + WSL status;
        the actual powershell probe happens lazily on first use.
        """
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("credstore._wsl_backend.is_wsl", lambda: True)
        monkeypatch.setattr(
            "credstore._wsl_backend.subprocess.run",
            lambda cmd, **kw: _fake_result(out="ok"),
        )

        from credstore._wsl_backend import WslBackend
        assert WslBackend.priority == 9.5

        # Now break powershell and verify the backend raises on use
        def _fail(cmd, **kw):
            raise FileNotFoundError("powershell.exe")
        monkeypatch.setattr("credstore._wsl_backend.subprocess.run", _fail)
        backend = WslBackend()
        with pytest.raises(FileNotFoundError, match="powershell.exe"):
            backend.get_password("svc", "usr")


# ═══════════════════════════════════════════════════════════════════════
# _run_powershell
# ═══════════════════════════════════════════════════════════════════════

class TestRunPowershell:

    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend.subprocess.run",
            lambda cmd, **kw: _fake_result(out="  hello  ", err="warn"),
        )
        from credstore._wsl_backend import _run_powershell
        rc, out, err = _run_powershell("Write-Output test")
        assert rc == 0
        assert out == "hello"
        assert err == "warn"

    def test_encoding_is_utf8_with_replace(self, monkeypatch):
        captured = {}

        def _capture(cmd, **kw):
            captured.update(kw)
            return _fake_result()

        monkeypatch.setattr("credstore._wsl_backend.subprocess.run", _capture)
        from credstore._wsl_backend import _run_powershell
        _run_powershell("Write-Output ok")
        assert captured["encoding"] == "utf-8"
        assert captured["errors"] == "replace"

    def test_nonzero_exit_code(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend.subprocess.run",
            lambda cmd, **kw: _fake_result(rc=5),
        )
        from credstore._wsl_backend import _run_powershell
        rc, _, _ = _run_powershell("exit 5")
        assert rc == 5


# ═══════════════════════════════════════════════════════════════════════
# _get_credential
# ═══════════════════════════════════════════════════════════════════════

class TestGetCredential:

    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, _b64("my-secret"), ""),
        )
        from credstore._wsl_backend import _get_credential
        assert _get_credential("target@svc") == "my-secret"

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (1, "", ""),
        )
        from credstore._wsl_backend import _get_credential
        assert _get_credential("missing@svc") is None

    def test_empty_stdout(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, "", ""),
        )
        from credstore._wsl_backend import _get_credential
        assert _get_credential("empty@svc") is None

    def test_decode_failure(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, "!!!not-base64!!!", ""),
        )
        from credstore._wsl_backend import _get_credential
        assert _get_credential("corrupt@svc") is None

    def test_utf16_decode_failure(self, monkeypatch):
        # unpaired surrogate → invalid UTF-16-LE
        bad = base64.b64encode(b"\x00\xd8\x00\x00").decode("ascii")
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, bad, ""),
        )
        from credstore._wsl_backend import _get_credential
        assert _get_credential("bad-utf16@svc") is None


# ═══════════════════════════════════════════════════════════════════════
# _get_credential_with_user
# ═══════════════════════════════════════════════════════════════════════

class TestGetCredentialWithUser:

    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, _b64("my-secret") + "|juzcn", ""),
        )
        from credstore._wsl_backend import _get_credential_with_user
        assert _get_credential_with_user("t@svc") == ("my-secret", "juzcn")

    def test_not_found(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (1, "", ""),
        )
        from credstore._wsl_backend import _get_credential_with_user
        assert _get_credential_with_user("t@svc") == (None, "")

    def test_empty_blob_preserves_username(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, "|juzcn", ""),
        )
        from credstore._wsl_backend import _get_credential_with_user
        assert _get_credential_with_user("t@svc") == ("", "juzcn")


# ═══════════════════════════════════════════════════════════════════════
# _set_credential / _delete_credential
# ═══════════════════════════════════════════════════════════════════════

class TestSetCredential:

    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, "", ""),
        )
        from credstore._wsl_backend import _set_credential
        assert _set_credential("t@svc", "u", "pwd") is True

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (1, "", "error"),
        )
        from credstore._wsl_backend import _set_credential
        assert _set_credential("t@svc", "u", "pwd") is False


class TestDeleteCredential:

    def test_success(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, "", ""),
        )
        from credstore._wsl_backend import _delete_credential
        assert _delete_credential("t@svc") is True

    def test_failure(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (1, "", "error"),
        )
        from credstore._wsl_backend import _delete_credential
        assert _delete_credential("t@svc") is False


# ═══════════════════════════════════════════════════════════════════════
# WslBackend ops
# ═══════════════════════════════════════════════════════════════════════

class TestWslBackendOps:

    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch):
        monkeypatch.setattr("platform.system", lambda: "Linux")
        monkeypatch.setattr("credstore._wsl_backend.is_wsl", lambda: True)
        monkeypatch.setattr(
            "credstore._wsl_backend.subprocess.run",
            lambda cmd, **kw: _fake_result(out="ok"),
        )

    def test_target_format(self):
        from credstore._wsl_backend import WslBackend
        assert WslBackend._target("credstore", "MY_KEY") == "MY_KEY@credstore"

    def test_get_password_miss(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (1, "", ""),
        )
        from credstore._wsl_backend import WslBackend
        assert WslBackend().get_password("svc", "usr") is None

    def test_get_password_hit(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, _b64("p4ssw0rd"), ""),
        )
        from credstore._wsl_backend import WslBackend
        assert WslBackend().get_password("svc", "usr") == "p4ssw0rd"

    def test_set_password_success(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, "", ""),
        )
        from credstore._wsl_backend import WslBackend
        WslBackend().set_password("svc", "usr", "secret")

    def test_set_password_failure(self, monkeypatch):
        from keyring.errors import PasswordSetError
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (1, "", "denied"),
        )
        from credstore._wsl_backend import WslBackend
        with pytest.raises(PasswordSetError, match="Failed to store"):
            WslBackend().set_password("svc", "usr", "secret")

    def test_delete_password_success(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (0, "", ""),
        )
        from credstore._wsl_backend import WslBackend
        WslBackend().delete_password("svc", "usr")

    def test_delete_password_failure(self, monkeypatch):
        from keyring.errors import PasswordDeleteError
        monkeypatch.setattr(
            "credstore._wsl_backend._run_powershell",
            lambda script: (1, "", "denied"),
        )
        from credstore._wsl_backend import WslBackend
        with pytest.raises(PasswordDeleteError, match="Failed to delete"):
            WslBackend().delete_password("svc", "usr")

    def test_get_password_foreign_layout(self, monkeypatch):
        """Entry stored by WinVaultKeyring (TargetName=service) is found.

        The compound target ``usr@svc`` misses; the service-target fallback
        reads it and matches the stored UserName.
        """
        def _run(script):
            if "GetCredentialWithUser" in script:
                return (0, _b64("oauthtoken") + "|usr", "")
            return (1, "", "")
        monkeypatch.setattr("credstore._wsl_backend._run_powershell", _run)
        from credstore._wsl_backend import WslBackend
        assert WslBackend().get_password("svc", "usr") == "oauthtoken"

    def test_get_password_foreign_layout_username_mismatch(self, monkeypatch):
        """Service-target entry with a different UserName is NOT claimed."""
        def _run(script):
            if "GetCredentialWithUser" in script:
                return (0, _b64("secret") + "|someone-else", "")
            return (1, "", "")
        monkeypatch.setattr("credstore._wsl_backend._run_powershell", _run)
        from credstore._wsl_backend import WslBackend
        assert WslBackend().get_password("svc", "usr") is None

    def test_delete_password_foreign_layout(self, monkeypatch):
        """Delete falls back to the service target when the compound misses."""
        deleted_targets: list[str] = []

        def _del(target):
            if target == "usr@svc":
                return False  # compound absent — mimic CredDelete NOT_FOUND
            deleted_targets.append(target)
            return True

        monkeypatch.setattr(
            "credstore._wsl_backend._delete_credential", _del,
        )
        monkeypatch.setattr(
            "credstore._wsl_backend._get_credential_with_user",
            lambda target: ("", "usr"),
        )
        from credstore._wsl_backend import WslBackend
        WslBackend().delete_password("svc", "usr")
        assert deleted_targets == ["svc"]

    def test_delete_password_foreign_layout_username_mismatch(self, monkeypatch):
        """A service-target entry owned by another username is left alone."""
        from keyring.errors import PasswordDeleteError
        monkeypatch.setattr(
            "credstore._wsl_backend._delete_credential", lambda target: False,
        )
        monkeypatch.setattr(
            "credstore._wsl_backend._get_credential_with_user",
            lambda target: ("", "someone-else"),
        )
        from credstore._wsl_backend import WslBackend
        with pytest.raises(PasswordDeleteError, match="Failed to delete"):
            WslBackend().delete_password("svc", "usr")
