"""Embedding (semantic-search) configuration tools.

embeddings_model_list     — list configured embedding providers (active ★)
embeddings_model_set      — upsert a provider endpoint (creates provider if new)
embeddings_model_switch   — switch the active embedding provider
embeddings_model_remove   — remove a provider from the config
embeddings_enable         — global on/off for semantic (hybrid) search

The managed section is the top-level ``embeddings`` of slife.yaml — the
first-class, shared config for memdb + memfiles.  Each provider is **one
OpenAI-compatible endpoint**: ``base_url`` + ``api_key`` and a single
``model`` (the id sent on ``/v1/embeddings``); ``active_model`` names the
active provider.  The vector dimension is never configured — it is
discovered from the endpoint at runtime (known model families guessed,
anything else probed before the vec0 tables are built).

After a persist the change is adopted and then every semantic index that
follows the section is rebuilt: the ``memdb`` / ``memfiles`` plugins through
their internal reload tools (declared per plugin as ``semantic_reload_tool``)
and the host's own tool-catalog drainer in-process.  One sequence, one entry
point (``_apply_embeddings_change``) — the tool catalog used to be missing from
it, so a provider switch left the tool index embedding against the replaced
endpoint until restart while the two indexes beside it followed.  A failed
reload degrades to "takes effect on restart" — it never blocks the persist.
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
    """Rebuild or drop every semantic index that follows the ``embeddings`` section.

    ONE loop over the plugin specs that declare ``semantic_reload_tool``, plus
    the semantic index that is NOT a plugin: the host's own tool catalog.  The
    spec field is what keeps the set of indexes in one place — the previous
    hand-written pair of plugin names meant the tool catalog was never reloaded
    at all, so switching the active provider (or switching semantic search off)
    left the tool index embedding against the endpoint it was built with until
    the next restart, while the two indexes beside it followed the change.

    ``enabled=True`` → the index rebuilds against the current section;
    ``False`` → it drops its embedder (index on disk kept).  Failures stay
    best-effort: an index that cannot reload degrades to "takes effect on
    restart", which is a fact the caller reports rather than an error.

    ``client.call_tool`` is async (MCPClient) — awaiting is mandatory; a bare
    call returns an un-awaited coroutine, the reload RPC is never sent, and the
    coroutine leaks with a "never awaited" warning.
    """
    notes: list[str] = []
    from slife.plugins.spec import PLUGIN_SPECS

    for spec in PLUGIN_SPECS.values():
        if spec.semantic_reload_tool is None:
            continue
        client = getattr(ctx, spec.ctx_field, None) if spec.ctx_field else None
        if client is None:
            notes.append(f"{spec.name}: plugin not connected — restart to apply")
            continue
        try:
            raw = await client.call_tool(spec.semantic_reload_tool, {"enabled": enabled})
            if isinstance(raw, str):
                raw = json.loads(raw)
            status = raw.get("status", raw) if isinstance(raw, dict) else raw
            notes.append(f"{spec.name}: {status}")
        except Exception as e:
            logger.warning("embeddings_reload_failed plugin=%s err=%s", spec.name, e)
            notes.append(f"{spec.name}: reload failed ({e}) — restart to apply")

    notes.append(await _reload_tool_index(ctx, enabled))
    return "; ".join(n for n in notes if n)


async def _reload_tool_index(ctx, enabled: bool) -> str:
    """Reload the host's tool-catalog index — this process's own, in-process.

    Present only where this process owns the index (``caps.catalog_drainer``):
    a subagent worker queries its parent's index and owns none, so there is
    nothing here to reload — and it says nothing rather than "restart to apply",
    which would be a promise a worker's restart cannot keep.
    """
    manager = getattr(getattr(ctx, "catalog", None), "semantic_manager", None)
    if manager is None:
        return ""
    name = "tool catalog"
    try:
        section = getattr(getattr(ctx, "config", None), "embeddings_config", None)
        if not enabled:
            await manager.disable()
            return f"{name}: disabled"
        if section is None:
            return f"{name}: no config in this context — restart to apply"
        await manager.reload(section)
        return f"{name}: {manager.state}"
    except Exception as e:
        logger.warning("embeddings_reload_failed index=%s err=%s", name, e)
        return f"{name}: reload failed ({e}) — restart to apply"


def _adopt_section(ctx, section: dict) -> None:
    """Put a freshly written ``embeddings`` section into this process's config.

    The tools write slife.yaml; the process holds a ``Config`` snapshot — and a
    worker holds ONLY the snapshot, so this is the one place a runtime change
    can reach it.  Everything that reads the section after the write depends on
    it: the health fact (``embeddings=enabled|disabled`` — and *which* provider
    is active), every index reload above, and the config a LATER subagent
    inherits.  Left alone, the report and the next spawn would describe the
    endpoint that was just replaced.
    """
    cfg = getattr(ctx, "config", None)
    if cfg is None:
        return
    from slife.config import EmbeddingsConfig

    cfg.embeddings_config = EmbeddingsConfig.from_dict(section)


async def _apply_embeddings_change(ctx, section: dict, enabled: bool = True) -> str:
    """After a persist: adopt the new section, then rebuild what depends on it.

    One entry point for all four mutation tools (set / switch / remove /
    enable) — the sequence is the same for each, and a tool that forgot a step
    would leave one consumer of the section describing the previous one.
    """
    _adopt_section(ctx, section)
    return await _hot_reload(ctx, enabled)


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
        if err := self._require_config():
            return err
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
        if err := self._require_config():
            return err

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
        reload_note = await _apply_embeddings_change(
            getattr(self, "_ctx", None), emb, enabled=True,
        )
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
        if err := self._require_config():
            return err

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
        reload_note = await _apply_embeddings_change(
            getattr(self, "_ctx", None), emb, enabled=True,
        )
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
        if err := self._require_config():
            return err

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
        reload_note = await _apply_embeddings_change(
            getattr(self, "_ctx", None), emb, enabled=True,
        )
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
        if err := self._require_config():
            return err

        enabled = bool(kwargs["enabled"])
        raw = read_config(self._config_path)
        emb = raw.setdefault(_EMBEDDINGS_KEY, {})
        if not isinstance(emb, dict):
            emb = {}
            raw[_EMBEDDINGS_KEY] = emb
        emb["enabled"] = enabled
        write_config(self._config_path, raw)
        reload_note = await _apply_embeddings_change(
            getattr(self, "_ctx", None), emb, enabled=enabled,
        )
        state = "enabled" if enabled else "disabled"
        logger.info("embeddings_%s", state)
        return f"[OK] Semantic search {state}. {reload_note}"
