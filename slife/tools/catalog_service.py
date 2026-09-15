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
    CatalogStore,
    EFF_DISABLED,
    EFF_UNAVAILABLE,
    FUNCTION_CATEGORIES,
)
from slife.tools.whitelist import ALWAYS_LOADED, is_meta_tool

if TYPE_CHECKING:
    from slife.tools.base import Tool
    from slife.tools.semantic import SemanticManager

logger = logging.getLogger(__name__)

#: The job-coding plugin name — its bare proxy tools are the ``job`` category.
JOB_PLUGIN_NAME = "job-coding"


def tool_descriptor(tool: "Tool") -> str:
    """Compact JSON of a tool's full descriptor (the catalog ``schema`` column)."""
    return json.dumps(
        {
            "name": getattr(tool, "name", ""),
            "description": getattr(tool, "description", ""),
            "inputSchema": getattr(tool, "parameters", {"type": "object", "properties": {}}),
        },
        ensure_ascii=False,
        separators=(",", ":"),
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

    async def session_start(self) -> None:
        """Snapshot server runtimes (eager-connect set) — main owner only."""
        if not self.write_owner:
            return
        await self._store.session_start()

    async def seed_inventory(self, tools: list["Tool"]) -> None:
        """Reseed session state: every registered function tool starts loaded.

        Mirrors today's behavior (all registered = all injected).  Rows are
        upserted (status preserved on update) then force-set ``loaded`` — the
        session's default; the turn-boundary eviction later trims by LRU.
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
                status="loaded",   # session default — eviction trims by LRU
            )
            # Force the session default even on an existing row from a prior
            # session whose status may say otherwise.
            await self._store.set_status(name, "loaded")

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
        (config) and UNAVAILABLE (server not up) each get their own hint.
        The caller materializes an execution instance for server-backed
        tools (the proxy) after a successful flip.
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
        if eff == EFF_UNAVAILABLE:
            server = row.get("source_id") or "?"
            return False, (
                f"Error: tool '{name}' is unavailable — server '{server}' is "
                f"not connected. Use mcp_connect first."
            )
        if eff == "loaded":
            return True, f"tool '{name}' is already loaded."

        await self._store.set_status(name, "loaded", bump=True)
        logger.info("catalog_tool_loaded name=%s", name)
        return True, f"[OK] Loaded '{name}'."

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
        evicted = await self._store.evict_lru(excess, protected=ALWAYS_LOADED)
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
        loaded: bool = False,
    ) -> bool:
        """Upsert a server-backed tool row (schema-change detection → re-embed)."""
        return await self._store.upsert_tool(
            name,
            description=description,
            category=category,
            source_id=server,
            schema=schema,
            enabled=None,
            status="loaded" if loaded else "unloaded",
        )

    async def sync_server_status(
        self, name: str, *, description: str = "", enabled: bool = True,
        runtime: str = "DISCONNECTED", error_reason: str = "",
        source: str | None = None,
    ) -> None:
        """Mirror a server's runtime/enabled (and provenance) into the table."""
        await self._store.upsert_server(
            name, description=description, enabled=enabled,
            runtime=runtime, error_reason=error_reason, source=source,
        )

    async def mark_all_servers_down(self) -> int:
        """The gateway child died — every external server it managed is
        unreachable.  Mark all server rows DISCONNECTED so the effective-status
        join drops their tools from injection immediately; the restart
        reconcile restores."""
        if not self.write_owner:
            return 0
        return await self._store.mark_all_servers_down()

    async def sync_config_servers(self, servers: dict) -> list[str]:
        """db ← tools.json5 at startup — tools.json5 IS the authoritative config.

        Covers BOTH hand-edits and agent-tool edits to the mcp/rest-api
        sections: every configured server gets a mirror row (enabled + source
        from the config, runtime preserved from the persisted row or
        DISCONNECTED), and catalog rows for servers no longer configured are
        purged (cascade deletes their tool rows).  Returns the config-removed
        names.  Main-owner only.
        """
        if not self.write_owner:
            return []
        configured: set[str] = set()
        for name, entry in servers.items():
            if not isinstance(name, str) or not isinstance(entry, dict):
                continue
            configured.add(name)
            existing = await self._store.get_server(name)
            src = entry.get("source")
            source_json = json.dumps(src) if isinstance(src, dict) else None
            await self._store.upsert_server(
                name,
                description=str(entry.get("description", "") or ""),
                enabled=entry.get("enabled", True) is not False,
                runtime=(existing or {}).get("runtime") or "DISCONNECTED",
                error_reason=(existing or {}).get("error_reason") or "",
                source=source_json,
            )
        current = await self._store.list_server_names()
        purged: list[str] = []
        for name in sorted(current - configured):
            await self._store.remove_server(name)
            purged.append(name)
        if purged:
            logger.info("catalog_sync_purged_config_removed servers=%r", purged)
        return purged

    async def server_category(self, name: str) -> str:
        """A server-backed tool's catalog category (``mcp`` vs ``rest-api``).

        Derived from the mirrored provenance dict:
        ``source.type == "rest_api"`` ⇒ ``rest-api``, else ``mcp``.
        """
        row = await self._store.get_server(name)
        source = (row or {}).get("source")
        if source:
            try:
                s = json.loads(source)
                if isinstance(s, dict) and s.get("type") == "rest_api":
                    return "rest-api"
            except (ValueError, TypeError):
                pass
        return "mcp"


def _tool_name(tool: "Tool") -> str:
    return getattr(tool, "name", "") or ""


def _is_external(tool: "Tool") -> bool:
    from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

    return isinstance(tool, MCPProxyTool) and getattr(tool, "_route", None) == ProxyRoute.EXTERNAL