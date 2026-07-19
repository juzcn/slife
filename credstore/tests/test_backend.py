"""Tests for credstore._backend — dual-write backend initialization."""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import credstore._backend as backend


# ── Helpers ───────────────────────────────────────────────────────────


def _mock_keyring_module():
    """Create a mock keyring module with get_keyring and set_keyring."""
    mock_kr = MagicMock()
    mock_kr.get_password.return_value = None
    return mock_kr


# ── get_system_keyring / _init_system ─────────────────────────────────


class TestGetSystemKeyring:
    """Tests for get_system_keyring and _init_system."""

    def test_returns_cached_instance(self):
        backend._system_keyring = None
        with patch("credstore._backend._init_system", return_value="mock_kr"):
            kr1 = backend.get_system_keyring()
            kr2 = backend.get_system_keyring()
            assert kr1 == kr2 == "mock_kr"

    def test_init_system_success(self):
        backend._system_keyring = None
        import keyring
        mock_kr = MagicMock()
        # Mock keyring.get_keyring at the real module level
        with patch.object(keyring, "get_keyring", return_value=mock_kr), \
             patch.object(keyring, "set_keyring"):
            result = backend._init_system()
            assert result is mock_kr
            mock_kr.get_password.assert_called_once_with("credstore", "__probe__")

    def test_init_system_get_keyring_raises(self):
        backend._system_keyring = None
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
        backend._system_keyring = None
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
        backend._system_keyring = None
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


# ── get_cryptfile / has_master_key ────────────────────────────────────


class TestGetCryptfile:
    """Tests for get_cryptfile."""

    def test_returns_none_initially(self):
        backend._cryptfile = None
        assert backend.get_cryptfile() is None

    def test_returns_set_value(self):
        backend._cryptfile = "mock_cf"
        try:
            assert backend.get_cryptfile() == "mock_cf"
        finally:
            backend._cryptfile = None


class TestHasMasterKey:
    """Tests for has_master_key."""

    def test_no_cryptfile_returns_false(self):
        backend._cryptfile = None
        assert backend.has_master_key() is False

    def test_cryptfile_no_file_returns_false(self, tmp_path):
        mock_cf = MagicMock()
        mock_cf.file_path = str(tmp_path / "nonexistent.crypt")
        backend._cryptfile = mock_cf
        try:
            assert backend.has_master_key() is False
        finally:
            backend._cryptfile = None

    def test_cryptfile_file_exists_returns_true(self, tmp_path):
        cf_path = tmp_path / "exists.crypt"
        cf_path.write_text("data")
        mock_cf = MagicMock()
        mock_cf.file_path = str(cf_path)
        backend._cryptfile = mock_cf
        try:
            assert backend.has_master_key() is True
        finally:
            backend._cryptfile = None


# ── init_backend ──────────────────────────────────────────────────────


class TestInitBackend:
    """Tests for init_backend."""

    def test_init_both_success(self):
        backend._system_keyring = None
        backend._cryptfile = None
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile") as mock_init_cf, \
             patch("credstore._backend.has_master_key", return_value=True):
            backend.init_backend()
            assert backend._system_keyring == "sys_kr"
            mock_init_cf.assert_called_once_with(None)

    def test_init_with_password(self):
        backend._system_keyring = None
        backend._cryptfile = None
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile") as mock_init_cf, \
             patch("credstore._backend.has_master_key", return_value=False):
            backend.init_backend(password="secret123")
            mock_init_cf.assert_called_once_with("secret123")

    def test_init_cryptfile_ready_with_instance(self):
        """When cryptfile is ready and has an instance, logs info."""
        backend._cryptfile = MagicMock()
        backend._system_keyring = None
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

    def test_import_error_sets_none(self):
        backend._cryptfile = "old"
        # Remove keyrings.cryptfile from sys.modules to force ImportError
        with patch.dict("sys.modules", {"keyrings.cryptfile.cryptfile": None}):
            # Make the import fail
            import builtins
            original_import = builtins.__import__
            def _fake_import(name, *args, **kwargs):
                if name == "keyrings.cryptfile.cryptfile":
                    raise ImportError("no cryptfile")
                return original_import(name, *args, **kwargs)
            with patch("builtins.__import__", side_effect=_fake_import):
                backend._init_cryptfile()
                assert backend._cryptfile is None

    def test_init_exception_sets_none(self):
        backend._cryptfile = "old"
        mock_cf_cls = MagicMock()
        mock_cf_cls.side_effect = ValueError("bad config")
        with patch.dict("sys.modules", {"keyrings.cryptfile.cryptfile": MagicMock(CryptFileKeyring=mock_cf_cls)}):
            backend._init_cryptfile()
            assert backend._cryptfile is None

    def test_init_success_with_password(self, tmp_path):
        backend._cryptfile = None
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
        backend._system_keyring = "sys_kr"
        backend._cryptfile = None
        with patch("credstore._backend._init_system", return_value="sys_kr"), \
             patch("credstore._backend._init_cryptfile"), \
             patch("credstore._backend.has_master_key", return_value=False):
            info = backend.get_backend_info()
            assert info["available"] is True
            assert info["cryptfile_ready"] is False

    def test_info_with_cryptfile(self):
        mock_cf = MagicMock()
        mock_cf._keyring_key = None
        backend._system_keyring = "sys_kr"
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
        backend._system_keyring = "sys_kr"
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
        try:
            with patch.object(backend, "has_master_key", return_value=True):
                name = backend.get_active_backend_name()
                assert "dual-write" in name
        finally:
            backend._system_keyring = None
            backend._cryptfile = None

    def test_system_only(self):
        backend._system_keyring = "kr"
        backend._cryptfile = None
        try:
            with patch.object(backend, "has_master_key", return_value=False):
                name = backend.get_active_backend_name()
                assert "system keyring only" in name
        finally:
            backend._system_keyring = None

    def test_cryptfile_only(self):
        backend._system_keyring = None
        backend._cryptfile = "cf"
        try:
            with patch.object(backend, "has_master_key", return_value=True):
                name = backend.get_active_backend_name()
                assert "cryptfile only" in name
        finally:
            backend._cryptfile = None

    def test_none(self):
        backend._system_keyring = None
        backend._cryptfile = None
        name = backend.get_active_backend_name()
        assert name == "none"
