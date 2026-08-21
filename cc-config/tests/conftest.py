"""Shared fixtures for cc-config tests — isolated config/settings paths.

The real ``~/.claude/cc-config.json`` is never touched: every test
points the config storage at a temp file and the settings writer at a
temp output path.
"""

from __future__ import annotations

import pytest

import cc_config._api as api
import cc_config._activate as act


@pytest.fixture
def config_path(tmp_path, monkeypatch) -> str:
    """Point the config storage at a temp file; returns the config dict path."""
    target = tmp_path / "cc-config.json"
    monkeypatch.setattr(api, "CONFIG_PATH", target)
    return str(target)


@pytest.fixture
def settings_path(tmp_path, monkeypatch) -> str:
    """Point the settings writer at a temp output path."""
    target = tmp_path / "settings-out.json"
    monkeypatch.setattr(act, "SETTINGS_PATH", target)
    return str(target)