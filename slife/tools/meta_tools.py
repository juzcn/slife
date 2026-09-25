"""Tool-system meta tools — the unified surface built on the catalog.

``tool_search``     — cross-category hybrid search over the shared catalog
``func_tool_load``  — load a function tool (flip status + materialize proxy)
``_func_tool_unload`` — unload a function tool by name (the harness also
                        evicts LRU at turn boundaries)

These are in the meta whitelist (``slife.tools.whitelist``) — always
injected, never evicted, not configurable.  ``func_tool_load`` is the ONLY
loader: the legacy ``mcp_tool_load`` alias is retired, its mcp/rest-api
materialization being this tool's own branch.
"""

from __future__ import annotations

import json
import re
import logging
from typing import TYPE_CHECKING, ClassVar

from slife.tools.base import Tool, make_params, require_params
from slife.tools.catalog import NA, effective_from_row
from slife.plugins.memdb.search import (  # the ONE hybrid-search implementation
    SCORE_BAND_HINT,
    annotate_scores,
    merge_hybrid,
)
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


def _schema_bytes(schema) -> int:
    """Descriptor size; the "no schema" sentinel reports 0 bytes.

    The column is NOT NULL and spells "no schema text" as ``'n/a'``, so a bare
    ``len()`` would report 3 bytes for every cli row — a size for something
    that has no size.
    """
    if not _has_schema(schema):
        return 0
    return len(schema)


def _has_schema(schema: str | None) -> bool:
    """True when a catalog row carries real schema text.

    "No schema" is stored as the ``'n/a'`` sentinel in a NOT NULL column, so a
    bare truthiness test reads the sentinel as a schema and hands ``'n/a'`` to
    ``json.loads`` — a loaded row then gets downgraded to "unloaded" with an
    "Expecting value" error.  Both readers here go through this one predicate.
    """
    return bool(schema) and schema != NA


class ToolSearchTool(Tool):
    """Cross-category catalog search (builtin/job/plugin/mcp/rest-api/skill/cli)."""

    name = "tool_search"
    category: ClassVar[str] = TOOL_META_CATEGORY
    description = (
        "Search the unified tool catalog across all six categories "
        "(builtin/job/plugin/mcp/rest-api/skill/cli). Returns name, category, source "
        "server, and effective status per tool — load a function tool with "
        "func_tool_load."
    )
    parameters = make_params(
        query={
            "type": "string",
            "description": "Search query (matches name/description/schema).",
        },
        # The filters ARE the catalog's columns — one parameter per column, so
        # the surface cannot drift from the table and every filter is a real
        # predicate the query sees.
        category={
            "type": "string",
            "description": "Filter by category (builtin|job|plugin|mcp|rest-api|skill|cli); empty = all.",
            "default": "",
        },
        source_id={
            "type": "string",
            "description": "Filter by owner: an mcp/rest-api server name, or a plugin name; empty = all.",
            "default": "",
        },
        status={
            "type": "string",
            "description": "Filter by status (enabled|disabled|error); empty = all.",
            "default": "",
        },
        load_status={
            "type": "string",
            "description": "Filter by load state (loaded|unloaded|n/a); empty = all.",
            "default": "",
        },
        mode={
            "type": "string",
            "description": "hybrid (semantic+keyword) | keyword (FTS5) | grep (regex).",
            "default": "hybrid",
        },
        limit={"type": "integer", "description": "Max results.", "default": 10},
    )

    async def execute(self, **kwargs) -> str:
        query: str = kwargs.get("query", "") or ""
        mode: str = kwargs.get("mode", "hybrid") or "hybrid"
        # One dict, straight from the parameters to the columns: absent and
        # empty mean "no filter" for every one of them (each column's values
        # are non-empty strings, so emptiness is unambiguous).
        filters = {
            key: kwargs.get(key)
            for key in ("category", "source_id", "status", "load_status")
            if kwargs.get(key) not in (None, "")
        }
        try:
            limit = max(1, min(int(kwargs.get("limit", 10) or 10), 50))
        except (TypeError, ValueError):
            limit = 10

        catalog, err = _require_catalog(_ctx(self), "search tools")
        if catalog is None:
            return err
        store = catalog.store

        hint = ""
        if not query.strip():
            # No query, no text to match: browse the catalog instead.  The
            # legs below would answer with whatever the semantic index holds
            # (unreachable for a row with no embedding) or nothing at all.
            results = await store.browse(limit=limit, filters=filters)
        elif mode == "grep":
            try:
                results = await store.search_grep(query, limit=limit, filters=filters)
            except re.error as e:
                return f"Error: invalid regex {query!r}: {e}"
        else:
            keyword_hits = await store.search_keyword(
                query, limit=limit * 2, filters=filters,
            )
            results = keyword_hits
            if mode == "hybrid":
                # The process's semantic surface: the drainer's own manager
                # where it runs one, otherwise a reader over the same shared
                # index.  Both answer ``query_ready`` / ``reason`` /
                # ``embed_query``, so a subagent gets real scores here instead
                # of a permanent keyword-only fallback.
                sem = getattr(catalog, "semantic_query", None)
                if sem is not None and await sem.query_ready():
                    emb = await sem.embed_query(query)
                    if emb:
                        semantic_hits = await store.search_semantic(
                            emb, limit=limit * 2, filters=filters,
                        )
                        results = merge_hybrid(
                            keyword_hits, semantic_hits, key_field="name",
                        )
                    else:
                        hint = sem.reason or "semantic search unavailable — keyword only."
                else:
                    hint = (
                        (sem.reason if sem is not None else "")
                        or "semantic search unavailable — keyword only."
                    )
        # The shared scoring contract, used by every other hybrid path
        # (turn_search, cabinet_search): a normalized 0-1 `similarity` per
        # result plus the band legend.  Without it this tool was the one place
        # where "nothing matched" and "the nearest neighbours are weak" looked
        # identical — a semantic leg always returns its k nearest, however far
        # away they are.
        if mode == "hybrid" and results:
            annotate_scores(results)
            hint = SCORE_BAND_HINT if not hint else f"{hint} · {SCORE_BAND_HINT}"

        rows = []
        for r in results:
            row = dict(r)
            entry = {
                "name": row.get("name", ""),
                "description": row.get("description", "").split(".")[0].strip()[:120],
                "category": row.get("category", ""),
                "source_id": row.get("source_id"),
                "schema_bytes": _schema_bytes(row.get("schema")),
                "status": effective_from_row(row),
            }
            # Only the semantic leg produces one — a keyword-only hit is not
            # scored, and inventing a number for it would be a lie.
            if row.get("similarity") is not None:
                entry["similarity"] = round(float(row["similarity"]), 3)
            rows.append(entry)

        # The filters ran in SQL, so this truncation only trims the ranked
        # result — it can no longer drop qualifying rows that a post-hoc
        # filter would have had to look past the candidate cutoff to find.
        rows = rows[:limit]
        payload = {"count": len(rows), "results": rows}
        if hint:
            payload["hint"] = hint
        return json.dumps(payload, ensure_ascii=False, indent=2)


class FuncToolLoadTool(Tool):
    """Load a function tool into the LLM tool list (status flip + materialize)."""

    name = "func_tool_load"
    category: ClassVar[str] = TOOL_META_CATEGORY
    description = (
        "Load a function tool (builtin/job/plugin/mcp/rest-api) into the LLM's "
        "tool list by name (find it with tool_search). The tool is in the list "
        "from the next request. Server-backed tools need their server connected."
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
                await catalog.store.set_load_status(full_name, "unloaded")
                return f"Error: '{full_name}' loaded but the MCP client is unavailable — no execution route."
            if not _has_schema(row.get("schema")):
                await catalog.store.set_load_status(full_name, "unloaded")
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
                await catalog.store.set_load_status(full_name, "unloaded")
                logger.warning("tool_load_materialize_failed name=%s err=%s", full_name, e)
                return f"Error: failed to materialize '{full_name}': {e}"
        return msg


class FuncToolUnloadTool(Tool):
    """Unload a function tool by name (the harness evicts LRU at boundaries)."""

    name = "_func_tool_unload"
    category: ClassVar[str] = TOOL_META_CATEGORY
    description = "Unload a function tool (builtin/job/plugin/mcp/rest-api) by name."
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
        # Drop the EXTERNAL proxy (it holds a live client), so unloading one of
        # those also takes its route away; stateless builtin tools / plugin
        # proxies stay in the execution pool and remain callable.  Unloading is
        # an injection operation, not a capability switch.
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