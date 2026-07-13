"""Configuration for slife agent — OpenClaw-compatible JSON5 format.

Two-level model hierarchy:
  providers:
    <provider-id>:           # connection config (shared)
      base_url, api_key, api
      models:
        - model: "<api-name>"  # API model name, doubles as local id
          name: "<display>"    # human-readable label
          reasoning, input, context_window, max_tokens, ...

Model refs: "provider-id/model-name"
"""

import json5
import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from slife.env import resolve_env
from slife.tools._config_io import with_fetched_at

logger = logging.getLogger(__name__)


@dataclass
class ModelConfig:
    """Configuration for a single LLM model."""

    ref: str                       # "deepseek/deepseek-v4-flash"
    provider: str                  # "deepseek"
    api_model: str                 # "deepseek-v4-flash" (sent to API)
    display_name: str              # "DeepSeek V4 Flash"
    api_key: str
    base_url: str = "https://api.deepseek.com"
    api: str = "openai-completions"
    supports_vision: bool = False
    max_tokens: int = 4096
    context_window: int = 131072
    temperature: float = 0.7
    top_p: float = 1.0
    thinking_enabled: bool = False
    reasoning_effort: str | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """Parse a model entry (OpenClaw field names → internal).

        model: API model name, doubles as local id (e.g. "deepseek-v4-flash")
        name: display label (e.g. "DeepSeek V4 Flash")
        reasoning: true → thinking_enabled
        input: ["text","image"] → supports_vision
        """
        api_model = data["model"]

        # model may contain provider prefix: "deepseek/deepseek-v4-flash"
        if "/" in api_model:
            provider, local_id = api_model.split("/", 1)
        else:
            provider = data.get("provider", "unknown")
            local_id = api_model

        ref = f"{provider}/{local_id}"
        display_name = data.get("name", api_model)
        thinking = data.get("reasoning", data.get("thinking_enabled", False))
        model_input = data.get("input", [])
        supports_vision = "image" in model_input if model_input else data.get(
            "supports_vision", False
        )

        return cls(
            ref=ref,
            provider=provider,
            api_model=api_model,
            display_name=display_name,
            api_key=data["api_key"],
            base_url=data.get("base_url", "https://api.deepseek.com"),
            api=data.get("api", "openai-completions"),
            supports_vision=supports_vision,
            max_tokens=data.get("max_tokens", 4096),
            context_window=data.get("context_window", 131072),
            temperature=data.get("temperature", 0.7),
            top_p=data.get("top_p", 1.0),
            thinking_enabled=bool(thinking),
            reasoning_effort=data.get("reasoning_effort"),
        )


@dataclass
class MCPConfig:
    """Configuration for the MCP wrapper and external MCP servers."""

    enabled: bool = False
    # Default: use the same Python interpreter that runs slife.
    # Avoids 'uv run' which can hit Windows file-lock errors when
    # uv tries to manage cached .exe wrappers.
    wrapper_command: str = sys.executable
    wrapper_args: list = None  # type: ignore[assignment]
    # HTTP endpoint for the MCP wrapper. Always set — slife probes this
    # URL first, falls back to spawning a child process via stdio.
    # The wrapper server also reads this when started standalone:
    #   python -m slife_mcp.server --transport http [--config slife.json5]
    wrapper_url: str = "http://127.0.0.1:9876/mcp"
    servers: dict[str, dict] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.wrapper_args is None:
            self.wrapper_args = ["-m", "slife_mcp.server"]
        if self.servers is None:
            self.servers = {}

    @classmethod
    def from_dict(cls, data: dict) -> "MCPConfig":
        """Parse mcp config section from JSON5 config.

        MCP is enabled when:
          - 'enabled: true' is set explicitly, OR
          - servers are configured (non-empty servers dict), OR
          - a custom wrapper is configured (wrapper.command, wrapper.args,
            or wrapper.url).
        An absent mcp section or empty mcp dict leaves MCP disabled.
        """
        if not isinstance(data, dict):
            return cls()

        servers = data.get("servers", {})
        if not isinstance(servers, dict):
            servers = {}

        wrapper = data.get("wrapper", {})
        if not isinstance(wrapper, dict):
            wrapper = {}

        explicit_enabled = data.get("enabled")
        has_servers = len(servers) > 0
        has_wrapper_cfg = bool(
            wrapper.get("command") or wrapper.get("args") or wrapper.get("url")
        )

        if not explicit_enabled and not has_servers and not has_wrapper_cfg:
            return cls()

        return cls(
            enabled=True,
            wrapper_command=wrapper.get("command", sys.executable),
            wrapper_args=wrapper.get("args", ["-m", "slife_mcp.server"]),
            wrapper_url=wrapper.get("url", "http://127.0.0.1:9876/mcp"),
            servers=servers,
        )


@dataclass
class Config:
    """Top-level configuration for slife."""

    models: list[ModelConfig]
    active_model_ref: str
    tools: list[dict]
    env: dict | None = None
    max_iterations: int = 10
    mcp_config: MCPConfig | None = None
    _path: Path | None = None

    def __post_init__(self):
        if self.mcp_config is None:
            self.mcp_config = MCPConfig()

    def save_mcp_server(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None, description: str = "", source: dict | None = None) -> None:
        """Persist an MCP server to the config file."""
        if not self._path:
            logger.warning("config_no_path action=save_mcp server=%s", name)
            return

        raw = json5.loads(self._path.read_text(encoding="utf-8"))
        mcp_section = raw.setdefault("mcp", {})
        servers = mcp_section.setdefault("servers", {})

        server_entry: dict = {"command": command, "args": args}
        if description:
            server_entry["description"] = description
        if env:
            server_entry["env"] = env
        source = with_fetched_at(source)
        if source:
            server_entry["source"] = source
        servers[name] = server_entry

        self._path.write_text(json5.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        self.mcp_config.servers[name] = server_entry
        logger.info("config_save_mcp server=%s", name)

    def remove_mcp_server(self, name: str) -> None:
        """Remove an MCP server from the config file."""
        if not self._path:
            logger.warning("config_no_path action=remove_mcp server=%s", name)
            return

        raw = json5.loads(self._path.read_text(encoding="utf-8"))
        mcp_section = raw.get("mcp", {})
        servers = mcp_section.get("servers", {})
        if name in servers:
            del servers[name]
            self._path.write_text(json5.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            self.mcp_config.servers.pop(name, None)
            logger.info("config_remove_mcp server=%s", name)

    def set_server_disclosure(self, name: str, disclosure: str) -> None:
        """Persist disclosure mode for an MCP server to the config file.

        Args:
            name: Server name.
            disclosure: 'eager' or 'lazy'.
        """
        if not self._path:
            logger.warning("config_no_path action=set_disclosure server=%s", name)
            return

        raw = json5.loads(self._path.read_text(encoding="utf-8"))
        servers = raw.setdefault("mcp", {}).setdefault("servers", {})
        if name in servers:
            if disclosure == "eager":
                servers[name].pop("disclosure", None)
            else:
                servers[name]["disclosure"] = disclosure
            self._path.write_text(json5.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
            # Update in-memory state
            if name in self.mcp_config.servers:
                if disclosure == "eager":
                    self.mcp_config.servers[name].pop("disclosure", None)
                else:
                    self.mcp_config.servers[name]["disclosure"] = disclosure
            logger.info("config_set_disclosure server=%s disclosure=%s", name, disclosure)

    @property
    def active_model(self) -> ModelConfig:
        """Return the currently active model configuration."""
        for m in self.models:
            if m.ref == self.active_model_ref:
                return m
        raise KeyError(
            f"Active model '{self.active_model_ref}' not found. "
            f"Available: {[m.ref for m in self.models]}"
        )

    # ── Parsing helpers ──────────────────────────────────────────────

    @staticmethod
    def _parse_models_section(models_section) -> tuple[list[ModelConfig], int]:
        """Parse the models section into ModelConfig instances.

        Supports both dict (providers) and flat-list formats.

        Returns:
            (models, provider_count) — provider_count is 0 for list format.
        """
        if isinstance(models_section, dict):
            return Config._parse_provider_models(models_section)
        elif isinstance(models_section, list):
            models = []
            for m in models_section:
                if not isinstance(m, dict):
                    continue
                models.append(ModelConfig.from_dict(resolve_env(m)))
            return models, 0
        return [], 0

    @staticmethod
    def _parse_provider_models(models_section: dict) -> tuple[list[ModelConfig], int]:
        """Parse provider-style models section.

        Each provider has shared api_key/base_url/api that models inherit.
        """
        providers = models_section.get("providers", {})
        if not isinstance(providers, dict):
            return [], 0

        all_models: list[ModelConfig] = []

        for provider_id, provider_cfg in providers.items():
            if not isinstance(provider_cfg, dict):
                continue

            provider_cfg = resolve_env(provider_cfg)
            defaults = {
                "api_key": provider_cfg.get("api_key", ""),
                "base_url": provider_cfg.get("base_url", ""),
                "api": provider_cfg.get("api", "openai-completions"),
            }

            model_list = provider_cfg.get("models", [])
            if not isinstance(model_list, list):
                continue

            seen_ids: set[str] = set()
            for m in model_list:
                if not isinstance(m, dict):
                    continue
                m = resolve_env(m)
                for key, value in defaults.items():
                    m.setdefault(key, value)
                m.setdefault("provider", provider_id)

                local_id = m["model"].split("/", 1)[-1]
                if local_id in seen_ids:
                    raise ValueError(
                        f"Duplicate model '{local_id}' in provider "
                        f"'{provider_id}'. Model names must be unique "
                        f"within a provider."
                    )
                seen_ids.add(local_id)
                all_models.append(ModelConfig.from_dict(m))

        return all_models, len(providers)

    @staticmethod
    def _parse_section(raw: dict, key: str, expected_type, default):
        """Safely extract a typed section from parsed JSON5, returning
        default if the value is missing or of the wrong type."""
        value = raw.get(key, default)
        return value if isinstance(value, expected_type) else default

    # ── Main loader ─────────────────────────────────────────────────

    @classmethod
    def from_json5(cls, path: str | Path = "slife.json5") -> "Config":
        """Load from JSON5 file with provider→model hierarchy."""
        path = Path(path)
        logger.info("config_load path=%s", path)
        if not path.exists():
            raise FileNotFoundError(
                f"Config file not found: {path}\n"
                f"Copy slife.json5.example → slife.json5 and edit it."
            )

        raw = json5.loads(path.read_text(encoding="utf-8"))

        # Models
        all_models, provider_count = cls._parse_models_section(
            raw.get("models", {})
        )
        if not all_models:
            raise ValueError(
                "No models defined. Add models.providers.<id>.models[]."
            )
        logger.info(
            "config_models count=%d providers=%d",
            len(all_models),
            provider_count,
        )

        # Agent
        agent = cls._parse_section(raw, "agent", dict, {})
        max_iterations = agent.get("max_iterations", 10)

        # Env — inject into os.environ so tools can reference vars via ${VAR}
        env_section = resolve_env(cls._parse_section(raw, "env", dict, {}))
        for key, value in env_section.items():
            os.environ[key] = str(value)
        logger.info("config_env_vars count=%d", len(env_section))

        # Tools (optional — auto-discovery handles defaults)
        tools = resolve_env(cls._parse_section(raw, "tools", list, []))

        # MCP
        mcp_config = MCPConfig.from_dict(raw.get("mcp", {}))
        if mcp_config.enabled:
            logger.info(
                "mcp_config wrapper=%s servers=%d",
                mcp_config.wrapper_command,
                len(mcp_config.servers),
            )

        config = cls(
            models=all_models,
            active_model_ref=raw.get("active_model", all_models[0].ref),
            tools=tools,
            env=env_section,
            max_iterations=max_iterations,
            mcp_config=mcp_config,
        )
        config._path = path
        return config
