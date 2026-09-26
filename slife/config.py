"""Configuration for Slife agent -- YAML format.

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

import logging
import os
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Callable, TypeVar

from slife.env import parse_env_ref, resolve_env, resolve_secret_value
from slife.a2a.config import A2AConfig
import slife.timeouts as _timeouts  # module ref (not the instance) — reload-safe, patchable

logger = logging.getLogger(__name__)

# Package directory — carries the git-tracked seed configs (slife.yaml,
# local_embed.yaml, tools.yaml) force-included into the wheel, plus the
# bundled skills tree.
_PKG_DIR = Path(__file__).resolve().parent


def _resolve_secret(value: str, *, accept_keyring_uri: bool = False) -> str:
    """Resolve a secret value through the full resolution chain.

    1. ``keyring:`` URI  → credstore (only when *accept_keyring_uri* is True)
    2. ``${VAR}``        → os.environ → credstore
    3. ``${VAR:-default}`` → os.environ → credstore → literal default
    4. plaintext         → as-is

    Unresolvable references are left as-is (lenient) — never raise, so a
    missing secret degrades to its literal form instead of breaking startup.
    """
    # keyring: URI
    if accept_keyring_uri:
        from credstore import is_keyring_uri, resolve_uri
        if is_keyring_uri(value):
            return resolve_uri(value)

    # ${VAR} / ${VAR:-default} (pure reference) — the shared lenient chain
    # (env → credstore → literal default).  ``parse_env_ref`` is the one
    # parser, so this and the gateway's tools-config resolver can't drift.
    if parse_env_ref(value) is not None:
        return resolve_secret_value(value)
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

    ``python -m slife myconf.yaml`` must use ``myconf.yaml`` (the docstring
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


def parse_cli_headless(argv: list[str]) -> bool:
    """Whether the worker protocol was asked for instead of the TUI."""
    return "--headless" in argv[1:]


def parse_cli_help(argv: list[str]) -> bool:
    """Whether ``-h`` / ``--help`` was asked for."""
    return any(a in ("-h", "--help") for a in argv[1:])


#: The command line, as ``--help`` prints it.  Kept here beside the scanners
#: that read it, and pinned by ``tests/test_main.py`` (every flag named below
#: must be one a scanner accepts), so the help cannot describe a surface the
#: entry points do not have.
CLI_USAGE = """\
Usage: slife [options] [config-path]

  config-path        use a specific config file; its parent directory becomes
                     the data dir (default: ~/.slife/slife.yaml, or the CWD in
                     a source checkout)
  --agent <id>       agent identity — a separate turns database and A2A mesh
                     name (default: slife)
  --lang <en|zh>     interface language (default: the OS locale)
  --headless         no TUI: speak the worker protocol over stdin/stdout, the
                     way subagent processes do
  -h, --help         show this message and exit
"""


def _parse_section(raw: dict, key: str, expected_type, default):
    """Safely extract a typed section from parsed YAML, returning
    *default* if the value is missing or of the wrong type."""
    value = raw.get(key, default)
    return value if isinstance(value, expected_type) else default


def _as_name_set(value) -> frozenset[str]:
    """Normalize a possibly-missing plugin-name list into a frozenset.

    Accepts a list of strings; anything else (None, a dict, a single
    string) yields an empty set — the "required" contract defaults to
    false.  Non-string entries are dropped, never a crash.
    """
    if not isinstance(value, list):
        return frozenset()
    return frozenset(v for v in value if isinstance(v, str))


def _jsonable(value: Any) -> Any:
    """The JSON face of one config field value.

    Sets become sorted lists, tuples become lists (JSON has neither), and
    containers — including nested dataclasses — are walked with THIS function
    rather than handed to ``dataclasses.asdict``, which deep-copies a tuple
    field as a tuple and would leave ``to_dict()`` returning something
    ``json.dumps`` merely tolerates.  Everything else —
    str/int/float/bool/None — rides as-is.
    """
    if isinstance(value, frozenset):
        return sorted(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _jsonable(getattr(value, f.name))
            for f in fields(value)
            if not f.name.startswith("_")
        }
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    return value


def _declares_tuple(f) -> bool:
    """Whether a dataclass field's annotation is a tuple type."""
    ann = f.type
    if ann is tuple or (isinstance(ann, str) and ann.startswith("tuple")):
        return True
    return getattr(ann, "__origin__", None) is tuple


def _nested(cls, data: Any):
    """The inverse of :func:`_jsonable` for a nested config dataclass.

    ``cls(**data)`` plus one repair: a field the class declares as a tuple
    arrives as a list, because that is what JSON does to ``to_dict``'s output
    (``ModelConfig.input_modalities`` is the one today).  Not repairing it
    would hand the child a config that type-lies — a list where the parent
    holds a tuple — which is exactly the drift this whole path exists to
    prevent.  ``None`` for a missing/malformed section, as before.
    """
    if not isinstance(data, dict):
        return None
    kwargs = dict(data)
    for f in fields(cls):
        if _declares_tuple(f) and isinstance(kwargs.get(f.name), list):
            kwargs[f.name] = tuple(kwargs[f.name])
    return cls(**kwargs)


def _flagged_names(section: list, key: str, flag: object) -> frozenset[str]:
    """Extract entry names whose *key* is exactly *flag*.

    Every tools.yaml category section carries the same ``[{name, key}]``
    policy shape; this is the one implementation (never raises on a malformed
    entry).  The identity comparison matches the exact boolean — ``enabled:
    false`` and ``autoload: true`` — never e.g. a truthy ``1``.
    """
    out: set[str] = set()
    for entry in section:
        if isinstance(entry, dict) and entry.get(key) is flag:
            name = entry.get("name")
            if isinstance(name, str) and name:
                out.add(name)
    return frozenset(out)


def _disabled_names(section: list) -> frozenset[str]:
    """Names with ``enabled: false`` in a ``[{name, enabled}]`` section list."""
    return _flagged_names(section, "enabled", False)


def _autoload_names(section: list) -> frozenset[str]:
    """Names with ``autoload: true`` in a ``[{name, autoload}]`` section list.

    The sibling of ``enabled``: the same entries, the same shape, one flag per
    tool.  It seeds the tool ``loaded`` at session start and keeps it out of
    LRU eviction.  Only the sections that hold function tools act on it — a
    ``skill`` / ``cli`` entry has no load state, so its flag is accepted and
    inert.
    """
    return _flagged_names(section, "autoload", True)


def _autoload_servers(*sections: dict) -> frozenset[str]:
    """Server names with ``autoload: true`` in the mcp / rest-api sections.

    An external tool's name is not knowable before its server connects, so the
    flag sits on the SERVER entry: every tool row that server mirrors is born
    ``loaded``.  It is the same key ``mcp_gateway`` reads to register that
    server's proxies wholesale — one flag, one meaning: bring it up loaded.
    """
    out: set[str] = set()
    for section in sections:
        if not isinstance(section, dict):
            continue
        for name, entry in section.items():
            if name and isinstance(entry, dict) and entry.get("autoload") is True:
                out.add(name)
    return frozenset(out)


@dataclass
class ModelConfig:
    """Configuration for a single LLM model."""

    ref: str                       # "deepseek/deepseek-v4-flash"
    provider: str                  # "deepseek"
    api_model: str                 # "deepseek-v4-flash" (sent to API)
    display_name: str              # "DeepSeek V4 Flash"
    api_key: str
    #: Endpoint the API key is sent to.  Empty means "not configured" — the
    #: DeepSeek host is NEVER an implicit fallback, or a non-DeepSeek model
    #: that omits base_url would silently send its key to api.deepseek.com.
    #: A concrete base_url is supplied by the config (seed + user).  An empty
    #: value surfaces as a clear failure when the model is actually used.
    base_url: str = ""
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
        base_url = data.get("base_url", "")
        compat = data.get("compat") if isinstance(data.get("compat"), dict) else None

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
        )


@dataclass
class MemdbConfig:
    """Configuration for the slife-memdb service.

    Always enabled -- slife-memdb is a built-in plugin.  Embedding config
    is a first-class top-level ``embeddings`` section (shared with memfiles),
    not part of the memdb section.  An empty marker dataclass kept for the
    ``config.memdb_config`` presence check (memory is always on).
    """


@dataclass
class EmbeddingsConfig:
    """First-class embeddings config — top-level ``embeddings`` section.

    - ``providers``: each provider is **one OpenAI-compatible endpoint** —
      ``base_url`` + ``api_key`` plus a single ``model`` (the id POSTed on
      ``/v1/embeddings``).  Model may be omitted; then the endpoint's first
      listed model on /v1/models is used as a fallback (a standard OpenAI
      listing has no ``active`` marker — all models are peers).
    - ``active_model``: names the active provider.

    The vector dimension is never configured — it is auto-detected from the
    endpoint at runtime (known model families guessed, others probed).

    memdb and memfiles share this one section.
    """

    providers: dict[str, dict] = field(default_factory=dict)
    active_model: str = ""
    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "EmbeddingsConfig":
        """Parse the top-level ``embeddings`` section from YAML config."""
        if not isinstance(data, dict):
            return cls()
        providers = data.get("providers", {})
        if not isinstance(providers, dict):
            providers = {}
        # ``active_model`` names a provider only (no provider/model ref).
        # A missing/stale ref falls back to the first provider.
        active = data.get("active_model", "")
        if "/" in active or active not in providers:
            active = next(iter(providers), "")
        return cls(
            providers=providers,
            active_model=active,
            enabled=bool(data.get("enabled", True)),
        )

    def active_endpoint(self) -> dict:
        """The active provider as one OpenAI-compatible endpoint.

        Returns ``{"provider", "base_url", "api_key", "model"}`` — the single
        resolver for "which endpoint do we embed against", fed by whichever
        representation the caller holds (the parsed section here, the raw yaml
        dict through ``embedding_config._active_endpoint``).  A provider named
        by ``active_model`` is already normalized by :meth:`from_dict`; an
        entry whose value is not a mapping is skipped rather than crashed on,
        since yaml can hold anything.  All-empty means "no endpoint", which
        every caller reads as the keyword-search fallback.
        """
        empty = {"provider": "", "base_url": "", "api_key": "", "model": ""}
        if not self.providers:
            return empty
        pid = self.active_model
        if pid not in self.providers or not isinstance(self.providers.get(pid), dict):
            pid = next(
                (k for k, v in self.providers.items() if isinstance(v, dict)), "",
            )
        pcfg = self.providers.get(pid) if pid else None
        if not isinstance(pcfg, dict):
            return empty
        return {
            "provider": pid,
            "base_url": pcfg.get("base_url", ""),
            "api_key": pcfg.get("api_key", ""),
            "model": pcfg.get("model", ""),
        }


@dataclass
class WechatConfig:
    """Configuration for the slife-wechat plugin.

    Optional -- only loaded when ``wechat.enabled`` is true.
    Session tokens are stored per-agent in ``wechat_<agent_name>.yaml``.
    """

    enabled: bool = True

    @classmethod
    def from_dict(cls, data: Any) -> "WechatConfig":
        """Parse wechat config section from YAML config.

        Defaults to enabled when the wechat section is absent -- the plugin
        is lightweight and only activates when wechat_login is called.
        Set ``wechat: { enabled: false }`` to explicitly opt out.
        """
        if not isinstance(data, dict):
            return cls()
        return cls(enabled=data.get("enabled", True))


#: The fields JSON cannot rebuild on its own — the inverse of what
#: :func:`_jsonable` does.  One entry per field whose type is not a plain
#: JSON value; everything absent here is carried verbatim.  This table plus
#: ``dataclasses.fields()`` IS the inheritance schema (see ``Config.to_dict``).
_FIELD_DECODERS: dict[str, Callable[[Any], Any]] = {
    "models": lambda v: [_nested(ModelConfig, m) for m in (v or [])],
    "memdb_config": lambda v: _nested(MemdbConfig, v),
    "embeddings_config": EmbeddingsConfig.from_dict,
    "wechat_config": lambda v: _nested(WechatConfig, v),
    "a2a_config": lambda v: _nested(A2AConfig, v),
    # Name sets ride as sorted lists; ``_as_name_set`` is their one reader.
    "plugins_required": _as_name_set,
    "autoload_tools": _as_name_set,
    "autoload_servers": _as_name_set,
    "disabled_jobs": _as_name_set,
    "disabled_skills": _as_name_set,
    "disabled_plugin": _as_name_set,
    "disabled_builtins": _as_name_set,
}


@dataclass
class Config:
    """Top-level configuration for Slife."""

    models: list[ModelConfig]
    active_model_ref: str
    tools: list[dict]
    env: dict | None = None
    max_iterations: int = 30  # model calls per turn; 0 = no cap
    context_floor: float = 0.2
    context_ceiling: float = 0.8
    tool_result_ceiling: float = 0.2  # max tool result = 20% of context window (HARD constraint, see DESIGN)
    # Recall caps — the per-turn context rebuild's own configuration.  The
    # rebuild replaces the whole context from a recall selection, so these
    # bound *what the model is sent*, not an increment on top of it.
    #: Rebuild the context from a recall selection each turn.  ``True``
    #: (default) — recall selects the context before every turn.  ``False`` —
    #: the context grows append-only.  The internal trim's ceiling applies in
    #: both modes (it bounds the window, not the selection).  The two modes are
    #: compatible: both maintain the same persisted live-context list and the
    #: same save-append path, so the flag can be flipped between runs without a
    #: migration.
    rebuild_message: bool = True
    #: Cosine similarity a semantic hit must reach to enter the context.
    #: Keyword hits are exempt (nothing measured them).  Measured against
    #: the active embedding model — see ``memdb/recall.RecallPolicy``.
    recall_min_similarity: float = 0.45
    #: Maximum turns in the selection.
    recall_limit: int = 40
    # Per-tool-result char budget for PERMANENT memory (save side).  The live
    # context keeps oversized results whole for the current turn; the
    # Turns DB stores a head+tail digest so a single result can never starve
    # session restore.  Results ≤ budget are stored as-is.  Tool output is
    # reproducible — re-run the tool to retrieve the full version.
    memory_tool_result_chars: int = 8000
    agent_name: str = "slife"
    # The tool-call budget — the ONLY sanctioned "total" deadline in the
    # system, injected through the prompt meta-parameters (`_timeout` /
    # `tool_timeout`).  DEVELOPER-OWNED: the user config key is ignored and
    # ``None`` resolves to the registry's work.tool_budget at construction
    # (call-time lookup, see __post_init__ — no def-time value capture).
    tool_timeout: float | None = None
    heartbeat_interval: int | None = None  # seconds — autonomous idle heartbeat
    # period; None resolves to the registry cadence pacing.heartbeat (the user
    # key ``agent.heartbeat_interval`` is the only override), and 0 disables
    # the heartbeat — see heartbeat_period()
    # Mid-turn input preemption: when True (default) a new inbound message may
    # cut into the running turn at the next safe iteration boundary; when
    # False it waits in the queue until the turn ends (the original behavior).
    cutin_enabled: bool = True
    memdb_config: MemdbConfig | None = None
    embeddings_config: EmbeddingsConfig | None = None
    wechat_config: WechatConfig | None = None
    a2a_config: A2AConfig | None = None
    subagent_config: dict | None = None
    # Plugins declared REQUIRED (core) via ``plugins.required`` in
    # slife.yaml — a required plugin that fails to become ready aborts
    # startup instead of limping on.  Defaults to empty = every plugin is
    # optional (failure warns and the session continues).
    plugins_required: frozenset[str] = field(default_factory=frozenset)
    cli_tools: dict = field(default_factory=dict)
    #: The loaded-function-tool threshold — tools.yaml's ``tool_load``
    #: section, the one remaining tool-system knob (default 100).
    tool_load_threshold: int = 100
    #: Tools marked ``autoload: true`` in a per-tool section entry (builtin /
    #: job) — seeded ``loaded`` at session start and never evicted.
    autoload_tools: frozenset[str] = field(default_factory=frozenset)
    #: Servers marked ``autoload: true`` in their mcp / rest-api entry — every
    #: tool row that server mirrors is seeded ``loaded`` and never evicted.
    autoload_servers: frozenset[str] = field(default_factory=frozenset)
    #: Per-entry ``enabled: false`` names from the tools.yaml ``job`` /
    #: ``skill`` sections (hidden from the catalog seed).
    disabled_jobs: frozenset[str] = field(default_factory=frozenset)
    disabled_skills: frozenset[str] = field(default_factory=frozenset)
    #: Per-entry ``enabled: false`` names from the ``plugin`` section — the
    #: built-in plugins' own tools.  A switched-off one is never REGISTERED
    #: (the registration path skips it, exactly as the builtin factory skips a
    #: disabled builtin), but it still gets a catalog row marked ``disabled``:
    #: the plugin keeps declaring its tool set, so the row is there to be found
    #: and to be recommended for switching on.
    disabled_plugin: frozenset[str] = field(default_factory=frozenset)
    #: Per-entry ``enabled: false`` names from the ``builtin`` section.  A
    #: disabled builtin is never REGISTERED (the factory skips it), but it still
    #: gets a catalog row, marked ``disabled`` — yaml declaring a tool the db
    #: had never heard of is a disagreement between the two, and ``tool_search``
    #: could not even report it as off.
    disabled_builtins: frozenset[str] = field(default_factory=frozenset)
    _path: Path | None = None
    _tools_path: Path | None = None  # tools.yaml sibling — set by from_yaml

    def __post_init__(self):
        # Resolve the tool budget at construction — call-time registry lookup,
        # so a patched registry is honored by bare Config() too.
        if self.tool_timeout is None:
            self.tool_timeout = _timeouts.timeouts.work.tool_budget
        if self.heartbeat_interval is None:
            # The cadence default is developer-owned (registry), the key is the
            # user's override — resolved here, never captured as a def-time
            # literal, so a patched registry is honored by a bare Config().
            self.heartbeat_interval = int(_timeouts.timeouts.pacing.heartbeat)
        if self.memdb_config is None:
            self.memdb_config = MemdbConfig()
        if self.embeddings_config is None:
            self.embeddings_config = EmbeddingsConfig()
        if self.wechat_config is None:
            self.wechat_config = WechatConfig()
        if self.a2a_config is None:
            self.a2a_config = A2AConfig()
        if self.subagent_config is None:
            self.subagent_config = {"max_subagents": 5}

    # ── Serialization (for subagent inheritance) ────────────────────
    #
    # The two directions are DERIVED from the dataclass, not written out by
    # hand.  A hand-written pair is a second definition of this schema, and a
    # second definition drifts: the previous pair silently dropped
    # ``embeddings_config`` and ``memory_tool_result_chars`` (so a worker's
    # health reported ``embeddings=disabled`` while its parent reported
    # ``enabled``) and never carried ``tool_load_threshold`` / ``autoload_*``
    # / ``disabled_*`` at all — nine fields, found only because a subagent's
    # tool search turned out to be keyword-only.  Deriving both directions
    # from ``dataclasses.fields()`` makes that class of loss impossible: a
    # new field is inherited unless it is explicitly private.
    #
    # ``slife/agent/roles.py``'s parity test asserts the round trip is a fixed
    # point, which is what keeps "the worker has the same config" true.

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict for subagent inheritance.

        Subagents receive this over ``SLIFE_CONFIG_FILE`` instead of reading
        the yaml file — they inherit the main agent's in-memory config.  Every
        public field is carried; ``_``-prefixed fields are process-local (the
        yaml/tools.yaml paths) and deliberately stay behind.
        """
        return {
            f.name: _jsonable(getattr(self, f.name))
            for f in fields(self)
            if not f.name.startswith("_")
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Config":
        """Reconstruct a Config from a dict — the inverse of ``to_dict()``.

        Used by subagents to deserialize the inherited config.  Unknown keys
        are ignored and absent ones fall back to their dataclass default, so a
        partial dict still constructs (a worker's config may predate a field
        the parent grew).
        """
        kwargs = {}
        for f in fields(cls):
            if f.name.startswith("_") or f.name not in data:
                continue
            decoder = _FIELD_DECODERS.get(f.name)
            kwargs[f.name] = (
                decoder(data[f.name]) if decoder is not None else data[f.name]
            )
        # The three fields with no dataclass default keep the historical
        # absent-value fallback, so `from_dict({})` still constructs.
        kwargs.setdefault("models", [])
        kwargs.setdefault("tools", [])
        kwargs.setdefault("active_model_ref", "")
        return cls(**kwargs)

    # ── Config file I/O helpers ─────────────────────────────────────

    def _read_config(self, action: str, server: str) -> dict | None:
        """Read and parse the YAML config file. Returns None if no path."""
        if not self._path:
            logger.warning("config_no_path action=%s server=%s", action, server)
            return None
        from slife.tools._config_io import read_config
        return read_config(self._path)

    def _write_config(self, raw: dict) -> None:
        """Write the YAML config back to disk."""
        assert self._path is not None
        from slife.tools._config_io import write_config
        write_config(self._path, raw)

    def _tools_config_path(self) -> Path:
        """The tools.yaml path this config owns.

        Set by :meth:`from_yaml` to the data-dir sibling of slife.yaml;
        falls back to the canonical data-dir default when unknown.
        """
        if self._tools_path is not None:
            return self._tools_path
        from slife.paths import get_tools_config_path
        return get_tools_config_path()

    def _read_tools_config(self, action: str, name: str) -> dict | None:
        """Read and parse tools.yaml. Returns None if no slife path set."""
        if not self._path:
            logger.warning("config_no_path action=%s name=%s", action, name)
            return None
        from slife.tools._config_io import read_config
        return read_config(self._tools_config_path())

    def _write_tools_config(self, raw: dict) -> None:
        """Write tools.yaml back to disk (own atomic temp+replace)."""
        from slife.tools._config_io import write_config
        write_config(self._tools_config_path(), raw)

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
        raw = self._read_tools_config("save_cli_tool", name)
        if raw is None:
            return False
        section = self._typed_section(raw, "cli")
        section[name] = dict(entry)
        self._write_tools_config(raw)
        logger.info("config_save_cli_tool name=%s", name)
        return True

    def save_skill_enabled(self, name: str, enabled: bool) -> bool:
        """Persist a skill's ``enabled`` into the ``skill`` section.

        The section is the authority for the disable — one section per
        category, the same per-entry ``{name, enabled}`` shape as ``builtin`` /
        ``plugin`` / ``job`` / ``cli`` (``_disabled_names`` reads it into
        ``Config.disabled_skills``).  Returns True if persisted, and the
        in-memory ``disabled_skills`` set is updated with the write so the
        switch takes effect in this process too.
        """
        if not self._path:
            logger.debug("config_no_path — skill %s in memory only", name)
            return False
        raw = self._read_tools_config("save_skill_enabled", name)
        if raw is None:
            return False
        section = raw.setdefault("skill", [])
        if not isinstance(section, list):
            logger.warning("config_skill_section_not_a_list — replacing")
            section = []
            raw["skill"] = section
        for entry in section:
            if isinstance(entry, dict) and entry.get("name") == name:
                entry["enabled"] = enabled
                break
        else:
            section.append({"name": name, "enabled": enabled})
        self._write_tools_config(raw)
        # The in-memory mirror moves with the file, exactly as ``save_cli_tool``
        # moves its own snapshot.  Every reader of this switch — ``skill_list``,
        # ``skill_use`` and the catalog mirror, through ``_disabled_skill_names``
        # — resolves the disable from ``disabled_skills``, so a write that left
        # it stale made the disable a per-process no-op: ``skill_set_enabled``
        # answered "[OK] disabled" while the skill stayed listed and usable
        # until the next restart.
        self.disabled_skills = _disabled_names(section)
        logger.info("config_save_skill_enabled name=%s enabled=%s", name, enabled)
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
        raw = self._read_tools_config("remove_cli_tool", name)
        if raw is None:
            return existed
        section = self._typed_section(raw, "cli")
        section.pop(name, None)
        self._write_tools_config(raw)
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
        """Extract subagent config with defaults from parsed YAML.

        The task bound is owned by the timeout registry (work.task_budget) —
        any ``task_timeout`` in YAML is ignored.  Only ``max_subagents``
        remains user-configurable.
        """
        sub_raw = raw.get("subagent")
        if isinstance(sub_raw, dict):
            return {"max_subagents": sub_raw.get("max_subagents", 5)}
        return {"max_subagents": 5}

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

                # Keyed on the model ID AS THE LOADER READS IT.  The provider
                # is explicit here (set just above), and ``from_dict`` then
                # keeps the id whole — ``local_id = api_model`` — so the ref is
                # "<provider>/<id>" and two entries collide exactly when their
                # ids do.
                #
                # Keying on the segment after "/" instead was a different
                # derivation from the loader's, and it refused configs the
                # loader loads fine: "meta-llama/llama-3-70b" and
                # "nousresearch/llama-3-70b" from one gateway are two distinct
                # models (distinct refs) that a last-segment test called
                # duplicates.  A genuine repeat — the same id twice — is still
                # caught, and only that.
                api_model = m.get("model")
                if not api_model:
                    # The refusal the flat-list path already raises, instead of
                    # a bare KeyError out of startup.
                    raise ValueError("Model entry missing 'model' field")
                if api_model in seen_ids:
                    raise ValueError(
                        f"Duplicate model '{api_model}' in provider "
                        f"'{provider_id}'. Model names must be unique "
                        f"within a provider."
                    )
                seen_ids.add(api_model)
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
        ref = parse_env_ref(api_key_raw)
        if ref is not None:
            var_name = ref[0]
            if os.environ.get(var_name) or exists_credential(var_name):
                return True, ""
            return False, var_name

        # Plaintext — already a key
        if api_key_raw:
            return True, ""

        return False, "API_KEY"

    @staticmethod
    def _seed_first_run_config(path: Path) -> None:
        """Seed the git-tracked default configs from the package.

        Copies any *missing* config among ``slife.yaml`` /
        ``tools.yaml`` from the package directory into ``path.parent``
        (the slife data dir) — the out-of-the-box defaults for a fresh
        install, and a supplement for existing installs that lack a newly
        added config.  ``local_embed.yaml`` seeds to ``~/.local-embed/``
        (local-embed is a separate standalone app).  Existing files are
        never overwritten.

        A freshly seeded ``slife.yaml`` is followed by an active-model
        API-key check: when the key is missing, prints setup
        instructions and exits gracefully (SystemExit).
        """
        import shutil

        path.parent.mkdir(parents=True, exist_ok=True)
        # Package directory — the git-tracked seed configs are force-included
        # into the wheel at build time (see pyproject.toml).
        pkg_dir = _PKG_DIR

        fresh = not path.exists()
        # Data-dir configs — slife.yaml, tools.yaml and sharefile.yaml
        # (the last two belong to built-in slife plugins) all live in the slife
        # data dir (path.parent), resolved via slife.paths.get_data_dir().
        # Seed each *missing* one from the bundled default; never overwrite.
        for name in ("slife.yaml", "tools.yaml", "sharefile.yaml"):
            target = path.parent / name
            if target.exists():
                continue
            pkg = pkg_dir / name
            if not pkg.exists():
                # slife.yaml must be present to configure anything; wheels
                # predating the git-tracked configs may lack tools.yaml.
                if name == "slife.yaml":
                    raise FileNotFoundError(
                        f"Config file not found: {path}\n"
                        f"Run: cp slife.yaml ~/.slife/slife.yaml"
                    )
                continue
            shutil.copy(pkg, target)
            # The seed ships 0644 and shutil.copy preserves that mode, but the
            # config is where plaintext API keys end up — tighten to owner-only
            # on POSIX so other local accounts can't read it.
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass  # non-POSIX or filesystem without chmod — best effort
            logger.info("config_seeded from=%s to=%s", pkg, target)
            print(f"\n  First run — created: {target}")

        # local-embed is a separate standalone app — it hosts its own config in
        # its own data dir (~/.local-embed, matching its standalone resolver).
        # Seed the same way: missing → copy, never overwrite.
        name = "local_embed.yaml"
        target = Path.home() / ".local-embed" / name
        if not target.exists():
            pkg = pkg_dir / name
            if pkg.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(pkg, target)
                try:
                    os.chmod(target, 0o600)
                except OSError:
                    pass  # non-POSIX or filesystem without chmod — best effort
                logger.info("config_seeded from=%s to=%s", pkg, target)
                print(f"\n  First run — created: {target}")

        if not fresh:
            return  # existing user config — the fresh-install key check is moot

        from slife.tools._config_io import read_config

        raw = read_config(path)
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
             or os.environ, else the ``:-default`` literal (the same
             lenient chain ``slife.env.resolve_secret_value`` documents)
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
            ref = parse_env_ref(str_value)
            if ref is not None:
                var_name, default = ref[0], ref[1]
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
                if default is not None:
                    # ${VAR:-default} — the literal fallback wins when neither
                    # shell nor credstore has the var (env.py's documented
                    # chain).  Previously the key was dropped entirely.
                    os.environ[key] = default
                    logger.info("env_from_default key=%s via=%s", key, var_name)
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
    def from_yaml(
        cls, path: str | Path = "slife.yaml",
        agent_name: str = "slife",
    ) -> "Config":
        """Load from YAML file with provider->model hierarchy.

        Args:
            path: Path to the YAML config file.
                  Defaults to ``~/.slife/slife.yaml``.
            agent_name: Agent identity key (``--agent`` on the CLI).
                      Defaults to ``"slife"``.  Used for memory isolation
                      and as the MQTT agent identity when Mosquitto is available.
        """
        path = Path(path).expanduser()
        logger.debug("config_load path=%s", path)
        # Seeds missing configs from the package defaults (slife.yaml +
        # tools.yaml into the data dir, local_embed.yaml into
        # ~/.local-embed); no-op for files the user already has.
        cls._seed_first_run_config(path)

        from slife.tools._config_io import read_config

        raw = read_config(path)

        # Tool configs live in tools.yaml (one section per tool category:
        # builtin / mcp / rest-api / job / cli / skill).  The seed above
        # guarantees the file sits next to slife.yaml in the data dir.
        tools_path = path.parent / "tools.yaml"
        tools_raw = read_config(tools_path)

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
        # tool_timeout is DEVELOPER-OWNED (registry work.tool_budget) — the
        # user config key is ignored (``None`` resolves in __post_init__).
        # This is the ONE sanctioned "total" deadline in the system (the
        # tool-call budget), tuned by developers.
        tool_timeout = None
        # The heartbeat cadence is user-overridable, but its DEFAULT is the
        # registry's (pacing.heartbeat) — an absent key resolves in
        # __post_init__, so the value has one seat.
        heartbeat_interval = agent.get("heartbeat_interval")
        cutin_enabled = agent.get("cutin_enabled", True)
        context_floor = agent.get("context_floor", 0.2)
        context_ceiling = agent.get("context_ceiling", 0.8)
        tool_result_ceiling = agent.get("tool_result_ceiling", 0.2)
        rebuild_message = agent.get("rebuild_message", True)
        recall_min_similarity = agent.get("recall_min_similarity", 0.45)
        recall_limit = agent.get("recall_limit", 40)
        memory_tool_result_chars = agent.get("memory_tool_result_chars", 8000)

        # Env -- inject into os.environ so child processes (MCP wrappers,
        # sub-agents) inherit credentials.  Resolution order:
        #   shell env  >  credstore  >  config value  >  config ${VAR}
        env_section = _parse_section(raw, "env", dict, {})
        cls._inject_env_vars(env_section)

        # Tools — the tools.yaml ``builtin`` section (optional; auto-discovery
        # handles defaults).  Lenient: a ${OPTIONAL_KEY} that isn't set must
        # not abort the whole app startup — it's left as-is for a downstream
        # resolver, like every other section.
        tools = _resolve_env_lenient(_parse_section(tools_raw, "builtin", list, []))

        # Memory -- built-in plugin, always enabled.  DB files live in
        # ~/.slife/<agent_name>.db — no configuration needed.
        memdb_config = MemdbConfig()

        # Embeddings -- first-class top-level section shared by memdb +
        # memfiles.  Each entry is an OpenAI-compatible endpoint; the model
        # is determined by the endpoint's /v1/models active model.
        embeddings_config = EmbeddingsConfig.from_dict(raw.get("embeddings", {}))
        logger.debug(
            "embeddings_config active=%s providers=%d enabled=%s",
            embeddings_config.active_model,
            len(embeddings_config.providers),
            embeddings_config.enabled,
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
            "subagent_config max_subagents=%d",
            subagent_config["max_subagents"],
        )

        # CLI tools — the tools.yaml ``cli`` section (managed, no config class)
        cli_tools = _parse_section(tools_raw, "cli", dict, {})
        # ``job`` / ``skill`` sections — a per-entry ``enabled`` list so every
        # yaml category section carries the same enable/disable policy
        # (functional consumption lives in the catalog seed/catalog-service;
        # here they are parsed + overlay names so a disabled entry is hidden).
        job_overrides = _parse_section(tools_raw, "job", list, [])
        skill_overrides = _parse_section(tools_raw, "skill", list, [])
        builtin_overrides = _parse_section(tools_raw, "builtin", list, [])
        # ``plugin`` — the built-in plugins' OWN tools, one section for the
        # ``plugin`` category (one section per category, like every other).
        # Their tool names are bare (turn_search, wechat_login, mcp_set), so a
        # flat entry list is all the config needs.
        plugin_overrides = _parse_section(tools_raw, "plugin", list, [])
        disabled_jobs = _disabled_names(job_overrides)
        disabled_skills = _disabled_names(skill_overrides)
        disabled_plugin = _disabled_names(plugin_overrides)
        disabled_builtins = _disabled_names(builtin_overrides)
        # ``autoload`` is a per-ENTRY flag, the sibling of ``enabled``: on a
        # builtin/job entry it names a tool; on an mcp/rest-api entry it names
        # a server (an external tool's name is unknown until it connects).
        # A skill/cli entry accepts it and nothing more — those rows carry no
        # load state to seed.
        autoload_tools = (
            _autoload_names(builtin_overrides)
            | _autoload_names(job_overrides)
            | _autoload_names(plugin_overrides)
        )
        mcp_section = _parse_section(tools_raw, "mcp", dict, {})
        mcp_servers = mcp_section.get("servers", {})
        autoload_servers = _autoload_servers(
            mcp_servers if isinstance(mcp_servers, dict) else {},
            _parse_section(tools_raw, "rest-api", dict, {}),
        )
        # The one tool-system knob left in the policy section:
        #   tool_load: {threshold: 100}
        tool_load_section = _parse_section(tools_raw, "tool_load", dict, {})
        try:
            tool_load_threshold = int(
                tool_load_section.get("threshold", 100) or 100
            )
            if tool_load_threshold <= 0:
                tool_load_threshold = 100
        except (TypeError, ValueError):
            tool_load_threshold = 100

        plugins_section = _parse_section(raw, "plugins", dict, {})
        # Required (core) plugins — named in ``plugins.required``.  A
        # required plugin that fails to become ready aborts startup; the
        # contract marker defaults to false (absent = all optional).
        plugins_required = _as_name_set(plugins_section.get("required", []))

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
            cutin_enabled=cutin_enabled,
            context_floor=context_floor,
            context_ceiling=context_ceiling,
            tool_result_ceiling=tool_result_ceiling,
            rebuild_message=rebuild_message,
            recall_min_similarity=recall_min_similarity,
            recall_limit=recall_limit,
            memory_tool_result_chars=memory_tool_result_chars,
            agent_name=agent_name,
            memdb_config=memdb_config,
            embeddings_config=embeddings_config,
            wechat_config=wechat_config,
            a2a_config=a2a_config,
            subagent_config=subagent_config,
            plugins_required=plugins_required,
            cli_tools=cli_tools,
            tool_load_threshold=tool_load_threshold,
            autoload_tools=autoload_tools,
            autoload_servers=autoload_servers,
            disabled_jobs=disabled_jobs,
            disabled_skills=disabled_skills,
            disabled_plugin=disabled_plugin,
            disabled_builtins=disabled_builtins,
        )
        config._path = path
        config._tools_path = tools_path
        # mcp-gateway is a built-in slife plugin — it resolves tools.yaml in
        # the same data dir as slife.yaml (via slife.paths.get_data_dir).
        # local-embed is a separate standalone app that resolves its own
        # ~/.local-embed/local_embed.yaml.  We do NOT set $TOOLS_FILE /
        # $LOCAL_EMBED_FILE — both plugins find the files the installer and
        # _seed_first_run_config (above) write.
        return config
