"""Tests for slife.os_detect — OS detection helpers."""

import pytest; pytestmark = pytest.mark.unit


import pytest
from unittest.mock import patch, mock_open

from slife.os_detect import is_windows, is_wsl, is_macos, is_linux


# ── is_windows ─────────────────────────────────────────────────────────────


class TestIsWindows:
    """Tests for is_windows()."""

    @patch("slife.os_detect.platform.system", return_value="Windows")
    @patch("slife.os_detect.is_wsl", return_value=False)
    def test_native_windows(self, _mock_wsl, _mock_system):
        """Returns True on native Windows."""
        assert is_windows() is True

    @patch("slife.os_detect.platform.system", return_value="Linux")
    @patch("slife.os_detect.is_wsl", return_value=True)
    def test_wsl_returns_true(self, _mock_wsl, _mock_system):
        """Returns True when running under WSL (Linux kernel on Windows host)."""
        assert is_windows() is True

    @patch("slife.os_detect.platform.system", return_value="Linux")
    @patch("slife.os_detect.is_wsl", return_value=False)
    def test_native_linux_returns_false(self, _mock_wsl, _mock_system):
        """Returns False on native Linux (not WSL)."""
        assert is_windows() is False

    @patch("slife.os_detect.platform.system", return_value="Darwin")
    @patch("slife.os_detect.is_wsl", return_value=False)
    def test_macos_returns_false(self, _mock_wsl, _mock_system):
        """Returns False on macOS."""
        assert is_windows() is False


# ── is_wsl ─────────────────────────────────────────────────────────────────


class TestIsWsl:
    """Tests for is_wsl() — /proc/version-based WSL detection."""

    def test_microsoft_in_proc_version(self):
        """Returns True when /proc/version contains 'microsoft' (WSL1)."""
        m = mock_open(read_data="Linux version ... Microsoft ...")
        with patch("builtins.open", m):
            assert is_wsl() is True

    def test_wsl_in_proc_version(self):
        """Returns True when /proc/version contains 'wsl' (WSL2)."""
        m = mock_open(read_data="Linux version ... WSL2 ...")
        with patch("builtins.open", m):
            assert is_wsl() is True

    def test_normal_linux_proc_version(self):
        """Returns False for a standard Linux /proc/version."""
        m = mock_open(read_data="Linux version 5.15.0-generic ...")
        with patch("builtins.open", m):
            assert is_wsl() is False

    @patch("builtins.open", side_effect=FileNotFoundError)
    def test_no_proc_version_file(self, _mock_open):
        """Returns False when /proc/version does not exist (macOS, etc.)."""
        assert is_wsl() is False

    @patch("builtins.open", side_effect=PermissionError)
    def test_permission_denied(self, _mock_open):
        """Returns False when /proc/version is unreadable."""
        assert is_wsl() is False


# ── is_macos ───────────────────────────────────────────────────────────────


class TestIsMacos:
    """Tests for is_macos()."""

    @patch("slife.os_detect.platform.system", return_value="Darwin")
    def test_darwin_returns_true(self, _mock_system):
        """Returns True when platform.system() is 'Darwin'."""
        assert is_macos() is True

    @patch("slife.os_detect.platform.system", return_value="Linux")
    def test_linux_returns_false(self, _mock_system):
        """Returns False on Linux."""
        assert is_macos() is False

    @patch("slife.os_detect.platform.system", return_value="Windows")
    def test_windows_returns_false(self, _mock_system):
        """Returns False on Windows."""
        assert is_macos() is False


# ── is_linux ───────────────────────────────────────────────────────────────


class TestIsLinux:
    """Tests for is_linux() — native Linux only, WSL excluded."""

    @patch("slife.os_detect.platform.system", return_value="Linux")
    @patch("slife.os_detect.is_wsl", return_value=False)
    def test_native_linux_returns_true(self, _mock_wsl, _mock_system):
        """Returns True on native Linux when not WSL."""
        assert is_linux() is True

    @patch("slife.os_detect.platform.system", return_value="Linux")
    @patch("slife.os_detect.is_wsl", return_value=True)
    def test_wsl_returns_false(self, _mock_wsl, _mock_system):
        """Returns False when running Linux under WSL."""
        assert is_linux() is False

    @patch("slife.os_detect.platform.system", return_value="Windows")
    @patch("slife.os_detect.is_wsl", return_value=False)
    def test_windows_returns_false(self, _mock_wsl, _mock_system):
        """Returns False on Windows."""
        assert is_linux() is False

    @patch("slife.os_detect.platform.system", return_value="Darwin")
    @patch("slife.os_detect.is_wsl", return_value=False)
    def test_macos_returns_false(self, _mock_wsl, _mock_system):
        """Returns False on macOS."""
        assert is_linux() is False


# ── Edge case: unknown platform ────────────────────────────────────────────


class TestUnknownPlatform:
    """Edge case: all helpers return False on an unrecognised platform."""

    @patch("slife.os_detect.platform.system", return_value="Java")
    @patch("slife.os_detect.is_wsl", return_value=False)
    def test_all_return_false_for_unknown_platform(self, _mock_wsl, _mock_system):
        """On an unknown platform all four helpers return False."""
        assert is_windows() is False
        assert is_macos() is False
        assert is_linux() is False
        assert is_wsl() is False
