"""Tests for _enumerate_wsl — WSL CredEnumerateW bridge."""
from __future__ import annotations

import base64
import subprocess

import pytest

from credstore._enumerate import _enumerate_wsl  # noqa: E402

pytestmark = pytest.mark.unit


class _FakeResult:
    def __init__(self, stdout="[]", rc=0, stderr=""):
        self.returncode = rc
        self.stdout = stdout
        self.stderr = stderr


def _mock_run(stdout="[]", rc=0):
    """Return a callable that mimics subprocess.run for keyword args."""
    def _run(cmd, **kw):
        return _FakeResult(stdout, rc)
    return _run


class TestEnumerateWsl:

    def test_successful_enumeration(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._enumerate.subprocess.run",
            _mock_run('["KEY_A|", "KEY_B|"]'),
        )
        result = _enumerate_wsl("credstore")
        assert result == [("KEY_A", ""), ("KEY_B", "")]

    def test_with_values(self, monkeypatch):
        b = base64.b64encode("secret".encode("utf-16-le")).decode("ascii")
        monkeypatch.setattr(
            "credstore._enumerate.subprocess.run",
            _mock_run(f'["KEY|{b}"]'),
        )
        result = _enumerate_wsl("credstore", with_values=True)
        assert result == [("KEY", "secret")]

    def test_nonzero_rc(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._enumerate.subprocess.run", _mock_run(rc=5),
        )
        assert _enumerate_wsl("credstore") == []

    def test_json_decode_error(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._enumerate.subprocess.run", _mock_run("not json"),
        )
        assert _enumerate_wsl("credstore") == []

    def test_powershell_not_found(self, monkeypatch):
        def _raise(cmd, **kw):
            raise FileNotFoundError("powershell.exe")
        monkeypatch.setattr("credstore._enumerate.subprocess.run", _raise)
        assert _enumerate_wsl("credstore") == []

    def test_timeout(self, monkeypatch):
        def _raise(cmd, **kw):
            raise subprocess.TimeoutExpired("ps", 15)
        monkeypatch.setattr("credstore._enumerate.subprocess.run", _raise)
        assert _enumerate_wsl("credstore") == []

    def test_dedup(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._enumerate.subprocess.run",
            _mock_run('["DUP|", "DUP|", "UNIQ|"]'),
        )
        result = _enumerate_wsl("credstore")
        assert len(result) == 2
        keys = {k for k, _ in result}
        assert keys == {"DUP", "UNIQ"}

    def test_with_values_decode_failure(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._enumerate.subprocess.run",
            _mock_run('["GOOD|dGVzdA==", "BAD|!!!x!!!"]'),
        )
        result = _enumerate_wsl("credstore", with_values=True)
        assert len(result) == 1
        assert result[0][0] == "GOOD"

    def test_no_separator_in_entry(self, monkeypatch):
        monkeypatch.setattr(
            "credstore._enumerate.subprocess.run",
            _mock_run('["PLAIN_KEY"]'),
        )
        result = _enumerate_wsl("credstore")
        assert result == [("PLAIN_KEY", "")]
