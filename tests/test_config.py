"""Tests for slife.config — configuration loading and model definitions."""

import json
import tempfile
from pathlib import Path

import pytest
import json5

from slife.config import Config, ModelConfig


# ── ModelConfig.from_dict ─────────────────────────────────────────────


class TestModelConfigFromDict:
    """Tests for ModelConfig.from_dict classmethod."""

    def test_minimal_dict(self):
        """Minimal valid model entry."""
        mc = ModelConfig.from_dict({
            "model": "gpt-4o",
            "api_key": "sk-test",
        })
        assert mc.api_model == "gpt-4o"
        assert mc.ref == "unknown/gpt-4o"
        assert mc.display_name == "gpt-4o"
        assert mc.api_key == "sk-test"
        assert mc.temperature == 0.7
        assert mc.max_tokens == 4096

    def test_model_with_provider_prefix(self):
        """model field may contain provider/model format."""
        mc = ModelConfig.from_dict({
            "model": "openai/gpt-4o",
            "api_key": "sk-test",
        })
        assert mc.provider == "openai"
        assert mc.api_model == "openai/gpt-4o"
        assert mc.ref == "openai/gpt-4o"

    def test_all_fields(self):
        """Full field set from dict."""
        mc = ModelConfig.from_dict({
            "model": "deepseek-v4-flash",
            "provider": "deepseek",
            "name": "DeepSeek V4 Flash",
            "api_key": "sk-key",
            "base_url": "https://custom.api/v1",
            "api": "openai-completions",
            "input": ["text", "image"],
            "max_tokens": 8192,
            "context_window": 200000,
            "temperature": 0.5,
            "top_p": 0.95,
            "reasoning": True,
            "reasoning_effort": "medium",
        })
        assert mc.display_name == "DeepSeek V4 Flash"
        assert mc.base_url == "https://custom.api/v1"
        assert mc.supports_vision is True
        assert mc.max_tokens == 8192
        assert mc.context_window == 200000
        assert mc.temperature == 0.5
        assert mc.top_p == 0.95
        assert mc.thinking_enabled is True
        assert mc.reasoning_effort == "medium"
        assert mc.ref == "deepseek/deepseek-v4-flash"

    def test_thinking_enabled_fallback_key(self):
        """thinking_enabled key also enables thinking."""
        mc = ModelConfig.from_dict({
            "model": "test",
            "api_key": "key",
            "thinking_enabled": True,
        })
        assert mc.thinking_enabled is True

    def test_supports_vision_fallback_key(self):
        """supports_vision key enables vision when no input list."""
        mc = ModelConfig.from_dict({
            "model": "test",
            "api_key": "key",
            "supports_vision": True,
        })
        assert mc.supports_vision is True

    def test_supports_vision_from_input_list(self):
        """input: ['image'] sets supports_vision."""
        mc = ModelConfig.from_dict({
            "model": "test",
            "api_key": "key",
            "input": ["image"],
        })
        assert mc.supports_vision is True

    def test_supports_vision_text_only(self):
        """input: ['text'] does not set supports_vision."""
        mc = ModelConfig.from_dict({
            "model": "test",
            "api_key": "key",
            "input": ["text"],
        })
        assert mc.supports_vision is False

    def test_empty_input_list(self):
        """Empty input list → no vision."""
        mc = ModelConfig.from_dict({
            "model": "test",
            "api_key": "key",
            "input": [],
        })
        assert mc.supports_vision is False  # falls back to supports_vision default

    def test_defaults_applied(self):
        """Missing optional fields get sensible defaults."""
        mc = ModelConfig.from_dict({
            "model": "test-model",
            "api_key": "test-key",
        })
        assert mc.base_url == "https://api.deepseek.com"
        assert mc.api == "openai-completions"
        assert mc.supports_vision is False
        assert mc.max_tokens == 4096
        assert mc.context_window == 131072
        assert mc.temperature == 0.7
        assert mc.top_p == 1.0
        assert mc.thinking_enabled is False
        assert mc.reasoning_effort is None

    def test_reasoning_truthy_values(self):
        """Non-boolean truthy reasoning values become True."""
        mc = ModelConfig.from_dict({
            "model": "test",
            "api_key": "key",
            "reasoning": 1,
        })
        assert mc.thinking_enabled is True

    def test_reasoning_falsy_values(self):
        """Falsy reasoning values become False."""
        mc = ModelConfig.from_dict({
            "model": "test",
            "api_key": "key",
            "reasoning": 0,
        })
        assert mc.thinking_enabled is False


# ── Config.from_json5 ─────────────────────────────────────────────────


class TestConfigFromJSON5:
    """Tests for Config.from_json5 classmethod."""

    def test_file_not_found(self):
        """Raises FileNotFoundError for missing config."""
        with pytest.raises(FileNotFoundError) as exc_info:
            Config.from_json5("/nonexistent/path/slife.json5")
        assert "not found" in str(exc_info.value)

    def test_minimal_config(self, tmp_path, monkeypatch):
        """Minimal valid JSON5 config with providers."""
        monkeypatch.setenv("DEEPSEEK_KEY", "env-key")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "deepseek": {
                        "api_key": "${DEEPSEEK_KEY}",
                        "models": [
                            {"model": "deepseek-v4-flash", "name": "Dv4 Flash"}
                        ],
                    }
                }
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.models) == 1
        assert config.models[0].api_key == "env-key"
        assert config.active_model_ref == "deepseek/deepseek-v4-flash"

    def test_list_style_models(self, tmp_path):
        """Config with models as a flat list."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": [
                {"model": "gpt-4o", "api_key": "sk-key", "provider": "openai"},
                {"model": "claude-3", "api_key": "sk-other", "provider": "anthropic"},
            ],
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.models) == 2
        assert config.models[0].ref == "openai/gpt-4o"
        assert config.models[1].ref == "anthropic/claude-3"

    def test_active_model_selection(self, tmp_path):
        """active_model field selects which model is active."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "deepseek": {
                        "api_key": "sk-key",
                        "models": [
                            {"model": "v4-flash", "name": "Flash"},
                            {"model": "v4-pro", "name": "Pro"},
                        ],
                    },
                    "openai": {
                        "api_key": "sk-oai",
                        "models": [
                            {"model": "gpt-4o", "name": "GPT-4o"},
                        ],
                    },
                }
            },
            "active_model": "openai/gpt-4o",
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.active_model.ref == "openai/gpt-4o"

    def test_no_models_raises(self, tmp_path):
        """Empty models section raises ValueError."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({"models": {}}))
        with pytest.raises(ValueError, match="No models defined"):
            Config.from_json5(str(cfg_path))

    def test_agent_config(self, tmp_path, monkeypatch):
        """Agent section configures max_iterations."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "d": {
                        "api_key": "${KEY}",
                        "models": [{"model": "m"}],
                    }
                }
            },
            "agent": {
                "max_iterations": 5,
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.max_iterations == 5

    def test_tools_config(self, tmp_path, monkeypatch):
        """Tools section is loaded correctly."""
        monkeypatch.setenv("SERPER_KEY", "serper-key")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "d": {
                        "api_key": "sk-key",
                        "models": [{"model": "m"}],
                    }
                }
            },
            "tools": [
                {"type": "shell", "timeout": 60},
                {"type": "serper", "api_key": "${SERPER_KEY}"},
            ],
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.tools) == 2
        assert config.tools[0] == {"type": "shell", "timeout": 60}
        assert config.tools[1] == {"type": "serper", "api_key": "serper-key"}

    def test_duplicate_model_in_provider_raises(self, tmp_path, monkeypatch):
        """Duplicate model names within a provider raise ValueError."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "deepseek": {
                        "api_key": "${KEY}",
                        "models": [
                            {"model": "same-name", "name": "First"},
                            {"model": "deepseek/same-name", "name": "Second"},
                        ],
                    }
                }
            },
        }))
        with pytest.raises(ValueError, match="Duplicate model"):
            Config.from_json5(str(cfg_path))

    def test_provider_defaults_inherited(self, tmp_path, monkeypatch):
        """Models inherit base_url and api_key from provider."""
        monkeypatch.setenv("KEY", "parent-key")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "deepseek": {
                        "base_url": "https://custom.deepseek.com",
                        "api_key": "${KEY}",
                        "api": "openai-completions",
                        "models": [
                            {"model": "v4-flash"},
                        ],
                    }
                }
            },
        }))
        config = Config.from_json5(str(cfg_path))
        m = config.models[0]
        assert m.base_url == "https://custom.deepseek.com"
        assert m.api_key == "parent-key"
        assert m.api == "openai-completions"


# ── Config.active_model ───────────────────────────────────────────────


class TestActiveModel:
    """Tests for Config.active_model property."""

    def test_returns_correct_model(self, sample_config):
        assert sample_config.active_model.ref == "deepseek/deepseek-v4-flash"

    def test_missing_model_raises_keyerror(self, sample_config):
        sample_config.active_model_ref = "nonexistent/model"
        with pytest.raises(KeyError) as exc_info:
            _ = sample_config.active_model
        assert "nonexistent/model" in str(exc_info.value)
        assert "Available" in str(exc_info.value)
