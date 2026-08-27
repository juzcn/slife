"""Shared fixtures for mcp_plugin tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_path(tmp_path, monkeypatch):
    """Point every config read/write at a throwaway file per test."""
    monkeypatch.setenv("MCP_PLUGIN_FILE", str(tmp_path / "mcp-plugin.json5"))
    yield
    # Import lazily so it never runs against a half-built package.
    try:
        import mcp_plugin.config as _cfg
        _cfg._CURRENT_PATH = None
    except ImportError:
        pass