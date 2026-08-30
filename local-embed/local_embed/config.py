"""local-embed config — load ``local_embed.json5``, path resolution.

Path precedence (mirrors mcp-plugin / credstore):
  1. ``$LOCAL_EMBED_FILE`` — a host (slife) exports this =
     ``<dir of slife.json5>/local_embed.json5`` before it launches the
     plugin child, so the config sits next to the host's config
  2. slife project root (dev): CWD is the slife source root
     (``pyproject.toml`` ``project.name == "slife"``) — ``./local_embed.json5``
     (credstore's ``is_slife_dev`` pattern)
  3. ``~/.local-embed/local_embed.json5`` (standalone default, credstore-style)

Config shape::

    {
      active_model: "bge-m3",
      models: {
        "bge-m3": { backend: "gguf", gguf_path: "…", device: "" },
        "bge-m3-transformer": { backend: "transformer", model: "BAAI/bge-m3" },
      },
      host: "127.0.0.1",    // standalone only
      port: 8000,           // standalone only
    }

``env`` (optional, top level) is injected into this process's environment
by :func:`apply_env` before any backend loads — a ``transformer`` ``model``
given as a HF *repo name* (e.g. ``BAAI/bge-m3``) resolves against the local
hub cache via ``HF_HUB_CACHE`` / ``HF_HUB_OFFLINE`` without the host
exporting anything::

    {
      active_model: "bge-m3-transformer",
      env: { HF_HUB_CACHE: "C:\\…\\HuggingFace\\hub", HF_HUB_OFFLINE: "1" },
      models: { "bge-m3-transformer": { backend: "transformer", model: "BAAI/bge-m3" } },
    }

Single-model convenience (still supported) — ``backend`` / ``model`` /
``gguf_path`` / ``device`` at the top level, exactly one model::

    { backend: "gguf", model: "bge-m3", gguf_path: "…", device: "" }

Reads are read-only at runtime — local-embed has no config-mutating tools
(mirrors mcp-plugin's self-hosted config, minus the persistence).
"""

from __future__ import annotations

import json
import json5
import logging
import os
import re
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def default_config_path() -> Path:
    """Standalone default: ``~/.local-embed/local_embed.json5``."""
    return Path.home() / ".local-embed" / "local_embed.json5"


def resolve_config_path() -> Path:
    """Return the local_embed.json5 path for this process.

    Precedence (mirrors mcp-plugin's ``resolve_config_path``):
    ``$LOCAL_EMBED_FILE`` > slife project root (dev) > standalone default.
    """
    env = os.environ.get("LOCAL_EMBED_FILE")
    if env:
        return Path(env).expanduser()
    if is_slife_dev():
        return Path("local_embed.json5")
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
        return json5.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info("config_not_found path=%s", path)
        return {}
    except (ValueError, OSError) as e:
        logger.error("config_parse_error path=%s err=%s", path, e)
        raise ValueError(f"Cannot parse config {path}: {e}") from e


def apply_env() -> dict:
    """Inject local-embed's ``env:`` config section into os.environ.

    A transformer backend loads its model by HF *repo name* (e.g.
    ``BAAI/bge-m3``); huggingface_hub resolves that name against the local
    hub cache, which defaults to ``~/.cache/huggingface``.  ``env:`` in the
    config makes the server self-contained — it exports ``HF_HUB_CACHE`` /
    ``HF_HUB_OFFLINE`` (or anything else) into its *own* process before any
    backend loads, and no external ``HF_*`` export is needed from the host.

    Precedence mirrors slife.json5's ``env:`` injection: an existing
    ``os.environ`` value wins, so a host can always override the config
    file.  Returns the effective env vars (for tests).
    """
    cfg = load_config()
    effective: dict = {}
    for key, value in (cfg.get("env") or {}).items():
        if os.environ.get(key):
            logger.info("env_from_shell key=%s", key)
            continue
        os.environ[key] = str(value)
        effective[key] = str(value)
        logger.info("env_injected key=%s", key)
    return effective


def resolve_engine_settings(overrides: "dict | None" = None) -> dict:
    """Merge config file + env overrides into engine settings.

    Precedence: env vars (plugin spawn) > config file > defaults.  Returns
    ``{"specs": [ModelSpec, ...], "active": str, "host", "port"}``.

    A ``models`` map (multi-model) takes precedence; otherwise the
    single-model top-level keys build one spec.
    """
    from local_embed.engine import ModelSpec

    apply_env()  # config env: → own process env, before any model loads
    cfg = load_config()
    overrides = overrides or {}

    def _pick(key: str, default):
        env_val = os.environ.get(f"LOCAL_EMBED_{key.upper()}")
        if env_val not in (None, ""):
            return env_val
        if key in overrides and overrides[key] not in (None, ""):
            return overrides[key]
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
            specs.append(
                ModelSpec(
                    name,
                    backend=m.get("backend", "gguf"),
                    model=m.get("model") or name,
                    gguf_path=m.get("gguf_path") or None,
                    device=m.get("device", ""),
                    max_tokens=int(m.get("max_tokens", 0) or 0),
                )
            )
        # Precedence: env override > config file > default (mirrors _pick).
        active = _pick("active_model", specs[0].name)
        if active not in {s.name for s in specs}:
            active = specs[0].name
    else:
        backend = _pick("backend", "gguf")
        model = _pick("model", "bge-m3")
        specs = [
            ModelSpec(
                model,
                backend=backend,
                model=model,
                gguf_path=_pick("gguf_path", "") or None,
                device=_pick("device", ""),
            )
        ]
        active = model

    return {
        "specs": specs,
        "active": active,
        "host": cfg.get("host", overrides.get("host", DEFAULT_HOST)),
        "port": int(cfg.get("port", overrides.get("port", DEFAULT_PORT))),
    }


_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")
_KNOWN_KEY_ORDER = ("active_model", "env", "models", "host", "port")


def _js_key(k: str) -> str:
    """Quote a key only when it is not a plain identifier."""
    return k if _IDENTIFIER.fullmatch(k) else json.dumps(k)


def _js_value(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v)
    return str(v)


def _ordered_items(cfg: dict) -> "list[tuple[str, object]]":
    known = [(k, cfg[k]) for k in _KNOWN_KEY_ORDER if k in cfg]
    rest = [(k, v) for k, v in cfg.items() if k not in _KNOWN_KEY_ORDER]
    return known + rest


def _render_members(pairs: "list[tuple[str, object]]", indent: int) -> str:
    pad = " " * indent
    out: "list[str]" = []
    for i, (k, v) in enumerate(pairs):
        last = i == len(pairs) - 1
        sep = "" if last else ","
        if isinstance(v, dict):
            out.append(f"{pad}{_js_key(k)}: {{")
            out.append(_render_members(list(v.items()), indent + 2))
            out.append(f"{pad}}}{sep}")
        elif isinstance(v, list):
            inner = ", ".join(_js_value(x) for x in v)
            out.append(f"{pad}{_js_key(k)}: [{inner}]{sep}")
        else:
            out.append(f"{pad}{_js_key(k)}: {_js_value(v)}{sep}")
    return "\n".join(out)


def render_json5(cfg: dict) -> str:
    """Serialise a config dict in the repo's hand-written JSON5 style.

    2-space indent, unquoted keys when they are identifiers, double-quoted
    strings, multiline objects, the file's header comment.  Known top-level
    keys keep the canonical order from ``local_embed.json5`` (``active_model``,
    ``env``, ``models``, ``host``, ``port``); extra keys append in their
    existing order.  Deterministic — the same dict always renders the same
    text, so writing twice is idempotent.
    """
    header = (
        "  // local-embed — one process, many local embedding models, ONE active.",
        "  // Config path: $LOCAL_EMBED_FILE > slife project root (dev) > ~/.local-embed/",
    )
    body = "\n".join(header) + "\n" + _render_members(_ordered_items(cfg), indent=2)
    return "{\n" + body + "\n}"


def write_config(cfg: dict, path: "Path | None" = None) -> Path:
    """Atomically write a full config dict to disk in the canonical JSON5 style.

    Serialises with :func:`render_json5` and replaces the file via a temp
    sibling, so a crashed write never truncates a good config.  A hand-edited
    file is normalised on rewrite (comments beyond the header are dropped) —
    configs are machine-read, and command-line writers canonicalise by design
    (same trade-off slife's config writer makes).
    """
    if path is None:
        path = resolve_config_path()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(render_json5(cfg), encoding="utf-8")
    os.replace(tmp, path)
    return path
