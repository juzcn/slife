"""Tests for local_embed.config — env: injection into the process env.

``apply_env`` reads the config's ``env:`` section and injects it into
``os.environ`` before any backend loads (shell env wins), mirroring
slife.json5's env handling — keeps a transformer repo-name model
self-contained without external HF_* exports.
"""

import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

from local_embed.config import apply_env, expand_value, resolve_engine_settings


class TestExpandValue:
    """${VAR} / ${VAR:-default} expansion from os.environ (no credstore)."""

    def test_default_fallback(self, monkeypatch):
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        value = expand_value("${HF_HUB_CACHE:-~/.cache/huggingface/hub}")
        assert value == "~/.cache/huggingface/hub"

    def test_set_var_wins_over_default(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_CACHE", "C:\\hub")
        value = expand_value("${HF_HUB_CACHE:-~/.cache/huggingface/hub}")
        assert value == "C:\\hub"

    def test_unset_no_default_left_literal(self, monkeypatch):
        monkeypatch.delenv("BGE_M3_GGUF_PATH", raising=False)
        assert expand_value("${BGE_M3_GGUF_PATH}") == "${BGE_M3_GGUF_PATH}"

    def test_plain_value_unchanged(self):
        assert expand_value("C:\\hub") == "C:\\hub"

    def test_multiple_refs(self, monkeypatch):
        monkeypatch.setenv("A", "x")
        monkeypatch.delenv("B", raising=False)
        assert expand_value("${A}/${B:-y}") == "x/y"


class TestApplyEnv:
    def test_injects_env_section(self, monkeypatch):
        monkeypatch.setattr("local_embed.config.load_config",
                            lambda: {"env": {"HF_HUB_CACHE": "C:\\hub"}})
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        effective = apply_env()
        assert effective == {"HF_HUB_CACHE": "C:\\hub"}
        assert os.environ["HF_HUB_CACHE"] == "C:\\hub"

    def test_env_placeholder_expanded(self, monkeypatch):
        monkeypatch.setattr(
            "local_embed.config.load_config",
            lambda: {"env": {"HF_HUB_CACHE": "${HF_HUB_CACHE:-~/.cache/huggingface/hub}"}},
        )
        monkeypatch.delenv("HF_HUB_CACHE", raising=False)
        effective = apply_env()
        assert effective == {"HF_HUB_CACHE": "~/.cache/huggingface/hub"}
        assert os.environ["HF_HUB_CACHE"] == "~/.cache/huggingface/hub"

    def test_shell_placeholder_wins(self, monkeypatch):
        monkeypatch.setattr(
            "local_embed.config.load_config",
            lambda: {"env": {"HF_HUB_OFFLINE": "${HF_HUB_OFFLINE:-1}"}},
        )
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        effective = apply_env()
        assert effective == {}
        assert os.environ["HF_HUB_OFFLINE"] == "0"

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


class TestGgufPathExpansion:
    def test_gguf_path_tilde_default_expanded(self, monkeypatch):
        monkeypatch.setattr(
            "local_embed.config.load_config",
            lambda: {
                "active_model": "bge-m3",
                "models": {"bge-m3": {"backend": "gguf",
                                      "gguf_path": "${BGE_M3_GGUF_PATH:-~/.local-embed/models/bge-m3-q4_k_m.gguf}"}},
            },
        )
        monkeypatch.delenv("BGE_M3_GGUF_PATH", raising=False)
        out = resolve_engine_settings()
        assert out["specs"][0].gguf_path == \
            str(Path.home() / ".local-embed" / "models" / "bge-m3-q4_k_m.gguf")

    def test_gguf_path_default_expanded(self, monkeypatch):
        monkeypatch.setattr(
            "local_embed.config.load_config",
            lambda: {
                "active_model": "bge-m3",
                "models": {"bge-m3": {"backend": "gguf",
                                      "gguf_path": "${BGE_M3_GGUF_PATH:-/data/model.gguf}"}},
            },
        )
        monkeypatch.delenv("BGE_M3_GGUF_PATH", raising=False)
        out = resolve_engine_settings()
        assert out["specs"][0].gguf_path == os.path.normpath("/data/model.gguf")

    def test_gguf_path_env_override(self, monkeypatch):
        monkeypatch.setattr(
            "local_embed.config.load_config",
            lambda: {
                "active_model": "bge-m3",
                "models": {"bge-m3": {"backend": "gguf",
                                      "gguf_path": "${BGE_M3_GGUF_PATH}"}},
            },
        )
        monkeypatch.setenv("BGE_M3_GGUF_PATH", "C:\\models\\bge.gguf")
        out = resolve_engine_settings()
        assert out["specs"][0].gguf_path == "C:\\models\\bge.gguf"

    def test_gguf_path_unset_left_literal(self, monkeypatch):
        monkeypatch.setattr(
            "local_embed.config.load_config",
            lambda: {
                "active_model": "bge-m3",
                "models": {"bge-m3": {"backend": "gguf", "gguf_path": "${BGE_M3_GGUF_PATH}"}},
            },
        )
        monkeypatch.delenv("BGE_M3_GGUF_PATH", raising=False)
        out = resolve_engine_settings()
        assert out["specs"][0].gguf_path == "${BGE_M3_GGUF_PATH}"
