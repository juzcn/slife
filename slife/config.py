"""Configuration for Slife agent -- JSON5 format.

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

from slife.env import resolve_env
from slife.a2a.config import A2AConfig

logger = logging.getLogger(__name__)


def _resolve_secret(value: str, *, accept_keyring_uri: bool = False) -> str:
    """Resolve a secret value through the full resolution chain.

    1. ``keyring:`` URI  → credstore (only when *accept_keyring_uri* is True)
    2. ``${VAR}``        → os.environ → credstore
    3. plaintext         → as-is
    """
    # keyring: URI
    if accept_keyring_uri:
        from credstore import is_keyring_uri, resolve_uri
        if is_keyring_uri(value):
            return resolve_uri(value)

    # ${VAR} reference
    if value.startswith("${") and value.endswith("}"):
        var_name = value[2:-1]
        env_val = os.environ.get(var_name)
        if env_val:
            return env_val
        cred_val = _try_credstore_lookup(var_name)
        if cred_val:
            return cred_val

    return value

_T = TypeVar("_T")

def _resolve_env_lenient(value: _T) -> _T:
    """Resolve ${VAR} references without raising on missing vars.

    Missing vars are left as-is (e.g. ``${DEEPSEEK_API_KEY}``) so
    downstream resolvers (credstore, defaults) get a chance.
    """
    try:
        return resolve_env(value)
    except KeyError:
        return value


def _try_credstore_lookup(key: str) -> str | None:
    """Look up an env var name in the credential store (credstore).

    The env var name IS the credential-store key — e.g. ``DEEPSEEK_API_KEY``.

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


def parse_cli_config_path(argv: list[str]) -> str | None:
    """Extract the first positional CLI arg as an explicit config path.

    ``python -m slife myconf.json5`` must use ``myconf.json5`` (the docstring
    promises it); flags (``--headless``, ``--agent <id>``, ``--lang <en|zh>``)
    are skipped along with their values.  Returns ``None`` when no positional
    path is given.
    """
    args = argv[1:]
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            if a in ("--agent", "--lang") and i + 1 < len(args):
                i += 2
                continue
            i += 1
            continue
        return a
    return None


def parse_cli_lang(argv: list[str]) -> str | None:
    """Extract ``--lang <en|zh>`` from CLI args; ``None`` → auto-detect.

    ``python -m slife --lang zh`` forces the TUI language; omitting the
    flag keeps the OS-locale detection in ``slife.ui.i18n``.  A missing or
    invalid value exits with a message — a typo must fail loudly, not
    silently fall back to the detected language.
    """
    args = argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--lang":
            if i + 1 >= len(args):
                raise SystemExit("--lang needs a value: en or zh")
            value = args[i + 1]
            if value not in ("en", "zh"):
                raise SystemExit(f"--lang must be 'en' or 'zh', got {value!r}")
            return value
        i += 1
    return None


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
    input_modalities: tuple[str, ...] = ("text",)
    max_tokens: int = 4096
    context_window: int = 131072
    temperature: float = 0.7
    top_p: float = 1.0
    thinking_enabled: bool = False
    reasoning_effort: str | None = None
    compat: dict | None = None          # compat config (e.g. {thinkingFormat: "openai"})
    cost: dict | None = None            # cost tracking (optional)
    supports_tool_calls: bool = True    # whether this model supports native tool/function calling

    @classmethod
    def from_dict(cls, data: dict) -> "ModelConfig":
        """Parse a model entry.

        model: API model name, doubles as local id (e.g. "deepseek-v4-flash")
        name: display label (e.g. "DeepSeek V4 Flash")
        reasoning: true ->thinking_enabled
        input: ["text","image"] ->supports_vision
        """
        api_model = data.get("model")
        if not api_model:
            raise ValueError("Model entry missing 'model' field")

        # model may contain provider prefix: "deepseek/deepseek-v4-flash"
        # When the model ID contains a slash, it could be either
        #   "provider/model" (e.g. "deepseek/deepseek-v4-flash")
        #   or "org/model" from a third-party catalog (e.g. "deepseek-ai/deepseek-v4-flash").
        # The explicit "provider" field from the provider block always wins.
        explicit_provider = data.get("provider")
        if explicit_provider:
            # Provider is known — keep the full model ID as local_id.
            provider = explicit_provider
            local_id = api_model
        elif "/" in api_model:
            provider, local_id = api_model.split("/", 1)
        else:
            provider = "unknown"
            local_id = api_model

        ref = f"{provider}/{local_id}"
        display_name = data.get("name", api_model)
        thinking = data.get("reasoning", False)
        model_input = data.get("input", [])
        supports_vision = "image" in (model_input or [])
        input_modalities = tuple(model_input) if model_input else ("text",)

        api_key_raw = data.get("api_key", "")
        context_window = data.get("context_window", 131072)
        max_tokens = data.get("max_tokens", 4096)
        base_url = data.get("base_url", "https://api.deepseek.com")
        compat = data.get("compat") if isinstance(data.get("compat"), dict) else None
        cost = data.get("cost") if isinstance(data.get("cost"), dict) else None

        return cls(
            ref=ref,
            provider=provider,
            api_model=api_model,
            display_name=display_name,
            api_key=_resolve_secret(api_key_raw, accept_keyring_uri=True),
            base_url=base_url,
            api=data.get("api", "openai-completions"),
            supports_vision=supports_vision,
            input_modalities=input_modalities,
            max_tokens=max_tokens,
            context_window=context_window,
            temperature=data.get("temperature", 0.7),
            top_p=data.get("top_p", 1.0),
            thinking_enabled=bool(thinking),
            reasoning_effort=data.get("reasoning_effort"),
            compat=compat,
            cost=cost,
        )


@dataclass
class MemdbConfig:
    """Configuration for the slife-memdb service.

    Always enabled -- slife-memdb is a built-in plugin.
    """

    embedding_model: str = "text-embedding-3-small"
    embedding_dim: int = 1536

    @classmethod
    def from_dict(cls, data: Any) -> "MemdbConfig":
        """Parse memdb config section from JSON5 config."""
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
    Session tokens are stored per-agent in ``wechat_<agent_name>.json5``.
    """

    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "WechatConfig":
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
    max_iterations: int = 30
    context_floor: float = 0.2
    context_ceiling: float = 0.8
    tool_result_ceiling: float = 0.2  # max tool result = 20% of context window (HARD constraint, see DESIGN)
    # Per-tool-result char budget for PERMANENT memory (save side).  The live
    # context keeps oversized results whole for the current turn; the
    # Turns DB stores a head+tail digest so a single result can never starve
    # session restore.  Results ≤ budget are stored as-is.  Tool output is
    # reproducible — re-run the tool to retrieve the full version.
    memory_tool_result_chars: int = 8000
    agent_name: str = "slife"
    tool_timeout: float = 60.0  # seconds, 0 to disable — applies to ALL tools
    heartbeat_interval: int = 60  # seconds — autonomous idle heartbeat period
    memdb_config: MemdbConfig | None = None
    wechat_config: WechatConfig | None = None
    a2a_config: A2AConfig | None = None
    subagent_config: dict | None = None
    plugins_external: list[dict] = field(default_factory=list)
    cli_tools: dict = field(default_factory=dict)
    _path: Path | None = None

    def __post_init__(self):
        if self.memdb_config is None:
            self.memdb_config = MemdbConfig()
        if self.wechat_config is None:
            self.wechat_config = WechatConfig()
        if self.a2a_config is None:
            self.a2a_config = A2AConfig()
        if self.subagent_config is None:
            self.subagent_config = {"max_subagents": 5, "task_timeout": 120}

    # ── Serialization (for subagent inheritance) ────────────────────

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict for subagent inheritance.

        Subagents receive this over ``SLIFE_CONFIG`` instead of reading
        the json5 file — they inherit the main agent's in-memory config.
        """
        from dataclasses import asdict

        return {
            "models": [asdict(m) for m in self.models],
            "active_model_ref": self.active_model_ref,
            "tools": self.tools,
            "env": self.env,
            "max_iterations": self.max_iterations,
            "tool_timeout": self.tool_timeout,
            "heartbeat_interval": self.heartbeat_interval,
            "context_floor": self.context_floor,
            "context_ceiling": self.context_ceiling,
            "tool_result_ceiling": self.tool_result_ceiling,
            "memory_tool_result_chars": self.memory_tool_result_chars,
            "agent_name": self.agent_name,
            "memdb_config": asdict(self.memdb_config) if self.memdb_config else None,
            "wechat_config": asdict(self.wechat_config) if self.wechat_config else None,
            "a2a_config": asdict(self.a2a_config) if self.a2a_config else None,
            "subagent_config": self.subagent_config,
            "plugins_external": self.plugins_external,
            "cli_tools": self.cli_tools,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Reconstruct a Config from a dict (inverse of ``to_dict()``).

        Used by subagents to deserialize ``SLIFE_CONFIG``.
        """
        models = [ModelConfig(**m) for m in data.get("models", [])]

        mem_cfg = data.get("memdb_config")
        if isinstance(mem_cfg, dict):
            mem_cfg = MemdbConfig(**mem_cfg)

        wc_cfg = data.get("wechat_config")
        if isinstance(wc_cfg, dict):
            wc_cfg = WechatConfig(**wc_cfg)

        a2a_cfg = data.get("a2a_config")
        if isinstance(a2a_cfg, dict):
            a2a_cfg = A2AConfig(**a2a_cfg)

        return Config(
            models=models,
            active_model_ref=data.get("active_model_ref", ""),
            tools=data.get("tools", []),
            env=data.get("env"),
            max_iterations=data.get("max_iterations", 30),
            tool_timeout=data.get("tool_timeout", 60.0),
            heartbeat_interval=data.get("heartbeat_interval", 60),
            context_floor=data.get("context_floor", 0.2),
            context_ceiling=data.get("context_ceiling", 0.8),
            tool_result_ceiling=data.get("tool_result_ceiling", 0.2),
            agent_name=data.get("agent_name", "slife"),
            memdb_config=mem_cfg,
            wechat_config=wc_cfg,
            a2a_config=a2a_cfg,
            subagent_config=data.get("subagent_config"),
            plugins_external=data.get("plugins_external", []),
            cli_tools=data.get("cli_tools", {}),
        )

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

    # ── CLI tool persistence ─────────────────────────────────────────

    def _typed_section(self, raw: dict, key: str) -> dict:
        section = raw.setdefault(key, {})
        if not isinstance(section, dict):
            section = {}
            raw[key] = section
        return section

    def save_cli_tool(
        self,
        name: str,
        command: str = "",
        description: str = "",
        install: str = "",
        source: dict | None = None,
        enabled: bool | None = None,
    ) -> bool:
        """Persist a CLI tool entry. Returns True if persisted to file.

        Always updates the in-memory ``cli_tools`` snapshot.
        """
        entry: dict = {"command": command, "description": description, "install": install}
        if source:
            entry["source"] = source
        if enabled is not None:
            entry["enabled"] = enabled
        # Always update in-memory snapshot
        self.cli_tools[name] = entry

        if not self._path:
            logger.debug("config_no_path — cli_tool %s in memory only", name)
            return False
        raw = self._read_config("save_cli_tool", name)
        if raw is None:
            return False
        section = self._typed_section(raw, "cli_tools")
        section[name] = dict(entry)
        self._write_config(raw)
        logger.info("config_save_cli_tool name=%s", name)
        return True

    def remove_cli_tool(self, name: str) -> bool:
        """Remove a CLI tool entry. Returns True if removed from file.

        Always updates the in-memory ``cli_tools`` snapshot.
        """
        # Always update in-memory snapshot
        existed = self.cli_tools.pop(name, None) is not None

        if not self._path:
            logger.debug("config_no_path — cli_tool %s removed from memory only", name)
            return existed
        raw = self._read_config("remove_cli_tool", name)
        if raw is None:
            return existed
        section = self._typed_section(raw, "cli_tools")
        section.pop(name, None)
        self._write_config(raw)
        logger.info("config_remove_cli_tool name=%s existed=%s", name, existed)
        return existed

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

    # ── First-run helpers ──────────────────────────────────────────

    @staticmethod
    def _check_active_provider_key(raw: dict) -> tuple[bool, str]:
        """Check whether the active model's provider API key is resolvable.

        Parses the just-seeded config to find the active model, its
        provider, and the provider's ``api_key`` field.  Returns
        ``(True, "")`` when the key can be resolved through env or
        credstore, or ``(False, hint)`` when it cannot — *hint* is the
        env-var name the user should set (e.g. ``"DEEPSEEK_API_KEY"``).
        """
        from credstore import exists_credential

        active_ref: str = raw.get("active_model", "")
        if "/" not in active_ref:
            return False, "API_KEY"
        provider_id = active_ref.split("/", 1)[0]

        providers: dict = raw.get("models", {}).get("providers", {})
        provider_cfg: dict = providers.get(provider_id, {})
        api_key_raw: str = str(provider_cfg.get("api_key", ""))

        if not api_key_raw:
            return False, "API_KEY"

        # keyring: URI
        if api_key_raw.startswith("keyring:"):
            from credstore import parse_keyring_uri
            parsed = parse_keyring_uri(api_key_raw)
            # parse_keyring_uri returns (service, key) tuple
            key_name = parsed[1] if parsed else api_key_raw
            return (bool(exists_credential(key_name)), str(key_name))

        # ${VAR} reference
        if api_key_raw.startswith("${") and api_key_raw.endswith("}"):
            var_name = api_key_raw[2:-1]
            if os.environ.get(var_name) or exists_credential(var_name):
                return True, ""
            return False, var_name

        # Plaintext — already a key
        if api_key_raw:
            return True, ""

        return False, "API_KEY"

    @staticmethod
    def _seed_first_run_config(path: Path) -> None:
        """Copy the bundled template config on first run.

        If the config file does not exist, copies the template from the
        package directory and checks whether the active model's API key
        is resolvable.  If the key is missing, prints setup instructions
        and exits gracefully (SystemExit).
        """
        import shutil

        path.parent.mkdir(parents=True, exist_ok=True)
        pkg_template = Path(__file__).parent / "slife.template.json5"
        if not pkg_template.exists():
            raise FileNotFoundError(
                f"Config file not found: {path}\n"
                f"Run: cp slife.template.json5 ~/.slife/slife.json5"
            )

        shutil.copy(pkg_template, path)
        # The template ships 0644 and shutil.copy preserves that mode, but the
        # config is where plaintext API keys end up — tighten to owner-only on
        # POSIX so other local accounts can't read it.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass  # non-POSIX or filesystem without chmod — best effort
        logger.info("config_seeded from=%s to=%s", pkg_template, path)
        print(f"\n  First run — created: {path}")

        raw = json5.loads(path.read_text(encoding="utf-8"))
        key_ok, key_hint = Config._check_active_provider_key(raw)
        if key_ok:
            print("  API key found — starting up.\n")
        else:
            print("  Set your API key and you're ready:")
            print(f"    credstore set {key_hint}")
            print("    slife\n")
            raise SystemExit(0)

    @staticmethod
    def _inject_env_vars(env_section: dict) -> None:
        """Inject env vars from config into os.environ.

        Resolution order:
          1. Already set in shell environment → keep
          2. credstore → the canonical source for secrets
          3. ``${VAR}`` reference → resolve VAR through credstore
             or os.environ
          4. Plain config value → inject directly
        """
        for key, value in env_section.items():
            str_value = str(value)
            # 1. Already set in environment (user's shell) -- keep it
            if os.environ.get(key):
                logger.debug("env_from_shell key=%s", key)
                continue
            # 2. Try credstore — canonical source for secrets
            cred_value = _try_credstore_lookup(key)
            if cred_value:
                os.environ[key] = cred_value
                logger.info("env_from_credstore key=%s", key)
                continue
            # 3. Config value is a ${VAR} reference
            if str_value.startswith("${") and str_value.endswith("}"):
                var_name = str_value[2:-1]
                if var_name != key:
                    cred_value = _try_credstore_lookup(var_name)
                    if cred_value:
                        os.environ[key] = cred_value
                        logger.info("env_from_credstore key=%s via=%s", key, var_name)
                        continue
                env_val = os.environ.get(var_name)
                if env_val:
                    os.environ[key] = env_val
                    logger.info("env_from_shell key=%s via=%s", key, var_name)
                    continue
                logger.warning(
                    "env_unresolved key=%s var=%s — credential not in shell or "
                    "credstore; child processes (MCP/subagent) will not have it. "
                    "Run: credstore set %s",
                    key, var_name, var_name,
                )
                continue
            # 4. Plain config value — inject directly
            os.environ[key] = str_value
            logger.info("env_from_config key=%s", key)
        logger.debug("config_env_vars count=%d", len(env_section))

    # ── Main loader ─────────────────────────────────────────────────

    @classmethod
    def from_json5(
        cls, path: str | Path = "slife.json5",
        agent_name: str = "slife",
    ) -> "Config":
        """Load from JSON5 file with provider->model hierarchy.

        Args:
            path: Path to the JSON5 config file.
                  Defaults to ``~/.slife/slife.json5``.
            agent_name: Agent identity key (``--agent`` on the CLI).
                      Defaults to ``"slife"``.  Used for memory isolation
                      and as the MQTT agent identity when Mosquitto is available.
        """
        path = Path(path).expanduser()
        logger.debug("config_load path=%s", path)
        if not path.exists():
            cls._seed_first_run_config(path)

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
        max_iterations = agent.get("max_iterations", 30)
        tool_timeout = agent.get("tool_timeout", 60.0)
        heartbeat_interval = agent.get("heartbeat_interval", 60)
        context_floor = agent.get("context_floor", 0.2)
        context_ceiling = agent.get("context_ceiling", 0.8)
        tool_result_ceiling = agent.get("tool_result_ceiling", 0.2)
        memory_tool_result_chars = agent.get("memory_tool_result_chars", 8000)

        # Env -- inject into os.environ so child processes (MCP wrappers,
        # sub-agents) inherit credentials.  Resolution order:
        #   shell env  >  credstore  >  config value  >  config ${VAR}
        env_section = _parse_section(raw, "env", dict, {})
        cls._inject_env_vars(env_section)

        # Tools (optional -- auto-discovery handles defaults).  Lenient: a
        # ${OPTIONAL_KEY} that isn't set must not abort the whole app startup —
        # it's left as-is for a downstream resolver, like every other section.
        tools = _resolve_env_lenient(_parse_section(raw, "tools", list, []))

        # Memory -- built-in plugin, always enabled.  DB files live in
        # ~/.slife/<agent_name>.db — no configuration needed.
        memdb_config = MemdbConfig.from_dict(raw.get("memdb", {}))
        logger.debug(
            "memdb_config embed=%s",
            memdb_config.embedding_model,
        )

        # WeChat -- optional plugin, enabled via wechat.enabled
        wechat_config = WechatConfig.from_dict(raw.get("wechat", {}))
        if wechat_config.enabled:
            logger.debug(
                "wechat_config agent_name=%s",
                agent_name,
            )

        # A2A — always parse config; enabled at runtime after mosquitto probe.
        a2a_config = A2AConfig.from_dict(raw.get("a2a"), agent_name=agent_name)
        if a2a_config.enabled:
            logger.debug(
                "a2a_config id=%s broker=%s:%d",
                a2a_config.agent_name,
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

        # CLI tools — managed section, no config class
        cli_tools = _parse_section(raw, "cli_tools", dict, {})

        # External plugins — registered via config (e.g. [{name, module}]).
        # These merge into the built-in source-scan result at discovery time,
        # and route through the same generic plugin lifecycle.
        plugins_section = _parse_section(raw, "plugins", dict, {})
        plugins_external = plugins_section.get("external")
        if not isinstance(plugins_external, list):
            plugins_external = []

        # Active model — a stale ref (provider renamed/removed) must not
        # crash startup; fall back to the first model.  Switching models in
        # the TUI persists the corrected ref.
        active_ref = raw.get("active_model", all_models[0].ref)
        if not any(m.ref == active_ref for m in all_models):
            logger.warning(
                "config_active_model_stale ref=%s fallback=%s",
                active_ref, all_models[0].ref,
            )
            active_ref = all_models[0].ref

        config = Config(
            models=all_models,
            active_model_ref=active_ref,
            tools=tools,
            env=env_section,
            max_iterations=max_iterations,
            tool_timeout=tool_timeout,
            heartbeat_interval=heartbeat_interval,
            context_floor=context_floor,
            context_ceiling=context_ceiling,
            tool_result_ceiling=tool_result_ceiling,
            memory_tool_result_chars=memory_tool_result_chars,
            agent_name=agent_name,
            memdb_config=memdb_config,
            wechat_config=wechat_config,
            a2a_config=a2a_config,
            subagent_config=subagent_config,
            plugins_external=plugins_external,
            cli_tools=cli_tools,
        )
        config._path = path
        # Point the mcp-plugin (external MCP plugin) at a config file living
        # next to slife.json5 — the plugin resolves it via $MCP_PLUGIN_FILE.
        if path is not None:
            os.environ["MCP_PLUGIN_FILE"] = str(path.parent / "mcp-plugin.json5")
        return config
