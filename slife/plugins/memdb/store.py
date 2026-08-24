"""Turn store — SQLite database with FTS5 + sqlite-vec hybrid search.

One row = one turn (user message + assistant's complete response).
No sessions, no lifecycle — each turn is independent and immutable.
Restore loads the most recent N turns by rowid.

Agent isolation is at the file level — each agent_name has its own .db file.
"""

import asyncio
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
    from the LLM would otherwise scan the whole table.
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
        self._vec_available = False  # sqlite-vec loaded? embeddings are optional
        # Serializes every mutating statement on the shared connection.  All
        # writers commit on the same aiosqlite connection; without this, one
        # coroutine's commit() can land between another's multi-statement
        # transaction (e.g. the drainer's delete-then-insert replace) and split
        # it — leaving a half-committed chunk set.
        self._write_lock = asyncio.Lock()

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
        if not self._vec_available:
            # sqlite-vec couldn't load (e.g. no bundled extension on this
            # platform) — degrade to keyword-only: no vec0 table, no
            # embedding writes, semantic search stays gated off.
            self._embedding_dim = 0
        await self._run_schema()
        logger.info(
            "store_ready path=%s wal=on vec_dim=%d model=%s",
            self._db_path, self._embedding_dim, embedding_model or "none",
        )

    async def reconfigure_for_embedding(
        self,
        embedding_dim: int,
        embedding_model: str = "",
    ) -> None:
        """Switch the live connection to a real embedding dimension.

        The initial ``setup`` runs with dim 0 (no vec0) so the first save
        never waits on the embedding model.  Once the model is loaded, this
        re-runs the schema on the SAME connection so the vec0 table is
        created with the real width.  Unlike ``setup`` it never reconnects,
        so a concurrent ``save_turn`` is not split across two handles
        (``_c`` stays valid from ``execute`` to ``commit``) and no handle
        leaks.

        Falls back to ``setup`` only when there is no live connection to
        upgrade (defensive — the store was closed).
        """
        if self._conn is None:
            await self.setup(
                embedding_dim=embedding_dim, embedding_model=embedding_model,
            )
            return
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        if not self._vec_available:
            await self._load_vec_extension()
            if not self._vec_available:
                self._embedding_dim = 0
        await self._run_schema()
        logger.info(
            "store_reconfigured vec_dim=%d model=%s",
            self._embedding_dim, embedding_model or "none",
        )

    async def _load_vec_extension(self) -> None:
        """Load sqlite-vec best-effort.

        Embeddings are optional: when the extension can't load (e.g. no
        bundled ``.dylib``/``.so`` for this platform), the store must still
        work — restore and keyword search are independent of vec, and
        semantic search is gated by ``_semantic_ready``.  A hard failure
        here would break the whole store (and session restore) for no gain.
        """
        try:
            import sqlite_vec
            await self._c.enable_load_extension(True)
            await self._c.load_extension(sqlite_vec.loadable_path())
            await self._c.enable_load_extension(False)
            row = await self._c.execute("SELECT vec_version()")
            version = await row.fetchone()
            logger.info("vec_loaded version=%s", version[0] if version else "unknown")
            self._vec_available = True
        except Exception as e:
            self._vec_available = False
            logger.warning("vec_unavailable err=%s — semantic search disabled (keyword only)", e)

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
                # A failed CREATE TRIGGER / FTS / vec0 statement leaves the
                # index missing with no production signal — log it loudly
                # (DEBUG would silently hide a structurally broken DB).
                logger.error("schema_stmt_error err=%s stmt=%.80s", e, stmt)
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
        prompt_tokens: int = 0,
        who_helped: str = "",
        what_model: str = "",
        channel: str = "",
        created_at: str | None = None,
        completed_at: str | None = None,
    ) -> int:
        """Insert a turn. Returns rowid.

        Embedding is an internal plugin concern — the background reindex
        embeds unembedded turns off the save path, so a slow GGUF embed never
        blocks the caller (large turns previously exceeded the 10s save
        timeout).  ``save_turn`` only persists the row.

        ``created_at`` is the user-input timestamp threaded from the TUI
        (the Enter-press moment); ``completed_at`` is the assistant
        completion timestamp (captured after the final ensure).  ``None``
        falls back to the current wall clock.

        ``token_count`` is the turn's cumulative total_tokens (billing);
        ``prompt_tokens`` is the LAST LLM call's prompt_tokens — the exact
        context size at turn end, which restore uses to prime the footer /
        _sys_note with the real exit-time occupancy instead of an estimate.
        """
        now = created_at or _now()
        done = completed_at or _now()
        messages_json = json.dumps(messages or [], ensure_ascii=False)

        async with self._write_lock:
            cursor = await self._c.execute(
                """INSERT INTO diary (user_message, messages, summary, tags,
                                      channel, created_at, completed_at,
                                      who_helped, what_model, token_count,
                                      prompt_tokens)
                   VALUES (?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)""",
                (user_message, messages_json, channel, now, done,
                 who_helped, what_model, token_count, prompt_tokens),
            )
            await self._c.commit()
        rowid = cursor.lastrowid
        assert rowid is not None  # insert just succeeded
        logger.debug("turn_saved rowid=%s", rowid)
        return rowid

    async def get_turn(self, rowid: int) -> dict | None:
        """Return a single turn by rowid."""
        cursor = await self._c.execute(
            "SELECT rowid, * FROM diary WHERE rowid = ?",
            (rowid,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_recent_turns(
        self, limit: int = 50, offset: int = 0, after_rowid: int = 0
    ) -> list[dict]:
        """Return the most recent N turns (from *offset*), newest-first.

        ``offset`` enables batched pagination: the caller fetches 20 at a
        time (newest batch first) and accumulates — each batch is already
        newest-first, so appending batches stays globally newest-first.

        ``after_rowid`` is the persisted live-context boundary (exclusive):
        only turns strictly after it are returned.  Restore passes the
        boundary stored by :meth:`get_context_start` so startup rebuilds
        exactly the context that was live at exit.
        """
        cursor = await self._c.execute(
            """SELECT rowid, user_message, messages, summary, tags,
                      channel, created_at, completed_at,
                      who_helped, what_model, token_count, prompt_tokens
               FROM diary
               WHERE rowid IN (
                   SELECT rowid FROM diary
                   WHERE rowid > ?
                   ORDER BY rowid DESC LIMIT ? OFFSET ?
               )
               ORDER BY rowid DESC""",
            (after_rowid, limit, offset),
        )
        return [dict(row) for row in await cursor.fetchall()]

    # ── Live-context boundary ────────────────────────────────────────
    #
    # The diary is the whole session history; the *live context* is the
    # slice the agent was actually working with (bounded to the window by
    # the internal trim).  ``context_start`` (stored in ``diary_meta``) marks the
    # boundary — every turn with ``rowid <= context_start`` is outside the
    # live context (trimmed or cleared), every newer turn is inside.
    # Restore reads from the boundary so startup rebuilds the exact
    # exit-time context instead of re-slicing an arbitrary percentage.
    # 0 (the default) means "everything" — the first-ever session.

    _CONTEXT_START_KEY = "context_start"

    async def get_context_start(self) -> int:
        """Return the persisted live-context start boundary (exclusive).

        Turns with ``rowid <= boundary`` are outside the live context.
        Absent (fresh DB) or non-numeric → 0 (restore everything).
        """
        cursor = await self._c.execute(
            "SELECT value FROM diary_meta WHERE key = ?",
            (self._CONTEXT_START_KEY,),
        )
        row = await cursor.fetchone()
        if not row:
            return 0
        try:
            return max(int(row[0]), 0)
        except (TypeError, ValueError):
            return 0

    async def set_context_start(self, rowid: int) -> None:
        """Write the live-context start boundary (exclusive)."""
        async with self._write_lock:
            await self._c.execute(
                "INSERT OR REPLACE INTO diary_meta (key, value) "
                "VALUES (?, ?)",
                (self._CONTEXT_START_KEY, str(max(int(rowid), 0))),
            )
            await self._c.commit()

    async def advance_context_start(self, count: int) -> int:
        """Advance the boundary past *count* diary rows; return new boundary.

        Moves the live-context start forward by *count* rows strictly after
        the current boundary and records the result.  Used by the internal
        trim (``AgentLoop._trim_after_save``), which removed that many
        oldest complete turns from the conversation.

        Clamps when fewer than *count* rows remain (a trim after a rollback
        can overshoot by dead rows) — the boundary never overshoots the
        latest row, so restore can only ever under-restore by a bounded,
        searchable margin.

        Callers wanting to flush the whole history (``clear_context``) pass
        a count large enough to clamp to the latest row.
        """
        current = await self.get_context_start()
        if count <= 0:
            return current
        cursor = await self._c.execute(
            "SELECT rowid FROM diary WHERE rowid > ? "
            "ORDER BY rowid ASC LIMIT ?",
            (current, count),
        )
        rows = [r[0] for r in await cursor.fetchall()]
        # Exactly `count` rows after the boundary → those are the trimmed
        # turns, the last one becomes the new (exclusive) boundary.  Fewer
        # rows remain → clamp to the latest row (see docstring).
        boundary = rows[-1] if len(rows) == count else (await self.latest_rowid() or 0)
        await self.set_context_start(boundary)
        logger.info(
            "context_start_advanced boundary=%s count=%d rows=%d",
            boundary, count, len(rows),
        )
        return boundary

    async def set_context_start_latest(self) -> int:
        """Move the boundary to the latest row — everything saved is outside
        the live context.  ``clear_context`` guarantees the next restore is
        a fresh start (only turns saved afterwards come back)."""
        boundary = await self.latest_rowid() or 0
        await self.set_context_start(boundary)
        logger.info("context_start_latest boundary=%s", boundary)
        return boundary

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
        count_row = await row.fetchone()
        total = count_row[0] if count_row else 0

        if query and query.strip():
            mode = mode.lower()
            if mode == "fts5" and _contains_cjk(query):
                # FTS5 unicode61 cannot match a whole-sentence CJK query —
                # search_keyword routes CJK to the LIKE fallback, so the count
                # must do the same or count/search disagree (turn_count=0
                # while turn_search returns hits).
                mode = "grep"
            if mode == "grep":
                # Escape LIKE metacharacters so a pattern containing %/_ matches
                # them literally; the ESCAPE '\' clause is required or the
                # escapes are a no-op. Backslashes must be doubled first — the
                # same rules as search_grep.
                safe = (
                    query.replace("\\", r"\\")
                         .replace("%", r"\%")
                         .replace("_", r"\_")
                )
                like_pattern = f"%{safe}%"
                where = "(user_message LIKE ? ESCAPE '\\' OR messages LIKE ? ESCAPE '\\')"
                params: list = [like_pattern, like_pattern]
            else:
                fts_query = _to_fts5_query(query)
                # FTS5 has no created_at — join the diary rowid so since/until
                # filter the same way as grep/time.
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
                count_row = await row2.fetchone()
                filtered = count_row[0] if count_row else 0
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
            count_row = await row2.fetchone()
            filtered = count_row[0] if count_row else 0
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
            count_row = await row2.fetchone()
            filtered = count_row[0] if count_row else 0
        else:
            filtered = total

        return {"total": total, "filtered": filtered,
                "since": since, "until": until,
                "query": query, "mode": mode if query else None}

    # ── Browse ─────────────────────────────────────────────────────

    async def list_recent(
        self, limit: int = 20,
        before_rowid: int | None = None,
        after_rowid: int | None = None,
    ) -> list[dict]:
        """List turns, newest first. Lightweight — no full messages.

        ``before_rowid`` / ``after_rowid`` anchor the window by rowid
        (exclusive) so the LLM can page the diary from a ``[Turn: N · …]``
        footnote: ``before_rowid`` = older turns only, ``after_rowid`` =
        newer turns only.
        """
        limit = _clamp_limit(limit)
        clauses: list[str] = []
        params: list = []
        if before_rowid is not None:
            clauses.append("rowid < ?")
            params.append(before_rowid)
        if after_rowid is not None:
            clauses.append("rowid > ?")
            params.append(after_rowid)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT rowid, user_message, summary, tags, created_at,
                      token_count, who_helped, what_model
               FROM diary{where}
               ORDER BY rowid DESC
               LIMIT ?""",
            params,
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def token_usage(
        self,
        rowid: int | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Token consumption by turn, optionally filtered.

        Returns the matching turns (newest-first) with their billing
        (``token_count`` = cumulative total_tokens) and context size
        (``prompt_tokens`` = the last call's prompt_tokens), plus a summary
        of totals / averages across the filtered set.

        ``rowid`` narrows to a single turn; ``since``/``until`` filter by
        ``created_at`` (ISO datetime, relative expressions accepted via
        :func:`_normalize_time_param`).
        """
        clauses: list[str] = []
        params: list = []
        if rowid is not None:
            clauses.append("rowid = ?")
            params.append(rowid)
        if since:
            since = _normalize_time_param(since, role="since")
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            until = _normalize_time_param(until, role="until")
            clauses.append("created_at <= ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = _clamp_limit(limit)
        params.append(limit)

        cursor = await self._c.execute(
            f"""SELECT rowid, user_message, created_at, completed_at,
                      token_count, prompt_tokens
               FROM diary{where}
               ORDER BY rowid DESC
               LIMIT ?""",
            params,
        )
        rows = [dict(r) for r in await cursor.fetchall()]

        total_billed = sum(r.get("token_count") or 0 for r in rows)
        total_context = sum(r.get("prompt_tokens") or 0 for r in rows)
        return {
            "turns": rows,
            "summary": {
                "count": len(rows),
                "total_token_count": total_billed,
                "total_prompt_tokens": total_context,
                "avg_token_count": (total_billed // len(rows))
                if rows else 0,
            },
            "filters": {"rowid": rowid, "since": since, "until": until},
        }

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
        async with self._write_lock:
            await self._c.execute(
                f"UPDATE diary SET {', '.join(updates)} WHERE rowid = ?",
                params,
            )
            await self._c.commit()

    async def latest_rowid(self) -> int | None:
        """Rowid of the newest turn, or None if the diary is empty."""
        cursor = await self._c.execute(
            "SELECT rowid FROM diary ORDER BY rowid DESC LIMIT 1"
        )
        row = await cursor.fetchone()
        return row[0] if row else None

    # ── Search ──────────────────────────────────────────────────────

    async def search_keyword(
        self, query: str, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """FTS5 keyword search with snippet highlighting."""
        limit = _clamp_limit(limit)
        # FTS5 unicode61 does not segment CJK — a whole-sentence Chinese
        # query becomes a phrase/run token that never matches a longer turn.
        # Substring matching is what Chinese users expect, so route CJK
        # queries to the LIKE fallback (same shape, so callers and
        # merge_hybrid are agnostic to the backend).
        if _contains_cjk(query):
            return await self._search_like(
                query, limit=limit, since=since, until=until,
            )
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
                          snippet(diary_fts, 0, '…', '…', '…', 40) AS snippet, rank
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

    async def _search_like(
        self, pattern: str, limit: int,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Substring (LIKE) search over the searchable columns.

        CJK fallback for :meth:`search_keyword` — FTS5 unicode61 cannot
        segment Chinese, so whole-sentence queries return nothing.  The
        pattern is split on whitespace and every word must appear in some
        column (AND semantics — the same space-splitting ``_to_fts5_query``
        applies), while each CJK word matches by substring.  Returns the
        same shape as ``search_keyword`` (``snippet`` + ``rank``); rank is
        a constant 0, ordering is newest-first.
        """
        words = [w for w in pattern.split() if w]
        if not words:
            return []
        and_clauses: list[str] = []
        params: list[str | int] = [words[0]]  # instr context anchors on the first word
        for w in words:
            # Escape LIKE metacharacters so %/_ match literally —
            # same escaping as search_grep.
            safe = (
                w.replace("\\", r"\\")
                 .replace("%", r"\%")
                 .replace("_", r"\_")
            )
            like = f"%{safe}%"
            and_clauses.append(
                "(user_message LIKE ? ESCAPE '\\' OR messages LIKE ? ESCAPE '\\'"
                " OR summary LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like, like])
        time_clauses = ""
        if since:
            since = _normalize_time_param(since, role="since")
            time_clauses += " AND created_at >= ?"
            params.append(since)
        if until:
            until = _normalize_time_param(until, role="until")
            time_clauses += " AND created_at <= ?"
            params.append(until)
        params.append(limit)
        cursor = await self._c.execute(
            f"""SELECT rowid, user_message, summary, tags, created_at,
                      substr(messages, max(0, instr(messages, ?) - 40), 160) AS snippet,
                      0 AS rank
               FROM diary
               WHERE {" AND ".join(and_clauses)}
                     {time_clauses}
               ORDER BY rowid DESC LIMIT ?""",
            params,
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug("search_like_cjk pattern=%s hits=%s", pattern[:80], len(results))
        return results

    async def search_semantic(
        self, embedding: list[float], limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """sqlite-vec KNN on turn_embedding, deduplicated by diary_rowid.

        A single turn can produce multiple chunks — we keep only the best
        (lowest distance) match per turn so the result list has one entry
        per turn.
        """
        # No vec0 table when embedding is disabled (dim 0) — semantic search
        # degrades to keyword-only (the caller keeps the FTS5 half).
        if self._embedding_dim <= 0:
            return []
        limit = _clamp_limit(limit)
        vec_blob = _serialize_f32(embedding)
        # Fetch extra rows to account for duplicate diary_rowid entries
        # (one turn → multiple chunks).  Dedup in Python: vec0 KNN does
        # not allow GROUP BY.
        #
        # With a since/until window, use a larger pool: vec0 KNN is global
        # nearest-neighbour — it cannot constrain the search inside the time
        # window — so the window is filtered in Python afterwards.  The wider
        # pool reduces the chance that the in-window turns are all outside the
        # fetched KNN results.
        fetch_limit = (limit * 8) if (since or until) else (limit * 2)
        # sqlite-vec forbids ANY auxiliary-column constraint — including a
        # JOIN ON — inside a KNN query ("illegal WHERE constraint on a vec0
        # auxiliary column").  So the KNN runs alone (no JOIN) and the diary
        # lookup is a separate query below.
        cursor = await self._c.execute(
            """SELECT rowid, diary_rowid, summary, tags, created_at, distance
               FROM diary_semantic
               WHERE turn_embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (vec_blob, fetch_limit),
        )
        # Deduplicate by diary_rowid — keep best (lowest) distance per turn
        seen: set[int] = set()
        results: list[dict] = []
        for row in await cursor.fetchall():
            r = dict(row)
            rid = r.get("diary_rowid")
            if rid is not None and rid not in seen:
                seen.add(rid)
                r["rowid"] = rid  # merge_hybrid keys on rowid (= diary_rowid)
                results.append(r)
        if since:
            since = _normalize_time_param(since, role="since")
            results = [r for r in results if r.get("created_at", "") >= since]
        if until:
            until = _normalize_time_param(until, role="until")
            results = [r for r in results if r.get("created_at", "") <= until]
        results = results[:limit]
        # Fetch user_message for the surviving turns — a second query, since
        # the KNN query must not join the diary table.
        if results:
            rowids = [r["diary_rowid"] for r in results]
            ph = ",".join("?" * len(rowids))
            cur = await self._c.execute(
                f"SELECT rowid, user_message FROM diary WHERE rowid IN ({ph})",
                rowids,
            )
            msgs = {r["rowid"]: r["user_message"] for r in await cur.fetchall()}
            for r in results:
                r["user_message"] = msgs.get(r["diary_rowid"], "")
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
        # no-op ; backslashes themselves must be doubled first.
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
        """Insert one chunk embedding for a turn.

        Each turn can produce multiple chunks — *chunk_index* is 0-based.
        Always INSERTs; the caller clears old chunks (``replace_embedding_chunks``
        does delete-then-insert atomically).
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

    async def replace_embedding_chunks(
        self, doc: dict, embeddings: list[list[float]],
    ) -> None:
        """Atomically replace a document's embedding chunks.

        Deletes the document's old chunks and inserts every new chunk in ONE
        transaction.  A crash (or error) mid-way rolls back to NO chunks —
        the document is fully unembedded again and gets re-indexed on the next
        pass, instead of being left half-indexed where the ``NOT IN
        diary_semantic`` unembedded query would mistake it for complete.
        ``doc`` is a drainer row: ``doc_id`` plus ``summary`` / ``tags`` /
        ``created_at`` (which are stored on every chunk for display).
        """
        if self._embedding_dim <= 0:
            return
        diary_rowid = doc["doc_id"]
        summary = doc.get("summary", "")
        tags = doc.get("tags", "")
        created_at = doc.get("created_at", "")
        vec_blobs = [_serialize_f32(emb) for emb in embeddings]
        async with self._write_lock:
            try:
                await self._c.execute(
                    "DELETE FROM diary_semantic WHERE diary_rowid = ?",
                    (diary_rowid,),
                )
                for idx, blob in enumerate(vec_blobs):
                    await self._c.execute(
                        """INSERT INTO diary_semantic
                           (turn_embedding, diary_rowid, chunk_index,
                            summary, tags, created_at)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (blob, diary_rowid, idx, summary, tags, created_at),
                    )
                await self._c.commit()
            except Exception:
                await self._c.rollback()
                raise
        logger.debug(
            "embedding_chunks_replaced diary_rowid=%s chunks=%d",
            diary_rowid, len(vec_blobs),
        )

    async def _clear_chunks(self, diary_rowid: int) -> None:
        """Delete all embedding chunks for a turn (prep for re-embed)."""
        async with self._write_lock:
            await self._c.execute(
                "DELETE FROM diary_semantic WHERE diary_rowid = ?",
                (diary_rowid,),
            )
            await self._c.commit()

    # A turn is "embeddable" when it has any text worth embedding.  Turns with
    # no user text AND no messages (or an empty message list) can never be
    # embedded — excluding them from the unembedded count lets the semantic
    # gate open instead of stalling forever on the same zero-text rows.
    _EMBEDDABLE_TEXT = (
        "trim(COALESCE(d.user_message, '')) != '' "
        "OR (d.messages IS NOT NULL AND trim(d.messages) NOT IN ('', '[]'))"
    )

    async def get_unembedded_docs(self, limit: int = 100) -> list[dict]:
        """Return documents (turns) that have no embedding in diary_semantic.

        These need re-indexing after embedding config is added or changed.
        Each row carries the SemanticManager drainer's shape: ``doc_id``
        (the turn rowid), ``text`` (the embed-ready turn text) and
        ``summary`` / ``tags`` / ``created_at``.
        """
        if self._embedding_dim <= 0:
            return []
        cursor = await self._c.execute(
            f"""SELECT d.rowid AS doc_id, d.user_message, d.messages, d.summary,
                      d.tags, d.created_at
               FROM diary d
               WHERE d.rowid NOT IN (
                   SELECT DISTINCT diary_rowid FROM diary_semantic
               )
                 AND ({self._EMBEDDABLE_TEXT})
               ORDER BY d.rowid
               LIMIT ?""",
            (limit,),
        )
        docs = []
        for row in await cursor.fetchall():
            r = dict(row)
            docs.append({
                "doc_id": r["doc_id"],
                "text": _turn_text_for_embedding(
                    r["user_message"], json.loads(r.get("messages") or "[]"),
                ),
                "summary": r.get("summary", ""),
                "tags": r.get("tags", ""),
                "created_at": r.get("created_at", ""),
            })
        return docs

    async def count_unembedded(self) -> int:
        """Count turns that need re-indexing."""
        if self._embedding_dim <= 0:
            return 0
        cursor = await self._c.execute(
            f"""SELECT COUNT(*) FROM diary d
               WHERE d.rowid NOT IN (
                   SELECT DISTINCT diary_rowid FROM diary_semantic
               )
                 AND ({self._EMBEDDABLE_TEXT})""",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def count_embedded(self) -> int:
        """Count distinct turns that have at least one embedding chunk."""
        if self._embedding_dim <= 0:
            return 0
        cursor = await self._c.execute(
            "SELECT COUNT(DISTINCT diary_rowid) FROM diary_semantic",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def clear_all_embeddings(self) -> int:
        """Delete all rows from diary_semantic. Returns count deleted."""
        async with self._write_lock:
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

    # Populate / refresh cached relative dates.  Refresh on calendar-date
    # rollover — a long-running server must not serve yesterday's "today".
    today_iso = today.isoformat()
    if _RELATIVE_DATES["today"] != today_iso:
        _RELATIVE_DATES["today"] = today_iso
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

    # Normalize offset-aware ISO datetimes to the local offset.  created_at
    # is stored in local time (via _now()); the LLM may pass UTC ("Z") or a
    # different offset, which would misorder a lexicographic comparison
    # across the offset boundary.  A naive datetime is already local and is
    # left unchanged.
    if "T" in value:
        try:
            dt = datetime.fromisoformat(value)
            if dt.tzinfo is not None:
                value = dt.astimezone().isoformat(timespec="seconds")
        except ValueError:
            pass  # not a parseable ISO datetime; pass through unchanged

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
    """True if *stmt* starts a CREATE TRIGGER that has a BEGIN body.

    Tolerates leading ``--`` comment lines (e.g. the comment block above the
    diary_au trigger): the comments accumulate into the same fragment and must
    not hide the CREATE TRIGGER keyword — otherwise the trigger body's interior
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


def _split_chunks_to_token_limit(chunks: list[str], max_tokens: int) -> list[str]:
    """Hard-split any chunk exceeding *max_tokens* so none is silently dropped.

    ``_chunk_text`` splits on paragraph boundaries, so a single newline-free
    paragraph longer than the limit becomes one oversized chunk.  Dropping it
    (the previous behavior) left the turn permanently unembedded, which kept
    ``count_unembedded()`` > 0 and locked the semantic-search gate off forever.
    Hard-splitting by character (never a partial code point) keeps every turn
    embeddable so the index can always complete.
    """
    if max_tokens <= 0:
        return chunks
    out: list[str] = []
    for c in chunks:
        char_limit = _char_limit_for_tokens(max_tokens, c)
        while len(c) > char_limit:
            out.append(c[:char_limit])
            c = c[char_limit:]
        if c:
            out.append(c)
    return out


def _char_limit_for_tokens(max_tokens: int, text: str) -> int:
    """Chars that fit in *max_tokens* for a mixed CJK/Latin string.

    CJK is ~1 char/token; Latin ~4 chars/token.  A fixed ``max_tokens * 4``
    over-allocates for CJK-heavy text, so a chunk that fits by characters still
    exceeds the model's real token limit and the embed fails — stalling the
    drainer (the completeness gate never opens).
    """
    if not text:
        return max_tokens
    cjk = sum(1 for ch in text if _contains_cjk(ch))
    other = len(text) - cjk
    est_tokens = cjk + other / 4
    per_char = est_tokens / len(text)
    return max(1, int(max_tokens / per_char))


def _contains_cjk(text: str) -> bool:
    """True if *text* contains CJK ideographs (incl. Extension A).

    SQLite FTS5's unicode61 tokenizer does not segment Chinese — a
    whole-sentence CJK query becomes a phrase/run token that never matches
    a longer stored turn.  Substring (LIKE) matching is what Chinese users
    expect, so search_keyword routes CJK queries to it.
    """
    return any(
        "㐀" <= ch <= "䶿" or "一" <= ch <= "鿿"
        for ch in text
    )


def _to_fts5_query(query: str) -> str:
    cleaned = query.replace('"', '').replace("'", "").replace("*", "")
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
