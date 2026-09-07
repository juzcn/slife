"""Tests for slife.plugins.mcp.logging — log-dir resolution contract.

mcp-plugin is a built-in slife plugin, so its log directory resolves like
every other built-in plugin's: ``SLIFE_LOG_DIR`` when the host exported it
(the per-session log then lands next to the main session log), else
``<slife data dir>/logs``.  There is no ``~/.mcp-plugin/logs`` standalone
location and no ``MCP_PLUGIN_LOG_DIR`` override.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

from slife.plugins.mcp.logging import resolve_log_dir


class TestResolveLogDir:
    def test_slife_log_dir_env_wins(self, monkeypatch, tmp_path):
        """When slife spawns us, SLIFE_LOG_DIR takes top priority."""
        monkeypatch.setenv("SLIFE_LOG_DIR", str(tmp_path / "slife" / "logs"))
        monkeypatch.setenv("MCP_PLUGIN_LOG_DIR", str(tmp_path / "old" / "logs"))
        assert resolve_log_dir() == tmp_path / "slife" / "logs"

    def test_mcp_plugin_log_dir_is_ignored(self, monkeypatch, tmp_path):
        """The folded plugin has no MCP_PLUGIN_LOG_DIR override."""
        monkeypatch.delenv("SLIFE_LOG_DIR", raising=False)
        monkeypatch.setenv("MCP_PLUGIN_LOG_DIR", str(tmp_path / "custom" / "logs"))
        assert resolve_log_dir() != tmp_path / "custom" / "logs"

    def test_default_is_slife_logs_dir(self, monkeypatch):
        """No env → slife's own <data_dir>/logs resolution (~/.slife/logs)."""
        from slife.paths import get_logs_dir

        monkeypatch.delenv("SLIFE_LOG_DIR", raising=False)
        monkeypatch.delenv("MCP_PLUGIN_LOG_DIR", raising=False)
        assert resolve_log_dir() == get_logs_dir()
