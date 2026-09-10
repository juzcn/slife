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


class TestAutoload:
    """autoload is PER MODEL — default false (lazy); true eager-loads that
    one model at startup while every unflagged model stays lazy."""

    @staticmethod
    def _out(monkeypatch, cfg):
        monkeypatch.delenv("LOCAL_EMBED_AUTOLOAD", raising=False)
        monkeypatch.setattr("local_embed.config.load_config", lambda: cfg)
        return resolve_engine_settings()

    def test_default_false_per_model(self, monkeypatch):
        out = self._out(monkeypatch, {
            "models": {"m": {"backend": "gguf", "gguf_path": "/x.gguf"}},
        })
        assert out["specs"][0].autoload is False

    def test_true_from_model_entry(self, monkeypatch):
        out = self._out(monkeypatch, {
            "models": {"m": {"backend": "gguf", "gguf_path": "/x.gguf",
                             "autoload": True}},
        })
        assert out["specs"][0].autoload is True

    def test_string_true_parsed(self, monkeypatch):
        out = self._out(monkeypatch, {
            "models": {"m": {"backend": "gguf", "gguf_path": "/x.gguf",
                             "autoload": "1"}},
        })
        assert out["specs"][0].autoload is True

    def test_mixed_flags_and_absent(self, monkeypatch):
        """One model flagged autoload, the other not — flags are per model."""
        out = self._out(monkeypatch, {
            "models": {
                "warm": {"backend": "gguf", "gguf_path": "/w.gguf", "autoload": True},
                "cold": {"backend": "gguf", "gguf_path": "/c.gguf"},
            },
        })
        assert {s.name: s.autoload for s in out["specs"]} == {
            "warm": True, "cold": False,
        }

    def test_single_model_top_level_autoload(self, monkeypatch):
        monkeypatch.delenv("LOCAL_EMBED_AUTOLOAD", raising=False)
        out = self._out(monkeypatch, {
            "backend": "gguf", "model": "bge-m3", "gguf_path": "/x.gguf",
            "autoload": True,
        })
        assert out["specs"][0].autoload is True

    def test_env_override_in_single_model_shape(self, monkeypatch):
        monkeypatch.setattr(
            "local_embed.config.load_config",
            lambda: {"backend": "gguf", "model": "bge-m3", "gguf_path": "/x.gguf"},
        )
        monkeypatch.setenv("LOCAL_EMBED_AUTOLOAD", "1")
        assert resolve_engine_settings()["specs"][0].autoload is True


class TestHostPortEnvOverride:
    """host/port honor LOCAL_EMBED_HOST / LOCAL_EMBED_PORT like every other
    key (the documented env-override precedence)."""

    def test_port_env_override(self, monkeypatch):
        monkeypatch.setattr("local_embed.config.load_config", lambda: {"port": 17347})
        monkeypatch.setenv("LOCAL_EMBED_PORT", "8080")
        out = resolve_engine_settings()
        assert out["port"] == 8080

    def test_host_env_override(self, monkeypatch):
        monkeypatch.setattr("local_embed.config.load_config", lambda: {"host": "127.0.0.1"})
        monkeypatch.setenv("LOCAL_EMBED_HOST", "0.0.0.0")
        out = resolve_engine_settings()
        assert out["host"] == "0.0.0.0"


class TestSingleModelMaxTokens:
    """Single-model config honors max_tokens (matches the multi-model branch)."""

    def test_single_model_max_tokens(self, monkeypatch):
        monkeypatch.setattr(
            "local_embed.config.load_config",
            lambda: {"backend": "gguf", "model": "bge-m3", "gguf_path": "/x.gguf", "max_tokens": 1234},
        )
        out = resolve_engine_settings()
        assert out["specs"][0].max_tokens == 1234
