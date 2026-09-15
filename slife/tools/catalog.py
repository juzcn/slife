"""Unified tool catalog store — the single shared ``tools.db``.

The authoritative catalog for all six tool categories
(builtin/job/mcp/rest-api/skill/cli).  Lives on disk (data dir, WAL) and is
opened by every agent process (main agent + subagent workers) at the same
path; SQLite gives concurrent readers + one writer, ``busy_timeout``
backs off the short load/unload writes.  Ops are deliberately store-shaped
(upserts, search, LRU, embed drainer contract) — policy lives in
:mod:`slife.tools.catalog_service`.

State model (see DESIGNER_NOTES §8.5): ``tool.status`` holds
``loaded | unloaded | error | NULL`` (NULL for skill/cli — no load concept),
and everything the injection gate needs is ON THE ROW.  ``error`` is the
connectivity verdict: whenever an external server is unusable — at startup
before it connects, on a disconnect, on a failed connect, when its gateway
child dies — the host marks that server's tools ``error``, and a successful
(re)connect resets them to their class default.  There is deliberately no
``server`` table: which servers to bring up lives in ``tools.json5``, what is
live right now lives in the gateway's pool, and this db only records the
result on the tool rows.  Writes: status flips (load/unload) and those
connectivity marks; eviction is the main agent's job.
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

#: Server-backed categories — a tool whose ``source_id`` names an external
#: server.  There is no ``server`` row to join: the tool's OWN ``status``
#: carries the connectivity verdict (``error`` when its server is down).
SERVER_CATEGORIES = frozenset({"mcp", "rest-api"})

#: Function-tool categories — the only ones with a load/unload status.
FUNCTION_CATEGORIES = frozenset({"builtin", "job", "mcp", "rest-api"})

# The stored status values for function tools.  ``error`` is the server-down
# verdict projected onto every tool of that server (see
# ``ToolCatalogService.mark_source_error``): a distinct state, NOT an unload —
# the row keeps saying "this tool belonged to a server that is not up".
STATUS_LOADED = "loaded"
STATUS_UNLOADED = "unloaded"
STATUS_ERROR = "error"

# Effective status labels (derived, never stored).
EFF_DISABLED = "disabled"
EFF_ERROR = "error"
EFF_NA = "n/a"

#: Schema revision this store expects (``catalog_schema.sql`` sets it; the
#: migration in :meth:`CatalogStore._migrate` moves an older file up to it).
SCHEMA_VERSION = 2

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


def _effective_status(trow: dict) -> str:
    """Derived effective status (never stored): disabled / error / loaded /
    unloaded / n/a.

    Everything it needs is ON THE ROW: a locally-disableable tool (builtin,
    job, skill, cli) reports ``disabled`` when its config mirror says so, and
    the rest is the row's own load state.  An external tool whose server went
    down was marked ``error`` by the reconcile, so the connectivity verdict
    arrives the same way every other fact does — no join, no server table.
    """
    if trow.get("category") not in SERVER_CATEGORIES and trow.get("enabled") == 0:
        return EFF_DISABLED
    status = trow.get("status")
    return status if status else EFF_NA


def effective_from_row(row: dict) -> str:
    """Effective status for a search/scan row (row-only — no server aliases)."""
    return _effective_status(row)


# Row prefix used by the scan/search queries.
_SCAN_COLS = (
    "t.name, t.description, t.category, t.source_id, t.schema, t.enabled, "
    "t.status, t.last_loaded"
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
        self._conn = conn
        await self._migrate()
        await self._run_schema()
        cursor = await self._c.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = row[0] if row else 0
        if version != SCHEMA_VERSION:
            logger.warning("catalog_schema_version_unknown version=%s", version)
        logger.info("catalog_ready path=%s", self._path)

    async def _migrate(self) -> None:
        """Bring an older db up to ``SCHEMA_VERSION`` before the schema runs.

        v2 dropped the ``server`` table: connection state lives on the tool
        rows now (``status='error'`` when a server is unusable), so the
        table is dead weight AND a stale source of truth.  ``CREATE TABLE IF
        NOT EXISTS`` would leave it in place forever — the only place a
        retired table can be removed is a migration step like this one.
        """
        cursor = await self._c.execute("PRAGMA user_version")
        row = await cursor.fetchone()
        version = row[0] if row else 0
        if version >= SCHEMA_VERSION:
            return
        if version < 2:
            await self._c.execute("DROP TABLE IF EXISTS server")
            logger.info("catalog_migrated from=%s to=2 dropped=server", version)

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

    async def purge_source(self, source_id: str) -> int:
        """Delete every tool row owned by one external server.

        The removal path (config removal, or a server disabled in
        ``tools.json5``): a server that is off owns no rows — its tools are
        re-mirrored when it connects again.  Returns the number of rows
        removed; embeddings follow via the FK cascade.
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "SELECT COUNT(*) FROM tool WHERE source_id = ?", (source_id,),
            )
            row = await cursor.fetchone()
            count = row[0] if row else 0
            await self._c.execute(
                "DELETE FROM tool WHERE source_id = ?", (source_id,),
            )
            # Embedding chunks go with the rows (tool_embeddings.name has
            # ON DELETE CASCADE and foreign_keys is ON).
            await self._c.commit()
        logger.info("catalog_source_purged source=%s tools=%d", source_id, count)
        return count

    async def purge_missing_sources(self, keep: "set[str]") -> "set[str]":
        """Purge the rows of every server NOT in *keep*; returns what was purged."""
        purged: set[str] = set()
        for source_id in sorted(await self.list_source_ids() - keep):
            await self.purge_source(source_id)
            purged.add(source_id)
        return purged

    async def list_source_ids(self) -> "set[str]":
        """Every server name that currently owns tool rows."""
        cursor = await self._c.execute(
            "SELECT DISTINCT source_id FROM tool WHERE source_id IS NOT NULL",
        )
        return {row[0] for row in await cursor.fetchall()}

    async def mark_source_error(self, source_id: str) -> int:
        """Mark one server's tools ``error`` — its server went down.

        The verdict lives on the rows themselves now (no server table to join
        for "is it connected"): ``error`` is a state of its own, so the row
        does not lose the fact that it *belonged* to a live server.  Reconnecting
        resets them (``reset_source_status``).
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET status = ? WHERE source_id = ? AND status IS NOT NULL",
                (STATUS_ERROR, source_id),
            )
            await self._c.commit()
        return cursor.rowcount

    async def mark_all_external_error(self) -> int:
        """Mark EVERY external tool ``error`` — the gateway child died.

        All of its servers are unreachable at once, so their tools must leave
        the injection set immediately rather than at the next reconcile.
        """
        async with self._write_lock:
            placeholders = ",".join("?" * len(SERVER_CATEGORIES))
            cursor = await self._c.execute(
                f"UPDATE tool SET status = ? "
                f"WHERE category IN ({placeholders}) AND status IS NOT NULL",
                (STATUS_ERROR, *sorted(SERVER_CATEGORIES)),
            )
            await self._c.commit()
        return cursor.rowcount

    async def reset_source_status(self, source_id: str, status: str) -> int:
        """Bring a server's ``error`` rows back to *status* (its class default).

        Only ``error`` rows move: a row the user loaded or unloaded keeps that
        state across a reconnect — the error mark is what a reconnect clears.
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tool SET status = ? WHERE source_id = ? AND status = ?",
                (status, source_id, STATUS_ERROR),
            )
            await self._c.commit()
        return cursor.rowcount

    async def remove_tool(self, name: str) -> None:
        """Delete a single tool row plus its embedding chunks.

        Used when a non-server tool disappears at runtime (a job file
        removed, a plugin dropping a tool) — the §8.5 "remove 清理干净"
        contract: a vanished tool must not linger as a stale catalog row
        that tool_search keeps returning.
        """
        async with self._write_lock:
            await self._c.execute("DELETE FROM tool WHERE name = ?", (name,))
            await self._c.execute(
                "DELETE FROM tool_embeddings WHERE name = ?", (name,),
            )
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

    async def get_effective(self, name: str) -> str | None:
        """Effective status for one tool name (None if unknown)."""
        row = await self.get_tool(name)
        if row is None:
            return None
        return _effective_status(row)

    async def scan_effective(
        self, category: str = "",
    ) -> list[dict]:
        """Every tool row, annotated with its effective status.

        Returns rows: ``name/description/category/source_id/schema/status/
        last_loaded/eff`` — the display/search shape.
        """
        clauses = ""
        params: list = []
        if category:
            clauses = "WHERE t.category = ?"
            params.append(category)
        cursor = await self._c.execute(
            f"SELECT {_SCAN_COLS} FROM tool t {clauses} "
            f"ORDER BY t.category, t.name",
            params,
        )
        rows = []
        for row in await cursor.fetchall():
            r = dict(row)
            r["eff"] = _effective_status(r)
            rows.append(r)
        return rows

    async def loaded_names(self) -> list[str]:
        """Injectable tool names: ``status == 'loaded'`` (and not disabled).

        Everything is on the row now.  An external tool whose server is down
        is not ``loaded`` — the reconcile marked it ``error`` — so it drops
        out of the injection set without a join, exactly as the retired
        server-row join used to arrange.
        """
        cursor = await self._c.execute(
            """SELECT name FROM tool
               WHERE status = 'loaded'
                 AND (category IN ('mcp','rest-api')
                      OR enabled IS NULL OR enabled = 1)
               ORDER BY name"""
        )
        return [row[0] for row in await cursor.fetchall()]

    async def count_loaded(self) -> int:
        cursor = await self._c.execute(
            """SELECT COUNT(*) FROM tool
               WHERE status = 'loaded'
                 AND (category IN ('mcp','rest-api')
                      OR enabled IS NULL OR enabled = 1)"""
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
                      te.embedding
               FROM tool_embeddings te
               JOIN tool t ON te.name = t.name
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