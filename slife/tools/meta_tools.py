"""Tool-system meta tools — the unified, catalog-native surface.

``tool_search``     — cross-category hybrid search over the shared catalog
``func-tool-load``  — load a function tool (flip status + materialize proxy)
``_unload_func_tool`` — unload a function tool (self-service; the harness
                        also evicts LRU at turn boundaries)

These are in the meta whitelist (``slife.tools.whitelist``) — always
injected, never evicted, not configurable.  The legacy ``mcp_tool_load``
delegates here (its mcp/rest-api branch) so old callers and subagents keep
working during the wrapper retirement.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, ClassVar

from slife.tools.base import Tool, make_params, require_params
from slife.tools.catalog import effective_from_row
from slife.tools.catalog_search import merge_hybrid
from slife.tools.whitelist import TOOL_META_CATEGORY

if TYPE_CHECKING:
    from slife.tools.catalog_service import ToolCatalogService

logger = logging.getLogger(__name__)


def _ctx(self_or_none):
    """The tool's runtime context (set by ``Tool.from_config``); None-safe."""
    return getattr(self_or_none, "_ctx", None) if self_or_none is not None else None


def _catalog(ctx) -> "ToolCatalogService | None":
    return getattr(ctx, "catalog", None) if ctx is not None else None


def _require_catalog(
    ctx, what: str,
) -> tuple["ToolCatalogService | None", str]:
    catalog = _catalog(ctx)
    if catalog is None:
        return None, f"Error: the tool catalog is not available — cannot {what}."
    return catalog, ""


class ToolSearchTool(Tool):
    """Cross-category catalog search (builtin/job/mcp/rest-api/skill/cli)."""

    name = "tool_search"
    category: ClassVar[str] = TOOL_META_CATEGORY
    description = (
        "Search the unified tool catalog across all six categories "
        "(builtin/job/mcp/rest-api/skill/cli). Returns name, category, source "
        "server, and effective status per tool — load a function tool with "
        "func-tool-load."
    )
    parameters = make_params(
        query={
            "type": "string",
            "description": "Search query (matches name/description/schema).",
        },
        category={
            "type": "string",
            "description": "Filter by category (builtin|job|mcp|rest-api|skill|cli); empty = all.",
            "default": "",
        },
        status={
            "type": "string",
            "description": "Filter by effective status: all|loaded|unloaded|disabled|unavailable.",
            "default": "all",
        },
        mode={
            "type": "string",
            "description": "hybrid (semantic+keyword) | keyword | grep.",
            "default": "hybrid",
        },
        limit={"type": "integer", "description": "Max results.", "default": 10},
    )

    async def execute(self, **kwargs) -> str:
        query: str = kwargs.get("query", "") or ""
        category: str = kwargs.get("category", "") or ""
        status: str = kwargs.get("status", "all") or "all"
        mode: str = kwargs.get("mode", "hybrid") or "hybrid"
        try:
            limit = max(1, min(int(kwargs.get("limit", 10) or 10), 50))
        except (TypeError, ValueError):
            limit = 10

        catalog, err = _require_catalog(_ctx(self), "search tools")
        if catalog is None:
            return err
        store = catalog.store

        hint = ""
        if mode == "grep":
            results = await store.search_grep(query, limit=limit, category=category)
        else:
            keyword_hits = await store.search_keyword(
                query, limit=limit * 2, category=category,
            )
            results = keyword_hits
            if mode == "hybrid":
                manager = getattr(catalog, "semantic_manager", None)
                if manager is not None and manager.semantic_ready:
                    embedder = manager.embedder
                    if embedder is not None and embedder.available:
                        emb = await embedder.embed_one(query)
                        if emb:
                            semantic_hits = await store.search_semantic(
                                emb, limit=limit * 2, category=category,
                            )
                            results = merge_hybrid(
                                keyword_hits, semantic_hits, key_field="name",
                            )
                    else:
                        hint = (manager.reason or
                                "semantic search unavailable — keyword only.")
                else:
                    hint = "semantic search unavailable — keyword only."
        results = results[:limit]

        rows = []
        for r in results:
            row = dict(r)
            eff = effective_from_row(row)
            if status != "all" and eff != status:
                continue
            rows.append({
                "name": row.get("name", ""),
                "description": row.get("description", "").split(".")[0].strip()[:120],
                "category": row.get("category", ""),
                "source_id": row.get("source_id"),
                "schema_bytes": len(row.get("schema") or ""),
                "status": eff,
            })

        payload = {"count": len(rows), "results": rows}
        if hint:
            payload["hint"] = hint
        return json.dumps(payload, ensure_ascii=False, indent=2)


class FuncToolLoadTool(Tool):
    """Load a function tool into the LLM tool list (status flip + materialize)."""

    name = "func-tool-load"
    category: ClassVar[str] = TOOL_META_CATEGORY
    description = (
        "Load a function tool (builtin/job/mcp/rest-api) into the LLM's tool "
        "list by name (find it with tool_search). Server-backed tools need "
        "their server connected."
    )
    parameters = make_params(
        full_name={
            "type": "string",
            "description": "Tool name; mcp/rest-api use '{server}__{tool}'.",
        },
    )

    async def execute(self, **kwargs) -> str:
        full_name: str = kwargs.get("full_name", "") or ""
        if err := require_params(full_name=full_name):
            return err
        ctx = _ctx(self)
        catalog, err = _require_catalog(ctx, "load tools")
        if catalog is None:
            return err
        registry = getattr(ctx, "registry", None)
        mcp = getattr(ctx, "mcp_client", None)

        row = (await catalog.store.get_tool(full_name)) or {}
        cat = row.get("category", "")

        # Gate first (refusals need no side effects), then flip status, then
        # materialize the execution instance for server-backed tools.
        ok, msg = await catalog.load_tool(full_name)
        if not ok:
            return msg

        if cat in ("mcp", "rest-api"):
            if registry is None or mcp is None:
                await catalog.store.set_status(full_name, "unloaded")
                return f"Error: '{full_name}' loaded but the MCP client is unavailable — no execution route."
            if not row.get("schema"):
                await catalog.store.set_status(full_name, "unloaded")
                return (
                    f"Error: '{full_name}' schema isn't synced yet — ensure its "
                    f"server is connected, then retry."
                )
            try:
                from slife.mcp.tool_adapter import create_proxy_tools

                desc = json.loads(row["schema"])
                proxy = create_proxy_tools(mcp, [{
                    "server": row["source_id"],
                    "name": desc.get("name") or "",
                    "description": desc.get("description", ""),
                    "inputSchema": desc.get("inputSchema", {"type": "object", "properties": {}}),
                }])[0]
                registry.register(proxy)
                logger.info("tool_load_materialized name=%s", full_name)
            except Exception as e:
                await catalog.store.set_status(full_name, "unloaded")
                logger.warning("tool_load_materialize_failed name=%s err=%s", full_name, e)
                return f"Error: failed to materialize '{full_name}': {e}"
        return msg


class UnloadFuncTool(Tool):
    """Unload a function tool (self-service; eviction is the harness's LRU)."""

    name = "_unload_func_tool"
    category: ClassVar[str] = TOOL_META_CATEGORY
    description = (
        "Unload a function tool (builtin/job/mcp/rest-api) from the loaded set "
        "by name, freeing a slot in the tool list. Whitelisted tools stay loaded."
    )
    parameters = make_params(
        full_name={
            "type": "string",
            "description": "Tool name to unload.",
        },
    )

    async def execute(self, **kwargs) -> str:
        full_name: str = kwargs.get("full_name", "") or ""
        if err := require_params(full_name=full_name):
            return err
        ctx = _ctx(self)
        catalog, err = _require_catalog(ctx, "unload tools")
        if catalog is None:
            return err
        ok, msg = await catalog.unload_tool(full_name)
        if not ok:
            return msg
        # Drop the EXTERNAL proxy (it holds a live client); stateless natives /
        # plugin proxies stay in the execution pool — the A4 gate refuses calls.
        registry = getattr(ctx, "registry", None)
        if registry is not None:
            from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

            tool = registry.get(full_name)
            if (
                isinstance(tool, MCPProxyTool)
                and getattr(tool, "_route", None) == ProxyRoute.EXTERNAL
            ):
                registry.unregister(full_name)
        return msg