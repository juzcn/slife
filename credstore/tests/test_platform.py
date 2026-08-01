"""Tests for credstore._platform — platform detection."""
from __future__ import annotations

import pytest


class TestIsWsl:
    """Tests for is_wsl() — both detection paths."""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch):
        monkeypatch.setattr("os.path.exists", lambda p: False)

    def test_wslinterop_file_present(self, monkeypatch):
        monkeypatch.setattr(
            "os.path.exists",
            lambda p: p == "/proc/sys/fs/binfmt_misc/WSLInterop",
        )
        from credstore._platform import is_wsl
        assert is_wsl() is True

    def test_proc_version_microsoft(self, monkeypatch):
        from unittest.mock import mock_open
        monkeypatch.setattr(
            "builtins.open",
            mock_open(read_data="Linux ... microsoft ...\n"),
        )
        from credstore._platform import is_wsl
        assert is_wsl() is True

    def test_proc_version_wsl(self, monkeypatch):
        from unittest.mock import mock_open
        monkeypatch.setattr(
            "builtins.open",
            mock_open(read_data="Linux ... WSL2 ...\n"),
        )
        from credstore._platform import is_wsl
        assert is_wsl() is True

    def test_no_wsl_indicators(self, monkeypatch):
        from unittest.mock import mock_open
        monkeypatch.setattr("os.path.exists", lambda p: False)
        monkeypatch.setattr(
            "builtins.open",
            mock_open(read_data="Linux version 5.15 ...\n"),
        )
        from credstore._platform import is_wsl
        assert is_wsl() is False

    def test_proc_version_unreadable(self, monkeypatch):
        def _open(p, *a, **kw):
            if p == "/proc/version":
                raise PermissionError("denied")
            raise FileNotFoundError(p)
        monkeypatch.setattr("builtins.open", _open)
        from credstore._platform import is_wsl
        assert is_wsl() is False

    def test_both_indicators_true(self, monkeypatch):
        monkeypatch.setattr("os.path.exists", lambda p: True)
        from credstore._platform import is_wsl
        assert is_wsl() is True
