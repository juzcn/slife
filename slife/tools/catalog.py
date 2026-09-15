"""Unified tool catalog store — the single shared ``tools.db``.

The authoritative catalog for all six tool categories
(builtin/job/mcp/rest-api/skill/cli).  Lives on disk (data dir, WAL) and is
opened by every agent process (main agent + subagent workers) at the same
path; SQLite gives concurrent readers + one writer, ``busy_timeout``
backs off the short load/unload writes.  Ops are deliberately store-shaped
(upserts, search, LRU, embed drainer contract) — policy lives in
:mod:`slife.tools.catalog_service`.

State model (see DESIGNER_NOTES §8.5): ``tool.status`` holds only
``loaded | unloaded | NULL`` (NULL for skill/cli — no load concept).
``error``/``disabled`` are NEVER stored — they are derived at query time by
joining the server row (:func:`_effective_status`).  ``server.runtime`` is
a mirror of the mcp wrapper's connection state; the host records, it never
reconnects.  Only writes: status flips (load/unload) and one session-start
snapshot of ``last_runtime``; eviction is the main agent's job.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
from pathlib import Path
from typing import Any

import aiosqlite

from slife.plugins.memdb.store import (
    _clamp_limit,
    _contains_cjk,
    _like_escape,
    _serialize_f32,
    _split_sql,
    _to_fts5_query,
    in_placeholders,
)
from slife.timeutil import now_local_seconds
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)

#: Server-backed categories — their ``enabled``/connectivity come from the
#: ``server`` row, not the ``tool`` row.
SERVER_CATEGORIES = frozenset({"mcp", "rest-api"})

#: Function-tool categories — the only ones with a load/unload status.
FUNCTION_CATEGORIES = frozenset({"builtin", "job", "mcp", "rest-api"})

# Effective status labels (derived, never stored).
EFF_DISABLED = "disabled"
EFF_UNAVAILABLE = "unavailable"
EFF_NA = "n/a"

#: Local ISO-seconds timestamp — the shared store convention.
_now = now_local_seconds


def _deserialize_f32(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance (1 - cosine similarity) between two vectors."""
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 1.0
    return max(0.0, min(2.0, 1.0 - dot / math.sqrt(na * nb)))


# ── Schema → text (semantic doc source) ─────────────────────────────

def _compact_schema(value: Any) -> str:
    """Compact JSON text of a tool's descriptor (dict/list or JSON str)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return ""


def _fold_children(spec: dict) -> str:
    """One level of nested parameter text (object properties / array items)."""
    schemas = None
    if spec.get("type") == "object":
        schemas = spec.get("properties")
    elif spec.get("type") == "array":
        items = spec.get("items")
        schemas = items.get("properties") if isinstance(items, dict) else None
    if not isinstance(schemas, dict):
        return ""
    parts = []
    for cname, cprop in schemas.items():
        if not isinstance(cprop, dict):
            continue
        ctype = cprop.get("type", "")
        cdesc = str(cprop.get("description", "")).strip()
        head = f"{cname} ({ctype})" if ctype else cname
        parts.append(f"{head}: {cdesc}" if cdesc else head)
    return "; ".join(parts)


def _param_line(name: str, spec: dict, required: bool) -> str:
    """One parameter as readable text: ``name (type, required): desc``."""
    ptype = str(spec.get("type", ""))
    desc = str(spec.get("description", "")).strip()
    children = _fold_children(spec)
    if children:
        desc = f"{desc} {children}".strip() if desc else children
    head = name
    if ptype:
        head += f" ({ptype}{', required' if required else ''})"
    elif required:
        head += " (required)"
    return f"{head}: {desc}" if desc else head


def _flatten_schema(schema_text: str) -> str:
    """Single flat readable text of a tool's ``schema`` column — the semantic doc.

    The column stores the complete tool descriptor as compact JSON
    (``{name, description, inputSchema}``) for function tools, so the doc
    covers name + description + each parameter + a return description.
    Skill rows hold SKILL.md text and simply flatten to "" (never embedded).
    """
    try:
        doc = json.loads(schema_text or "")
    except (ValueError, TypeError):
        return ""
    if not isinstance(doc, dict):
        return ""

    name = doc.get("name")
    description = doc.get("description")
    inner = doc.get("inputSchema") if isinstance(doc.get("inputSchema"), dict) else doc
    if not isinstance(inner, dict):
        inner = {}

    lines: list[str] = []
    if isinstance(name, str) and name.strip():
        lines.append(f"name: {name.strip()}")
    if isinstance(description, str) and description.strip():
        lines.append(description.strip())

    required_raw = inner.get("required")
    required = set(required_raw) if isinstance(required_raw, list) else set()
    props = inner.get("properties")
    if isinstance(props, dict):
        params = [
            _param_line(pname, spec, pname in required)
            for pname, spec in props.items() if isinstance(spec, dict)
        ]
        if params:
            lines.append("params: " + "; ".join(params))

    for key in ("returns", "return", "result", "response"):
        ret = inner.get(key)
        if ret is None:
            continue
        ret_desc = ret.get("description") if isinstance(ret, dict) else ret
        if isinstance(ret_desc, str) and ret_desc.strip():
            lines.append(f"returns: {ret_desc.strip()}")
            break
    return "\n".join(lines)


def _effective_status(trow: dict, srow: dict | None) -> str:
    """Derived effective status (never stored): DISABLED/UNAVAILABLE/loaded/unloaded.

    Priority per the DESIGNER_NOTES §8.5 state model: a server-backed tool's
    ``enabled`` cascade comes from the server row (config wins over fault);
    a function tool's runtime state is ``loaded``/``unloaded``; skill/cli
    rows have no load concept and report ``n/a``.
    """
    cat = trow.get("category", "")
    if cat in SERVER_CATEGORIES:
        if srow is None:                       # no server row — not connectable
            return EFF_UNAVAILABLE
        if srow.get("enabled") != 1:
            return EFF_DISABLED
        if srow.get("runtime") != "CONNECTED":
            return EFF_UNAVAILABLE
    elif trow.get("enabled") == 0:
        return EFF_DISABLED
    status = trow.get("status")
    return status if status else EFF_NA


def effective_from_row(row: dict) -> str:
    """Effective status for a search/scan row (which carries ``s_*`` aliases)."""
    srow = None
    if row.get("category", "") in SERVER_CATEGORIES and "s_enabled" in row:
        srow = {
            "enabled": row.get("s_enabled"),
            "runtime": row.get("s_runtime"),
        }
    return _effective_status(row, srow)


# Row-join prefix used by the scan/search queries (server fields aliased).
_SCAN_COLS = (
    "t.name, t.description, t.category, t.source_id, t.schema, t.enabled, "
    "t.status, t.last_loaded, "
    "s.enabled AS s_enabled, s.runtime AS s_runtime, s.error_reason AS s_error"
)


class CatalogStore:
    """The shared catalog: SQLite file, WAL, FTS5 keyword + BLOB semantic.

    One instance per process (main agent / subagent / tests) on the same
    path.  Writers serialize on a per-instance ``asyncio.Lock`` (the WAL
    reader/writer split handles cross-process contention).  All ops are
    store-shaped; policy (whitelist, thresholds, refusal texts) is applied
    by :class:`~slife.tools.catalog_service.ToolCatalogService`.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._write_lock = asyncio.Lock()

    @property
    def _c(self) -> aiosqlite.Connection:
        assert self._conn is not None
        return self._conn

    # ── Lifecycle ──────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the file db with the WAL cross-process pragmas + schema."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(self._path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")
        busy_ms = int(_timeouts.timeouts.storage.sqlite_busy * 1000)
        await conn.execute(f"PRAGMA busy_timeout={busy_ms}")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA user_version")
        self._conn = conn
        await self._run_schema()
        cursor = await self._c.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = row[0] if row else 0
        if version != 1:
            logger.warning("catalog_schema_version_unknown version=%s", version)
        logger.info("catalog_ready path=%s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _run_schema(self) -> None:
        schema_path = Path(__file__).parent / "catalog_schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        # Execute each statement individually — FTS/trigger virtual tables can
        # hang in aiosqlite's executescript.
        for stmt in _split_sql(schema_sql):
            stmt = stmt.strip()
            if not stmt:
                continue
            try:
                await self._c.execute(stmt)
            except Exception as e:
                logger.error("catalog_schema_stmt_error err=%s stmt=%.80s", e, stmt)
        await self._c.commit()

    # ── Upserts ────────────────────────────────────────────────────

    async def upsert_tool(
        self,
        name: str,
        *,
        description: str = "",
        category: str,
        source_id: str | None = None,
        schema: str | None = None,
        enabled: bool | None = None,
        status: str | None = None,
    ) -> bool:
        """Upsert a tool row; returns True iff the ``schema`` text changed.

        ``status`` is only applied to a NEW row — an existing row keeps its
        loaded/unloaded state (a plugin re-register must not clobber a user
        unload).  ``enabled`` is only applied when not None (mcp/rest-api
        upserts pass None and keep the column NULL).  A schema edit drops the
        stale embedding row so the drainer re-embeds; keyword search stays
        live through the FTS5 update trigger.
        """
        async with self._write_lock:
            prev = await self._c.execute(
                "SELECT schema FROM tool WHERE name = ?", (name,),
            )
            prev_row = await prev.fetchone()
            prev_schema = prev_row[0] if prev_row is not None else None
            schema_changed = ((prev_schema or "") != (schema or ""))
            new_row = prev_row is None
            await self._c.execute(
                """INSERT INTO tool(name, description, category, source_id, schema,
                                    enabled, status, last_loaded)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       description = excluded.description,
                       category    = excluded.category,
                       source_id   = excluded.source_id,
                       schema      = excluded.schema,
                       enabled     = COALESCE(excluded.enabled, tool.enabled)
                """,
                (name, description, category, source_id, schema,
                 (1 if enabled else 0) if enabled is not None else None,
                 status if new_row else None, None),
            )
            if new_row or schema_changed:
                await self._c.execute(
                    "DELETE FROM tool_embeddings WHERE name = ?", (name,),
                )
            await self._c.commit()
        return schema_changed

    async def upsert_server(
        self,
        name: str,
        *,
        description: str = "",
        enabled: bool = True,
        runtime: str = "DISCONNECTED",
        error_reason: str = "",
        source: str | None = None,
    ) -> None:
        """Upsert the server row (runtime/error mirror of the wrapper pool).

        ``enabled`` mirrors tools.json5 — the host never flips it directly
        (enable/disable flow through the wrapper's ``mcp_set_enabled``).
        ``last_runtime`` is owned by the session-start snapshot, not here.
        ``source`` is the provenance dict JSON (``source.type == "rest_api"``
        distinguishes the rest-api category).
        """
        async with self._write_lock:
            await self._c.execute(
                """INSERT INTO server(name, description, enabled, runtime,
                                      error_reason, source)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       description  = excluded.description,
                       enabled      = excluded.enabled,
                       runtime      = excluded.runtime,
                       error_reason = excluded.error_reason,
                       source       = excluded.source
                """,
                (name, description, 1 if enabled else 0, runtime, error_reason, source),
            )
            await self._c.commit()

    async def remove_server(self, server: str) -> int:
        """Delete the server row (tool rows cascade). Returns tools removed."""
        async with self._write_lock:
            cursor = await self._c.execute(
                "SELECT COUNT(*) FROM tool WHERE source_id = ?", (server,),
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
            await self._c.execute(
                "DELETE FROM tool WHERE source_id = ?", (server,),
            )
            await self._c.execute(
                "DELETE FROM server WHERE name = ?", (server,),
            )
            await self._c.commit()
        logger.info("catalog_server_removed server=%s tools=%d", server, count)
        return count

    async def session_start(self) -> None:
        """Snapshot current runtimes into ``last_runtime`` — the eager-connect
        set for the NEXT session is whatever was live at THIS session's start."""
        async with self._write_lock:
            await self._c.execute("UPDATE server SET last_runtime = runtime")
            await self._c.commit()

    # ── Status flips ───────────────────────────────────────────────

    async def set_status(self, name: str, status: str | None, *, bump: bool = False) -> int:
        """Set a tool's ``status`` (loaded/unloaded/NULL); bump → LRU refresh.

        Only meaningful for function-tool rows (skill/cli stay NULL); the
        store is lenient — callers guard with the category.
        """
        last_loaded = _now() if (bump and status == "loaded") else None
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET status = ?, last_loaded = "
                "CASE WHEN ? IS NULL THEN last_loaded ELSE ? END"
                " WHERE name = ?",
                (status, last_loaded, last_loaded, name),
            )
            await self._c.commit()
        return cursor.rowcount

    async def touch(self, name: str) -> None:
        """Bump a loaded tool's ``last_loaded`` (LRU recency) on successful use.

        ``evict_lru`` orders NULL-first then by ``last_loaded``; without a
        per-use bump every seeded tool stays NULL and eviction degrades to a
        plain alphabetical slice (never the least-recently-used).  Only
        touched when the row is still ``loaded`` so a call made after an
        eviction never re-arms an evicted tool.
        """
        async with self._write_lock:
            await self._c.execute(
                "UPDATE tool SET last_loaded = ?"
                " WHERE name = ? AND status = 'loaded'",
                (_now(), name),
            )
            await self._c.commit()

    async def set_enabled(self, name: str, enabled: bool | None) -> int:
        """Set a local category's ``enabled`` (mcp/rest-api pass None → noop)."""
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET enabled = ? WHERE name = ?",
                (1 if enabled else 0) if enabled is not None else None,
                (name,),
            )
            await self._c.commit()
        return cursor.rowcount

    # ── Reads / effective status ───────────────────────────────────

    async def rows_for_names(self, names) -> list[dict]:
        """Catalog rows (name + schema) for *names* — the injection schema
        source.  The loop builds its LLM tool list from this column, so the
        catalog (not tool code, not a live MCP fetch) owns the injected
        schemas."""
        names = [n for n in names]
        if not names:
            return []
        ph = in_placeholders(len(names))
        cursor = await self._c.execute(
            f"SELECT name, schema FROM tool WHERE name IN ({ph})", names,
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_tool(self, name: str) -> dict | None:
        cursor = await self._c.execute(
            "SELECT name, description, category, source_id, schema, enabled, "
            "status, last_loaded FROM tool WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_server(self, name: str) -> dict | None:
        cursor = await self._c.execute(
            "SELECT name, description, enabled, runtime, error_reason, "
            "last_runtime, source FROM server WHERE name = ?",
            (name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_server_names(self) -> set[str]:
        cursor = await self._c.execute("SELECT name FROM server")
        return {row[0] for row in await cursor.fetchall()}

    async def mark_all_servers_down(self) -> int:
        """Set every server's runtime to DISCONNECTED — used when the gateway
        child dies (every external server it managed is unreachable).  The
        effective-status join then drops their tools immediately; a restart
        reconcile restores.  Returns rows touched."""
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE server SET runtime = 'DISCONNECTED'",
            )
            await self._c.commit()
        return cursor.rowcount

    async def get_effective(self, name: str) -> str | None:
        """Effective status for one tool name (None if unknown)."""
        row = await self.get_tool(name)
        if row is None:
            return None
        server = None
        if row.get("category") in SERVER_CATEGORIES:
            server = await self.get_server(row.get("source_id") or "")
        return _effective_status(row, server)

    async def scan_effective(
        self, category: str = "",
    ) -> list[dict]:
        """Every tool row joined with its server, annotated with effective status.

        Returns rows: ``name/description/category/source_id/schema/status/
        eff/source_id/runtime/enabled_eff`` — the display/search shape.
        """
        clauses = ""
        params: list = []
        if category:
            clauses = "WHERE t.category = ?"
            params.append(category)
        cursor = await self._c.execute(
            f"SELECT {_SCAN_COLS} FROM tool t "
            f"LEFT JOIN server s ON s.name = t.source_id {clauses} "
            f"ORDER BY t.category, t.name",
            params,
        )
        rows = []
        for row in await cursor.fetchall():
            r = dict(row)
            srow = None
            if r.get("category") in SERVER_CATEGORIES:
                srow = {
                    "enabled": r.pop("s_enabled"),
                    "runtime": r.pop("s_runtime"),
                    "error_reason": r.pop("s_error"),
                }
            else:
                r.pop("s_enabled", None)
                r.pop("s_runtime", None)
                r.pop("s_error", None)
            r["eff"] = _effective_status(r, srow)
            r["runtime"] = (srow or {}).get("runtime")
            rows.append(r)
        return rows

    async def loaded_names(self) -> list[str]:
        """Injectable tool names: effective status == loaded.

        The join hides (a) server-backed tools whose server isn't enabled or
        connected, and (b) config-disabled local tools — without writing any
        status.  ``loaded`` rows are exactly the LLM tool-list names
        (whitelist is added by the service/loop).
        """
        cursor = await self._c.execute(
            """SELECT t.name FROM tool t
               LEFT JOIN server s ON s.name = t.source_id
               WHERE t.status = 'loaded'
                 AND (t.category NOT IN ('mcp','rest-api')
                      OR (s.enabled = 1 AND s.runtime = 'CONNECTED'))
                 AND (t.category IN ('mcp','rest-api')
                      OR t.enabled IS NULL OR t.enabled = 1)
               ORDER BY t.name"""
        )
        return [row[0] for row in await cursor.fetchall()]

    async def count_loaded(self) -> int:
        cursor = await self._c.execute(
            """SELECT COUNT(*) FROM tool t
               LEFT JOIN server s ON s.name = t.source_id
               WHERE t.status = 'loaded'
                 AND (t.category NOT IN ('mcp','rest-api')
                      OR (s.enabled = 1 AND s.runtime = 'CONNECTED'))
                 AND (t.category IN ('mcp','rest-api')
                      OR t.enabled IS NULL OR t.enabled = 1)"""
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def evict_lru(self, limit: int, protected: frozenset | set = frozenset()) -> list[str]:
        """Evict the oldest-by-``last_loaded`` loaded tools, down to best effort.

        Evicts up to *limit* rows (the caller bounds it to the over-threshold
        excess).  ``protected`` (the meta whitelist) is never evicted.  Rows
        with a NULL ``last_loaded`` sort oldest (evict first).  Returns the
        evicted names (they now have ``status='unloaded'``).
        """
        limit = max(0, limit)
        if limit == 0:
            return []
        protected_expr = ""
        params: list = [limit]
        if protected:
            protected_expr = f" AND name NOT IN ({in_placeholders(len(protected))})"
            params = [*sorted(protected), limit]
        async with self._write_lock:
            cursor = await self._c.execute(
                f"""SELECT name FROM tool
                    WHERE status = 'loaded'{protected_expr}
                    ORDER BY (last_loaded IS NULL) DESC, last_loaded ASC, name
                    LIMIT ?""",
                params,
            )
            names = [row[0] for row in await cursor.fetchall()]
            if names:
                ph = in_placeholders(len(names))
                await self._c.execute(
                    f"UPDATE tool SET status = 'unloaded'"
                    f" WHERE name IN ({ph}) AND status = 'loaded'",
                    names,
                )
            await self._c.commit()
        if names:
            logger.info("catalog_evict count=%d names=%r", len(names), names)
        return names

    # ── Search ─────────────────────────────────────────────────────

    async def search_keyword(
        self, query: str, limit: int = 20, category: str = "",
    ) -> list[dict]:
        """FTS5 keyword search, CJK-routed to :meth:`_search_like`.

        Cross-category (no server-join gate; effective status is layered on
        by the caller via ``scan_effective``/``eff_map``).
        """
        limit = _clamp_limit(limit)
        if _contains_cjk(query):
            return await self._search_like(query, limit=limit, category=category)
        fts_query = _to_fts5_query(query)
        clauses = ""
        params: list = [fts_query]
        if category:
            clauses += " AND t.category = ?"
            params.append(category)
        params.append(limit)
        try:
            cursor = await self._c.execute(
                f"""SELECT {_SCAN_COLS},
                          snippet(tool_fts, 1, '…', '…', '…', 40) AS snippet, rank
                   FROM tool_fts fts
                   JOIN tool t ON fts.rowid = t.rowid
                   LEFT JOIN server s ON s.name = t.source_id
                   WHERE tool_fts MATCH ?{clauses}
                   -- bm25 weights (fts cols: name, description, category,
                   -- source_id, schema): name dominates, schema generic JSON
                   -- words (string/array) get 0.5.
                   ORDER BY bm25(tool_fts, 5.0, 2.0, 1.0, 1.0, 0.5) LIMIT ?""",
                params,
            )
            return [dict(row) for row in await cursor.fetchall()]
        except aiosqlite.OperationalError as e:
            logger.debug("catalog_search_keyword_parse_error query=%s err=%s", query, e)
            return []

    async def _search_like(
        self, pattern: str, limit: int,
        category: str = "",
    ) -> list[dict]:
        """Substring (LIKE) search — CJK fallback for :meth:`search_keyword`.

        Space-split words all must match (AND semantics) across name /
        description / category / schema; each CJK word matches by substring.
        """
        words = [w for w in pattern.split() if w]
        if not words:
            return []
        and_clauses: list[str] = []
        params: list = [words[0]]  # instr context anchors on the first word
        for w in words:
            safe = _like_escape(w)
            like = f"%{safe}%"
            and_clauses.append(
                "(t.name LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\'"
                " OR t.category LIKE ? ESCAPE '\\' OR t.schema LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like, like])
        where = " AND ".join(and_clauses)
        if category:
            where += " AND t.category = ?"
            params.append(category)
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT {_SCAN_COLS},
                      substr(t.description, max(0, instr(t.description, ?) - 40), 160) AS snippet,
                      0 AS rank
               FROM tool t
               LEFT JOIN server s ON s.name = t.source_id
               WHERE {where}
               ORDER BY t.category, t.name LIMIT ?""",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def search_grep(
        self, pattern: str, limit: int = 20, category: str = "",
    ) -> list[dict]:
        """Exact substring search over the catalog columns."""
        limit = _clamp_limit(limit)
        safe = _like_escape(pattern)
        like_pattern = f"%{safe}%"
        clauses = ""
        params: list = [
            pattern, like_pattern, like_pattern, like_pattern, like_pattern,
        ]
        if category:
            clauses += " AND t.category = ?"
            params.append(category)
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT {_SCAN_COLS},
                      substr(t.description, max(0, instr(t.description, ?) - 40), 160) AS snippet,
                      0 AS rank
               FROM tool t
               LEFT JOIN server s ON s.name = t.source_id
               WHERE (t.name LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\'
                      OR t.category LIKE ? ESCAPE '\\' OR t.schema LIKE ? ESCAPE '\\')
                     {clauses}
               ORDER BY t.category, t.name LIMIT ?""",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def search_semantic(
        self, vec: list[float], limit: int = 20, category: str = "",
    ) -> list[dict]:
        """Brute-force cosine KNN over stored BLOB vectors — one row per tool.

        A long schema's chunks are scored by their closest chunk, aggregated
        back to one row per tool.  Vectors whose width differs from *vec*
        are skipped (stale rows from a previous embedding model).
        """
        limit = _clamp_limit(limit)
        clauses = ""
        params: list = []
        if category:
            clauses = " AND t.category = ?"
            params.append(category)
        cursor = await self._c.execute(
            f"""SELECT t.name, t.description, t.category, t.source_id, t.schema,
                      t.enabled, t.status, t.last_loaded,
                      s.enabled AS s_enabled, s.runtime AS s_runtime,
                      s.error_reason AS s_error,
                      te.embedding
               FROM tool_embeddings te
               JOIN tool t ON te.name = t.name
               LEFT JOIN server s ON s.name = t.source_id
               WHERE 1=1{clauses}""",
            params,
        )
        best: dict[str, dict] = {}
        for row in await cursor.fetchall():
            r = dict(row)
            stored = _deserialize_f32(r.pop("embedding"))
            if len(stored) != len(vec):
                continue
            r["distance"] = _cosine_distance(vec, stored)
            cur = best.get(r["name"])
            if cur is None or r["distance"] < cur["distance"]:
                best[r["name"]] = r
        results = sorted(best.values(), key=lambda x: x["distance"])[:limit]
        logger.debug("catalog_search_semantic hits=%s", len(results))
        return results

    # ── Embedding drainer contract (one document = one tool) ───────

    async def count_unembedded(self) -> int:
        """Tools with no embedding chunk and a non-empty flattenable schema."""
        cursor = await self._c.execute(
            """SELECT schema FROM tool
               WHERE name NOT IN (SELECT DISTINCT name FROM tool_embeddings)"""
        )
        return sum(
            1 for (schema_text,) in await cursor.fetchall()
            if _flatten_schema(schema_text or "").strip()
        )

    async def get_unembedded_docs(self, limit: int = 100) -> list[dict]:
        """Drainer docs: ``doc_id`` = tool ``name``, ``text`` = flattened schema."""
        cursor = await self._c.execute(
            """SELECT name, schema FROM tool
               WHERE name NOT IN (SELECT DISTINCT name FROM tool_embeddings)
               ORDER BY name
               LIMIT ?""",
            (limit,),
        )
        docs = []
        for row in await cursor.fetchall():
            text = _flatten_schema(row[1] or "")
            if not text.strip():
                continue
            docs.append({"doc_id": row[0], "text": text})
        return docs

    async def replace_embedding_chunks(
        self, doc: dict, embeddings: list[list[float]], *, model: str = "",
    ) -> None:
        """Atomically replace one tool's embedding chunks (delete+insert, one tx)."""
        name = doc["doc_id"]
        if not model:
            model = (await self.get_meta("embedding_model")) or ""
        vec_blobs = [_serialize_f32(emb) for emb in embeddings]
        async with self._write_lock:
            try:
                await self._c.execute(
                    "DELETE FROM tool_embeddings WHERE name = ?", (name,),
                )
                for idx, blob in enumerate(vec_blobs):
                    await self._c.execute(
                        """INSERT INTO tool_embeddings(name, chunk_index, embedding, model)
                           VALUES (?, ?, ?, ?)""",
                        (name, idx, blob, model),
                    )
                await self._c.commit()
            except Exception:
                await self._c.rollback()
                raise
        logger.debug("catalog_embedding_chunks_replaced name=%s chunks=%d", name, len(vec_blobs))

    async def drop_embeddings(self) -> int:
        """Delete all embedding rows. Returns chunk rows deleted."""
        async with self._write_lock:
            cursor = await self._c.execute("SELECT COUNT(*) FROM tool_embeddings")
            row = await cursor.fetchone()
            count = row[0] if row else 0
            await self._c.execute("DELETE FROM tool_embeddings")
            await self._c.commit()
        logger.info("catalog_embeddings_dropped count=%d", count)
        return count

    async def count_embedded(self) -> int:
        cursor = await self._c.execute(
            "SELECT COUNT(DISTINCT name) FROM tool_embeddings",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Meta ───────────────────────────────────────────────────────

    async def get_meta(self, key: str) -> str | None:
        cursor = await self._c.execute(
            "SELECT value FROM meta WHERE key = ?", (key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self._c.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
                (key, value),
            )
            await self._c.commit()