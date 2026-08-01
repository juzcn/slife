"""Tests for credstore._backend — dual-write backend initialization."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("clean_backend_state")]

import credstore._backend as backend


# ── get_system_keyring / _init_system ─────────────────────────────────


class TestGetSystemKeyring:
    """Tests for get_system_keyring and _init_system."""

    def test_returns_cached_instance(self):
        with patch("credstore._backend._init_system", return_value="mock_kr"):
            kr1 = backend.get_system_keyring()
            kr2 = backend.get_system_keyring()
            assert kr1 == kr2 == "mock_kr"

    def test_init_system_success(self):
        import keyring

        mock_kr = MagicMock()
        with patch.object(keyring, "get_keyring", return_value=mock_kr), \
             patch.object(keyring, "set_keyring"):
            result = backend._init_system()
            assert result is mock_kr
            mock_kr.get_password.assert_called_once_with("credstore", "__probe__")

    def test_init_system_get_keyring_raises(self):
        mock_fail = MagicMock()
        mock_fail.Keyring = type("FailKeyring", (), {})
        mock_backends = MagicMock()
        mock_backends.fail = mock_fail
        mock_keyring = MagicMock()
        mock_keyring.backends = mock_backends
        mock_keyring.get_keyring.side_effect = RuntimeError("no backend")
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            result = backend._init_system()
            assert result is None

    def test_init_system_fail_keyring(self):
        from keyring.backends.fail import Keyring as FailKeyring

        mock_fail = MagicMock()
        mock_fail.Keyring = FailKeyring
        mock_backends = MagicMock()
        mock_backends.fail = mock_fail
        mock_keyring = MagicMock()
        mock_keyring.backends = mock_backends
        mock_keyring.get_keyring.return_value = FailKeyring()
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            result = backend._init_system()
            assert result is None

    def test_init_system_probe_raises(self):
        mock_kr = MagicMock()
        mock_kr.get_password.side_effect = OSError("keyring locked")
        mock_fail = MagicMock()
        mock_fail.Keyring = type("FailKeyring", (), {})
        mock_backends = MagicMock()
        mock_backends.fail = mock_fail
        mock_keyring = MagicMock()
        mock_keyring.backends = mock_backends
        mock_keyring.get_keyring.return_value = mock_kr
        with patch.dict("sys.modules", {"keyring": mock_keyring}):
            result = backend._init_system()
            assert result is None

    def test_init_system_probe_raises_wsl_tolerated(self, monkeypatch):
        """On WSL, a probe failure is tolerated — WslBackend is still used.

        The WSL path instantiates ``WslBackend`` directly instead of
        going through ``keyring.get_keyring()``, avoiding irrelevant
        Linux backends (SecretService, keyrings.alt chainer, etc.) that
        can trip over encoding errors on WSL.
        """
        monkeypatch.setattr(backend, "is_wsl", lambda: True)

        mock_wsl = MagicMock()
        mock_wsl.get_password.side_effect = OSError("powershell cold start")
        mock_wsl_mod = MagicMock()
        mock_wsl_mod.WslBackend = MagicMock(return_value=mock_wsl)

        mock_keyring = MagicMock()
        with patch.dict("sys.modules", {
            "keyring": mock_keyring,
            "credstore._wsl_backend": mock_wsl_mod,
        }):
            result = backend._init_system()
            # On WSL, probe failure does NOT return None — backend is viable
            mock_wsl.get_password.assert_called_once_with(
                "credstore", "__probe__"
            )
            assert result is mock_wsl


# ── get_cryptfile / has_master_key ────────────────────────────────────


class TestGetCryptfile:
    """Tests for get_cryptfile."""

    def test_returns_none_initially(self):
        assert backend.get_cryptfile() is None

    def test_returns_set_value(self):
        backend._cryptfile = "mock_cf"
        assert backend.get_cryptfile() == "mock_cf"


class TestHasMasterKey:
    """Tests for has_master_key."""

    def test_no_cryptfile_returns_false(self):
        assert backend.has_master_key() is False

    def test_cryptfile_no_file_returns_false(self, tmp_path):
        mock_cf = MagicMock()
        mock_cf.file_path = str(tmp_path / "nonexistent.crypt")
        backend._cryptfile = mock_cf
        assert backend.has_master_key() is False

    def test_cryptfile_file_exists_returns_true(self, tmp_path):
        cf_path = tmp_path / "exists.crypt"
        cf_path.write_text("data")
        mock_cf = MagicMock()
        mock_cf.file_path = str(cf_path)
        backend._cryptfile = mock_cf
        assert backend.has_master_key() is True


# ── init_backend ──────────────────────────────────────────────────────


class TestInitBackend:
    """Tests for init_backend."""

    def test_init_both_success(self):
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile") as mock_init_cf, \
             patch("credstore._backend.has_master_key", return_value=True):
            backend.init_backend()
            assert backend._system_keyring == "sys_kr"
            mock_init_cf.assert_called_once_with(None)

    def test_init_with_password(self):
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile") as mock_init_cf, \
             patch("credstore._backend.has_master_key", return_value=False):
            backend.init_backend(password="secret123")
            mock_init_cf.assert_called_once_with("secret123")

    def test_init_cryptfile_ready_with_instance(self):
        """When cryptfile is ready and has an instance, logs info."""
        backend._cryptfile = MagicMock()
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile"), \
             patch("credstore._backend.has_master_key", return_value=True):
            backend.init_backend()


# ── reinit_cryptfile ──────────────────────────────────────────────────


class TestReinitCryptfile:
    """Tests for reinit_cryptfile."""

    def test_reinit_logs_on_ready(self):
        with patch("credstore._backend._init_cryptfile") as mock_init, \
             patch("credstore._backend.has_master_key", return_value=True):
            backend.reinit_cryptfile("new_pw")
            mock_init.assert_called_once_with("new_pw")


# ── _init_cryptfile ───────────────────────────────────────────────────


class TestInitCryptfile:
    """Tests for _init_cryptfile."""

    def test_import_error_sets_none(self, monkeypatch):
        backend._cryptfile = "old"
        # Setting a sys.modules entry to None makes Python's import
        # machinery raise ImportError — no __import__ hook needed.
        monkeypatch.setitem(sys.modules, "keyrings.cryptfile.cryptfile", None)
        backend._init_cryptfile()
        assert backend._cryptfile is None

    def test_init_exception_sets_none(self):
        backend._cryptfile = "old"
        mock_cf_cls = MagicMock()
        mock_cf_cls.side_effect = ValueError("bad config")
        with patch.dict(
            "sys.modules",
            {"keyrings.cryptfile.cryptfile": MagicMock(CryptFileKeyring=mock_cf_cls)},
        ):
            backend._init_cryptfile()
            assert backend._cryptfile is None

    def test_init_success_with_password(self, tmp_path):
        cf_path = tmp_path / "test.crypt"
        cf_path.write_text("")

        mock_instance = MagicMock()
        mock_cf_cls = MagicMock(return_value=mock_instance)
        mock_cryptfile_mod = MagicMock(CryptFileKeyring=mock_cf_cls)

        with patch.dict("sys.modules", {"keyrings.cryptfile.cryptfile": mock_cryptfile_mod}), \
             patch("credstore._config.get_cryptfile_path", return_value=str(cf_path)):
            backend._init_cryptfile(password="pw123")
            assert backend._cryptfile is mock_instance
            assert mock_instance.keyring_key == "pw123"
            assert mock_instance.file_path == str(cf_path)


# ── _ensure_dir ───────────────────────────────────────────────────────


class TestEnsureDir:
    """Tests for _ensure_dir."""

    def test_creates_dir_if_missing(self, tmp_path):
        new_dir = tmp_path / "new_subdir"
        backend._ensure_dir(new_dir)
        assert new_dir.exists()

    def test_noop_if_exists(self, tmp_path):
        backend._ensure_dir(tmp_path)
        assert tmp_path.exists()

    def test_chmod_on_posix(self, tmp_path):
        new_dir = tmp_path / "posix_dir"
        with patch.object(os, "name", "posix"):
            backend._ensure_dir(new_dir)
            assert new_dir.exists()


# ── get_backend_info ──────────────────────────────────────────────────


class TestGetBackendInfo:
    """Tests for get_backend_info."""

    def test_basic_info_no_cryptfile(self):
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile"), \
             patch("credstore._backend.has_master_key", return_value=False):
            info = backend.get_backend_info()
            assert info["available"] is True
            assert info["cryptfile_ready"] is False

    def test_info_with_cryptfile(self):
        mock_cf = MagicMock()
        mock_cf._keyring_key = None
        backend._cryptfile = mock_cf
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile"), \
             patch("credstore._backend.has_master_key", return_value=True), \
             patch("credstore._config.get_cryptfile_path", return_value="/tmp/test.crypt"):
            info = backend.get_backend_info()
            assert info["cryptfile_ready"] is True
            assert info["cryptfile_path"] == "/tmp/test.crypt"
            assert info["cryptfile_locked"] is True

    def test_info_cryptfile_unlocked(self):
        mock_cf = MagicMock()
        mock_cf._keyring_key = "some_key"
        backend._cryptfile = mock_cf
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile"), \
             patch("credstore._backend.has_master_key", return_value=True), \
             patch("credstore._config.get_cryptfile_path", return_value="/tmp/test.crypt"):
            info = backend.get_backend_info()
            assert info["cryptfile_locked"] is False


# ── get_active_backend_name ───────────────────────────────────────────


class TestGetActiveBackendName:
    """Tests for get_active_backend_name."""

    def test_dual_write(self):
        backend._system_keyring = "kr"
        backend._cryptfile = MagicMock()
        with patch.object(backend, "has_master_key", return_value=True):
            name = backend.get_active_backend_name()
            assert "dual-write" in name

    def test_system_only(self):
        backend._system_keyring = "kr"
        with patch.object(backend, "has_master_key", return_value=False):
            name = backend.get_active_backend_name()
            assert "system keyring only" in name

    def test_cryptfile_only(self):
        backend._cryptfile = "cf"
        with patch.object(backend, "has_master_key", return_value=True):
            name = backend.get_active_backend_name()
            assert "cryptfile only" in name

    def test_none(self):
        name = backend.get_active_backend_name()
        assert name == "none"


# ── read_cryptfile_entry ────────────────────────────────────────────


class TestReadCryptfileEntry:
    """Tests for read_cryptfile_entry."""

    def test_returns_none_when_cryptfile_is_none(self):
        assert backend.read_cryptfile_entry("key", "pw") is None

    def test_reads_from_unlocked_cryptfile(self, monkeypatch):
        from ._mocks import MockCryptfileBackend

        data = {"key": "secret123"}
        cf = MockCryptfileBackend(data, keyring_key="test-pw")
        monkeypatch.setattr(backend, "_cryptfile", cf)
        # read_cryptfile_entry calls get_cryptfile() + unlocked_cryptfile.
        # keyring uses (service, username) — mock looks up username only.
        result = backend.read_cryptfile_entry("key", "test-pw", "svc")
        assert result == "secret123"
