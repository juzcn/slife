"""Embedding (semantic-search) configuration tools.

embeddings_model_list     — list configured providers + models (active ★)
embeddings_model_set      — upsert a model on a provider (creates provider if new)
embeddings_model_switch   — switch the active embedding model/endpoint
embeddings_model_remove   — remove a model (or provider) from the config
embeddings_enable         — global on/off for semantic (hybrid) search

The managed section is the top-level ``embeddings`` of slife.json5 — the
first-class, shared config for memdb + memfiles.  It mirrors the LLM
``models.providers`` shape: each provider is an OpenAI-compatible endpoint
(``base_url`` + ``api_key``) with an optional ``models`` list; ``active_model``
(``"provider/model"`` or bare ``"provider"``) is configuration-authoritative.

After a persist, the running memdb + memfiles plugins are asked to reload
their semantic index via their internal ``__memory_reload_semantic`` /
``__memfiles_reload_semantic`` tools (hot reload).  A failed reload degrades
to "takes effect on restart" — it never blocks the persist.
"""

from __future__ import annotations

import json
import logging
from typing import ClassVar

from slife.tools._config_io import _ConfigPathMixin, read_config, write_config
from slife.tools.base import Tool, make_params

logger = logging.getLogger(__name__)

_EMBEDDINGS_KEY = "embeddings"


def _embeddings_section(raw: dict) -> dict:
    """Get or create the top-level embeddings.providers: section."""
    emb = raw.setdefault(_EMBEDDINGS_KEY, {})
    if not isinstance(emb, dict):
        emb = {}
        raw[_EMBEDDINGS_KEY] = emb
    providers = emb.setdefault("providers", {})
    if not isinstance(providers, dict):
        providers = {}
        emb["providers"] = providers
    return emb


def _active_ref(cfg: dict) -> str:
    """Return the active_model ref (``"pid"`` or ``"pid/model"``)."""
    return cfg.get("active_model", "")


def _provider_models(pcfg: dict) -> list:
    """Get or create a provider's models list."""
    models = pcfg.setdefault("models", [])
    if not isinstance(models, list):
        models = []
        pcfg["models"] = models
    return models


def _hot_reload(ctx, enabled: bool = True) -> str:
    """Ask the running memdb + memfiles plugins to reload their semantic index.

    ``enabled=True`` → manager.enable() (rebuild); ``False`` → manager.disable().
    Each plugin has an internal ``__*_reload_semantic`` tool.  Failures are
    best-effort — a plugin that is down (or not started) degrades to
    "takes effect on restart".
    """
    notes: list[str] = []
    targets = (
        ("memdb", getattr(ctx, "memdb_client", None), "__memory_reload_semantic"),
        ("memfiles", getattr(ctx, "memfiles_client", None), "__memfiles_reload_semantic"),
    )
    for name, client, tool in targets:
        if client is None:
            notes.append(f"{name}: plugin not connected — restart to apply")
            continue
        try:
            raw = client.call_tool(tool, {"enabled": enabled})
            if isinstance(raw, str):
                raw = json.loads(raw)
            status = raw.get("status", raw) if isinstance(raw, dict) else raw
            notes.append(f"{name}: {status}")
        except Exception as e:
            logger.warning("embeddings_reload_failed plugin=%s err=%s", name, e)
            notes.append(f"{name}: reload failed ({e}) — restart to apply")
    return "; ".join(notes)


class _EmbeddingsConfigTool(_ConfigPathMixin, Tool):
    """Shared ``__init__``/``from_config`` for the embeddings-mutation tools.

    All of them need the live config path and ``ToolContext`` (for the
    plugin clients used by hot reload).

    Not a real tool — placeholder class attrs only to pass
    ``Tool.__init_subclass__`` validation; excluded from auto-discovery.
    """

    name = "_embeddings_config_tool"
    description = "embeddings config tool base (placeholder)"
    parameters: ClassVar[dict] = {"type": "object", "properties": {}}
    _skip_auto_register: ClassVar[bool] = True


# ── List embeddings ──────────────────────────────────────────────────


class ListEmbeddingsTool(_ConfigPathMixin, Tool):
    """List configured embedding providers + models."""

    name: ClassVar[str] = "embeddings_model_list"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "List configured embedding providers and models (active model marked ★)."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {},
        "required": [],
    }

    async def execute(self, **_kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."
        raw = read_config(self._config_path)
        emb = raw.get(_EMBEDDINGS_KEY, {})
        if not isinstance(emb, dict):
            return "No embeddings configured."
        providers = emb.get("providers", {})
        if not isinstance(providers, dict) or not providers:
            return "No embeddings configured. Add a provider with embeddings_model_set."
        active = _active_ref(emb)
        enabled = emb.get("enabled", True)
        lines = []
        total = 0
        for pid, pcfg in providers.items():
            if not isinstance(pcfg, dict):
                continue
            base = pcfg.get("base_url", "")
            key = pcfg.get("api_key", "")
            key_disp = "set" if key else "not set"
            models = pcfg.get("models", [])
            if not isinstance(models, list):
                models = []
            lines.append(f"\n## {pid}  (base: {base}, api_key: {key_disp})")
            if not models:
                ref = pid
                star = "★" if ref == active else " "
                lines.append(f"  {star} `{ref}` — (model auto-discovered from endpoint)")
                total += 1
            else:
                for m in models:
                    if not isinstance(m, dict):
                        continue
                    model_id = m.get("model", "?")
                    ref = f"{pid}/{model_id}"
                    star = "★" if ref == active else " "
                    dim = m.get("dim", "?")
                    lines.append(f"  {star} `{ref}`  dim={dim}")
                    total += 1
        lines.insert(0, f"**{total} embedding model(s)** configured. "
                        f"Active: `{active}`  enabled={enabled}")
        return "\n".join(lines)


# ── Set embedding model ──────────────────────────────────────────────


class SetEmbeddingsTool(_EmbeddingsConfigTool):
    """Add or update an embedding model on a provider."""

    name: ClassVar[str] = "embeddings_model_set"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "Add/update an embedding model (upsert; creates provider if new; "
        "hot-reloads the semantic index)."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Provider ID, created if new.",
            },
            "model": {
                "type": "string",
                "description": "Embedding model id (e.g. bge-m3).",
            },
            "base_url": {
                "type": "string",
                "description": "OpenAI-compatible base URL (required for new providers).",
            },
            "api_key": {
                "type": "string",
                "description": "API key (${VAR} ref or plaintext); required for new providers.",
            },
            "dim": {
                "type": "integer",
                "description": "Embedding dimension (auto-discovered when omitted).",
            },
        },
        "required": ["provider", "model"],
    }

    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        raw = read_config(self._config_path)
        emb = _embeddings_section(raw)
        providers = emb["providers"]

        pid = kwargs["provider"]
        model_id = kwargs["model"]

        # Provider: get or create
        if pid not in providers or not isinstance(providers[pid], dict):
            if "base_url" not in kwargs:
                return (
                    f"Error: provider '{pid}' does not exist. "
                    f"Provide base_url and api_key to create it."
                )
            providers[pid] = {}
        pcfg = providers[pid]
        if not isinstance(pcfg, dict):
            pcfg = {}
            providers[pid] = pcfg

        if "base_url" in kwargs:
            pcfg["base_url"] = kwargs["base_url"]
        if "api_key" in kwargs:
            pcfg["api_key"] = kwargs["api_key"]

        models = _provider_models(pcfg)
        replaced = False
        for i, m in enumerate(models):
            if isinstance(m, dict) and m.get("model") == model_id:
                entry = {**m, "model": model_id}
                if "dim" in kwargs:
                    entry["dim"] = kwargs["dim"]
                models[i] = entry
                replaced = True
                break
        if not replaced:
            entry = {"model": model_id}
            if "dim" in kwargs:
                entry["dim"] = kwargs["dim"]
            models.append(entry)

        # If no active_model yet, this becomes the active one.
        if not _active_ref(emb):
            emb["active_model"] = f"{pid}/{model_id}"

        write_config(self._config_path, raw)
        action = "Updated" if replaced else "Added"
        ref = f"{pid}/{model_id}"
        reload_note = _hot_reload(getattr(self, "_ctx", None), enabled=True)
        logger.info("embeddings_model_%s ref=%s", action.lower(), ref)
        return f"[OK] {action} embedding model `{ref}`. {reload_note}"


# ── Switch embedding model ───────────────────────────────────────────


class SwitchEmbeddingsTool(_EmbeddingsConfigTool):
    """Switch the active embedding model/endpoint."""

    name: ClassVar[str] = "embeddings_model_switch"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "Switch the active embedding model/endpoint; ref from "
        "embeddings_model_list."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Ref to activate ('provider/model' or bare 'provider').",
            },
        },
        "required": ["ref"],
    }

    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        ref = kwargs["ref"]
        raw = read_config(self._config_path)
        emb = raw.get(_EMBEDDINGS_KEY, {})
        if not isinstance(emb, dict):
            return "Error: no embeddings configured."

        pid = ref.split("/", 1)[0]
        providers = emb.get("providers", {})
        if not isinstance(providers, dict) or pid not in providers:
            return f"Error: provider '{pid}' not found. Use embeddings_model_list."

        if "/" in ref:
            mid = ref.split("/", 1)[1]
            pcfg = providers[pid]
            models = pcfg.get("models", []) if isinstance(pcfg, dict) else []
            found = any(
                isinstance(m, dict) and m.get("model") == mid
                for m in models
            )
            if not found:
                return f"Error: model '{mid}' not found in provider '{pid}'. Use embeddings_model_list."

        old = _active_ref(emb) or "(none)"
        emb["active_model"] = ref
        write_config(self._config_path, raw)
        reload_note = _hot_reload(getattr(self, "_ctx", None), enabled=True)
        logger.info("embeddings_model_switched from=%s to=%s", old, ref)
        return f"[OK] Switched active embedding model from `{old}` to `{ref}`. {reload_note}"


# ── Remove embedding model ───────────────────────────────────────────


class RemoveEmbeddingsTool(_EmbeddingsConfigTool):
    """Remove an embedding model (or a whole provider)."""

    name: ClassVar[str] = "embeddings_model_remove"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "Remove an embedding model by ref ('provider/model', or bare "
        "'provider' for the whole provider); cannot remove the active model."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": "Ref to remove ('provider/model' or bare 'provider').",
            },
        },
        "required": ["ref"],
    }

    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        ref = kwargs["ref"]
        pid = ref.split("/", 1)[0]
        raw = read_config(self._config_path)
        emb = raw.get(_EMBEDDINGS_KEY, {})
        if not isinstance(emb, dict):
            return "Error: no embeddings configured."
        providers = emb.get("providers", {})
        if not isinstance(providers, dict) or pid not in providers:
            return f"Error: provider '{pid}' not found."

        if _active_ref(emb) == ref:
            return (
                f"Error: cannot remove the active model `{ref}`. "
                f"Switch to another model first with embeddings_model_switch."
            )

        if "/" in ref:
            mid = ref.split("/", 1)[1]
            pcfg = providers[pid]
            models = pcfg.get("models", []) if isinstance(pcfg, dict) else []
            removed = False
            for i, m in enumerate(models):
                if isinstance(m, dict) and m.get("model") == mid:
                    del models[i]
                    removed = True
                    break
            if not removed:
                return f"Error: model '{mid}' not found in provider '{pid}'."
            if not models:
                del providers[pid]
        else:
            del providers[pid]

        if not providers:
            raw.pop(_EMBEDDINGS_KEY, None)

        write_config(self._config_path, raw)
        reload_note = _hot_reload(getattr(self, "_ctx", None), enabled=True)
        logger.info("embeddings_model_removed ref=%s", ref)
        return f"[OK] Removed `{ref}`. {reload_note}"


# ── Enable/disable embeddings ────────────────────────────────────────


class EnableEmbeddingsTool(_EmbeddingsConfigTool):
    """Global on/off switch for semantic (hybrid) search."""

    name: ClassVar[str] = "embeddings_enable"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "Enable or disable semantic (hybrid) search globally."
    )
    parameters: ClassVar[dict] = make_params(
        enabled={
            "type": "boolean",
            "description": "Enable or disable semantic search.",
        },
    )

    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        enabled = bool(kwargs["enabled"])
        raw = read_config(self._config_path)
        emb = raw.setdefault(_EMBEDDINGS_KEY, {})
        if not isinstance(emb, dict):
            emb = {}
            raw[_EMBEDDINGS_KEY] = emb
        emb["enabled"] = enabled
        write_config(self._config_path, raw)
        reload_note = _hot_reload(getattr(self, "_ctx", None), enabled=enabled)
        state = "enabled" if enabled else "disabled"
        logger.info("embeddings_%s", state)
        return f"[OK] Semantic search {state}. {reload_note}"
