"""Tool catalog store — in-memory SQLite with FTS5 + BLOB-vector semantic search.

One row = one external MCP tool, keyed by ``full_name`` (``{server}__{tool}``).
The store lives entirely in memory (``:memory:``), created when the wrapper
loads and populated from the live connection pool — it is by construction
identical to the tools the runtime can actually use; nothing persists to
disk.  Keyword search uses FTS5 (with a LIKE fallback for CJK, which
unicode61 cannot segment); semantic search is brute-force cosine over
f32-BLOB vectors (the corpus is small — tens to hundreds of tools — so a
linear scan beats loading the sqlite-vec binary extension).

The store is a "document source" for :class:`mcp_plugin.semantic.SemanticManager`
(``count_unembedded`` / ``get_unembedded_docs`` / ``replace_embedding``) —
one tool = one document, never chunked.
"""

import asyncio
import logging
import math
import struct
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 200


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _serialize_f32(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


def _deserialize_f32(blob: bytes) -> list[float]:
    return list(struct.unpack(f"{len(blob) // 4}f", blob))


def _clamp_limit(limit: int) -> int:
    """Clamp a search limit to a sane positive range.

    SQLite treats a negative LIMIT as unlimited — a malformed/negative limit
    from the LLM would otherwise scan the whole table.
    """
    if limit is None or limit < 1:
        return 20
    return min(limit, _MAX_SEARCH_LIMIT)


def _contains_cjk(text: str) -> bool:
    """True if *text* contains CJK ideographs (incl. Extension A).

    SQLite FTS5's unicode61 tokenizer does not segment Chinese — a
    whole-sentence CJK query becomes a phrase/run token that never matches a
    longer stored description.  Substring (LIKE) matching is what Chinese
    users expect, so keyword search routes CJK queries to it.
    """
    return any(
        "㐀" <= ch <= "䶿" or "一" <= ch <= "鿿"
        for ch in text
    )


def _to_fts5_query(query: str) -> str:
    cleaned = query.replace('"', "").replace("'", "").replace("*", "")
    words = cleaned.split()
    if not words:
        return '""'
    # Quote FTS5 reserved operators so a literal "and"/"or"/"not"/"near"
    # doesn't become a syntax error (e.g. "foo AND bar" → "foo AND AND bar").
    quoted = [
        f'"{w}"' if w.lower() in ("and", "or", "not", "near") else w
        for w in words
    ]
    return " AND ".join(quoted)


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


def _looks_like_trigger_start(stmt: str) -> bool:
    """True if *stmt* starts a CREATE TRIGGER that has a BEGIN body.

    Tolerates leading ``--`` comment lines (e.g. the comment block above the
    trigger): the comments accumulate into the same fragment and must not
    hide the CREATE TRIGGER keyword — otherwise the trigger body's interior
    semicolons split into orphan fragments and the trigger is never created.
    """
    first_non_comment = next(
        (
            ln.strip()
            for ln in stmt.splitlines()
            if ln.strip() and not ln.strip().startswith("--")
        ),
        "",
    )
    upper = first_non_comment.upper()
    return upper.startswith("CREATE TRIGGER") and "BEGIN" in upper


def _looks_like_trigger_end(stmt: str) -> bool:
    """True if *stmt* ends a trigger body (``END;``)."""
    stripped = stmt.strip().rstrip(";").strip().upper()
    return stripped.endswith("END")


def _split_sql(sql_text: str) -> list[str]:
    """Split SQL text on semicolons, respecting quotes and comments.

    Multi-statement constructs (CREATE TRIGGER … BEGIN … END) are kept
    together so SQLite can parse them as a single statement.  Otherwise the
    interior INSERT / DELETE would be split into orphaned fragments.
    """
    statements = []
    current: list[str] = []
    in_single = False
    in_double = False
    in_line = False
    in_block = False
    in_trigger = False  # track CREATE TRIGGER … BEGIN … END blocks

    chars = list(sql_text)
    i = 0
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""

        if in_line:
            current.append(ch)
            if ch == "\n":
                in_line = False
            i += 1
            continue

        if in_block:
            current.append(ch)
            if ch == "*" and nxt == "/":
                current.append(nxt)
                i += 2
                in_block = False
                continue
            i += 1
            continue

        if in_single:
            current.append(ch)
            if ch == "'":
                in_single = False
            i += 1
            continue

        if in_double:
            current.append(ch)
            if ch == '"':
                in_double = False
            i += 1
            continue

        if ch == "-" and nxt == "-":
            in_line = True
            current.append(ch)
            i += 1
            continue

        if ch == "/" and nxt == "*":
            in_block = True
            current.append(ch)
            current.append(nxt)
            i += 2
            continue

        if ch == "'" and not in_double:
            in_single = not in_single
        elif ch == '"' and not in_single:
            in_double = not in_double
        elif ch == ";" and not in_single and not in_double:
            current.append(ch)
            stmt = "".join(current)
            # Detect start of a multi-statement TRIGGER block
            if not in_trigger and _looks_like_trigger_start(stmt):
                in_trigger = True
            # A TRIGGER body ends with END;
            if in_trigger and _looks_like_trigger_end(stmt):
                in_trigger = False
            if in_trigger:
                # Keep accumulating — this semicolon is inside the trigger body
                i += 1
                continue
            statements.append(stmt)
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    remainder = "".join(current).strip()
    if remainder:
        statements.append(remainder)
    return statements


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

    async def sync_server(self, server: str, tools: list[dict]) -> dict:
        """Upsert *server*'s live tools; delete tools the server no longer offers.

        PRESERVES the per-tool ``enabled`` flag on re-upsert (user-set status
        survives reconnects and description edits).  Returns
        ``{"upserted": n, "deleted": m}``.
        """
        now = _now()
        upserted = 0
        names: list[str] = []
        async with self._write_lock:
            for t in tools:
                name = t.get("name", "")
                if not name:
                    continue
                names.append(name)
                full_name = t.get("full_name") or f"{server}__{name}"
                desc = t.get("description", "") or ""
                cursor = await self._c.execute(
                    """INSERT INTO tools(
                           full_name, server, name, description, enabled,
                           last_seen, created_at)
                       VALUES (?, ?, ?, ?, 1, ?, ?)
                       ON CONFLICT(full_name) DO UPDATE SET
                           server     = excluded.server,
                           name       = excluded.name,
                           description = excluded.description,
                           last_seen  = excluded.last_seen
                       -- enabled deliberately NOT in the update list: a
                       -- user-set disabled flag survives re-sync
                    """,
                    (full_name, server, name, desc, now, now),
                )
                upserted += 1
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
        """Delete every tool row for *server* (FK cascades embeddings)."""
        async with self._write_lock:
            cursor = await self._c.execute(
                "DELETE FROM tools WHERE server = ?", (server,),
            )
            await self._c.commit()
        logger.info("tool_remove_server server=%s deleted=%d", server, cursor.rowcount)
        return cursor.rowcount

    async def set_tool_enabled(self, full_name: str, enabled: bool) -> bool:
        """Set the per-tool enabled flag. Returns True if the tool exists."""
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tools SET enabled = ? WHERE full_name = ?",
                (1 if enabled else 0, full_name),
            )
            await self._c.commit()
        return cursor.rowcount > 0

    async def disable_server_tools(self, server: str) -> int:
        """Mark every tool of *server* disabled (0). Used by
        ``mcp_set_enabled(false)`` so a disabled server's catalog entries
        are searchable but not usable."""
        return await self._set_server_tools(server, enabled=False)

    async def enable_server_tools(self, server: str) -> int:
        """Mark every tool of *server* enabled (1) — ``mcp_set_enabled(true)``."""
        return await self._set_server_tools(server, enabled=True)

    async def _set_server_tools(self, server: str, *, enabled: bool) -> int:
        async with self._write_lock:
            cursor = await self._c.execute(
                "UPDATE tools SET enabled = ? WHERE server = ?",
                (1 if enabled else 0, server),
            )
            await self._c.commit()
        return cursor.rowcount

    async def get_tool(self, full_name: str) -> dict | None:
        """Fetch one tool row (with full_name/server/name/description/enabled)."""
        cursor = await self._c.execute(
            "SELECT full_name, server, name, description, enabled FROM tools WHERE full_name = ?",
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
        ``description`` / ``enabled`` — the persisted view used to compare
        against a live server's tools.
        """
        cursor = await self._c.execute(
            "SELECT full_name, server, name, description, enabled "
            "FROM tools WHERE server = ?",
            (server,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    # ── Search ─────────────────────────────────────────────────────

    async def search_keyword(
        self, query: str, limit: int = 20,
        server: str | None = None, include_disabled: bool = True,
    ) -> list[dict]:
        """FTS5 keyword search with snippet highlighting."""
        limit = _clamp_limit(limit)
        # FTS5 unicode61 does not segment CJK — route Chinese queries to the
        # LIKE fallback (same shape, so callers and merge_hybrid are agnostic).
        if _contains_cjk(query):
            return await self._search_like(
                query, limit=limit, server=server, include_disabled=include_disabled,
            )
        fts_query = _to_fts5_query(query)
        clauses = ""
        params: list = [fts_query]
        if server:
            clauses += " AND t.server = ?"
            params.append(server)
        if not include_disabled:
            clauses += " AND t.enabled = 1"
        params.append(limit)
        try:
            cursor = await self._c.execute(
                f"""SELECT t.full_name, t.server, t.name, t.description, t.enabled,
                          snippet(tools_fts, 3, '…', '…', '…', 40) AS snippet, rank
                   FROM tools_fts fts
                   JOIN tools t ON fts.rowid = t.rowid
                   WHERE tools_fts MATCH ?{clauses}
                   ORDER BY rank LIMIT ?""",
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
        server: str | None = None, include_disabled: bool = True,
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
                "(full_name LIKE ? ESCAPE '\\' OR server LIKE ? ESCAPE '\\'"
                " OR name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like, like])
        where = " AND ".join(and_clauses)
        if server:
            where += " AND server = ?"
            params.append(server)
        if not include_disabled:
            where += " AND enabled = 1"
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT full_name, server, name, description, enabled,
                      substr(description, max(0, instr(description, ?) - 40), 160) AS snippet,
                      0 AS rank
               FROM tools
               WHERE {where}
               ORDER BY full_name LIMIT ?""",
            params,
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug("tool_search_like_cjk pattern=%s hits=%s", pattern[:80], len(results))
        return results

    async def search_grep(
        self, pattern: str, limit: int = 20,
        server: str | None = None, include_disabled: bool = True,
    ) -> list[dict]:
        """Exact substring search over the tool catalog columns."""
        limit = _clamp_limit(limit)
        safe = _like_escape(pattern)
        like_pattern = f"%{safe}%"
        clauses = ""
        params: list = [pattern, like_pattern, like_pattern, like_pattern, like_pattern]
        if server:
            clauses += " AND server = ?"
            params.append(server)
        if not include_disabled:
            clauses += " AND enabled = 1"
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT full_name, server, name, description, enabled,
                      substr(description, max(0, instr(description, ?) - 40), 160) AS snippet,
                      0 AS rank
               FROM tools
               WHERE (full_name LIKE ? ESCAPE '\\' OR server LIKE ? ESCAPE '\\'
                      OR name LIKE ? ESCAPE '\\' OR description LIKE ? ESCAPE '\\')
                     {clauses}
               ORDER BY full_name LIMIT ?""",
            params,
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug("tool_search_grep pattern=%s hits=%s", pattern[:80], len(results))
        return results

    async def search_semantic(
        self, vec: list[float], limit: int = 20,
        server: str | None = None, include_disabled: bool = True,
    ) -> list[dict]:
        """Brute-force cosine KNN over stored BLOB vectors.

        Vectors whose width differs from *vec* are skipped (defensive against
        stale rows from a previous embedding model).
        """
        limit = _clamp_limit(limit)
        cursor = await self._c.execute(
            """SELECT t.full_name, t.server, t.name, t.description, t.enabled,
                      te.embedding
               FROM tool_embeddings te
               JOIN tools t ON te.full_name = t.full_name"""
        )
        matches: list[dict] = []
        for row in await cursor.fetchall():
            r = dict(row)
            if server and r["server"] != server:
                continue
            if not include_disabled and not r["enabled"]:
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
        """Count tools with no embedding row (the semantic index backlog)."""
        cursor = await self._c.execute(
            """SELECT COUNT(*) FROM tools
               WHERE full_name NOT IN (SELECT full_name FROM tool_embeddings)""",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def get_unembedded_docs(self, limit: int = 100) -> list[dict]:
        """Return tools lacking an embedding, in the drainer's doc shape.

        Each doc carries ``doc_id`` (the full_name) and ``text``
        (``"{server} {name}\\n{description}"``) — one tool, one embedding.
        """
        cursor = await self._c.execute(
            """SELECT full_name, server, name, description FROM tools
               WHERE full_name NOT IN (SELECT full_name FROM tool_embeddings)
               ORDER BY full_name
               LIMIT ?""",
            (limit,),
        )
        docs = []
        for row in await cursor.fetchall():
            r = dict(row)
            docs.append({
                "doc_id": r["full_name"],
                "text": f"{r['server']} {r['name']}\n{r.get('description', '')}",
            })
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
