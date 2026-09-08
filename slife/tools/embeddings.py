"""Embedding (semantic-search) configuration tools.

embeddings_model_list     — list configured embedding providers (active ★)
embeddings_model_set      — upsert a provider endpoint (creates provider if new)
embeddings_model_switch   — switch the active embedding provider
embeddings_model_remove   — remove a provider from the config
embeddings_enable         — global on/off for semantic (hybrid) search

The managed section is the top-level ``embeddings`` of slife.json5 — the
first-class, shared config for memdb + memfiles.  Each provider is **one
OpenAI-compatible endpoint**: ``base_url`` + ``api_key`` and a single
``model`` (the id sent on ``/v1/embeddings``); ``active_model`` names the
active provider.  The vector dimension is never configured — it is
discovered from the endpoint at runtime (known model families guessed,
anything else probed before the vec0 tables are built).

After a persist, the running memdb + memfiles plugins are asked to reload
their semantic index via their internal ``__memory_reload_semantic`` /
``__memfiles_reload_semantic`` tools (hot reload).  A failed reload degrades
to "takes effect on restart" — it never blocks the persist.
"""

from __future__ import annotations

import json
import logging
from typing import ClassVar

from slife.tools._config_io import (
    _ConfigPathMixin,
    config_write_locked,
    read_config,
    write_config,
)
from slife.tools.base import Tool, make_params

logger = logging.getLogger(__name__)

_EMBEDDINGS_KEY = "embeddings"


def _embeddings_section(raw: dict) -> dict:
    """Get or create the top-level embeddings section."""
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
    """Return the active_model ref (a provider id)."""
    return cfg.get("active_model", "")


async def _hot_reload(ctx, enabled: bool = True) -> str:
    """Ask the running memdb + memfiles plugins to reload their semantic index.

    ``enabled=True`` → manager.enable() (rebuild); ``False`` → manager.disable().
    Each plugin has an internal ``__*_reload_semantic`` tool.  Failures are
    best-effort — a plugin that is down (or not started) degrades to
    "takes effect on restart".

    ``client.call_tool`` is async (MCPClient) — awaiting is mandatory; a bare
    call returns an un-awaited coroutine, the reload RPC is never sent, and the
    coroutine leaks with a "never awaited" warning.
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
            raw = await client.call_tool(tool, {"enabled": enabled})
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
    """List configured embedding providers."""

    name: ClassVar[str] = "embeddings_model_list"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "List configured embedding providers (active provider marked ★)."
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
            model = pcfg.get("model", "")
            model_disp = f"`{model}`" if model else "(endpoint default)"
            star = "★" if pid == active else " "
            lines.append(
                f"  {star} `{pid}`  model={model_disp}  "
                f"(base: {base}, api_key: {key_disp})"
            )
            total += 1
        lines.insert(0, f"**{total} embedding provider(s)** configured. "
                        f"Active: `{active}`  enabled={enabled}")
        return "\n".join(lines)


# ── Set embedding provider ───────────────────────────────────────────


class SetEmbeddingsTool(_EmbeddingsConfigTool):
    """Add or update an embedding provider endpoint."""

    name: ClassVar[str] = "embeddings_model_set"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "Add/update an embedding provider (an OpenAI-compatible endpoint; "
        "upsert, creates provider if new; hot-reloads the semantic index)."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Provider ID, created if new.",
            },
            "base_url": {
                "type": "string",
                "description": "OpenAI-compatible base URL (required for new providers).",
            },
            "api_key": {
                "type": "string",
                "description": "API key (${VAR} ref or plaintext); required for new providers.",
            },
            "model": {
                "type": "string",
                "description": "Embedding model id on this endpoint (e.g. bge-m3); "
                               "omitted → the endpoint default is used.",
            },
        },
        "required": ["provider"],
    }

    @config_write_locked
    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        raw = read_config(self._config_path)
        emb = _embeddings_section(raw)
        providers = emb["providers"]

        pid = kwargs["provider"]
        created = False
        if pid not in providers or not isinstance(providers[pid], dict):
            if "base_url" not in kwargs:
                return (
                    f"Error: provider '{pid}' does not exist. "
                    f"Provide base_url and api_key to create it."
                )
            providers[pid] = {}
            created = True
        pcfg = providers[pid]
        if not isinstance(pcfg, dict):
            pcfg = {}
            providers[pid] = pcfg

        if "base_url" in kwargs:
            pcfg["base_url"] = kwargs["base_url"]
        if "api_key" in kwargs:
            pcfg["api_key"] = kwargs["api_key"]
        if "model" in kwargs:
            pcfg["model"] = kwargs["model"]

        # If no active provider yet, this becomes the active one.
        if not _active_ref(emb):
            emb["active_model"] = pid

        write_config(self._config_path, raw)
        action = "Created" if created else "Updated"
        reload_note = await _hot_reload(getattr(self, "_ctx", None), enabled=True)
        logger.info("embeddings_model_%s provider=%s", action.lower(), pid)
        return f"[OK] {action} embedding provider `{pid}`. {reload_note}"


# ── Switch embedding provider ────────────────────────────────────────


class SwitchEmbeddingsTool(_EmbeddingsConfigTool):
    """Switch the active embedding provider."""

    name: ClassVar[str] = "embeddings_model_switch"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "Switch the active embedding provider; ref from embeddings_model_list."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Provider id to activate.",
            },
        },
        "required": ["provider"],
    }

    @config_write_locked
    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        pid = kwargs["provider"]
        raw = read_config(self._config_path)
        emb = raw.get(_EMBEDDINGS_KEY, {})
        if not isinstance(emb, dict):
            return "Error: no embeddings configured."
        providers = emb.get("providers", {})
        if not isinstance(providers, dict) or pid not in providers:
            return f"Error: provider '{pid}' not found. Use embeddings_model_list."

        old = _active_ref(emb) or "(none)"
        emb["active_model"] = pid
        write_config(self._config_path, raw)
        reload_note = await _hot_reload(getattr(self, "_ctx", None), enabled=True)
        logger.info("embeddings_model_switched from=%s to=%s", old, pid)
        return f"[OK] Switched active embedding provider from `{old}` to `{pid}`. {reload_note}"


# ── Remove embedding provider ────────────────────────────────────────


class RemoveEmbeddingsTool(_EmbeddingsConfigTool):
    """Remove an embedding provider."""

    name: ClassVar[str] = "embeddings_model_remove"
    category: ClassVar[str] = "embeddings"
    description: ClassVar[str] = (
        "Remove an embedding provider by id; cannot remove the active provider."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "provider": {
                "type": "string",
                "description": "Provider id to remove.",
            },
        },
        "required": ["provider"],
    }

    @config_write_locked
    async def execute(self, **kwargs) -> str:
        if not self._config_path:
            return "Error: config path not available."

        pid = kwargs["provider"]
        raw = read_config(self._config_path)
        emb = raw.get(_EMBEDDINGS_KEY, {})
        if not isinstance(emb, dict):
            return "Error: no embeddings configured."
        providers = emb.get("providers", {})
        if not isinstance(providers, dict) or pid not in providers:
            return f"Error: provider '{pid}' not found."

        if _active_ref(emb) == pid:
            return (
                f"Error: cannot remove the active provider `{pid}`. "
                f"Switch to another provider first with embeddings_model_switch."
            )

        del providers[pid]
        if not providers:
            raw.pop(_EMBEDDINGS_KEY, None)

        write_config(self._config_path, raw)
        reload_note = await _hot_reload(getattr(self, "_ctx", None), enabled=True)
        logger.info("embeddings_model_removed provider=%s", pid)
        return f"[OK] Removed `{pid}`. {reload_note}"


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

    @config_write_locked
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
        reload_note = await _hot_reload(getattr(self, "_ctx", None), enabled=enabled)
        state = "enabled" if enabled else "disabled"
        logger.info("embeddings_%s", state)
        return f"[OK] Semantic search {state}. {reload_note}"
