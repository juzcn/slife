"""Tool catalog store — in-memory SQLite with FTS5 + BLOB-vector semantic search.

One row = one external MCP tool, keyed by ``full_name`` (``{server}__{tool}``).
The store lives entirely in memory (``:memory:``), created when the wrapper
loads and populated from the live connection pool — it is by construction
identical to the tools the runtime can actually use; nothing persists to
disk.  Keyword search uses FTS5 (with a LIKE fallback for CJK, which
unicode61 cannot segment); semantic search is brute-force cosine over
f32-BLOB vectors (the corpus is small — tens to hundreds of tools — so a
linear scan beats loading the sqlite-vec binary extension).

Each tool row also carries its complete ``tools/list`` descriptor —
``{name, description, inputSchema}`` — as compact JSON in ``input_schema``
(one column = one text).  Keyword indexes name/server/description/schema
together; the semantic vector is sourced from this single column ALONE
(flattened to readable text at embed time), so it covers name +
description + parameters + return description.

The store is a "document source" for :class:`mcp_plugin.semantic.SemanticManager`
(``count_unembedded`` / ``get_unembedded_docs`` / ``replace_embedding``) —
one tool = one document, never chunked.
"""

import asyncio
import json
import logging
import math
import struct
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiosqlite

from slife.plugins.memdb.store import (
    _clamp_limit,
    _contains_cjk,
    _serialize_f32,
    _split_sql,
    _to_fts5_query,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _deserialize_f32(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _like_escape(pattern: str) -> str:
    """Escape LIKE metacharacters so ``%``/``_`` match literally."""
    return (
        pattern.replace("\\", r"\\")
        .replace("%", r"\%")
        .replace("_", r"\_")
    )


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


#
# ── Schema → text (semantic doc source) ───────────────────────────────
#

def _compact_schema(value: Any) -> str:
    """Compact JSON text for a tool's ``inputSchema`` (dict/list or JSON str)."""
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
    """Single flat readable text of a tool's full schema — the semantic doc.

    The embedding vector represents the ``input_schema`` column alone, and
    that column stores the COMPLETE ``tools/list`` descriptor as compact JSON
    (``{name, description, inputSchema}``) — so the doc covers name +
    description + each parameter + a return description.  A bare input schema
    (``{type, properties}`` with no descriptor wrapper) is also handled for
    robustness.  JSON boilerplate is dropped; text that is missing or
    unparseable yields "" — such a tool simply never gets a vector.
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




class ToolStore:
    """Manages the in-memory mcp-plugin tool catalog."""

    def __init__(self):
        self._conn: aiosqlite.Connection | None = None
        # Serializes every mutating statement on the shared connection.  All
        # writers commit on the same aiosqlite connection; without this, one
        # coroutine's commit() can land between another's multi-statement
        # transaction and split it — leaving a half-applied batch.
        self._write_lock = asyncio.Lock()

    @property
    def _c(self):
        assert self._conn is not None
        return self._conn

    # ── Lifecycle ──────────────────────────────────────────────────

    async def open(self) -> None:
        """Open the catalog as a fresh in-memory database.

        Created at wrapper load and repopulated from the live connection
        pool, so the catalog can never drift from what the runtime actually
        offers.  No file, no WAL — the connection lives for the process.
        """
        self._conn = await aiosqlite.connect(":memory:")
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._run_schema()
        logger.info("tool_store_ready catalog=in-memory")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _run_schema(self) -> None:
        schema_path = Path(__file__).parent / "schema.sql"
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
                logger.error("schema_stmt_error err=%s stmt=%.80s", e, stmt)
        await self._c.commit()

    # ── Catalog ────────────────────────────────────────────────────

    async def sync_server(
        self, server: str, tools: list[dict], auto_load: bool = False,
    ) -> dict:
        """Upsert *server*'s live tools; delete tools it no longer offers.

        Also upserts the server's per-mcp row (``enabled`` preserved from the
        current state, ``auto_load`` refreshed from the connection config).
        Returns ``{"upserted": n, "deleted": m}``.
        """
        now = _now()
        upserted = 0
        names: list[str] = []
        async with self._write_lock:
            # Per-mcp row first (tools FK-cascade from it).  enabled is NOT
            # touched on conflict — a server that was disabled stays disabled.
            await self._c.execute(
                """INSERT INTO servers(name, enabled, auto_load)
                   VALUES (?, 1, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       auto_load = excluded.auto_load
                """,
                (server, 1 if auto_load else 0),
            )
            for t in tools:
                name = t.get("name", "")
                if not name:
                    continue
                names.append(name)
                full_name = t.get("full_name") or f"{server}__{name}"
                desc = t.get("description", "") or ""
                # Full tool schema text: the complete tools/list descriptor
                # {name, description, inputSchema} as one compact JSON — the
                # semantic vector is sourced from THIS column alone, so it
                # covers name + description + parameters + return desc.
                schema_text = _compact_schema(t)
                # Detect an edit of that text (handled by the ON CONFLICT
                # update below): the vector's source changed → drop the stale
                # vector so the drainer re-embeds (keyword stays live through
                # the FTS5 triggers regardless).
                prev = await self._c.execute(
                    "SELECT description, input_schema FROM tools WHERE full_name = ?",
                    (full_name,),
                )
                prev_row = await prev.fetchone()
                schema_changed = (
                    prev_row is not None and (prev_row[1] or "") != schema_text
                )
                await self._c.execute(
                    """INSERT INTO tools(
                           full_name, server, name, description, input_schema,
                           last_seen, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(full_name) DO UPDATE SET
                           server       = excluded.server,
                           name         = excluded.name,
                           description  = excluded.description,
                           input_schema = excluded.input_schema,
                           last_seen    = excluded.last_seen
                    """,
                    (full_name, server, name, desc, schema_text, now, now),
                )
                upserted += 1
                if schema_changed or prev_row is None:
                    # prev_row None ⇒ new tool, never embedded — delete is a
                    # cheap no-op that also covers an embedding row left from a
                    # removed-and-re-added tool in the same batch.
                    await self._c.execute(
                        "DELETE FROM tool_embeddings WHERE full_name = ?",
                        (full_name,),
                    )
            # Remove tools this server no longer advertises.
            if names:
                ph = ",".join("?" * len(names))
                cursor = await self._c.execute(
                    f"DELETE FROM tools WHERE server = ? AND name NOT IN ({ph})",
                    (server, *names),
                )
            else:
                cursor = await self._c.execute(
                    "DELETE FROM tools WHERE server = ?", (server,),
                )
            deleted = cursor.rowcount
            await self._c.commit()
        logger.info(
            "tool_sync_server server=%s upserted=%d deleted=%d",
            server, upserted, deleted,
        )
        return {"upserted": upserted, "deleted": deleted}

    async def remove_server(self, server: str) -> int:
        """Delete the server's per-mcp row and every tool row for it.

        Returns how many tools were removed (the servers row is also dropped).
        """
        async with self._write_lock:
            cursor = await self._c.execute(
                "DELETE FROM tools WHERE server = ?", (server,),
            )
            deleted = cursor.rowcount
            await self._c.execute(
                "DELETE FROM servers WHERE name = ?", (server,),
            )
            await self._c.commit()
        logger.info("tool_remove_server server=%s deleted=%d", server, deleted)
        return deleted

    async def set_server_enabled(self, server: str, enabled: bool) -> int:
        """Flip the server's per-mcp enabled flag — the only enable/disable
        state that exists (per-mcp only, no per-tool flags)."""
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE servers SET enabled = ? WHERE name = ?",
                (1 if enabled else 0, server),
            )
            await self._c.commit()
        return cursor.rowcount

    async def get_server(self, server: str) -> dict | None:
        """Return the server's per-mcp row (``enabled`` / ``auto_load``)."""
        cursor = await self._c.execute(
            "SELECT name, enabled, auto_load FROM servers WHERE name = ?",
            (server,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_tool(self, full_name: str) -> dict | None:
        """Fetch one catalog row (full_name/server/name/description/input_schema)."""
        cursor = await self._c.execute(
            "SELECT full_name, server, name, description, input_schema "
            "FROM tools WHERE full_name = ?",
            (full_name,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def count_tools(self) -> int:
        cursor = await self._c.execute("SELECT COUNT(*) FROM tools")
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_by_server(self, server: str) -> int:
        cursor = await self._c.execute(
            "SELECT COUNT(*) FROM tools WHERE server = ?", (server,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def list_tools_by_server(self, server: str) -> list[dict]:
        """Return every catalog row for *server*.

        One dict per tool: ``full_name`` / ``server`` / ``name`` /
        ``description`` / ``input_schema`` — the catalog view of a
        server's tools.
        """
        cursor = await self._c.execute(
            "SELECT full_name, server, name, description, input_schema "
            "FROM tools WHERE server = ?",
            (server,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── Search ─────────────────────────────────────────────────────

    async def search_keyword(
        self, query: str, limit: int = 20,
        server: str | None = None,
    ) -> list[dict]:
        """FTS5 keyword search with snippet highlighting.

        Only tools of enabled, non-auto_load servers are discoverable —
        disabled and auto_load servers' tools never surface.
        """
        limit = _clamp_limit(limit)
        # FTS5 unicode61 does not segment CJK — route Chinese queries to the
        # LIKE fallback (same shape, so callers and merge_hybrid are agnostic).
        if _contains_cjk(query):
            return await self._search_like(query, limit=limit, server=server)
        fts_query = _to_fts5_query(query)
        clauses = ""
        params: list = [fts_query]
        if server:
            clauses += " AND t.server = ?"
            params.append(server)
        params.append(limit)
        try:
            cursor = await self._c.execute(
                f"""SELECT t.full_name, t.server, t.name, t.description,
                          snippet(tools_fts, 3, '…', '…', '…', 40) AS snippet, rank
                   FROM tools_fts fts
                   JOIN tools t ON fts.rowid = t.rowid
                   JOIN servers s ON s.name = t.server
                        AND s.enabled = 1 AND s.auto_load = 0
                   WHERE tools_fts MATCH ?{clauses}
                   -- bm25 column weights: name/server dominate, description 1,
                   -- input_schema 0.5 so generic JSON words (string/array)
                   -- don't inflate while param/return terms still match.
                   ORDER BY bm25(tools_fts, 5.0, 5.0, 2.0, 1.0, 0.5) LIMIT ?""",
                params,
            )
            results = [dict(row) for row in await cursor.fetchall()]
            logger.debug("tool_search_keyword query=%s hits=%s", query, len(results))
            return results
        except aiosqlite.OperationalError as e:
            logger.debug("tool_search_keyword_parse_error query=%s err=%s", query, e)
            return []

    async def _search_like(
        self, pattern: str, limit: int,
        server: str | None = None,
    ) -> list[dict]:
        """Substring (LIKE) search — CJK fallback for :meth:`search_keyword`.

        Space-split words all must match (AND semantics) across the four
        searchable columns; each CJK word matches by substring.  Returns the
        same shape as ``search_keyword`` (snippet + rank=0).
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
                "(t.full_name LIKE ? ESCAPE '\\' OR t.server LIKE ? ESCAPE '\\'"
                " OR t.name LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\'"
                " OR t.input_schema LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like, like, like])
        where = " AND ".join(and_clauses)
        if server:
            where += " AND t.server = ?"
            params.append(server)
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT t.full_name, t.server, t.name, t.description,
                      substr(t.description, max(0, instr(t.description, ?) - 40), 160) AS snippet,
                      0 AS rank
               FROM tools t
               JOIN servers s ON s.name = t.server
                    AND s.enabled = 1 AND s.auto_load = 0
               WHERE {where}
               ORDER BY t.full_name LIMIT ?""",
            params,
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug("tool_search_like_cjk pattern=%s hits=%s", pattern[:80], len(results))
        return results

    async def search_grep(
        self, pattern: str, limit: int = 20,
        server: str | None = None,
    ) -> list[dict]:
        """Exact substring search over the tool catalog columns.

        Only tools of enabled, non-auto_load servers are discoverable.
        """
        limit = _clamp_limit(limit)
        safe = _like_escape(pattern)
        like_pattern = f"%{safe}%"
        clauses = ""
        params: list = [
            pattern, like_pattern, like_pattern, like_pattern, like_pattern, like_pattern,
        ]
        if server:
            clauses += " AND t.server = ?"
            params.append(server)
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT t.full_name, t.server, t.name, t.description,
                      substr(t.description, max(0, instr(t.description, ?) - 40), 160) AS snippet,
                      0 AS rank
               FROM tools t
               JOIN servers s ON s.name = t.server
                    AND s.enabled = 1 AND s.auto_load = 0
               WHERE (t.full_name LIKE ? ESCAPE '\\' OR t.server LIKE ? ESCAPE '\\'
                      OR t.name LIKE ? ESCAPE '\\' OR t.description LIKE ? ESCAPE '\\'
                      OR t.input_schema LIKE ? ESCAPE '\\')
                     {clauses}
               ORDER BY t.full_name LIMIT ?""",
            params,
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug("tool_search_grep pattern=%s hits=%s", pattern[:80], len(results))
        return results

    async def search_semantic(
        self, vec: list[float], limit: int = 20,
        server: str | None = None,
    ) -> list[dict]:
        """Brute-force cosine KNN over stored BLOB vectors.

        Only tools of enabled, non-auto_load servers are discoverable.
        Vectors whose width differs from *vec* are skipped (defensive against
        stale rows from a previous embedding model).
        """
        limit = _clamp_limit(limit)
        cursor = await self._c.execute(
            """SELECT t.full_name, t.server, t.name, t.description,
                      te.embedding
               FROM tool_embeddings te
               JOIN tools t ON te.full_name = t.full_name
               JOIN servers s ON s.name = t.server
                    AND s.enabled = 1 AND s.auto_load = 0"""
        )
        matches: list[dict] = []
        for row in await cursor.fetchall():
            r = dict(row)
            if server and r["server"] != server:
                continue
            stored = _deserialize_f32(r.pop("embedding"))
            if len(stored) != len(vec):
                continue
            r["distance"] = _cosine_distance(vec, stored)
            matches.append(r)
        matches.sort(key=lambda x: x["distance"])
        results = matches[:limit]
        logger.debug("tool_search_semantic hits=%s", len(results))
        return results

    # ── Embedding drainer contract ─────────────────────────────────

    async def count_unembedded(self) -> int:
        """Count tools with no embedding row and a non-empty schema text.

        Tools whose flattened schema is empty (no vector source) are skipped
        so they can never wedge the semantic gate open.
        """
        cursor = await self._c.execute(
            """SELECT input_schema FROM tools
               WHERE full_name NOT IN (SELECT full_name FROM tool_embeddings)""",
        )
        return sum(
            1 for (schema_text,) in await cursor.fetchall()
            if _flatten_schema(schema_text or "").strip()
        )

    async def get_unembedded_docs(self, limit: int = 100) -> list[dict]:
        """Return tools lacking an embedding, in the drainer's doc shape.

        Each doc carries ``doc_id`` (the full_name) and ``text`` (the
        full-descriptor column flattened to readable text) — the semantic
        vector is sourced from the ``input_schema`` column alone, one tool
        = one vector.  Tools with no flattenable text produce no doc.
        """
        cursor = await self._c.execute(
            """SELECT full_name, input_schema FROM tools
               WHERE full_name NOT IN (SELECT full_name FROM tool_embeddings)
               ORDER BY full_name
               LIMIT ?""",
            (limit,),
        )
        docs = []
        for row in await cursor.fetchall():
            text = _flatten_schema(row[1] or "")
            if not text.strip():
                continue  # no schema text → no vector, and not "unembedded"
            docs.append({"doc_id": row[0], "text": text})
        return docs

    async def replace_embedding(self, full_name: str, vec: list[float], model: str) -> None:
        """Store (or replace) one tool's embedding vector."""
        async with self._write_lock:
            await self._c.execute(
                """INSERT OR REPLACE INTO tool_embeddings(full_name, embedding, model)
                   VALUES (?, ?, ?)""",
                (full_name, _serialize_f32(vec), model),
            )
            await self._c.commit()

    async def drop_embeddings(self) -> int:
        """Delete all embedding rows. Returns count deleted."""
        async with self._write_lock:
            cursor = await self._c.execute("SELECT COUNT(*) FROM tool_embeddings")
            row = await cursor.fetchone()
            count = row[0] if row else 0
            await self._c.execute("DELETE FROM tool_embeddings")
            await self._c.commit()
        logger.info("tool_embeddings_dropped count=%d", count)
        return count

    async def count_embedded(self) -> int:
        cursor = await self._c.execute("SELECT COUNT(*) FROM tool_embeddings")
        row = await cursor.fetchone()
        return row[0] if row else 0

    # ── Meta / maintenance ─────────────────────────────────────────

    async def get_meta(self, key: str) -> str | None:
        cursor = await self._c.execute(
            "SELECT value FROM tools_meta WHERE key = ?", (key,),
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    async def set_meta(self, key: str, value: str) -> None:
        async with self._write_lock:
            await self._c.execute(
                "INSERT OR REPLACE INTO tools_meta(key, value) VALUES (?, ?)",
                (key, value),
            )
            await self._c.commit()
