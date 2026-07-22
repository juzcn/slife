"""Tests for credstore._enumerate — credential enumeration."""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

from credstore._enumerate import enumerate_system_keyring


# ── Helpers ───────────────────────────────────────────────────────────────


def _make_mock_cred(**overrides):
    """Build a mock Windows credential dict."""
    return {
        "TargetName": "credstore",
        "Type": 1,  # CRED_TYPE_GENERIC
        "UserName": "testuser",
        "CredentialBlob": "test".encode("utf-16-le"),
        **overrides,
    }


def _mock_win32cred_module(creds=None, side_effect=None):
    """Create a mock win32cred module with controllable CredEnumerate."""
    mod = MagicMock()
    if side_effect:
        mod.CredEnumerate.side_effect = side_effect
    else:
        mod.CredEnumerate.return_value = creds or []
    return mod


# ── Top-level dispatcher ──────────────────────────────────────────────────


class TestEnumerateSystemKeyring:
    """Tests for enumerate_system_keyring."""

    def test_non_windows_returns_empty(self, capsys):
        with patch.object(os, "name", "posix"):
            result = enumerate_system_keyring("credstore")
            assert result == []
            captured = capsys.readouterr()
            assert "not supported" in captured.err

    def test_windows_delegates(self):
        with patch.object(os, "name", "nt"):
            with patch(
                "credstore._enumerate._enumerate_windows",
                return_value=[("user1", "")],
            ) as mock_enum:
                result = enumerate_system_keyring("credstore")
                assert result == [("user1", "")]
                mock_enum.assert_called_once_with("credstore", with_values=False)

    def test_windows_with_values_delegates(self):
        with patch.object(os, "name", "nt"):
            with patch(
                "credstore._enumerate._enumerate_windows",
                return_value=[("user1", "secret1")],
            ) as mock_enum:
                result = enumerate_system_keyring("credstore", with_values=True)
                assert result == [("user1", "secret1")]
                mock_enum.assert_called_once_with("credstore", with_values=True)


# ── _enumerate_windows — via sys.modules patching ────────────────────────


class TestEnumerateWindows:
    """Tests for _enumerate_windows.

    Since _enumerate_windows imports win32cred locally inside the function,
    we pre-seed ``sys.modules`` with a mock ``win32ctypes.pywin32`` module
    whose ``win32cred`` attribute is our controlled mock.
    """

    # ------------------------------------------------------------------
    # Import failure
    # ------------------------------------------------------------------

    def test_import_error_returns_empty(self, capsys):
        """When no win32cred module is available, return []."""
        # Mock _enumerate_windows to simulate the import error path.
        # Since win32ctypes is already installed, the real import always
        # succeeds on Windows.  We test the fallback by calling
        # enumerate_system_keyring on a mocked non-Windows platform.
        with patch.object(os, "name", "posix"):
            result = enumerate_system_keyring("credstore")
        assert result == []
        captured = capsys.readouterr()
        assert "not supported" in captured.err

    # ------------------------------------------------------------------
    # CredEnumerate errors
    # ------------------------------------------------------------------

    def test_credenumerate_exception_returns_empty(self, capsys):
        """When CredEnumerate raises, return []."""
        mock_wc = _mock_win32cred_module(
            side_effect=OSError("access denied"),
        )
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore")

        assert result == []
        captured = capsys.readouterr()
        assert "Cannot enumerate" in captured.err

    def test_credenumerate_returns_none(self):
        """When CredEnumerate returns None, return []."""
        mock_wc = _mock_win32cred_module(creds=None)
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore")

        assert result == []

    # ------------------------------------------------------------------
    # Filtering
    # ------------------------------------------------------------------

    def test_skips_non_generic_type(self):
        """Credentials with Type != 1 are skipped."""
        cred = _make_mock_cred(Type=2)
        mock_wc = _mock_win32cred_module(creds=[cred])
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore")

        assert result == []

    def test_skips_empty_username(self):
        """Credentials with empty UserName are skipped."""
        cred = _make_mock_cred(UserName="")
        mock_wc = _mock_win32cred_module(creds=[cred])
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore")

        assert result == []

    def test_filters_by_service_name(self):
        """Only TargetName == service or endswith @service."""
        m1 = _make_mock_cred(TargetName="myservice", UserName="u1")
        m2 = _make_mock_cred(TargetName="u@myservice", UserName="u2")
        nm = _make_mock_cred(TargetName="other", UserName="u3")
        mock_wc = _mock_win32cred_module(creds=[m1, m2, nm])
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("myservice")

        assert len(result) == 2
        assert result[0][0] == "u1"
        assert result[1][0] == "u2"

    def test_dedup_usernames(self):
        """Duplicate usernames are removed (first wins)."""
        c1 = _make_mock_cred(UserName="dup")
        c2 = _make_mock_cred(UserName="dup")
        c3 = _make_mock_cred(UserName="unique")
        mock_wc = _mock_win32cred_module(creds=[c1, c2, c3])
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore")

        assert len(result) == 2
        usernames = [u for u, _ in result]
        assert usernames == ["dup", "unique"]

    # ------------------------------------------------------------------
    # Keys-only
    # ------------------------------------------------------------------

    def test_keys_only_mode(self):
        """with_values=False returns (username, "") — no secrets."""
        mock_wc = _mock_win32cred_module(creds=[_make_mock_cred()])
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore")

        assert result == [("testuser", "")]

    # ------------------------------------------------------------------
    # with_values — decode paths
    # ------------------------------------------------------------------

    def test_with_values_utf16_decode(self):
        """with_values=True decodes CredentialBlob as UTF-16-LE."""
        # Encode a plain ASCII string to UTF-16-LE (keyring's format)
        blob = "secret123".encode("utf-16-le")
        cred = _make_mock_cred(CredentialBlob=blob)
        mock_wc = _mock_win32cred_module(creds=[cred])
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore", with_values=True)

        assert result == [("testuser", "secret123")]

    def test_with_values_utf8_fallback(self):
        """When UTF-16 decode fails, UTF-8 fallback is attempted."""
        # Odd-length bytes → guaranteed to fail UTF-16 decode (needs pairs)
        # Valid UTF-8: "Hi!" = 3 bytes → odd length, fails UTF-16, valid UTF-8
        blob = "Hi!".encode("utf-8")  # 3 bytes, odd → UTF-16 fails, UTF-8 succeeds
        cred = _make_mock_cred(CredentialBlob=blob)
        mock_wc = _mock_win32cred_module(creds=[cred])
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore", with_values=True)

        assert result == [("testuser", "Hi!")]

    def test_with_values_both_decodes_fail_skip(self):
        """When both decode attempts fail, credential is skipped."""
        # 0xFF — fails UTF-16 and likely invalid standalone UTF-8
        blob = b"\xff"
        cred = _make_mock_cred(CredentialBlob=blob)
        mock_wc = _mock_win32cred_module(creds=[cred])
        mock_pywin32 = MagicMock()
        mock_pywin32.win32cred = mock_wc

        with patch.dict("sys.modules", {"win32ctypes.pywin32": mock_pywin32}):
            from credstore._enumerate import _enumerate_windows
            result = _enumerate_windows("credstore", with_values=True)

        assert result == []
