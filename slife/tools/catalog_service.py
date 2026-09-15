"""Tool catalog orchestration — policy over :class:`CatalogStore`.

The store is a dumb data layer; this service owns the semantics from
DESIGNER_NOTES §8.5: seed/reseed on session start, the effective-status
refusals for ``tool_load`` / ``_unload_function_tool``, the per-turn
injection snapshot (loaded ∧ whitelist), and main-agent-only curatorship.
Instances are per-process (main agent + subagent workers open the same
file); ``write_owner`` marks the single process allowed to run session
reseed and threshold eviction.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from slife.tools.catalog import (
    STATUS_LOADED,
    STATUS_UNLOADED,
    CatalogStore,
    EFF_DISABLED,
    EFF_ERROR,
    FUNCTION_CATEGORIES,
)
from slife.tools.whitelist import ALWAYS_LOADED, is_meta_tool

if TYPE_CHECKING:
    from slife.tools.base import Tool
    from slife.tools.semantic import SemanticManager

logger = logging.getLogger(__name__)

#: The job-coding plugin name — its bare proxy tools are the ``job`` category.
JOB_PLUGIN_NAME = "job-coding"


def descriptor_json(name: str, description: str, input_schema) -> str:
    """Compact JSON of a tool descriptor — the catalog ``schema`` column.

    Strictly the tool def: ``name``, ``description`` and ``inputSchema`` (the
    arguments as a plain JSON Schema).  No other keys — the shape the loop
    re-serializes into an OpenAI function definition, keeping the stored
    schema and the injected one the same thing.
    """
    return json.dumps(
        {
            "name": name,
            "description": description,
            "inputSchema": input_schema,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def tool_descriptor(tool: "Tool") -> str:
    """Compact JSON of a tool's full descriptor (the catalog ``schema`` column)."""
    return descriptor_json(
        getattr(tool, "name", ""),
        getattr(tool, "description", ""),
        getattr(tool, "parameters", {"type": "object", "properties": {}}),
    )


def catalog_category(tool: "Tool") -> str:
    """Catalog category of a registered tool instance.

    - natives → ``builtin``;
    - built-in plugin proxies (DIRECT/WRAPPER, bare names) → ``job`` for the
      job-coding plugin, else ``builtin``;
    - external MCP proxies → ``mcp`` (the reconcile corrects ``rest-api``
      from the server config's ``source.type``).
    """
    from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

    if isinstance(tool, MCPProxyTool):
        if getattr(tool, "_route", None) == ProxyRoute.EXTERNAL:
            return "mcp"
        server = getattr(tool, "server", "") or ""
        return "job" if server == JOB_PLUGIN_NAME else "builtin"
    return "builtin"


class ToolCatalogService:
    """Session-facing orchestration over the shared catalog store."""

    def __init__(
        self,
        store: CatalogStore,
        *,
        threshold: int = 100,
        write_owner: bool = True,
        preload: tuple[str, ...] = (),
    ):
        self._store = store
        self.threshold = threshold
        self.write_owner = write_owner
        self.preload = frozenset(preload)
        #: The host's semantic actor (set by AgentService after startup) —
        #: tool_search reads its embedder for hybrid retrieval.
        self.semantic_manager: "SemanticManager | None" = None

    @property
    def store(self) -> CatalogStore:
        return self._store

    # ── Session lifecycle ──────────────────────────────────────────

    async def seed_inventory(self, tools: list["Tool"]) -> None:
        """Reconcile the registered tool set into the catalog.

        A NEW row gets the session default: ``loaded`` for the always-loaded
        set (the harness pair + the meta tools, plus anything listed under
        ``tool_load.preload``), ``unloaded`` for everything else — discovery
        alone never puts a tool into the injection set.  An EXISTING row keeps
        its state: this sync mirrors WHICH tools are registered, it never
        overrides what the model (or a previous session) decided to load.

        Main-owner only (a subagent worker never reseeds).
        """
        if not self.write_owner:
            return
        for tool in tools:
            name = _tool_name(tool)
            if not name:
                continue
            external = _is_external(tool)
            await self._store.upsert_tool(
                name,
                description=getattr(tool, "description", "") or "",
                category=catalog_category(tool),
                source_id=getattr(tool, "server", None) if external else None,
                schema=tool_descriptor(tool),
                enabled=None if external else True,
                # Only a brand-new row sees this — upsert_tool applies status
                # on INSERT alone, which is exactly the keep-existing rule.
                status=self.default_status(name),
            )

    def default_status(self, name: str) -> str:
        """What a NEW catalog row starts as: always-loaded → loaded, else unloaded."""
        if is_meta_tool(name) or name in self.preload:
            return STATUS_LOADED
        return STATUS_UNLOADED

    # ── Injection snapshot ─────────────────────────────────────────

    async def snapshot_loaded(self) -> frozenset[str]:
        """Injectable tool names this turn: effective-loaded ∪ always-loaded."""
        names = await self._store.loaded_names()
        return frozenset(names) | ALWAYS_LOADED

    async def effective_status(self, name: str) -> str | None:
        """Effective status for one name (None if unknown)."""
        return await self._store.get_effective(name)

    async def is_meta(self, name: str) -> bool:
        return is_meta_tool(name)

    # ── Load / unload (shared across main agent and workers) ───────

    async def load_tool(self, name: str) -> tuple[bool, str]:
        """Flip a function tool to ``loaded``; returns (ok, message).

        Refusal matrix uses the derived effective status — DISABLED
        (config) and ERROR (the tool's server is not up) each get their own
        hint.  The caller materializes an execution instance for
        server-backed tools (the proxy) after a successful flip.
        """
        row = await self._store.get_tool(name)
        if row is None:
            return False, (f"Error: tool '{name}' is unknown — see tool_search.")

        cat = row.get("category", "")
        if cat not in FUNCTION_CATEGORIES:
            return False, (
                f"Error: tool '{name}' has no load/unload state "
                f"(category '{cat}')."
            )

        eff = await self._store.get_effective(name)
        if eff == EFF_DISABLED:
            return False, f"Error: tool '{name}' is disabled — enable it first."
        if eff == EFF_ERROR:
            server = row.get("source_id") or "?"
            return False, (
                f"Error: tool '{name}' is unavailable — server '{server}' is "
                f"not up right now (its tools are marked error). Check it with "
                f"mcp_list (or rest_api_list): it reconnects on its own, or "
                f"re-enable it with the matching *_set_enabled."
            )
        if eff == "loaded":
            return True, f"tool '{name}' is already loaded."

        await self._store.set_status(name, "loaded", bump=True)
        logger.info("catalog_tool_loaded name=%s", name)
        return True, f"[OK] Loaded '{name}'."

    async def touch(self, name: str) -> None:
        """Record a tool use — the LRU recency the eviction policy orders by.

        Write-owner only: the worker/seeding subagent never evicts, so the
        main agent alone feeds the order.  The registry calls this after a
        successful ``execute`` so a just-used tool is never the first eviction
        victim (the pre-fix behavior evicted the alphabetically-first natives).
        """
        if not self.write_owner:
            return
        await self._store.touch(name)

    async def unload_tool(self, name: str) -> tuple[bool, str]:
        """Flip a function tool to ``unloaded``; refuses meta/skill/cli."""
        if is_meta_tool(name):
            return False, (
                f"Error: cannot unload '{name}' — it is a meta tool "
                f"(always loaded, not configurable)."
            )
        row = await self._store.get_tool(name)
        if row is None:
            return False, f"Error: tool '{name}' is unknown."
        cat = row.get("category", "")
        if cat not in FUNCTION_CATEGORIES:
            return False, (
                f"Error: tool '{name}' has no load/unload state "
                f"(category '{cat}')."
            )
        if row.get("status") != "loaded":
            return True, f"tool '{name}' is already unloaded."
        await self._store.set_status(name, "unloaded")
        logger.info("catalog_tool_unloaded name=%s", name)
        return True, f"[OK] Unloaded '{name}'."

    # ── Threshold eviction (main-owner policy) ─────────────────────

    async def evict_to_threshold(self) -> list[str]:
        """Evict oldest-by-``last_loaded`` loaded tools down to the threshold.

        Never evicts the always-loaded carve-outs (harness pairs + meta
        whitelist) or non-function (skill/cli) rows.
        Main-owner only — a subagent worker inherits the curator's budget
        and never squeezes it (a worker's turn stays within what the agent
        left loaded).
        """
        if not self.write_owner:
            return []
        count = await self._store.count_loaded()
        excess = count - self.threshold
        if excess <= 0:
            return []
        # Protected = the always-loaded carve-outs ∪ the configured preload
        # set (tools.json5 ``tool_load: {preload: [...]}`` — an explicit
        # "never evict these" list a user can tune).
        protected = set(ALWAYS_LOADED) | set(self.preload)
        evicted = await self._store.evict_lru(excess, protected=protected)
        if evicted:
            logger.info(
                "catalog_evict_to_threshold threshold=%d evicted=%d/%d",
                self.threshold, len(evicted), excess,
            )
        return evicted

    # ── Reconcile support (per-server/plugin sync helpers) ─────────

    async def upsert_external_tool(
        self,
        name: str,
        *,
        server: str,
        description: str,
        schema: str,
        category: str = "mcp",
    ) -> bool:
        """Upsert a server-backed tool row (schema-change detection → re-embed).

        A newly seen tool lands ``unloaded`` (and always will — an external
        name is never in the whitelist), while an existing row keeps whatever
        the model decided.  ``tool_load`` is the only way into the injection
        set.
        """
        return await self._store.upsert_tool(
            name,
            description=description,
            category=category,
            source_id=server,
            schema=schema,
            enabled=None,
            status=self.default_status(name),
        )

    async def mark_source_error(self, source: str) -> int:
        """Mark one server's tools ``error`` — the server is unusable.

        The single verdict for every unavailable case: not yet connected at
        startup, disconnected, a failed connect, or a dead gateway child.
        ``error`` is a state of its own, so the row still says the tool
        belongs to a server that is simply not up.
        """
        if not self.write_owner:
            return 0
        return await self._store.mark_source_error(source)

    async def mark_all_external_error(self) -> int:
        """Mark EVERY external tool ``error`` — nothing is live right now.

        Used both at catalog init (no server has connected yet) and when the
        gateway child dies (all of its servers are unreachable at once).
        """
        if not self.write_owner:
            return 0
        return await self._store.mark_all_external_error()

    async def mark_server_connected(self, source: str) -> int:
        """A server (re)connected — its ``error`` tools become ``unloaded``.

        Only ``error`` rows move: a tool the user had loaded keeps that state
        across a reconnect, because the reconnect clears the error mark
        rather than resetting the whole server.
        """
        if not self.write_owner:
            return 0
        return await self._store.reset_source_status(source, STATUS_UNLOADED)

    async def purge_source(self, source: str) -> int:
        """Drop every row of a server that left the config (main-owner only)."""
        if not self.write_owner:
            return 0
        return await self._store.purge_source(source)

    async def purge_unconfigured_sources(self, configured: "set[str]") -> "set[str]":
        """Purge the rows of every server NOT in *configured* (main-owner only).

        The mirror image of the removed ``sync_config_servers``: tools.json5
        is still the authority, but it is now compared against the servers
        that actually OWN tool rows instead of against a server table.
        """
        if not self.write_owner:
            return set()
        purged = await self._store.purge_missing_sources(configured)
        if purged:
            logger.info("catalog_purged_config_removed servers=%r", sorted(purged))
        return purged


def _tool_name(tool: "Tool") -> str:
    return getattr(tool, "name", "") or ""


def _is_external(tool: "Tool") -> bool:
    from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

    return isinstance(tool, MCPProxyTool) and getattr(tool, "_route", None) == ProxyRoute.EXTERNAL