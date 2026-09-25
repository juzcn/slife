"""local-embed config — load ``local_embed.yaml``, path resolution.

Path precedence (mirrors mcp-gateway / credstore):
  1. ``$LOCAL_EMBED_FILE`` — a host (slife) exports this =
     ``<dir of slife.yaml>/local_embed.yaml`` before it launches the
     plugin child, so the config sits next to the host's config
  2. slife project root (dev): CWD is the slife source root
     (``pyproject.toml`` ``project.name == "slife"``) — ``./local_embed.yaml``
     (credstore's ``is_slife_dev`` pattern)
  3. ``~/.local-embed/local_embed.yaml`` (standalone default, credstore-style)

Config shape::

    models:
      "bge-m3":
        backend: "gguf"
        gguf_path: "…"
        device: ""
        autoload: false
      "bge-m3-transformer":
        backend: "transformer"
        model: "BAAI/bge-m3"
    host: "127.0.0.1"    # standalone only
    port: 17347          # standalone only

Every configured model is a peer — there is no ``active_model`` (a standard
OpenAI embeddings backend has no such concept).  Each request names the
model it wants via ``POST /v1/embeddings``'s ``model`` field.  A stale
``active_model`` key in an existing config is ignored.

``autoload`` is PER MODEL (a field on a model entry, default ``false``):
model weights are large and memory-hungry, so a model is only materialised
on the first request that names it (lazy).  ``autoload: true`` on one model
eager-loads it in the background shortly after the server starts — the
memory cost is paid up front for that model in exchange for a warm first
embed, while every unflagged model stays lazy.  The single-model
convenience shape accepts a top-level ``autoload`` key the same way.

``env`` (optional, top level) is injected into this process's environment
by :func:`apply_env` before any backend loads — a ``transformer`` ``model``
given as a HF *repo name* (e.g. ``BAAI/bge-m3``) resolves against the local
hub cache via ``HF_HUB_CACHE`` / ``HF_HUB_OFFLINE`` without the host
exporting anything.  Values support ``${VAR}`` / ``${VAR:-default}``
expansion from ``os.environ`` (see :func:`expand_value`), so the shipped
config can carry portable placeholders instead of machine-specific paths::

    env:
      HF_HUB_CACHE: "${HF_HUB_CACHE:-~/.cache/huggingface/hub}"
      HF_HUB_OFFLINE: "${HF_HUB_OFFLINE:-0}"
    models:
      "bge-m3-transformer":
        backend: "transformer"
        model: "BAAI/bge-m3"

Single-model convenience (still supported) — ``backend`` / ``model`` /
``gguf_path`` / ``device`` at the top level, exactly one model::

    backend: "gguf"
    model: "bge-m3"
    gguf_path: "…"
    device: ""

Reads are read-only at runtime — local-embed has no config-mutating tools
(mirrors mcp-gateway's self-hosted config, minus the persistence).
"""

from __future__ import annotations

import logging
import os
import re
import tomllib
from pathlib import Path

from ruamel.yaml.error import YAMLError

from local_embed._yaml_doc import new_yaml, render_document

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
#: Default bind port.  Chosen to be an uncommon, fixed port (outside the
#: OS ephemeral ranges and the common 8000/8080/3000 dev-port cluster) so a
#: host that configures ``base_url`` against it doesn't collide with other
#: local services.
DEFAULT_PORT = 17347

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_value(value: str) -> str:
    """Expand ``${VAR}`` / ``${VAR:-default}`` references from os.environ.

    Lenient and env-only (no credstore — local-embed is a standalone
    package): a ``${VAR}`` with no default stays literal when VAR is
    unset, so a fresh install degrades gracefully instead of erroring.
    Mirrors the syntax of slife.yaml's ``${VAR:-default}`` fallback.
    """
    def _sub(m: re.Match) -> str:
        name, default = m.group(1), m.group(2)
        val = os.environ.get(name)
        if val is not None:
            return val
        if default is not None:
            return default
        return m.group(0)

    return _ENV_REF_RE.sub(_sub, value)


def default_config_path() -> Path:
    """Standalone default: ``~/.local-embed/local_embed.yaml``."""
    return Path.home() / ".local-embed" / "local_embed.yaml"


def resolve_config_path() -> Path:
    """Return the local_embed.yaml path for this process.

    Precedence (mirrors mcp-gateway's ``resolve_config_path``):
    ``$LOCAL_EMBED_FILE`` > slife project root (dev) > standalone default.
    """
    env = os.environ.get("LOCAL_EMBED_FILE")
    if env:
        return Path(env).expanduser()
    if is_slife_dev():
        return Path("local_embed.yaml")
    return default_config_path()


def is_slife_dev() -> bool:
    """Whether we're running from the slife source root (credstore-style).

    Returns True when the CWD contains a ``pyproject.toml`` with
    ``project.name == "slife"``.
    """
    try:
        data = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    except Exception:
        return False
    return data.get("project", {}).get("name") == "slife"


def load_config(path: "Path | None" = None) -> dict:
    """Load the local-embed config dict, ``{}`` when the file is absent.

    A file that exists but cannot be parsed raises (a broken config must
    not be silently replaced by defaults).
    """
    if path is None:
        path = resolve_config_path()
    try:
        raw = new_yaml().load(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("config_not_found path=%s", path)
        return {}
    except (YAMLError, ValueError, OSError) as e:
        logger.error("config_parse_error path=%s err=%s", path, e)
        raise ValueError(f"Cannot parse config {path}: {e}") from e
    # An empty file loads as None and a top-level list as a sequence; neither is
    # a usable config, and both are surfaced rather than silently becoming {}.
    if not isinstance(raw, dict):
        raise ValueError(f"Cannot parse config {path}: not a mapping")
    return raw


def apply_env() -> dict:
    """Inject local-embed's ``env:`` config section into os.environ.

    A transformer backend loads its model by HF *repo name* (e.g.
    ``BAAI/bge-m3``); huggingface_hub resolves that name against the local
    hub cache, which defaults to ``~/.cache/huggingface``.  ``env:`` in the
    config makes the server self-contained — it exports ``HF_HUB_CACHE`` /
    ``HF_HUB_OFFLINE`` (or anything else) into its *own* process before any
    backend loads, and no external ``HF_*`` export is needed from the host.

    Precedence mirrors slife.yaml's ``env:`` injection: an existing
    ``os.environ`` value wins, so a host can always override the config
    file.  Returns the effective env vars (for tests).
    """
    cfg = load_config()
    effective: dict = {}
    for key, value in (cfg.get("env") or {}).items():
        if os.environ.get(key):
            logger.info("env_from_shell key=%s", key)
            continue
        expanded = expand_value(str(value))
        os.environ[key] = expanded
        effective[key] = expanded
        logger.info("env_injected key=%s value=%r", key, expanded)
    return effective


def _to_bool(value, default: bool = False) -> bool:
    """Lenient boolean from yaml/env: ``true``/``1``/``yes``/``on`` → True."""
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def resolve_engine_settings() -> dict:
    """Merge config file + env overrides into engine settings.

    Precedence: env vars (plugin spawn) > config file > defaults.  Returns
    ``{"specs": [ModelSpec, ...], "host", "port"}`` — no active model; every
    configured model is a peer named by the request.  ``autoload`` is
    PER MODEL on each spec (default False = lazy loading — the model is
    materialised only on the first request that names it).

    A ``models`` map (multi-model) takes precedence; otherwise the
    single-model top-level keys build one spec.
    """
    from local_embed.engine import ModelSpec

    apply_env()  # config env: → own process env, before any model loads
    cfg = load_config()

    def _pick(key: str, default):
        env_val = os.environ.get(f"LOCAL_EMBED_{key.upper()}")
        if env_val not in (None, ""):
            return env_val
        if key in cfg and cfg[key] not in (None, ""):
            return cfg[key]
        return default

    specs: list = []
    models_cfg = cfg.get("models")
    if isinstance(models_cfg, dict) and models_cfg:
        for name, m in models_cfg.items():
            if not isinstance(m, dict):
                continue
            # env override may point at the single model keyed by its name
            # Path().expanduser() resolves a ``~`` default (e.g. ~/.local-embed/models/…)
            # and normalises separators on Windows.
            gguf_path = (str(Path(expand_value(m["gguf_path"])).expanduser())
                         if m.get("gguf_path") else None)
            specs.append(
                ModelSpec(
                    name,
                    backend=m.get("backend", "gguf"),
                    model=m.get("model") or name,
                    gguf_path=gguf_path,
                    device=m.get("device", ""),
                    max_tokens=int(m.get("max_tokens", 0) or 0),
                    autoload=_to_bool(m.get("autoload", False)),
                )
            )
    else:
        backend = _pick("backend", "gguf")
        model = _pick("model", "bge-m3")
        specs = [
            ModelSpec(
                model,
                backend=backend,
                model=model,
                gguf_path=(str(Path(expand_value(_pick("gguf_path", ""))).expanduser()).strip()
                   or None),
                device=_pick("device", ""),
                max_tokens=int(_pick("max_tokens", 0) or 0),
                # The single-model shape accepts ``autoload`` at the top
                # level (or the LOCAL_EMBED_AUTOLOAD env override).
                autoload=_to_bool(_pick("autoload", False)),
            )
        ]

    return {
        "specs": specs,
        "host": _pick("host", DEFAULT_HOST),
        "port": int(_pick("port", DEFAULT_PORT)),
    }


_KNOWN_KEY_ORDER = ("env", "models", "host", "port")

#: Written into a config this package creates from scratch.  An existing file
#: keeps whatever header it has — the writer preserves the document — so this
#: is only the blank-slate case (the first ``local-embed set`` on a machine
#: with no config yet).
_HEADER = """\
# local-embed — one process, many local embedding models; the request names
# the model (standard OpenAI semantics — no 'active' model).
# Config path: $LOCAL_EMBED_FILE > slife project root (dev) > ~/.local-embed/
"""


def _ordered(cfg: dict) -> dict:
    """*cfg* with known top-level keys first, in the seed's canonical order."""
    known = [(k, cfg[k]) for k in _KNOWN_KEY_ORDER if k in cfg]
    rest = [(k, v) for k, v in cfg.items() if k not in _KNOWN_KEY_ORDER]
    return dict(known + rest)


def render_yaml(cfg: dict) -> str:
    """Serialise *cfg* from scratch: canonical key order plus the header.

    The blank-slate renderer — :func:`write_config` uses it only when there is
    no existing document to edit.
    """
    return _HEADER + render_document("", _ordered(cfg))


def write_config(cfg: dict, path: "Path | None" = None) -> Path:
    """Atomically write a full config dict to disk.

    An existing file is **edited in place**: its comments, key order and quote
    style survive, because the write applies the difference to the loaded
    document rather than re-serialising the dict (see
    :mod:`local_embed._yaml_doc`).  A file that does not exist yet is rendered
    fresh with the canonical key order and the header comment.  Either way the
    replacement is a temp sibling, so a crashed write never truncates a good
    config.
    """
    if path is None:
        path = resolve_config_path()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        current = ""
    text = render_document(current, _ordered(cfg)) if current.strip() else render_yaml(cfg)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return path
