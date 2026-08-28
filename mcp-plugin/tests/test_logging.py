"""Tests for mcp_plugin.logging — log-dir resolution contract.

The external-plugin log contract: when slife spawns mcp_plugin it exports
``SLIFE_LOG_DIR`` / ``SLIFE_PLUGIN_NAME``, so the per-session log follows
slife's convention (same directory, plugin-named file).  Standalone keeps
the ``MCP_PLUGIN_LOG_DIR`` override and the ``~/.mcp-plugin/logs`` default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from mcp_plugin.logging import resolve_log_dir


class TestResolveLogDir:
    def test_slife_log_dir_env_wins(self, monkeypatch, tmp_path):
        """When slife spawns us, SLIFE_LOG_DIR takes top priority."""
        monkeypatch.setenv("SLIFE_LOG_DIR", str(tmp_path / "slife" / "logs"))
        monkeypatch.setenv("MCP_PLUGIN_LOG_DIR", str(tmp_path / "old" / "logs"))
        assert resolve_log_dir() == tmp_path / "slife" / "logs"

    def test_standalone_override_after_slife(self, monkeypatch, tmp_path):
        """No SLIFE_LOG_DIR → MCP_PLUGIN_LOG_DIR is used."""
        monkeypatch.delenv("SLIFE_LOG_DIR", raising=False)
        monkeypatch.setenv("MCP_PLUGIN_LOG_DIR", str(tmp_path / "custom" / "logs"))
        assert resolve_log_dir() == tmp_path / "custom" / "logs"

    def test_standalone_default_under_home(self, monkeypatch):
        """Neither env var set → ~/.mcp-plugin/logs."""
        monkeypatch.delenv("SLIFE_LOG_DIR", raising=False)
        monkeypatch.delenv("MCP_PLUGIN_LOG_DIR", raising=False)
        result = resolve_log_dir()
        assert result == Path.home() / ".mcp-plugin" / "logs"
