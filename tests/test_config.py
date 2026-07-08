"""Tests for configuration loading (slife.config)."""

import json
import tempfile
from pathlib import Path

import pytest

from slife.config import Config, ModelConfig


# ══════════════════════════════════════════════════════════════════════
# ModelConfig
# ══════════════════════════════════════════════════════════════════════


class TestModelConfigFromDict:
    """Tests for ModelConfig.from_dict()."""

    def test_minimal_model(self, basic_model_dict):
        """Minimal model dict — only required fields."""
        mc = ModelConfig.from_dict(basic_model_dict)
        assert mc.api_model == "deepseek-v4-flash"
        assert mc.ref == "unknown/deepseek-v4-flash"  # no provider field, defaults to "unknown"
        assert mc.display_name == "deepseek-v4-flash"  # falls back to model name
        assert mc.api_key == "sk-test123"
        assert mc.thinking_enabled is False
        assert mc.supports_vision is False
        assert mc.max_tokens == 4096  # default
        assert mc.context_window == 131072  # default
        assert mc.temperature == 0.7  # default
        assert mc.top_p == 1.0  # default
        assert mc.api == "openai-completions"  # default
        assert mc.base_url == "https://api.deepseek.com"  # default
        assert mc.reasoning_effort is None

    def test_model_with_provider_in_model_field(self):
        """Provider prefix in model field is parsed."""
        mc = ModelConfig.from_dict({
            "model": "deepseek/deepseek-v4-flash",
            "api_key": "sk-test",
        })
        assert mc.provider == "deepseek"
        assert mc.api_model == "deepseek/deepseek-v4-flash"
        assert mc.ref == "deepseek/deepseek-v4-flash"

    def test_model_with_explicit_provider_field(self):
        """Provider field is used when model has no prefix."""
        mc = ModelConfig.from_dict({
            "model": "gpt-5",
            "provider": "openai",
            "api_key": "sk-test",
        })
        assert mc.provider == "openai"
        assert mc.ref == "openai/gpt-5"

    def test_model_provider_prefix_takes_priority(self):
        """Provider in model field wins over provider field."""
        mc = ModelConfig.from_dict({
            "model": "ds/deepseek-v4-flash",
            "provider": "ignored",
            "api_key": "sk-test",
        })
        assert mc.provider == "ds"
        assert mc.ref == "ds/deepseek-v4-flash"

    def test_full_model(self, full_model_dict):
        """Full model dict with all options."""
        mc = ModelConfig.from_dict(full_model_dict)
        assert mc.ref == "deepseek/deepseek-v4-pro"
        assert mc.provider == "deepseek"
        assert mc.api_model == "deepseek/deepseek-v4-pro"
        assert mc.display_name == "DeepSeek V4 Pro"
        assert mc.api_key == "sk-test456"
        assert mc.base_url == "https://api.deepseek.com"
        assert mc.api == "openai-completions"
        assert mc.thinking_enabled is True
        assert mc.reasoning_effort == "high"
        assert mc.supports_vision is True
        assert mc.max_tokens == 8192
        assert mc.context_window == 131072
        assert mc.temperature == 0.7
        assert mc.top_p == 1.0

    def test_thinking_from_thinking_enabled_field(self):
        """thinking_enabled field (alternative to reasoning)."""
        mc = ModelConfig.from_dict({
            "model": "test-model",
            "api_key": "sk-test",
            "thinking_enabled": True,
        })
        assert mc.thinking_enabled is True

    def test_thinking_disabled_by_default(self):
        """thinking_enabled defaults to False."""
        mc = ModelConfig.from_dict({
            "model": "test-model",
            "api_key": "sk-test",
        })
        assert mc.thinking_enabled is False

    def test_supports_vision_from_input_list(self):
        """Vision support detected from input field."""
        mc = ModelConfig.from_dict({
            "model": "vision-model",
            "api_key": "sk-test",
            "input": ["text", "image"],
        })
        assert mc.supports_vision is True

    def test_supports_vision_text_only(self):
        """No vision when only text in input."""
        mc = ModelConfig.from_dict({
            "model": "text-model",
            "api_key": "sk-test",
            "input": ["text"],
        })
        assert mc.supports_vision is False

    def test_supports_vision_from_explicit_field(self):
        """supports_vision field used when input is missing."""
        mc = ModelConfig.from_dict({
            "model": "vision-model",
            "api_key": "sk-test",
            "supports_vision": True,
        })
        assert mc.supports_vision is True

    def test_custom_defaults(self):
        """Custom values for optional fields."""
        mc = ModelConfig.from_dict({
            "model": "custom-model",
            "api_key": "sk-custom",
            "base_url": "https://custom.api.com/v1",
            "api": "custom-api",
            "max_tokens": 2048,
            "context_window": 65536,
            "temperature": 0.3,
            "top_p": 0.9,
        })
        assert mc.base_url == "https://custom.api.com/v1"
        assert mc.api == "custom-api"
        assert mc.max_tokens == 2048
        assert mc.context_window == 65536
        assert mc.temperature == 0.3
        assert mc.top_p == 0.9

    def test_reasoning_effort_none_when_not_specified(self):
        """reasoning_effort is None when not in dict."""
        mc = ModelConfig.from_dict({
            "model": "test-model",
            "api_key": "sk-test",
            "reasoning": True,
        })
        assert mc.reasoning_effort is None


# ══════════════════════════════════════════════════════════════════════
# Config.from_json5
# ══════════════════════════════════════════════════════════════════════


class TestConfigFromJson5:
    """Tests for Config.from_json5()."""

    def test_load_minimal_config(self, temp_config_file):
        """Load a minimal valid JSON5 config."""
        config = Config.from_json5(temp_config_file)
        assert len(config.models) == 1
        assert config.active_model.ref == "deepseek/deepseek-v4-flash"
        assert len(config.tools) == 1
        assert config.tools[0]["type"] == "shell"
        assert config.max_iterations == 10

    def test_load_multi_model_config(self, temp_multi_config_file):
        """Load config with multiple models and providers."""
        config = Config.from_json5(temp_multi_config_file)
        assert len(config.models) == 3
        assert config.active_model_ref == "deepseek/deepseek-v4-pro"
        assert config.active_model.ref == "deepseek/deepseek-v4-pro"
        assert config.max_iterations == 20
        assert "Custom system prompt" in config.system_prompt
        assert len(config.tools) == 2

    def test_active_model_defaults_to_first_model(self, temp_config_file):
        """Without active_model field, first model is active."""
        config = Config.from_json5(temp_config_file)
        assert config.active_model_ref == "deepseek/deepseek-v4-flash"
        assert config.active_model.api_model == "deepseek-v4-flash"

    def test_file_not_found_raises(self):
        """Missing config file raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError, match="Config file not found"):
            Config.from_json5("nonexistent_file_xyz.json5")

    def test_no_models_raises(self):
        """Config with no models raises ValueError."""
        content = '{ tools: [] }'
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json5", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)
        try:
            with pytest.raises(ValueError, match="No models defined"):
                Config.from_json5(path)
        finally:
            path.unlink(missing_ok=True)

    def test_duplicate_model_in_provider_raises(self):
        """Duplicate model names within a provider raise ValueError."""
        content = """{
            models: {
                providers: {
                    deepseek: {
                        base_url: "https://api.deepseek.com",
                        api_key: "sk-test",
                        models: [
                            { model: "deepseek-v4-flash" },
                            { model: "deepseek-v4-flash" },
                        ]
                    }
                }
            },
            tools: []
        }"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json5", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)
        try:
            with pytest.raises(ValueError, match="Duplicate model"):
                Config.from_json5(path)
        finally:
            path.unlink(missing_ok=True)

    def test_list_format_models(self):
        """Models specified as a list (flat format)."""
        content = """{
            models: [
                { model: "gpt-5", api_key: "sk-test", provider: "openai" },
            ],
            tools: []
        }"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json5", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)
        try:
            config = Config.from_json5(path)
            assert len(config.models) == 1
            assert config.models[0].ref == "openai/gpt-5"
        finally:
            path.unlink(missing_ok=True)

    def test_env_var_resolution_in_config(self, monkeypatch):
        """Environment variables in config are resolved."""
        monkeypatch.setenv("MY_API_KEY", "env-resolved-key")
        content = """{
            models: [
                {
                    model: "gpt-5",
                    api_key: "${MY_API_KEY}",
                    provider: "openai"
                },
            ],
            tools: [
                { type: "serper", api_key: "${MY_API_KEY}" },
            ]
        }"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json5", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)
        try:
            config = Config.from_json5(path)
            assert config.models[0].api_key == "env-resolved-key"
            assert config.tools[0]["api_key"] == "env-resolved-key"
        finally:
            path.unlink(missing_ok=True)

    def test_provider_defaults_inherited_by_models(self):
        """Provider-level api_key, base_url, api are inherited."""
        content = """{
            models: {
                providers: {
                    testp: {
                        base_url: "https://test.api.com",
                        api_key: "sk-provider-key",
                        api: "custom-api",
                        models: [
                            { model: "m1" },
                            { model: "m2", api_key: "sk-override" },
                        ]
                    }
                }
            },
            tools: []
        }"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json5", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)
        try:
            config = Config.from_json5(path)
            assert config.models[0].api_key == "sk-provider-key"
            assert config.models[0].base_url == "https://test.api.com"
            assert config.models[0].api == "custom-api"
            # Second model overrides api_key
            assert config.models[1].api_key == "sk-override"
        finally:
            path.unlink(missing_ok=True)

    def test_provider_with_same_model_name_across_different_providers(self):
        """Same model name across different providers is allowed."""
        content = """{
            models: {
                providers: {
                    p1: {
                        base_url: "https://a.com",
                        api_key: "k1",
                        models: [{ model: "same-name" }]
                    },
                    p2: {
                        base_url: "https://b.com",
                        api_key: "k2",
                        models: [{ model: "same-name" }]
                    }
                }
            },
            tools: []
        }"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json5", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)
        try:
            config = Config.from_json5(path)
            assert len(config.models) == 2
            refs = {m.ref for m in config.models}
            assert refs == {"p1/same-name", "p2/same-name"}
        finally:
            path.unlink(missing_ok=True)


# ══════════════════════════════════════════════════════════════════════
# Config.active_model property
# ══════════════════════════════════════════════════════════════════════


class TestActiveModel:
    """Tests for Config.active_model property."""

    def test_returns_correct_model(self, temp_config_file):
        """active_model returns the matching ModelConfig."""
        config = Config.from_json5(temp_config_file)
        active = config.active_model
        assert active.ref == "deepseek/deepseek-v4-flash"
        assert isinstance(active, ModelConfig)

    def test_raises_keyerror_for_missing_ref(self, temp_config_file):
        """KeyError raised when active_model_ref doesn't match any model."""
        config = Config.from_json5(temp_config_file)
        config.active_model_ref = "nonexistent/model"
        with pytest.raises(KeyError, match="Active model"):
            _ = config.active_model


# ══════════════════════════════════════════════════════════════════════
# Config defaults
# ══════════════════════════════════════════════════════════════════════


class TestConfigDefaults:
    """Tests for default Config values."""

    def test_default_system_prompt(self, temp_config_file):
        """Default system prompt is set when not in config."""
        config = Config.from_json5(temp_config_file)
        assert "slife" in config.system_prompt
        assert "web_search" in config.system_prompt

    def test_default_max_iterations(self, temp_config_file):
        """Default max_iterations is 10."""
        config = Config.from_json5(temp_config_file)
        assert config.max_iterations == 10

    def test_empty_tools(self):
        """Config works with empty tools list."""
        content = """{
            models: [
                { model: "m1", api_key: "k1", provider: "p1" },
            ],
            tools: []
        }"""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json5", delete=False, encoding="utf-8"
        ) as f:
            f.write(content)
            f.flush()
            path = Path(f.name)
        try:
            config = Config.from_json5(path)
            assert config.tools == []
        finally:
            path.unlink(missing_ok=True)
