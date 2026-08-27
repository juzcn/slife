"""Smoke tests for mcp_plugin.config (expanded in Phase C)."""

from __future__ import annotations


def test_import_config():
    """The config module imports and exposes the version."""
    import mcp_plugin
    assert mcp_plugin.__version__