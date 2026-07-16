"""Tests for Slife.config — configuration loading and model definitions."""

import json
import logging
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


# ── MCPConfig.from_dict ────────────────────────────────────────────────


class TestMCPConfigFromDict:
    """Tests for MCPConfig.from_dict edge cases."""

    def test_non_dict_returns_default(self):
        """Non-dict input returns default MCPConfig."""
        from slife.config import MCPConfig
        result = MCPConfig.from_dict("not a dict")
        assert result.servers == {}

    def test_list_returns_default(self):
        """List input returns default MCPConfig."""
        from slife.config import MCPConfig
        result = MCPConfig.from_dict([1, 2, 3])
        assert result.servers == {}

    def test_non_dict_wrapper(self):
        """Non-dict wrapper field is ignored."""
        from slife.config import MCPConfig
        result = MCPConfig.from_dict({"wrapper": "not-a-dict"})
        assert result.wrapper_command  # default preserved (non-empty)

    def test_non_dict_servers(self):
        """Non-dict servers field uses empty dict."""
        from slife.config import MCPConfig
        result = MCPConfig.from_dict({"servers": [1, 2]})
        assert result.servers == {}

    def test_custom_wrapper(self):
        """Custom wrapper command and args are parsed."""
        from slife.config import MCPConfig
        result = MCPConfig.from_dict({
            "wrapper": {
                "command": "python",
                "args": ["-m", "my_mcp"],
            },
        })
        assert result.wrapper_command == "python"
        assert result.wrapper_args == ["-m", "my_mcp"]

    def test_servers_parsed(self):
        """Server entries are parsed and stored."""
        from slife.config import MCPConfig
        result = MCPConfig.from_dict({
            "servers": {
                "fs": {"command": "npx", "args": ["-y", "server-filesystem"]},
            },
        })
        assert "fs" in result.servers

    def test_wrapper_command_parsed(self):
        """wrapper.command is parsed from config."""
        from slife.config import MCPConfig
        result = MCPConfig.from_dict({
            "wrapper": {"command": "/usr/bin/python3"},
        })
        assert result.wrapper_command == "/usr/bin/python3"


# ── Config.save_mcp_server / remove_mcp_server ──────────────────────────


class TestConfigMCPSaveRemove:
    """Tests for Config.save_mcp_server and remove_mcp_server."""

    def test_save_server_persists_to_file(self, tmp_path, monkeypatch):
        """save_mcp_server writes to the JSON5 config file."""
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
        }))
        config = Config.from_json5(str(cfg_path))
        assert config._path is not None

        config.save_mcp_server("fs", "npx", ["-y", "server-filesystem"],
                               env={"NODE_ENV": "production"})

        # Re-read config file to verify persistence
        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        servers = raw["mcp"]["servers"]
        assert "fs" in servers
        assert servers["fs"]["command"] == "npx"
        assert servers["fs"]["args"] == ["-y", "server-filesystem"]
        assert servers["fs"]["env"] == {"NODE_ENV": "production"}
        # In-memory state also updated
        assert "fs" in config.mcp_config.servers

    def test_save_server_without_env(self, tmp_path, monkeypatch):
        """save_mcp_server works without env parameter."""
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
        }))
        config = Config.from_json5(str(cfg_path))

        config.save_mcp_server("test_srv", "echo", ["hello"])

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert "test_srv" in raw["mcp"]["servers"]
        assert "env" not in raw["mcp"]["servers"]["test_srv"]

    def test_save_server_no_path_warns(self, caplog):
        """save_mcp_server without _path logs warning but doesn't crash."""
        from slife.config import Config, ModelConfig
        mc = ModelConfig(
            ref="test/m",
            provider="test",
            api_model="m",
            display_name="M",
            api_key="k",
        )
        config = Config(models=[mc], active_model_ref="test/m", tools=[])

        with caplog.at_level(logging.WARNING):
            config.save_mcp_server("fs", "cmd", ["arg"])
        assert "config_no_path" in caplog.text

    def test_remove_server_persists(self, tmp_path, monkeypatch):
        """remove_mcp_server removes from file and in-memory state."""
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
            "mcp": {
                "servers": {
                    "to_remove": {"command": "echo", "args": ["bye"]},
                    "to_keep": {"command": "echo", "args": ["hi"]},
                }
            },
        }))
        config = Config.from_json5(str(cfg_path))

        config.remove_mcp_server("to_remove")

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        servers = raw["mcp"]["servers"]
        assert "to_remove" not in servers
        assert "to_keep" in servers
        assert "to_remove" not in config.mcp_config.servers

    def test_remove_server_no_path_warns(self, caplog):
        """remove_mcp_server without _path logs warning but doesn't crash."""
        from slife.config import Config, ModelConfig
        mc = ModelConfig(
            ref="test/m",
            provider="test",
            api_model="m",
            display_name="M",
            api_key="k",
        )
        config = Config(models=[mc], active_model_ref="test/m", tools=[])

        with caplog.at_level(logging.WARNING):
            config.remove_mcp_server("nonexistent")
        assert "config_no_path" in caplog.text

    def test_save_server_with_source(self, tmp_path, monkeypatch):
        """save_mcp_server with source stores it in JSON5 with fetched_at."""
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
        }))
        config = Config.from_json5(str(cfg_path))

        config.save_mcp_server(
            "gh", "npx",
            ["-y", "anyapi-mcp-server", "--spec", "https://example.com/api.yaml"],
            source={"url": "https://github.com/quiloos39/anyapi-mcp-server", "type": "mcp_package", "version": "1.2.0"},
        )

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        server = raw["mcp"]["servers"]["gh"]
        assert server["source"]["url"] == "https://github.com/quiloos39/anyapi-mcp-server"
        assert server["source"]["type"] == "mcp_package"
        assert server["source"]["version"] == "1.2.0"
        assert "fetched_at" in server["source"]

    def test_save_server_without_source(self, tmp_path, monkeypatch):
        """save_mcp_server without source is backward compatible."""
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
        }))
        config = Config.from_json5(str(cfg_path))

        config.save_mcp_server("srv", "echo", ["hello"])
        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert "source" not in raw["mcp"]["servers"]["srv"]

    def test_save_server_with_none_source(self, tmp_path, monkeypatch):
        """save_mcp_server with source=None does not write source key."""
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
        }))
        config = Config.from_json5(str(cfg_path))

        config.save_mcp_server("srv", "echo", ["hello"], source=None)
        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert "source" not in raw["mcp"]["servers"]["srv"]

    def test_save_server_with_url_and_headers(self, tmp_path, monkeypatch):
        """save_mcp_server with URL and headers stores both."""
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
        }))
        config = Config.from_json5(str(cfg_path))

        config.save_mcp_server(
            "web", "node", ["server.js"],
            url="http://localhost:3000",
            headers={"Authorization": "Bearer token"},
            description="A web server",
        )

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        srv = raw["mcp"]["servers"]["web"]
        assert srv["url"] == "http://localhost:3000"
        assert srv["headers"] == {"Authorization": "Bearer token"}
        assert srv["description"] == "A web server"

    def test_set_server_disclosure_eager(self, tmp_path, monkeypatch):
        """set_server_disclosure eager removes disclosure key."""
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
            "mcp": {
                "servers": {
                    "mysrv": {"command": "echo", "args": [], "disclosure": "lazy"},
                },
            },
        }))
        config = Config.from_json5(str(cfg_path))
        # Pre-populate in-memory state with disclosure=lazy
        config.mcp_config.servers["mysrv"]["disclosure"] = "lazy"

        config.set_server_disclosure("mysrv", "eager")

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert "disclosure" not in raw["mcp"]["servers"]["mysrv"]
        assert "disclosure" not in config.mcp_config.servers["mysrv"]

    def test_set_server_disclosure_lazy(self, tmp_path, monkeypatch):
        """set_server_disclosure lazy adds disclosure key."""
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
            "mcp": {
                "servers": {
                    "mysrv": {"command": "echo", "args": []},
                },
            },
        }))
        config = Config.from_json5(str(cfg_path))
        config.mcp_config.servers["mysrv"] = {"command": "echo", "args": []}

        config.set_server_disclosure("mysrv", "lazy")

        raw = json5.loads(cfg_path.read_text(encoding="utf-8"))
        assert raw["mcp"]["servers"]["mysrv"]["disclosure"] == "lazy"
        assert config.mcp_config.servers["mysrv"]["disclosure"] == "lazy"

    def test_set_server_disclosure_no_path(self, caplog):
        """set_server_disclosure without _path logs warning."""
        from slife.config import Config, ModelConfig
        mc = ModelConfig(
            ref="test/m", provider="test", api_model="m",
            display_name="M", api_key="k",
        )
        config = Config(models=[mc], active_model_ref="test/m", tools=[])
        config.mcp_config.servers["mysrv"] = {"command": "cmd"}

        with caplog.at_level(logging.WARNING):
            config.set_server_disclosure("mysrv", "lazy")
        assert "config_no_path" in caplog.text


# ── MemoryConfig ──────────────────────────────────────────────────────


class TestMemoryConfigFromDict:
    """Tests for MemoryConfig.from_dict."""

    def test_non_dict_returns_default(self):
        from slife.config import MemoryConfig
        result = MemoryConfig.from_dict("not a dict")
        assert result.db_path == "~/.slife/slife.db"

    def test_non_dict_embedding(self):
        from slife.config import MemoryConfig
        result = MemoryConfig.from_dict({"embedding": "not a dict"})
        assert result.embedding_model == "text-embedding-3-small"
        assert result.embedding_dim == 1536

    def test_custom_values(self):
        from slife.config import MemoryConfig
        result = MemoryConfig.from_dict({
            "db_path": "/custom/path.db",
            "embedding": {"model": "custom-model", "dim": 768},
        })
        assert result.db_path == "/custom/path.db"
        assert result.embedding_model == "custom-model"
        assert result.embedding_dim == 768


# ── parse_cli_agent / parse_cli_user ───────────────────────────────────


class TestParseCLI:
    def test_parse_cli_agent_found(self):
        from slife.config import parse_cli_agent
        result = parse_cli_agent(["slife", "--agent", "my-agent"])
        assert result == "my-agent"

    def test_parse_cli_agent_missing(self):
        from slife.config import parse_cli_agent
        result = parse_cli_agent(["slife"])
        assert result is None

    def test_parse_cli_agent_no_value(self):
        from slife.config import parse_cli_agent
        result = parse_cli_agent(["slife", "--agent"])
        assert result is None

    def test_parse_cli_user_found(self):
        from slife.config import parse_cli_user
        result = parse_cli_user(["slife", "--user", "bob"])
        assert result == "bob"

    def test_parse_cli_user_default(self):
        from slife.config import parse_cli_user
        result = parse_cli_user(["slife"])
        assert result == "default"


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
    """Tests for A2A config with agent name."""

    def test_with_agent_name(self, tmp_path, monkeypatch):
        monkeypatch.setenv("KEY", "sk-test")
        cfg_path = tmp_path / "slife.json5"
        cfg_path.write_text(json5.dumps({
            "models": {
                "providers": {
                    "p": {"api_key": "${KEY}", "models": [{"model": "m"}]},
                },
            },
            "mqtt": {
                "host": "mqtt.example.com",
                "port": 1883,
            },
        }))
        config = Config.from_json5(str(cfg_path), agent_name="slife-1")
        assert config.a2a_config is not None
        assert config.a2a_config.agent_id is not None


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
        assert config.max_iterations == 10

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

    def test_mcp_section_parsed(self, tmp_path, monkeypatch):
        """MCP section in config is parsed into MCPConfig."""
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
            "mcp": {
                "enabled": True,
                "wrapper": {"command": "python", "args": ["-m", "slife.plugins.mcp"]},
                "servers": {"srv1": {"command": "npx", "args": []}},
            },
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.mcp_config is not None
        assert config.mcp_config.wrapper_command == "python"
        assert "srv1" in config.mcp_config.servers

    def test_mcp_always_available(self, tmp_path, monkeypatch):
        """MCP wrapper is always configured even when absent from config."""
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
        }))
        config = Config.from_json5(str(cfg_path))
        assert config.mcp_config is not None
        assert config.mcp_config.servers == {}

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
