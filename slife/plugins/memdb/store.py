"""Turn store — SQLite database with FTS5 + sqlite-vec hybrid search.

One row = one turn (user message + assistant's complete response).
No sessions, no lifecycle — each turn is independent and immutable.
Restore loads the most recent N turns by rowid.

Agent isolation is at the file level — each agent_id has its own .db file.
"""

import json
import logging
import struct
from datetime import date, datetime, timedelta
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIM = 1536


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _serialize_f32(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


_MAX_SEARCH_LIMIT = 200


def _clamp_limit(limit: int) -> int:
    """Clamp a search limit to a sane positive range.

    SQLite treats a negative LIMIT as unlimited — a malformed/negative limit
    from the LLM would otherwise scan the whole table (REVIEW M6).
    """
    if limit is None or limit < 1:
        return 20
    return min(limit, _MAX_SEARCH_LIMIT)


class SessionStore:
    """Manages the Slife memory database — turn-based, no sessions."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._embedding_dim = DEFAULT_EMBEDDING_DIM

    @property
    def _c(self):
        assert self._conn is not None
        return self._conn

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── Lifecycle ──────────────────────────────────────────────────

    async def setup(
        self,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
        embedding_model: str = "",
    ) -> None:
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._load_vec_extension()
        await self._run_schema()
        logger.info(
            "store_ready path=%s wal=on vec_dim=%d model=%s",
            self._db_path, self._embedding_dim, embedding_model or "none",
        )

    async def _load_vec_extension(self) -> None:
        import sqlite_vec
        await self._c.enable_load_extension(True)
        await self._c.load_extension(sqlite_vec.loadable_path())
        await self._c.enable_load_extension(False)
        row = await self._c.execute("SELECT vec_version()")
        version = await row.fetchone()
        logger.info("vec_loaded version=%s", version[0] if version else "unknown")

    async def _run_schema(self) -> None:
        schema_path = Path(__file__).parent / "schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        schema_sql = schema_sql.replace("float[1536]", f"float[{self._embedding_dim}]")
        # Execute each statement individually — vec0 virtual tables
        # can hang in aiosqlite's executescript.
        for stmt in _split_sql(schema_sql):
            stmt = stmt.strip()
            if not stmt:
                continue
            # vec0 rejects float[0] — skip the semantic table when
            # no embedding backend is configured.
            if self._embedding_dim <= 0 and "vec0" in stmt:
                logger.debug("schema_skip_vec0 dim=%d reason=no_embedding_backend", self._embedding_dim)
                continue
            try:
                await self._c.execute(stmt)
            except Exception as e:
                logger.debug("schema_stmt_error err=%s stmt=%.80s", e, stmt)
        await self._c.commit()

        # Detect and fix embedding dimension mismatch after model change.
        # CREATE TABLE IF NOT EXISTS won't alter a vec0 table whose
        # dimension no longer matches.  We drop it so the next statement
        # recreates with the correct dimension — old embeddings are
        # invalid anyway (different model → different vector space).
        await self._maybe_migrate_vec_dimension()
        logger.debug("schema_ready path=%s", self._db_path)

    async def _maybe_migrate_vec_dimension(self) -> None:
        """Drop and recreate ``diary_semantic`` if the embedding config changed.

        Two triggers:
        1.  **Dimension mismatch** — the vec0 ``float[N]`` column doesn't
            match the current model's output dimension.  Inserting wrong-sized
            vectors would fail silently.
        2.  **Model identity change** — same dimension, different model
            (e.g. ``text-embedding-ada-002`` → ``text-embedding-3-small``,
            both 1536).  Vectors live in different spaces and hybrid search
            would mix incompatible scores.

        Skips when no embedding backend is configured (dim ≤ 0).

        When either triggers, the old ``diary_semantic`` table is dropped.
        A background reindex will repopulate it with the new model's vectors.
        The new model identity is recorded in ``diary_meta`` so future
        same-dimension switches are also detected.
        """
        import re

        # No embedding backend → no vec0 table to migrate.
        if self._embedding_dim <= 0:
            return

        # ── Check stored model identity ──────────────────────────
        cursor = await self._c.execute(
            "SELECT value FROM diary_meta WHERE key = 'embedding_model'",
        )
        row = await cursor.fetchone()
        stored_model: str = row[0] if (row and isinstance(row[0], str)) else ""
        model_identity = self._embedding_model or ""

        # ── Check current vec0 dimension ─────────────────────────
        cursor = await self._c.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type='table' AND name='diary_semantic'",
        )
        row = await cursor.fetchone()
        create_sql = row[0] if (row and row[0] and isinstance(row[0], str)) else ""
        existing_dim = 0
        if create_sql:
            match = re.search(r"float\[(\d+)\]", create_sql)
            if match:
                existing_dim = int(match.group(1))

        dim_changed = existing_dim and existing_dim != self._embedding_dim
        model_changed = (
            model_identity
            and stored_model
            and stored_model != model_identity
        )
        table_missing = not create_sql

        if not dim_changed and not model_changed and not table_missing:
            # Record model identity if not yet stored (first run / upgrade).
            if model_identity and not stored_model:
                await self._c.execute(
                    "INSERT OR REPLACE INTO diary_meta (key, value) "
                    "VALUES ('embedding_model', ?)",
                    (model_identity,),
                )
                await self._c.commit()
            return

        reason = (
            f"dim {existing_dim}→{self._embedding_dim}"
            if dim_changed
            else f"model {stored_model}→{model_identity}"
            if model_changed
            else "table missing"
        )
        logger.info(
            "vec_migrate reason=%s action=drop_diary_semantic", reason,
        )
        await self._c.execute("DROP TABLE IF EXISTS diary_semantic")
        await self._c.commit()

        # Recreate with the correct dimension.
        schema_path = Path(__file__).parent / "schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")
        schema_sql = schema_sql.replace(
            "float[1536]", f"float[{self._embedding_dim}]",
        )
        for stmt in _split_sql(schema_sql):
            stmt = stmt.strip()
            if not stmt or "diary_semantic" not in stmt:
                continue
            try:
                await self._c.execute(stmt)
            except Exception as e:
                logger.debug(
                    "vec_recreate_error err=%s stmt=%.80s", e, stmt,
                )
        await self._c.commit()

        # Persist the new model identity.
        if model_identity:
            await self._c.execute(
                "INSERT OR REPLACE INTO diary_meta (key, value) "
                "VALUES ('embedding_model', ?)",
                (model_identity,),
            )
            await self._c.commit()

        logger.info("vec_migrated reason=%s", reason)

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("store_closed path=%s", self._db_path)

    # ── Turn CRUD ──────────────────────────────────────────────────

    async def save_turn(
        self,
        user_message: str = "",
        messages: list[dict] | None = None,
        token_count: int = 0,
        who_helped: str = "",
        what_model: str = "",
        channel: str = "",
        embedder=None,
    ) -> int:
        """Insert a turn. Returns rowid. Generates embedding if embedder available."""
        now = _now()
        messages_json = json.dumps(messages or [], ensure_ascii=False)

        cursor = await self._c.execute(
            """INSERT INTO diary (user_message, messages, summary, tags,
                                  channel, created_at, who_helped, what_model, token_count)
               VALUES (?, ?, '', '', ?, ?, ?, ?, ?)""",
            (user_message, messages_json, channel, now, who_helped, what_model, token_count),
        )
        await self._c.commit()
        rowid = cursor.lastrowid
        assert rowid is not None  # insert just succeeded
        logger.debug("turn_saved rowid=%s", rowid)

        # Embed the turn text in chunks.  Long turns (multi-tool calls,
        # large file reads) are split at paragraph boundaries so every
        # turn contributes to semantic search — no silent skipping.
        if embedder is not None and embedder.available:
            embed_text = _turn_text_for_embedding(user_message, messages or [])
            if embed_text.strip():
                try:
                    chunks = _chunk_text(embed_text)
                    # Filter chunks that exceed the token limit (unlikely
                    # with 2000-char chunks, but a safety net).
                    valid_chunks = [
                        c for c in chunks
                        if len(c) // 4 <= embedder.max_tokens
                    ]
                    if valid_chunks:
                        embeddings = await embedder.embed(valid_chunks)
                        if embeddings:
                            for idx, emb in enumerate(embeddings):
                                if emb:
                                    await self.upsert_embedding(
                                        diary_rowid=rowid, chunk_index=idx,
                                        summary="", tags="",
                                        created_at=now, turn_embedding=emb,
                                    )
                except Exception as e:
                    logger.debug("embedding_save_skipped rowid=%s err=%s", rowid, e)

        return rowid

    async def get_turn(self, rowid: int) -> dict | None:
        """Return a single turn by rowid."""
        cursor = await self._c.execute(
            "SELECT rowid, * FROM diary WHERE rowid = ?",
            (rowid,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_recent_turns(self, limit: int = 50) -> list[dict]:
        """Return the most recent N turns, oldest-first for restore."""
        cursor = await self._c.execute(
            """SELECT rowid, user_message, messages, summary, tags,
                      channel, created_at, who_helped, what_model, token_count
               FROM diary
               WHERE rowid IN (
                   SELECT rowid FROM diary
                   ORDER BY rowid DESC LIMIT ?
               )
               ORDER BY rowid ASC""",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def has_turns(self) -> bool:
        """Check if there are any turns."""
        cursor = await self._c.execute(
            "SELECT rowid FROM diary LIMIT 1",
        )
        return await cursor.fetchone() is not None

    async def count_turns(
        self,
        since: str | None = None, until: str | None = None,
        query: str | None = None, mode: str = "fts5",
    ) -> dict:
        """Count turns, optionally filtered by time or search query.

        Returns {total, filtered, since, until, query, mode}.
        """
        row = await self._c.execute("SELECT COUNT(*) FROM diary")
        total = (await row.fetchone())[0]

        if query and query.strip():
            mode = mode.lower()
            if mode == "grep":
                safe = query.replace("%", r"\%").replace("_", r"\_")
                like_pattern = f"%{safe}%"
                where = "user_message LIKE ? OR messages LIKE ?"
                params: list = [like_pattern, like_pattern]
            else:
                fts_query = _to_fts5_query(query)
                # FTS5 has no created_at — join the diary rowid so since/until
                # filter the same way as grep/time (REVIEW M6).
                time_clauses = ""
                time_params: list[str] = []
                if since:
                    since = _normalize_time_param(since, role="since")
                    time_clauses += " AND d.created_at >= ?"
                    time_params.append(since)
                if until:
                    until = _normalize_time_param(until, role="until")
                    time_clauses += " AND d.created_at <= ?"
                    time_params.append(until)
                row2 = await self._c.execute(
                    f"""SELECT COUNT(*) FROM diary_fts fts
                        JOIN diary d ON fts.rowid = d.rowid
                        WHERE diary_fts MATCH ?{time_clauses}""",
                    (fts_query, *time_params),
                )
                filtered = (await row2.fetchone())[0]
                return {"total": total, "filtered": filtered,
                        "query": query, "mode": mode,
                        "since": since, "until": until}

            if since:
                since = _normalize_time_param(since, role="since")
                where += " AND created_at >= ?"
                params.append(since)
            if until:
                until = _normalize_time_param(until, role="until")
                where += " AND created_at <= ?"
                params.append(until)
            row2 = await self._c.execute(
                f"SELECT COUNT(*) FROM diary WHERE {where}", params,
            )
            filtered = (await row2.fetchone())[0]
        elif since or until:
            clauses: list[str] = []
            params = []
            if since:
                since = _normalize_time_param(since, role="since")
                clauses.append("created_at >= ?")
                params.append(since)
            if until:
                until = _normalize_time_param(until, role="until")
                clauses.append("created_at <= ?")
                params.append(until)
            where = " AND ".join(clauses)
            row2 = await self._c.execute(
                f"SELECT COUNT(*) FROM diary WHERE {where}", params,
            )
            filtered = (await row2.fetchone())[0]
        else:
            filtered = total

        return {"total": total, "filtered": filtered,
                "since": since, "until": until,
                "query": query, "mode": mode if query else None}

    # ── Browse ─────────────────────────────────────────────────────

    async def list_recent(self, limit: int = 20) -> list[dict]:
        """List recent turns, newest first. Lightweight — no full messages."""
        limit = _clamp_limit(limit)
        cursor = await self._c.execute(
            """SELECT rowid, user_message, summary, tags, created_at,
                      token_count, who_helped, what_model
               FROM diary
               ORDER BY rowid DESC
               LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ── Summarize ──────────────────────────────────────────────────

    async def update_summary(
        self, rowid: int,
        summary: str | None = None, tags: str | None = None,
    ) -> None:
        """Write summary and/or tags for a turn."""
        updates = []
        params: list = []
        if summary is not None:
            updates.append("summary = ?")
            params.append(summary)
        if tags is not None:
            updates.append("tags = ?")
            params.append(tags)
        if not updates:
            return
        params.append(rowid)
        await self._c.execute(
            f"UPDATE diary SET {', '.join(updates)} WHERE rowid = ?",
            params,
        )
        await self._c.commit()

    # ── Search ──────────────────────────────────────────────────────

    async def search_keyword(
        self, query: str, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """FTS5 keyword search with snippet highlighting."""
        limit = _clamp_limit(limit)
        fts_query = _to_fts5_query(query)
        time_clauses = ""
        time_params: list[str] = []
        if since:
            since = _normalize_time_param(since, role="since")
            time_clauses += " AND d.created_at >= ?"
            time_params.append(since)
        if until:
            until = _normalize_time_param(until, role="until")
            time_clauses += " AND d.created_at <= ?"
            time_params.append(until)
        try:
            cursor = await self._c.execute(
                f"""SELECT d.rowid, d.user_message, d.summary, d.tags, d.created_at,
                          snippet(diary_fts, 3, '…', '…', '…', 40) AS snippet, rank
                   FROM diary_fts fts
                   JOIN diary d ON fts.rowid = d.rowid
                   WHERE diary_fts MATCH ?{time_clauses}
                   ORDER BY rank LIMIT ?""",
                (fts_query, *time_params, limit),
            )
            results = [dict(row) for row in await cursor.fetchall()]
            logger.debug("search_keyword query=%s hits=%s", query, len(results))
            return results
        except aiosqlite.OperationalError as e:
            logger.debug("search_keyword_parse_error query=%s err=%s", query, e)
            return []

    async def search_semantic(
        self, embedding: list[float], limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """sqlite-vec KNN on turn_embedding, deduplicated by diary_rowid.

        A single turn can produce multiple chunks — we keep only the best
        (lowest distance) match per turn so the result list has one entry
        per turn.
        """
        limit = _clamp_limit(limit)
        vec_blob = _serialize_f32(embedding)
        # Fetch extra rows to account for duplicate diary_rowid entries
        # (one turn → multiple chunks).  Dedup in Python: vec0 KNN does
        # not allow GROUP BY.
        fetch_limit = (limit * 4) if (since or until) else (limit * 2)
        cursor = await self._c.execute(
            """SELECT ds.diary_rowid AS rowid, d.user_message,
                      ds.summary, ds.tags, ds.created_at, ds.distance
               FROM diary_semantic ds
               JOIN diary d ON ds.diary_rowid = d.rowid
               WHERE ds.turn_embedding MATCH ? AND ds.k = ?
               ORDER BY ds.distance""",
            (vec_blob, fetch_limit),
        )
        # Deduplicate by diary_rowid — keep best (lowest) distance per turn
        seen: set[int] = set()
        results: list[dict] = []
        for row in await cursor.fetchall():
            r = dict(row)
            rid = r.get("rowid")
            if rid is not None and rid not in seen:
                seen.add(rid)
                results.append(r)
        if since:
            since = _normalize_time_param(since, role="since")
            results = [r for r in results if r.get("created_at", "") >= since]
        if until:
            until = _normalize_time_param(until, role="until")
            results = [r for r in results if r.get("created_at", "") <= until]
        results = results[:limit]
        logger.debug("search_semantic hits=%s", len(results))
        return results

    async def search_time(
        self, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Time-range browsing of turns."""
        limit = _clamp_limit(limit)
        clauses: list[str] = []
        params: list[str | int] = []
        if since:
            since = _normalize_time_param(since, role="since")
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            until = _normalize_time_param(until, role="until")
            clauses.append("created_at <= ?")
            params.append(until)
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        else:
            where = ""
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT rowid, user_message, summary, tags, created_at, token_count
               FROM diary {where} ORDER BY created_at DESC LIMIT ?""",
            params,
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug("search_time since=%s until=%s hits=%s", since, until, len(results))
        return results

    async def search_grep(
        self, pattern: str, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Exact substring search over user_message + messages."""
        limit = _clamp_limit(limit)
        # Escape LIKE metacharacters so a pattern containing %/_ matches them
        # literally.  The ESCAPE '\' clause is required or the escapes are a
        # no-op (REVIEW M5); backslashes themselves must be doubled first.
        safe = (
            pattern.replace("\\", r"\\")
                   .replace("%", r"\%")
                   .replace("_", r"\_")
        )
        like_pattern = f"%{safe}%"
        time_clauses = ""
        time_params: list[str] = []
        if since:
            since = _normalize_time_param(since, role="since")
            time_clauses += " AND created_at >= ?"
            time_params.append(since)
        if until:
            until = _normalize_time_param(until, role="until")
            time_clauses += " AND created_at <= ?"
            time_params.append(until)
        cursor = await self._c.execute(
            f"""SELECT rowid, user_message, summary, tags, created_at,
                      substr(messages, max(0, instr(messages, ?) - 40), 160) AS context
               FROM diary
               WHERE (user_message LIKE ? ESCAPE '\\' OR messages LIKE ? ESCAPE '\\')
                     {time_clauses}
               ORDER BY rowid DESC LIMIT ?""",
            (pattern, like_pattern, like_pattern, *time_params, limit),
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug("search_grep pattern=%s hits=%s", pattern[:80], len(results))
        return results

    # ── Embedding ───────────────────────────────────────────────────

    async def upsert_embedding(
        self, diary_rowid: int, chunk_index: int,
        summary: str, tags: str, created_at: str,
        turn_embedding: list[float],
    ) -> None:
        """Insert or update one chunk embedding for a turn.

        Each turn can produce multiple chunks — *chunk_index* is 0-based.
        When re-embedding (e.g. after summary update), all chunks for
        the same *diary_rowid* are replaced.
        """
        vec_blob = _serialize_f32(turn_embedding)
        await self._c.execute(
            """INSERT INTO diary_semantic
               (turn_embedding, diary_rowid, chunk_index, summary, tags, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (vec_blob, diary_rowid, chunk_index, summary, tags, created_at),
        )
        await self._c.commit()
        logger.debug("embedding_upserted diary_rowid=%s chunk=%s", diary_rowid, chunk_index)

    async def _clear_chunks(self, diary_rowid: int) -> None:
        """Delete all embedding chunks for a turn (prep for re-embed)."""
        await self._c.execute(
            "DELETE FROM diary_semantic WHERE diary_rowid = ?",
            (diary_rowid,),
        )
        await self._c.commit()

    async def get_unembedded_turns(self, limit: int = 100) -> list[dict]:
        """Return turns that have no embedding in diary_semantic.

        These need re-indexing after embedding config is added or changed.
        Returns lightweight rows: rowid, user_message, messages, created_at.
        """
        cursor = await self._c.execute(
            """SELECT d.rowid, d.user_message, d.messages, d.created_at
               FROM diary d
               WHERE d.rowid NOT IN (
                   SELECT DISTINCT diary_rowid FROM diary_semantic
               )
               ORDER BY d.rowid
               LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def count_unembedded(self) -> int:
        """Count turns that need re-indexing."""
        cursor = await self._c.execute(
            """SELECT COUNT(*) FROM diary d
               WHERE d.rowid NOT IN (
                   SELECT DISTINCT diary_rowid FROM diary_semantic
               )""",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def clear_all_embeddings(self) -> int:
        """Delete all rows from diary_semantic. Returns count deleted."""
        cursor = await self._c.execute("SELECT COUNT(*) FROM diary_semantic")
        row = await cursor.fetchone()
        count = row[0] if row else 0
        await self._c.execute("DELETE FROM diary_semantic")
        await self._c.commit()
        logger.info("embeddings_cleared count=%d", count)
        return count

    async def has_embedding(self, diary_rowid: int) -> bool:
        cursor = await self._c.execute(
            "SELECT rowid FROM diary_semantic WHERE diary_rowid = ? LIMIT 1",
            (diary_rowid,),
        )
        return await cursor.fetchone() is not None


# ── Helpers ─────────────────────────────────────────────────────────


# Relative-date patterns that LLMs may pass verbatim despite being told
# to compute ISO datetimes.  We convert them server-side so search
# doesn't silently return zero results.
_RELATIVE_DATES: dict[str, str] = {
    "today": "",
    "yesterday": "",
    "tomorrow": "",
    "now": "",
}


def _normalize_time_param(value: str, role: str = "since") -> str:
    """Convert relative date expressions to ISO datetimes.

    LLMs sometimes pass ``"yesterday"`` or ``"today"`` literally
    instead of computing ISO datetimes.  This normalises those
    expressions so string-comparison against ``created_at`` works.

    Additionally, when *role* is ``"until"`` and *value* is date-only
    (10 chars, ``YYYY-MM-DD``), we advance by one day.  A bare-date
    ``until`` would otherwise exclude all records on that day because
    their ``created_at`` timestamps sort *after* the date-only string
    (``"2026-07-20T14:39:19" > "2026-07-20"``).
    """
    today = date.today()

    # Populate cached relative dates if needed
    if not _RELATIVE_DATES["today"]:
        _RELATIVE_DATES["today"] = today.isoformat()
        _RELATIVE_DATES["yesterday"] = (today - timedelta(days=1)).isoformat()
        _RELATIVE_DATES["tomorrow"] = (today + timedelta(days=1)).isoformat()
        _RELATIVE_DATES["now"] = datetime.now().astimezone().isoformat(
            timespec="seconds",
        )

    key = value.strip().lower()
    if key in _RELATIVE_DATES:
        value = _RELATIVE_DATES[key]

    # Date-only until: advance one day so records on that day are
    # included (created_at has a time component that sorts after
    # the bare date).
    if role == "until" and len(value) == 10 and "T" not in value:
        try:
            d = date.fromisoformat(value)
            value = (d + timedelta(days=1)).isoformat()
        except ValueError:
            pass  # Not a valid ISO date; pass through unchanged

    return value


def _split_sql(sql_text: str) -> list[str]:
    """Split SQL text on semicolons, respecting quotes and comments.

    Multi-statement constructs (CREATE TRIGGER … BEGIN … END) are kept
    together so SQLite can parse them as a single statement.  Otherwise
    the interior INSERT / DELETE would be split into orphaned fragments.
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
                in_block = False
                i += 2
                continue
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


def _looks_like_trigger_start(stmt: str) -> bool:
    """True if *stmt* starts a CREATE TRIGGER that has a BEGIN body."""
    upper = stmt.strip().upper()
    return upper.startswith("CREATE TRIGGER") and "BEGIN" in upper


def _looks_like_trigger_end(stmt: str) -> bool:
    """True if *stmt* ends a trigger body (``END;``)."""
    stripped = stmt.strip().rstrip(";").strip().upper()
    return stripped.endswith("END")


def _turn_text_for_embedding(user_message: str, messages: list[dict]) -> str:
    """Extract turn text for embedding: user message + assistant + tool results.

    No truncation — the caller checks against the model's token limit
    and skips embedding entirely if the text is too long.
    """
    parts = [user_message]
    for msg in messages:
        content = msg.get("content", "")
        if content and msg.get("role") in ("assistant", "tool"):
            parts.append(content)
    return "\n".join(p for p in parts if p)


# ── Chunking ────────────────────────────────────────────────────────

CHUNK_SIZE_CHARS = 2000     # ~500 tokens — well under bge-m3's 8192 limit
CHUNK_OVERLAP_LINES = 1     # carry last paragraph into the next chunk


def _chunk_text(
    text: str,
    chunk_size: int = CHUNK_SIZE_CHARS,
    overlap_lines: int = CHUNK_OVERLAP_LINES,
) -> list[str]:
    """Split *text* into overlapping chunks on paragraph boundaries.

    Each chunk is at most *chunk_size* characters (soft limit — a single
    paragraph that exceeds the limit becomes its own chunk).  The last
    *overlap_lines* paragraphs of chunk N become the first paragraphs of
    chunk N+1, preserving cross-chunk context for the embedding model.
    """
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    if not paragraphs:
        return []

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for para in paragraphs:
        # If adding this paragraph would exceed the limit and we already
        # have content, flush the current chunk.
        if current and current_len + len(para) > chunk_size:
            chunks.append("\n".join(current))
            # Keep the last *overlap_lines* paragraphs as context for
            # the next chunk.
            if overlap_lines and len(current) > overlap_lines:
                current = current[-overlap_lines:]
                current_len = sum(len(p) for p in current)
            else:
                current = []
                current_len = 0
        current.append(para)
        current_len += len(para)

    if current:
        chunks.append("\n".join(current))

    return chunks or [text]


def _to_fts5_query(query: str) -> str:
    cleaned = query.replace('"', '').replace("'", "").replace("*", "")
    words = cleaned.split()
    if not words:
        return '""'
    if len(words) == 1:
        return words[0]
    return " AND ".join(words)
