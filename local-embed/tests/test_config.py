"""Tests for local_embed.config — env: injection into the process env.

``apply_env`` reads the config's ``env:`` section and injects it into
``os.environ`` before any backend loads (shell env wins), mirroring
slife.json5's env handling — keeps a transformer repo-name model
self-contained without external HF_* exports.
"""

import os

import pytest

pytestmark = pytest.mark.unit

from local_embed.config import apply_env


class TestApplyEnv:
    def test_injects_env_section(self, monkeypatch):
        monkeypatch.setattr("local_embed.config.load_config",
                            lambda: {"env": {"HF_HUB_CACHE": "C:\\hub"}})
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        effective = apply_env()
        assert effective == {"HF_HUB_CACHE": "C:\\hub"}
        assert os.environ["HF_HUB_CACHE"] == "C:\\hub"

    def test_shell_env_wins(self, monkeypatch):
        monkeypatch.setattr("local_embed.config.load_config",
                            lambda: {"env": {"HF_HUB_OFFLINE": "1"}})
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        effective = apply_env()
        assert effective == {}
        assert os.environ["HF_HUB_OFFLINE"] == "0"

    def test_no_env_section_noop(self, monkeypatch):
        monkeypatch.setattr("local_embed.config.load_config", lambda: {})
        assert apply_env() == {}

    def test_empty_env_section_noop(self, monkeypatch):
        monkeypatch.setattr("local_embed.config.load_config",
                            lambda: {"env": {}})
        assert apply_env() == {}
