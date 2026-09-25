"""Tests for slife.os_detect — OS detection helpers."""

import pytest; pytestmark = pytest.mark.unit

from unittest.mock import patch, mock_open

from slife.os_detect import is_wsl


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
