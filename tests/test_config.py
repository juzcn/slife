"""Tests for Slife.config — configuration loading and model definitions."""

import pytest; pytestmark = pytest.mark.unit


# pyright: reportAttributeAccessIssue=false, reportArgumentType=false, reportOptionalMemberAccess=false

import logging

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

    def test_reasoning_missing_disables_thinking(self):
        """Absent reasoning key → thinking disabled."""
        mc = ModelConfig.from_dict({
            "model": "test",
            "api_key": "key",
        })
        assert mc.thinking_enabled is False

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
        assert mc.supports_vision is False

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

    def test_file_not_found(self, tmp_path):
        """Raises FileNotFoundError for missing config."""
        missing = tmp_path / "nonexistent" / "slife.json5"
        with pytest.raises(FileNotFoundError) as exc_info:
            Config.from_json5(str(missing))
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

    def test_stale_active_model_falls_back(self, tmp_path, caplog):
        """Stale active_model ref falls back to first model, no crash."""
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
                }
            },
            "active_model": "removed-provider/gone-model",
        }))
        with caplog.at_level(logging.WARNING, logger="slife.config"):
            config = Config.from_json5(str(cfg_path))
        assert config.active_model_ref == "deepseek/v4-flash"
        assert config.active_model.ref == "deepseek/v4-flash"
        assert "config_active_model_stale" in caplog.text

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
        monkeypatch.setenv("MY_KEY", "my-key-value")
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
                {"name": "execute_shell", "timeout": 60},
                {"name": "run_python_script"},
            ],
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.tools) == 2
        assert config.tools[0] == {"name": "execute_shell", "timeout": 60}
        assert config.tools[1] == {"name": "run_python_script"}

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


# ── MemdbConfig ──────────────────────────────────────────────────────


class TestMemdbConfigFromDict:
    """Tests for MemdbConfig.from_dict."""

    def test_non_dict_returns_default(self):
        from slife.config import MemdbConfig
        result = MemdbConfig.from_dict("not a dict")
        assert result.embedding_model == "text-embedding-3-small"

    def test_non_dict_embedding(self):
        from slife.config import MemdbConfig
        result = MemdbConfig.from_dict({"embedding": "not a dict"})
        assert result.embedding_model == "text-embedding-3-small"
        assert result.embedding_dim == 1536

    def test_custom_values(self):
        from slife.config import MemdbConfig
        result = MemdbConfig.from_dict({
            "embedding": {"model": "custom-model", "dim": 768},
        })
        assert result.embedding_model == "custom-model"
        assert result.embedding_dim == 768


# ── parse_cli_agent ────────────────────────────────────────────────────


class TestParseCLI:
    def test_parse_cli_agent_found(self):
        from slife.config import parse_cli_agent
        result = parse_cli_agent(["slife", "--agent", "bob"])
        assert result == "bob"

    def test_parse_cli_agent_default(self):
        from slife.config import parse_cli_agent
        result = parse_cli_agent(["slife"])
        assert result == "slife"

    def test_parse_cli_lang_found(self):
        from slife.config import parse_cli_lang
        assert parse_cli_lang(["slife", "--lang", "zh"]) == "zh"
        assert parse_cli_lang(["slife", "--lang", "en"]) == "en"
        # flag after a positional is still found
        assert parse_cli_lang(["slife", "myconf.json5", "--lang", "zh"]) == "zh"

    def test_parse_cli_lang_default(self):
        from slife.config import parse_cli_lang
        assert parse_cli_lang(["slife"]) is None
        assert parse_cli_lang(["slife", "--agent", "bob"]) is None

    def test_parse_cli_lang_invalid_value(self):
        from slife.config import parse_cli_lang
        with pytest.raises(SystemExit):
            parse_cli_lang(["slife", "--lang", "fr"])

    def test_parse_cli_lang_missing_value(self):
        from slife.config import parse_cli_lang
        with pytest.raises(SystemExit):
            parse_cli_lang(["slife", "--lang"])

    def test_parse_cli_config_path_skips_lang(self):
        from slife.config import parse_cli_config_path
        assert parse_cli_config_path(["slife", "--lang", "zh", "myconf.json5"]) == "myconf.json5"
        assert parse_cli_config_path(
            ["slife", "--agent", "bob", "--lang", "zh", "myconf.json5"]
        ) == "myconf.json5"
        assert parse_cli_config_path(["slife", "--lang", "zh"]) is None


# ── Config.from_json5 — subagent / A2A ────────────────────────────────


class TestConfigSubagentDefault:
    """Tests for _load_subagent_config."""

    def test_defaults_when_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {"api_key": "${KEY}", "models": [{"model": "m"}]},
                },
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.subagent_config == {"max_subagents": 5, "task_timeout": 120}

    def test_custom_values(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {"api_key": "${KEY}", "models": [{"model": "m"}]},
                },
            },
            "subagent": {"max_subagents": 3, "task_timeout": 60},
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.subagent_config == {"max_subagents": 3, "task_timeout": 60}

    def test_non_dict_uses_defaults(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {"api_key": "${KEY}", "models": [{"model": "m"}]},
                },
            },
            "subagent": "not-a-dict",
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.subagent_config == {"max_subagents": 5, "task_timeout": 120}


class TestConfigA2A:
    """Tests for A2A config — agent_name derived from user."""

    def test_agent_name_from_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {"api_key": "${KEY}", "models": [{"model": "m"}]},
                },
            },
            "a2a": {
                "broker": {"host": "mqtt.example.com", "port": 1883},
            },
        }))
        config = Config.from_json5(str(cfg_path), agent_name="bob")
        assert config.a2a_config is not None
        assert config.a2a_config.agent_name == "bob"
        assert config.a2a_config.enabled is True  # auto-enabled when a2a config present

    def test_mqtt_key_is_ignored(self, tmp_path, monkeypatch):
        """The old ``mqtt`` section key is not read — the section is ``a2a``."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {"api_key": "${KEY}", "models": [{"model": "m"}]},
                },
            },
            "mqtt": {
                "broker": {"host": "mqtt.example.com", "port": 1883},
            },
        }))
        config = Config.from_json5(str(cfg_path), agent_name="bob")
        assert config.a2a_config is not None
        assert config.a2a_config.broker_host == "localhost"
        assert config.a2a_config.broker_port == 1883


# ── Config.from_json5 edge cases ────────────────────────────────────────


class TestConfigEnvInjection:
    """Tests for env section injection into os.environ."""

    def test_env_section_injects_to_os_environ(self, tmp_path, monkeypatch):
        """Values from the env section are injected into os.environ."""
        monkeypatch.setenv("PROV_KEY", "sk-test")
        # Remove test var if exists
        monkeypatch.delenv("MY_TOOL_KEY", raising=False)

        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {
                        "api_key": "${PROV_KEY}",
                        "models": [{"model": "m"}],
                    },
                },
            },
            "env": {
                "MY_TOOL_KEY": "tool-secret-123",
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.env == {"MY_TOOL_KEY": "tool-secret-123"}


class TestConfigFromJSON5EdgeCases:
    """Tests for Config.from_json5 edge cases not covered elsewhere."""

    def test_providers_not_dict(self, tmp_path, monkeypatch):
        """Non-dict providers field is treated as empty."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": ["not", "a", "dict"],
            },
        }))
        with pytest.raises(ValueError, match="No models defined"):
            Config.from_json5(str(cfg_path))

    def test_provider_cfg_not_dict(self, tmp_path, monkeypatch):
        """Non-dict provider entry is skipped."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "bad_provider": "not a dict",
                    "good_provider": {
                        "api_key": "${KEY}",
                        "models": [{"model": "valid_model"}],
                    },
                },
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.models) == 1
        assert config.models[0].ref == "good_provider/valid_model"

    def test_models_not_list(self, tmp_path, monkeypatch):
        """Non-list models field in provider is skipped."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p1": {
                        "api_key": "${KEY}",
                        "models": "not-a-list",
                    },
                    "p2": {
                        "api_key": "${KEY}",
                        "models": [{"model": "real_model"}],
                    },
                },
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.models) == 1
        assert config.models[0].ref == "p2/real_model"

    def test_model_entry_not_dict(self, tmp_path, monkeypatch):
        """Non-dict model entry in list is skipped."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p1": {
                        "api_key": "${KEY}",
                        "models": [
                            "not-a-dict",
                            {"model": "good_model"},
                        ],
                    },
                },
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.models) == 1
        assert config.models[0].ref == "p1/good_model"

    def test_list_style_non_dict_entry(self, tmp_path):
        """Non-dict entry in list-style models section is skipped."""
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": [
                "not-a-dict",
                {"model": "gpt-4o", "api_key": "sk-key"},
            ],
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.models) == 1
        assert config.models[0].ref == "unknown/gpt-4o"

    def test_agent_not_dict(self, tmp_path, monkeypatch):
        """Non-dict agent section uses defaults."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {
                        "api_key": "${KEY}",
                        "models": [{"model": "m"}],
                    },
                },
            },
            "agent": "not-a-dict",
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.max_iterations == 30

    def test_env_not_dict(self, tmp_path, monkeypatch):
        """Non-dict env section uses empty dict."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {
                        "api_key": "${KEY}",
                        "models": [{"model": "m"}],
                    },
                },
            },
            "env": "not-a-dict",
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.env == {}

    def test_tools_not_list(self, tmp_path, monkeypatch):
        """Non-list tools section uses empty list."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {
                        "api_key": "${KEY}",
                        "models": [{"model": "m"}],
                    },
                },
            },
            "tools": "not-a-list",
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.tools == []

    def test_provider_models_empty_list(self, tmp_path, monkeypatch):
        """Provider with empty models list contributes no models."""
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        # Only provider with real model so it's collected
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p1": {
                        "api_key": "${KEY}",
                        "models": [],
                    },
                    "p2": {
                        "api_key": "${KEY}",
                        "models": [{"model": "solo"}],
                    },
                },
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert len(config.models) == 1
        assert config.models[0].ref == "p2/solo"


# ── Strict config keys (no OpenClaw aliases) ──────────────────────────


class TestModelConfigStrictKeys:
    """Model entries read only slife's own snake_case keys — no aliases."""

    def test_id_key_not_read(self):
        """The legacy ``id`` key is ignored; 'model' is the only id source."""
        with pytest.raises(ValueError, match="missing"):
            ModelConfig.from_dict({"id": "claude-3", "api_key": "sk-test"})

    def test_camel_case_keys_not_read(self):
        """camelCase keys (apiKey, contextWindow, maxTokens, baseUrl)
        are ignored — a camelCase-only entry falls back to defaults."""
        mc = ModelConfig.from_dict({"model": "test", "apiKey": "sk-camel-key"})
        assert mc.api_key == ""
        assert mc.context_window == 131072
        assert mc.max_tokens == 4096
        assert mc.base_url == "https://api.deepseek.com"

    def test_snake_case_keys_read(self):
        mc = ModelConfig.from_dict({
            "model": "test", "api_key": "sk-snake",
            "context_window": 99999, "max_tokens": 666,
            "base_url": "https://snake.api/v1",
        })
        assert mc.api_key == "sk-snake"
        assert mc.context_window == 99999
        assert mc.max_tokens == 666
        assert mc.base_url == "https://snake.api/v1"

    def test_compat_field(self):
        mc = ModelConfig.from_dict({
            "model": "test", "api_key": "key",
            "compat": {"thinkingFormat": "openai"},
        })
        assert mc.compat == {"thinkingFormat": "openai"}

    def test_cost_field(self):
        mc = ModelConfig.from_dict({
            "model": "test", "api_key": "key",
            "cost": {"input": 0.003, "output": 0.015},
        })
        assert mc.cost == {"input": 0.003, "output": 0.015}

    def test_non_dict_compat_is_none(self):
        mc = ModelConfig.from_dict({"model": "test", "api_key": "key", "compat": "not-a-dict"})
        assert mc.compat is None

    def test_missing_compat_and_cost_are_none(self):
        mc = ModelConfig.from_dict({"model": "test", "api_key": "key"})
        assert mc.compat is None
        assert mc.cost is None

    def test_missing_model_raises(self):
        with pytest.raises(ValueError, match="missing"):
            ModelConfig.from_dict({"api_key": "key"})
