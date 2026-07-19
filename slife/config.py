"""Configuration for Slife agent -- OpenClaw-compatible JSON5 format.

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

from credstore import resolve_uri, is_keyring_uri
from slife.env import resolve_env
from slife.tools._config_io import with_fetched_at
from slife.a2a.config import A2AConfig

logger = logging.getLogger(__name__)


def _resolve_api_key(value: str) -> str:
    """Resolve an api_key value through the full chain.

    1. ``keyring:`` URI  → credstore
    2. ``${VAR}``        → os.environ → credstore
    3. plaintext         → as-is (legacy, logged warning)
    """
    import os

    # keyring: URI
    from credstore import is_keyring_uri, resolve_uri
    if is_keyring_uri(value):
        return resolve_uri(value)

    # ${VAR} reference
    if value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        # os.environ first (user may have resolved it by now)
        env_val = os.environ.get(var_name)
        if env_val:
            return env_val
        # credstore fallback
        cred_val = _try_credstore_lookup(var_name)
        if cred_val:
            return cred_val
        # Can't resolve — return as-is (will fail in LLMClient with clear error)
        return value

    # Plaintext
    if value and len(value) >= 12:
        logger.warning("plaintext_api_key — store with: credstore set ...")
    return value


def _resolve_mcp_env_var(value: str) -> str:
    """Resolve a ``${VAR}`` reference in MCP server env.

    Same logic as _resolve_api_key but for env var values.
    """
    import os

    if value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        env_val = os.environ.get(var_name)
        if env_val:
            return env_val
        cred_val = _try_credstore_lookup(var_name)
        if cred_val:
            return cred_val
    return value


def _resolve_env_lenient(value):
    """Resolve ${VAR} references without raising on missing vars.

    Missing vars are left as-is (e.g. ``${DEEPSEEK_API_KEY}``) so
    downstream resolvers (credstore, defaults) get a chance.
    """
    try:
        return resolve_env(value)
    except KeyError:
        return value


def _try_credstore_lookup(key: str) -> str | None:
    """Look up an env var name in credstore (keyring).

    The env var name IS the credstore key — e.g. ``DEEPSEEK_API_KEY``.

    Returns the credential value, or None if not found or credstore
    is unavailable.
    """
    try:
        from credstore import get_credential
        return get_credential(key)
    except Exception:
        return None


def parse_cli_agent(argv: list[str]) -> str:
    """Extract ``--agent <value>`` from CLI args. Defaults to ``"slife"``.

    The agent identity isolates memory on multi-user machines and serves
    as the A2A network identity on the MQTT mesh.
    """
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--agent" and i + 1 < len(args):
            return args[i + 1]
        i += 1
    return "slife"


def _parse_section(raw: dict, key: str, expected_type, default):
    """Safely extract a typed section from parsed JSON5, returning
    *default* if the value is missing or of the wrong type."""
    value = raw.get(key, default)
    return value if isinstance(value, expected_type) else default


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
        """Parse a model entry (OpenClaw field names ->internal).

        model: API model name, doubles as local id (e.g. "deepseek-v4-flash")
        name: display label (e.g. "DeepSeek V4 Flash")
        reasoning: true ->thinking_enabled
        input: ["text","image"] ->supports_vision
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
            api_key=_resolve_api_key(data["api_key"]),
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
    """Configuration for the MCP wrapper and external MCP servers.

    Always enabled -- slife-mcp is a built-in plugin.
    """

    wrapper_command: str = sys.executable
    wrapper_args: list = None  # type: ignore[assignment]
    servers: dict[str, dict] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.wrapper_args is None:
            self.wrapper_args = ["-m", "slife.plugins.mcp.server"]
        if self.servers is None:
            self.servers = {}

    @classmethod
    def from_dict(cls, data: dict) -> "MCPConfig":
        """Parse mcp config section from JSON5 config."""
        if not isinstance(data, dict):
            return cls()

        servers = data.get("servers", {})
        if not isinstance(servers, dict):
            servers = {}

        # Resolve ${VAR} in each server's env section
        for sname, scfg in servers.items():
            if isinstance(scfg, dict) and "env" in scfg:
                senv = scfg["env"]
                if isinstance(senv, dict):
                    for k, v in senv.items():
                        senv[k] = _resolve_mcp_env_var(str(v))

        wrapper = data.get("wrapper", {})
        if not isinstance(wrapper, dict):
            wrapper = {}

        return cls(
            wrapper_command=wrapper.get("command", sys.executable),
            wrapper_args=wrapper.get("args", ["-m", "slife.plugins.mcp.server"]),
            servers=servers,
        )


@dataclass
class MemoryConfig:
    """Configuration for the slife-memory service.

    Always enabled -- slife-memory is a built-in plugin.
    """

    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    @classmethod
    def from_dict(cls, data: dict) -> "MemoryConfig":
        """Parse memory config section from JSON5 config."""
        if not isinstance(data, dict):
            return cls()
        emb = data.get("embedding", {})
        if not isinstance(emb, dict):
            emb = {}
        return cls(
            embedding_model=emb.get("model", "text-embedding-3-small"),
            embedding_dim=emb.get("dim", 1536),
        )


@dataclass
class WechatConfig:
    """Configuration for the slife-wechat plugin.

    Optional -- only loaded when ``wechat.enabled`` is true.
    Session tokens are stored per-agent in ``wechat_<agent_id>.json5``.
    """

    enabled: bool = True

    @classmethod
    def from_dict(cls, data: dict) -> "WechatConfig":
        """Parse wechat config section from JSON5 config.

        Defaults to enabled when the wechat section is absent -- the plugin
        is lightweight and only activates when wechat_login is called.
        Set ``wechat: { enabled: false }`` to explicitly opt out.
        """
        if not isinstance(data, dict):
            return cls()
        return cls(enabled=data.get("enabled", True))


@dataclass
class Config:
    """Top-level configuration for Slife."""

    models: list[ModelConfig]
    active_model_ref: str
    tools: list[dict]
    env: dict | None = None
    max_iterations: int = 10
    context_floor: float = 0.2
    context_ceiling: float = 0.8
    tool_result_ceiling: float = 0.2  # max tool result = 20% of context window
    agent_id: str = "slife"
    mcp_config: MCPConfig | None = None
    memory_config: MemoryConfig | None = None
    wechat_config: WechatConfig | None = None
    a2a_config: A2AConfig | None = None
    subagent_config: dict | None = None
    _path: Path | None = None

    def __post_init__(self):
        if self.mcp_config is None:
            self.mcp_config = MCPConfig()
        if self.memory_config is None:
            self.memory_config = MemoryConfig()
        if self.wechat_config is None:
            self.wechat_config = WechatConfig()
        if self.a2a_config is None:
            self.a2a_config = A2AConfig()

    # ── Config file I/O helpers ─────────────────────────────────────

    def _read_config(self, action: str, server: str) -> dict | None:
        """Read and parse the JSON5 config file. Returns None if no path."""
        if not self._path:
            logger.warning("config_no_path action=%s server=%s", action, server)
            return None
        from slife.tools._config_io import read_config
        return read_config(self._path)

    def _write_config(self, raw: dict) -> None:
        """Write the JSON5 config back to disk."""
        assert self._path is not None
        from slife.tools._config_io import write_config
        write_config(self._path, raw)

    def save_mcp_server(self, name: str, command: str, args: list[str], env: dict[str, str] | None = None, description: str = "", source: dict | None = None, url: str = "", headers: dict[str, str] | None = None) -> None:
        """Persist an MCP server to the config file."""
        raw = self._read_config("save_mcp", name)
        if raw is None:
            return

        servers = raw.setdefault("mcp", {}).setdefault("servers", {})
        server_entry: dict = {"command": command, "args": args}
        if url:
            server_entry["url"] = url
        if headers:
            server_entry["headers"] = headers
        if description:
            server_entry["description"] = description
        if env:
            server_entry["env"] = dict(env)
        source = with_fetched_at(source)
        if source:
            server_entry["source"] = source
        servers[name] = server_entry

        self._write_config(raw)
        self.mcp_config.servers[name] = server_entry
        logger.info("config_save_mcp server=%s", name)

    def remove_mcp_server(self, name: str) -> None:
        """Remove an MCP server from the config file."""
        raw = self._read_config("remove_mcp", name)
        if raw is None:
            return

        servers = raw.get("mcp", {}).get("servers", {})
        if name in servers:
            del servers[name]
            self._write_config(raw)
            self.mcp_config.servers.pop(name, None)
            logger.info("config_remove_mcp server=%s", name)

    def set_server_disclosure(self, name: str, disclosure: str) -> None:
        """Persist disclosure mode for an MCP server to the config file.

        Args:
            name: Server name.
            disclosure: 'eager' or 'lazy'.
        """
        raw = self._read_config("set_disclosure", name)
        if raw is None:
            return

        servers = raw.setdefault("mcp", {}).setdefault("servers", {})
        if name in servers:
            if disclosure == "eager":
                servers[name].pop("disclosure", None)
            else:
                servers[name]["disclosure"] = disclosure
            self._write_config(raw)
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
    def _load_subagent_config(raw: dict) -> dict:
        """Extract subagent config with defaults from parsed JSON5."""
        sub_raw = raw.get("subagent")
        if isinstance(sub_raw, dict):
            return {
                "max_subagents": sub_raw.get("max_subagents", 5),
                "task_timeout": sub_raw.get("task_timeout", 120),
            }
        return {"max_subagents": 5, "task_timeout": 120}

    @staticmethod
    def _parse_models_section(models_section) -> tuple[list[ModelConfig], int]:
        """Parse the models section into ModelConfig instances.

        Supports both dict (providers) and flat-list formats.

        Returns:
            (models, provider_count) -- provider_count is 0 for list format.
        """
        if isinstance(models_section, dict):
            return Config._parse_provider_models(models_section)
        elif isinstance(models_section, list):
            models = []
            for m in models_section:
                if not isinstance(m, dict):
                    continue
                models.append(ModelConfig.from_dict(_resolve_env_lenient(m)))
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

            provider_cfg = _resolve_env_lenient(provider_cfg)
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
                m = _resolve_env_lenient(m)
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

    # ── Main loader ─────────────────────────────────────────────────

    @classmethod
    def from_json5(
        cls, path: str | Path = "slife.json5",
        agent_id: str = "slife",
    ) -> "Config":
        """Load from JSON5 file with provider->model hierarchy.

        Args:
            path: Path to the JSON5 config file.
                  Defaults to ``~/.slife/slife.json5``.
            agent_id: Agent identity key (``--agent`` on the CLI).
                      Defaults to ``"slife"``.  Used for memory isolation
                      and as the MQTT agent identity when Mosquitto is available.
        """
        path = Path(path).expanduser()
        logger.debug("config_load path=%s", path)
        if not path.exists():
            # First run: copy the bundled template config to ~/.slife/.
            import shutil
            path.parent.mkdir(parents=True, exist_ok=True)
            pkg_template = Path(__file__).parent / "slife.template.json5"
            if pkg_template.exists():
                shutil.copy(pkg_template, path)
                logger.info("config_seeded from=%s to=%s", pkg_template, path)
                print(f"\n  First run — created: {path}")
                print(f"  Set your API key and you're ready:")
                print(f"    credstore set DEEPSEEK_API_KEY")
                print(f"    slife\n")
                raise SystemExit(0)
            raise FileNotFoundError(
                f"Config file not found: {path}\n"
                f"Run: cp slife.template.json5 ~/.slife/slife.json5"
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
        logger.debug(
            "config_models count=%d providers=%d",
            len(all_models),
            provider_count,
        )

        # Agent
        agent = _parse_section(raw, "agent", dict, {})
        max_iterations = agent.get("max_iterations", 10)
        context_floor = agent.get("context_floor", 0.2)
        context_ceiling = agent.get("context_ceiling", 0.8)
        tool_result_ceiling = agent.get("tool_result_ceiling", 0.2)

        # Env -- inject into os.environ so tools can reference vars via ${VAR}
        # Resolution order: os.environ ->credstore (keyring) ->config value
        env_section = _parse_section(raw, "env", dict, {})
        for key, value in env_section.items():
            # 1. Already set in environment (user's shell) -- keep it
            if os.environ.get(key):
                continue
            # 2. Try credstore
            cred_value = _try_credstore_lookup(key)
            if cred_value:
                os.environ[key] = cred_value
                continue
            # 3. Fall back to config value (with env var resolution)
            try:
                resolved = resolve_env(str(value))
            except KeyError:
                resolved = str(value)
            if resolved and not str(resolved).startswith("${"):
                os.environ[key] = str(resolved)
        logger.debug("config_env_vars count=%d", len(env_section))

        # Tools (optional -- auto-discovery handles defaults)
        tools = resolve_env(_parse_section(raw, "tools", list, []))

        # MCP (built-in plugin, always enabled)
        # Resolve ${VAR} in MCP server env vars
        mcp_raw = _resolve_env_lenient(raw.get("mcp", {}))
        mcp_config = MCPConfig.from_dict(mcp_raw)
        logger.debug(
            "mcp_config wrapper=%s servers=%d",
            mcp_config.wrapper_command,
            len(mcp_config.servers),
        )

        # Memory -- built-in plugin, always enabled.  DB files live in
        # ~/.slife/<agent_id>.db — no configuration needed.
        memory_config = MemoryConfig.from_dict(raw.get("memory", {}))
        logger.debug(
            "memory_config embed=%s",
            memory_config.embedding_model,
        )

        # WeChat -- optional plugin, enabled via wechat.enabled
        wechat_config = WechatConfig.from_dict(raw.get("wechat", {}))
        if wechat_config.enabled:
            logger.debug(
                "wechat_config agent_id=%s",
                agent_id,
            )

        # A2A/MQTT — always parse config; enabled at runtime after mosquitto probe
        a2a_config = A2AConfig.from_dict(raw.get("mqtt"), agent_id=agent_id)
        if a2a_config.enabled:
            logger.debug(
                "a2a_config id=%s broker=%s:%d",
                a2a_config.agent_id,
                a2a_config.broker_host,
                a2a_config.broker_port,
            )

        # Subagent -- always available (no enabled flag), local stdin/stdout workers
        subagent_config = cls._load_subagent_config(raw)
        logger.debug(
            "subagent_config max_subagents=%d task_timeout=%d",
            subagent_config["max_subagents"],
            subagent_config["task_timeout"],
        )

        config = cls(
            models=all_models,
            active_model_ref=raw.get("active_model", all_models[0].ref),
            tools=tools,
            env=env_section,
            max_iterations=max_iterations,
            context_floor=context_floor,
            context_ceiling=context_ceiling,
            tool_result_ceiling=tool_result_ceiling,
            agent_id=agent_id,
            mcp_config=mcp_config,
            memory_config=memory_config,
            wechat_config=wechat_config,
            a2a_config=a2a_config,
            subagent_config=subagent_config,
        )
        config._path = path
        return config
