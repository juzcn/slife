"""Embedding configuration helpers — read, write, report.

Manages the top-level ``embeddings`` section of ``slife.json5`` — the
first-class, shared config for memdb + memfiles semantic search.  Two
levels mirror the LLM ``models.providers`` shape: each provider is an
OpenAI-compatible endpoint (``base_url`` + ``api_key``) with an optional
``models`` list; ``active_model`` (``"provider/model"`` or ``"provider"``)
is configuration-authoritative.  The embedder itself is owned by
``SemanticManager`` (semantic.py); this module never mutates it.
"""

import logging

from slife.paths import get_config_path
from slife.tools._config_io import ConfigParseError, read_config, write_config

logger = logging.getLogger(__name__)

_CONFIG_PATH = get_config_path()


def _read_raw() -> dict:
    """Read the full slife.json5 dict, returning {} on failure.

    This is a read-only helper for the embeddings section; an unparseable
    config must not crash the caller, it just means "no usable section".
    (Mutating paths deliberately do *not* swallow the error — see
    :func:`slife.tools._config_io.read_config`.)
    """
    try:
        return read_config(_CONFIG_PATH)
    except ConfigParseError:
        logger.error("embeddings_config_unparseable path=%s", _CONFIG_PATH)
        return {}


def _write_raw(raw: dict) -> None:
    """Write the full slife.json5 dict."""
    write_config(_CONFIG_PATH, raw)


# ── Public API ────────────────────────────────────────────────────────


def read_embedding_config() -> dict | None:
    """Return the current top-level *embeddings* section, or None if absent."""
    raw = _read_raw()
    emb = raw.get("embeddings")
    if not isinstance(emb, dict):
        return None
    return dict(emb)


def write_embedding_config(cfg: dict) -> None:
    """Write (overwrite) the top-level *embeddings* section with *cfg*."""
    raw = _read_raw()
    raw["embeddings"] = cfg
    _write_raw(raw)
    logger.info("embeddings_config_written keys=%s", list(cfg.keys()))


def _active_endpoint(cfg: dict) -> dict:
    """Resolve the active provider + model ref from an embeddings section.

    Returns ``{"provider": str, "base_url": str, "api_key": str,
    "model": str, "dim": int}`` — ``model``/``dim`` empty when not
    configured.  ``active_model`` is ``"provider/model"`` or bare
    ``"provider"``; a bare provider defers the model to the endpoint's
    /v1/models active model (or first entry).
    """
    providers = cfg.get("providers", {})
    if not isinstance(providers, dict) or not providers:
        return {"provider": "", "base_url": "", "api_key": "",
                "model": "", "dim": 0}
    active_ref = cfg.get("active_model", "")
    pid = active_ref.split("/", 1)[0] if active_ref else next(iter(providers))
    pcfg = providers.get(pid)
    if not isinstance(pcfg, dict):
        pcfg = {}
        pid = next(iter(providers))
        pcfg = providers.get(pid)
    if not isinstance(pcfg, dict):
        return {"provider": "", "base_url": "", "api_key": "",
                "model": "", "dim": 0}
    mid = active_ref.split("/", 1)[1] if "/" in active_ref else ""
    dim = 0
    if mid:
        for m in (pcfg.get("models") or []):
            if isinstance(m, dict) and m.get("model") == mid:
                dim = int(m.get("dim", 0) or 0)
                break
    elif pcfg.get("models"):
        # No explicit model — the endpoint's active model wins, but a
        # configured first-entry dim is a useful provisional width.
        first = next((m for m in pcfg["models"] if isinstance(m, dict)), None)
        if first:
            dim = int(first.get("dim", 0) or 0)
    return {
        "provider": pid,
        "base_url": pcfg.get("base_url", ""),
        "api_key": pcfg.get("api_key", ""),
        "model": mid,
        "dim": dim,
    }


def get_active_endpoint() -> dict:
    """Return the resolved active endpoint dict (see ``_active_endpoint``).

    The single source of truth for what memdb/memfiles embed against.
    """
    cfg = read_embedding_config()
    if cfg is None:
        return {"provider": "", "base_url": "", "api_key": "",
                "model": "", "dim": 0}
    return _active_endpoint(cfg)


def make_check_report() -> dict:
    """Build the semantic config-facts report for the plugins' ``__check``.

    Raw technical facts only (configured/provider/model/dimension/available);
    the harness's ``system_health`` interprets them into health entries.
    """
    cfg = read_embedding_config()

    if cfg is None:
        return {
            "configured": False,
            "provider": "",
            "model": "",
            "dimension": 0,
            "available": False,
            "hint": (
                "No embeddings configured. Semantic search (hybrid mode) is "
                "unavailable. Keyword search (grep / fts5 / time) still works. "
                "Add an OpenAI-compatible endpoint with embeddings_model_set "
                "(provider + base_url + api_key)."
            ),
        }

    ep = _active_endpoint(cfg)
    if not ep["base_url"]:
        return {
            "configured": True,
            "provider": ep["provider"],
            "model": ep["model"],
            "dimension": ep["dim"],
            "available": False,
            "hint": (
                f"Provider '{ep['provider']}' has no base_url configured. "
                "Fix it with embeddings_model_set."
            ),
        }

    # Probe the actual endpoint — same config path the rest of this module
    # uses so the availability probe and the section read can't disagree.
    from slife.plugins.memdb.embeddings import EmbeddingClient
    client = EmbeddingClient.from_config(config_path=str(_CONFIG_PATH), quiet=True)

    result: dict = {
        "configured": True,
        "provider": ep["provider"],
        "model": ep["model"],
        "dimension": client.dimension if client.available else ep["dim"],
        "available": client.available,
        "base_url": ep["base_url"],
        "enabled": bool(cfg.get("enabled", True)),
    }

    if client.available:
        result["hint"] = (
            f"API embedding ready: provider={ep['provider']} "
            f"model={client._model or ep['model']} (dim={client.dimension})"
        )
    else:
        result["hint"] = (
            f"API embedding unavailable (base_url={ep['base_url']}). "
            "Check the endpoint is reachable and the openai package is "
            "installed. Keyword search (grep/fts5/time) still works."
        )

    return result
