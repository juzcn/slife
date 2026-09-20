"""Turn store — SQLite database with FTS5 + sqlite-vec hybrid search.

One row = one turn (user message + assistant's complete response).
No sessions, no lifecycle — each turn is independent and immutable.
Restore loads the most recent N turns by rowid.

Agent isolation is at the file level — each agent_name has its own .db file.
"""

import asyncio
import json
import logging
import re
import struct
from collections.abc import Callable
from pathlib import Path

import aiosqlite
import sqlparse

from slife.timeutil import normalize_time_bound, now_local_seconds
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIM = 1536

#: Local ISO-seconds timestamp — the shared helper under the store's name
#: (memfiles/mcp_gateway stores alias it the same way).
_now = now_local_seconds


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


async def _fetch_all_bounded(cursor) -> list:
    """Fetch a heavy read's rows with no inner timeout.

    DB reads are NOT timed out by design: reads never mutate, so a cancel
    cannot split a transaction, and the calling tool's overall bound at the
    loop still applies.  Writes are never wrapped here for the same reason —
    cancelling between multi-statement write statements could split a
    transaction.
    """
    return await cursor.fetchall()


def _like_escape(pattern: str) -> str:
    """Escape LIKE metacharacters so ``%``/``_`` match literally.

    Shared by every hybrid-search store (memdb / memfiles / mcp_gateway) —
    the ESCAPE '\\' clause requires the backslash to be doubled first.
    """
    return pattern.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


class VecStoreLifecycleMixin:
    """sqlite-vec store lifecycle shared by memdb and memfiles.

    ``setup`` / ``reconfigure_for_embedding`` / ``_load_vec_extension`` /
    ``_run_schema`` / ``_maybe_migrate_vec_tables`` are the same algorithm
    in both stores; this base parameterizes only what differs — the vec0
    semantic tables to migrate, the meta table that records the active
    embedding model, the schema file, and the structured-log keys.
    """

    #: vec0 semantic table names this store migrates (dropped + recreated
    #: when the embedding dimension, the model, or the distance metric
    #: differs from the schema's).
    _semantic_tables: tuple[str, ...] = ()
    #: Meta table holding the ``embedding_model`` key.
    _meta_table: str = "meta"
    #: Directory holding this store's ``schema.sql``.
    _schema_dir: Path
    #: Structured-log key prefix (memdb uses ``store_*``, memfiles
    #: ``memfiles_*``).
    _store_log_key: str = "store"

    #: State owned by the concrete store (its ``__init__`` / ``setup``).
    _db_path: Path
    _conn: "aiosqlite.Connection | None"
    _embedding_dim: int
    _embedding_model: str
    _vec_available: bool
    #: Why sqlite-vec could not load ("" when it did) — a fact ``__check``
    #: reports, so the report can name the cause instead of "unavailable".
    _vec_reason: str

    @property
    def _c(self) -> "aiosqlite.Connection":
        """The live connection — the concrete store asserts and returns it."""
        raise NotImplementedError

    async def setup(
        self,
        embedding_dim: int = DEFAULT_EMBEDDING_DIM,
        embedding_model: str = "",
    ) -> None:
        self._embedding_dim = embedding_dim
        self._embedding_model = embedding_model
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(
            str(self._db_path), timeout=_timeouts.timeouts.storage.sqlite_busy,
        )
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        # Embeddings are optional AND semantically deferred: when no real
        # dimension is applied here (dim == 0), don't pay to load the
        # sqlite-vec DLL inside the startup-critical lifespan.  The
        # reconfigure path (SemanticManager.enable after the handshake)
        # loads it the first time a real dimension is applied.
        if embedding_dim > 0:
            await self._load_vec_extension()
        if not self._vec_available:
            # sqlite-vec couldn't load (e.g. no bundled extension on this
            # platform) — degrade to keyword-only: no vec0 table, no
            # embedding writes, semantic search stays gated off.
            self._embedding_dim = 0
        await self._run_schema()
        logger.info(
            "%s_ready path=%s wal=on vec_dim=%d model=%s",
            self._store_log_key, self._db_path, self._embedding_dim,
            embedding_model or "none",
        )

    async def reconfigure_for_embedding(
        self, embedding_dim: int, embedding_model: str = "",
    ) -> None:
        """Switch the live connection to a real embedding dimension.

        The initial ``setup`` runs with dim 0 (no vec0) so the first save
        never waits on the embedding model.  Once the model is loaded, this
        re-runs the schema on the SAME connection so the vec0 tables are
        created with the real width.  Unlike ``setup`` it never reconnects,
        so a concurrent save is not split across two handles and no handle
        leaks.  Falls back to ``setup`` when there is no live connection to
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
            "%s_reconfigured vec_dim=%d model=%s",
            self._store_log_key, self._embedding_dim,
            embedding_model or "none",
        )

    async def _load_vec_extension(self) -> None:
        """Load sqlite-vec best-effort, recording WHY when it cannot.

        Embeddings are optional: when the extension can't load, the store
        must still work — the vec0 tables are skipped and semantic search
        stays gated off.  A hard failure here would break keyword-only
        operation for no gain.

        The reason is kept as a fact because the two causes are not the same
        thing: a wheel missing this OS's binary is a packaging accident, while
        ``enable_load_extension`` being ABSENT is the interpreter — CPython
        builds sqlite3 without loadable-extension support unless configured
        with ``--enable-loadable-sqlite-extensions`` (Apple's system Python and
        the python.org installers are the common ones).  No extension can load
        under such a Python, sqlite-vec included, whatever the OS ships.
        """
        if not hasattr(self._c, "enable_load_extension"):
            self._vec_available = False
            self._vec_reason = (
                "this Python's sqlite3 cannot load extensions "
                "(no enable_load_extension)"
            )
            logger.warning(
                "%s_vec_unavailable err=no_enable_load_extension — semantic "
                "search disabled (keyword only)", self._store_log_key,
            )
            return
        try:
            import sqlite_vec
            await self._c.enable_load_extension(True)
            await self._c.load_extension(sqlite_vec.loadable_path())
            await self._c.enable_load_extension(False)
            row = await self._c.execute("SELECT vec_version()")
            version = await row.fetchone() if row else None
            logger.info(
                "%s_vec_loaded version=%s", self._store_log_key,
                version[0] if version else "unknown",
            )
            self._vec_available = True
            self._vec_reason = ""
        except Exception as e:
            self._vec_available = False
            self._vec_reason = str(e)
            logger.warning(
                "%s_vec_unavailable err=%s — semantic search disabled "
                "(keyword only)", self._store_log_key, e,
            )

    async def _run_schema(self) -> None:
        """Run this store's ``schema.sql`` and reconcile embedding state.

        Statement-by-statement (vec0 tables hang in aiosqlite's
        ``executescript``); vec0 CREATEs are skipped when no embedding
        backend is configured.  Then migrate away from a stale vec0
        dimension/model and run the store-specific post-schema audit.
        """
        keep = None if self._embedding_dim > 0 else _not_vec0_create
        await run_schema(
            self._c, self._schema_dir / "schema.sql",
            self._embedding_dim, keep=keep,
            log_prefix=f"{self._store_log_key}_schema",
        )
        await self._maybe_migrate_vec_tables()
        await self._post_schema_check()
        logger.debug("schema_ready path=%s", self._db_path)

    async def _post_schema_check(self) -> None:
        """Store-specific schema post-check — nothing by default."""

    async def _maybe_migrate_vec_tables(self) -> None:
        """Drop + recreate the vec0 tables when they no longer match the schema.

        ``CREATE TABLE IF NOT EXISTS`` never touches an existing table, so
        three things have to be compared by reading the LIVE DDL:

        - **dimension** — a change resizes the table, and old vectors are
          invalid;
        - **model** — a different embedding model is a different vector space,
          even at the same width (the identity is kept in ``_meta_table``);
        - **distance metric** — a table created before the schema declared
          ``distance_metric=cosine`` still measures L2, and the search's
          0–1 ``similarity`` is ``1 - distance``: on an L2 table that is not
          the cosine, so the numbers would be plausible and wrong rather than
          visibly broken.  Compared against the SCHEMA FILE rather than a
          constant, so the two cannot drift.

        A rebuilt table is empty, and the background drainer repopulates it —
        the same path a model switch already takes.  Skips when no embedding
        backend is configured (dim ≤ 0): there is nothing to size the table
        for yet, and the next start with a backend does the work.
        """
        import re

        if self._embedding_dim <= 0:
            return
        cursor = await self._c.execute(
            f"SELECT value FROM {self._meta_table} "
            "WHERE key = 'embedding_model'",
        )
        row = await cursor.fetchone()
        stored_model = row[0] if (row and isinstance(row[0], str)) else ""
        model_identity = self._embedding_model or ""
        schema_metric = self._schema_vec_metric()

        migrated = False
        for sem in self._semantic_tables:
            cursor = await self._c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (sem,),
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
                model_identity and stored_model and stored_model != model_identity
            )
            metric_changed = bool(create_sql) and (
                self._vec_metric(create_sql) != schema_metric
            )
            table_missing = not create_sql
            if dim_changed or model_changed or metric_changed or table_missing:
                logger.info(
                    "%s_vec_migrate table=%s dim=%s→%s model=%s→%s metric=%s→%s",
                    self._store_log_key, sem, existing_dim,
                    self._embedding_dim, stored_model, model_identity,
                    self._vec_metric(create_sql), schema_metric,
                )
                await self._c.execute(f"DROP TABLE IF EXISTS {sem}")
                migrated = True
        if migrated:
            await self._c.commit()
            # Recreate only the vec0 CREATE statements, at the new width.
            await run_schema(
                self._c, self._schema_dir / "schema.sql",
                self._embedding_dim, keep=_vec0_create,
                log_prefix=f"{self._store_log_key}_vec_recreate",
            )
        # Persist the new model identity (the first run records it too).
        if model_identity and model_identity != stored_model:
            await self._c.execute(
                f"INSERT OR REPLACE INTO {self._meta_table} (key, value) "
                "VALUES ('embedding_model', ?)",
                (model_identity,),
            )
            await self._c.commit()

    @staticmethod
    def _vec_metric(create_sql: str) -> str:
        """The distance metric a vec0 CREATE declares (sqlite-vec's default is L2)."""
        import re

        m = re.search(
            r"distance_metric\s*=\s*(\w+)", create_sql or "", re.IGNORECASE,
        )
        return m.group(1).lower() if m else "l2"

    def _schema_vec_metric(self) -> str:
        """The metric this store's ``schema.sql`` declares for its vec0 tables."""
        try:
            text = (self._schema_dir / "schema.sql").read_text(encoding="utf-8")
        except OSError:
            return "l2"          # unreadable schema → do not force a rebuild
        return self._vec_metric(text)


class SessionStore(VecStoreLifecycleMixin):
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

    # ── Lifecycle (VecStoreLifecycleMixin) ─────────────────────────

    _semantic_tables = ("diary_semantic",)
    _meta_table = "diary_meta"
    _schema_dir = Path(__file__).parent

    async def _post_schema_check(self) -> None:
        """Audit the diary schema for the legacy ``prompt_tokens`` column.

        A diary table still carrying ``prompt_tokens`` predates the rename to
        ``context_tokens`` (CREATE IF NOT EXISTS never alters an existing
        table).  The new code SELECT/INSERTs ``context_tokens``, so such a DB
        fails on the next save or restore — surface the one-time migration
        path loudly.
        """
        try:
            cursor = await self._c.execute("PRAGMA table_info(diary)")
            cols = [r[1] for r in await cursor.fetchall()]
            if "prompt_tokens" in cols and "context_tokens" not in cols:
                logger.error(
                    "diary_legacy_column prompt_tokens still present — run "
                    "`python scripts/migrate_context_tokens.py` to rename to "
                    "context_tokens (path=%s)", self._db_path,
                )
        except Exception:
            pass

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
        context_tokens: int = 0,
        who_helped: str = "",
        what_model: str = "",
        channel: str = "",
        channel_data: str = "",
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
        ``context_tokens`` is the LAST LLM call's prompt_tokens plus its
        completion_tokens — the exact token count of the persisted history
        as the next request would re-send it, which restore uses to prime
        ``_turn_prompt`` with the real exit-time occupancy instead of an
        estimate.

        ``channel`` is the identity string; ``channel_data`` is the JSON
        payload for the channel's own fields (A2A peer name, subagent
        name/task, …), written to the sibling ``turn_channel`` table under
        the same lock/commit as the diary row.  An empty payload writes no
        row (see :meth:`get_recent_turns`, which tolerates missing rows).
        """
        now = created_at or _now()
        done = completed_at or _now()
        messages_json = json.dumps(messages or [], ensure_ascii=False)

        async with self._write_lock:
            cursor = await self._c.execute(
                """INSERT INTO diary (user_message, messages, summary, tags,
                                      channel, created_at, completed_at,
                                      who_helped, what_model, token_count,
                                      context_tokens)
                   VALUES (?, ?, '', '', ?, ?, ?, ?, ?, ?, ?)""",
                (user_message, messages_json, channel, now, done,
                 who_helped, what_model, token_count, context_tokens),
            )
            if channel_data and channel_data != "{}":
                # Per-channel payload rides a sibling row — CREATE IF NOT
                # EXISTS covers existing DBs, no ALTER/migration needed.
                await self._c.execute(
                    "INSERT INTO turn_channel (turn_id, data) VALUES (?, ?)",
                    (cursor.lastrowid, channel_data),
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
                      who_helped, what_model, token_count, context_tokens
               FROM diary
               WHERE rowid IN (
                   SELECT rowid FROM diary
                   WHERE rowid > ?
                   ORDER BY rowid DESC LIMIT ? OFFSET ?
               )
               ORDER BY rowid DESC""",
            (after_rowid, limit, offset),
        )
        rows = [dict(row) for row in await cursor.fetchall()]
        if rows:
            # Merge the per-channel payload rows (sibling table, one per
            # turn).  Missing rows (pre-feature turns / schema-bypass
            # fixtures) read back as "{}" so callers fall back to the
            # identity string.  The table itself is created on existing DBs
            # by setup() → _run_schema (CREATE IF NOT EXISTS).
            ids = [r["rowid"] for r in rows]
            placeholders = in_placeholders(len(ids))
            cur = await self._c.execute(
                "SELECT turn_id, data FROM turn_channel "
                f"WHERE turn_id IN ({placeholders})",
                ids,
            )
            payloads = {r["turn_id"]: r["data"] for r in await cur.fetchall()}
            for r in rows:
                r["channel_data"] = payloads.get(r["rowid"], "{}")
        return rows

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
        the current boundary and records the result.  The same cut-op backs
        every context cut: the internal trim (``AgentLoop._trim_after_save``)
        passes the turns it evicted; ``clear_context`` passes a deliberately
        generous count so the window runs to the end — a one-shot clear is
        one big trim.

        The rule is always the same: the boundary becomes the last row in
        the *count*-row window, or stays put when no rows remain.  It never
        moves backward and never overshoots the newest row (a generous count
        from a clear simply lands on the last row).
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
        # Last row in the *count*-row window is the new (exclusive) boundary;
        # nothing left after the current boundary → stay put.  No derived
        # "latest row" snapshot — the window itself is authoritative.
        boundary = rows[-1] if rows else current
        await self.set_context_start(boundary)
        logger.info(
            "context_start_advanced boundary=%s count=%d rows=%d",
            boundary, count, len(rows),
        )
        return boundary

    async def has_turns(self) -> bool:
        """Check if there are any turns.

        Test-only helper — no production callers (recounted by
        ``count_turns`` / the unembedded queries where it matters).
        """
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

        if query and query.strip() and mode.lower() == "grep":
            # grep is a regex, so the count goes through the SAME scan the
            # search does — otherwise turn_count and turn_search would report
            # different numbers for one query, which is the disagreement the
            # fts5 fallback below already guards against.
            hits = await self._grep_scan(
                re.compile(query), since, until, hard_limit=self._GREP_SCAN_LIMIT,
            )
            return {"total": total, "filtered": len(hits), "query": query,
                    "mode": "grep", "since": since, "until": until}

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
                like_pattern = f"%{_like_escape(query)}%"
                where = "(user_message LIKE ? ESCAPE '\\' OR messages LIKE ? ESCAPE '\\')"
                params: list = [like_pattern, like_pattern]
            else:
                fts_query = _to_fts5_query(query)
                # FTS5 has no created_at — join the diary rowid so since/until
                # filter the same way as grep/time.
                time_clauses = ""
                time_params: list[str] = []
                if since:
                    since = normalize_time_bound(since, role="since")
                    time_clauses += " AND d.created_at >= ?"
                    time_params.append(since)
                if until:
                    until = normalize_time_bound(until, role="until")
                    time_clauses += " AND d.created_at <= ?"
                    time_params.append(until)
                try:
                    row2 = await self._c.execute(
                        f"""SELECT COUNT(*) FROM diary_fts fts
                            JOIN diary d ON fts.rowid = d.rowid
                            WHERE diary_fts MATCH ?{time_clauses}""",
                        (fts_query, *time_params),
                    )
                    count_row = await row2.fetchone()
                    filtered = count_row[0] if count_row else 0
                except aiosqlite.OperationalError as e:
                    # Mirror search_keyword's guard (D3): a MATCH syntax error
                    # must not make count and search disagree — treat it as no
                    # matches, never surface {"error": ...}.
                    logger.debug(
                        "turn_count_fts_parse_error query=%s err=%s", query, e,
                    )
                    filtered = 0
                return {"total": total, "filtered": filtered,
                        "query": query, "mode": mode,
                        "since": since, "until": until}

            if since:
                since = normalize_time_bound(since, role="since")
                where += " AND created_at >= ?"
                params.append(since)
            if until:
                until = normalize_time_bound(until, role="until")
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
                since = normalize_time_bound(since, role="since")
                clauses.append("created_at >= ?")
                params.append(since)
            if until:
                until = normalize_time_bound(until, role="until")
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
        (exclusive) so the LLM can page the diary from a
        ``[INFO: {"turn_id": N, …}]`` footnote: ``before_rowid`` = older
        turns only, ``after_rowid`` = newer turns only.
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
        (``context_tokens`` = the last call's prompt + completion tokens,
        i.e. the persisted history the next request would re-send), plus a
        summary of totals / averages across the filtered set.

        ``rowid`` narrows to a single turn; ``since``/``until`` filter by
        ``created_at`` (ISO datetime/date or a relative word like ``today``,
        normalized via :func:`~slife.timeutil.normalize_time_bound`).
        """
        clauses: list[str] = []
        params: list = []
        if rowid is not None:
            clauses.append("rowid = ?")
            params.append(rowid)
        if since:
            since = normalize_time_bound(since, role="since")
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            until = normalize_time_bound(until, role="until")
            clauses.append("created_at <= ?")
            params.append(until)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = _clamp_limit(limit)
        params.append(limit)

        cursor = await self._c.execute(
            f"""SELECT rowid, created_at,
                      token_count, context_tokens
               FROM diary{where}
               ORDER BY rowid DESC
               LIMIT ?""",
            params,
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        for r in rows:
            r["turn_id"] = r.pop("rowid")

        total_billed = sum(r.get("token_count") or 0 for r in rows)
        total_context = sum(r.get("context_tokens") or 0 for r in rows)
        return {
            "turns": rows,
            "summary": {
                "count": len(rows),
                "total_token_count": total_billed,
                "total_context_tokens": total_context,
                "avg_token_count": (total_billed // len(rows))
                if rows else 0,
                "avg_context_tokens": (total_context // len(rows))
                if rows else 0,
            },
            "filters": {"turn_id": rowid, "since": since, "until": until},
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
            since = normalize_time_bound(since, role="since")
            time_clauses += " AND d.created_at >= ?"
            time_params.append(since)
        if until:
            until = normalize_time_bound(until, role="until")
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
            results = [dict(row) for row in await _fetch_all_bounded(cursor)]
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
            safe = _like_escape(w)
            like = f"%{safe}%"
            and_clauses.append(
                "(user_message LIKE ? ESCAPE '\\' OR messages LIKE ? ESCAPE '\\'"
                " OR summary LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\')"
            )
            params.extend([like, like, like, like])
        time_clauses = ""
        if since:
            since = normalize_time_bound(since, role="since")
            time_clauses += " AND created_at >= ?"
            params.append(since)
        if until:
            until = normalize_time_bound(until, role="until")
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
        results = [dict(row) for row in await _fetch_all_bounded(cursor)]
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
        for row in await _fetch_all_bounded(cursor):
            r = dict(row)
            rid = r.get("diary_rowid")
            if rid is not None and rid not in seen:
                seen.add(rid)
                r["rowid"] = rid  # merge_hybrid keys on rowid (= diary_rowid)
                results.append(r)
        if since:
            since = normalize_time_bound(since, role="since")
            results = [r for r in results if r.get("created_at", "") >= since]
        if until:
            until = normalize_time_bound(until, role="until")
            results = [r for r in results if r.get("created_at", "") <= until]
        results = results[:limit]
        # Fetch user_message for the surviving turns — a second query, since
        # the KNN query must not join the diary table.
        if results:
            rowids = [r["diary_rowid"] for r in results]
            ph = in_placeholders(len(rowids))
            cur = await self._c.execute(
                f"SELECT rowid, user_message FROM diary WHERE rowid IN ({ph})",
                rowids,
            )
            msgs = {r["rowid"]: r["user_message"] for r in await _fetch_all_bounded(cur)}
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
            since = normalize_time_bound(since, role="since")
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            until = normalize_time_bound(until, role="until")
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
        results = [dict(row) for row in await _fetch_all_bounded(cursor)]
        logger.debug("search_time since=%s until=%s hits=%s", since, until, len(results))
        return results

    #: Rows examined per regex ``grep``.  A regex cannot use an index, so grep
    #: scans (newest first); the cap keeps the worst case bounded on a
    #: long-lived diary while sitting far above any realistic hit count.
    _GREP_SCAN_LIMIT = 20000

    async def _grep_scan(
        self, rx: "re.Pattern", since: str | None, until: str | None,
        hard_limit: int,
    ) -> list[dict]:
        """Rows whose text matches *rx*, newest first, at most *hard_limit*.

        The time bounds stay in SQL (indexed, and they need no regex); only
        the TEXT predicate runs here, because SQLite has no regexp engine.
        The search and the count both go through this, so
        ``turn_search(mode="grep")`` and ``turn_count(mode="grep")`` cannot
        disagree about what matched.
        """
        where = ""
        params: list = []
        if since:
            since = normalize_time_bound(since, role="since")
            where += " AND created_at >= ?"
            params.append(since)
        if until:
            until = normalize_time_bound(until, role="until")
            where += " AND created_at <= ?"
            params.append(until)
        cursor = await self._c.execute(
            f"""SELECT rowid, user_message, summary, tags, created_at, messages
                FROM diary WHERE 1=1{where}
                ORDER BY rowid DESC LIMIT ?""",
            (*params, self._GREP_SCAN_LIMIT),
        )
        hits: list[dict] = []
        for row in await _fetch_all_bounded(cursor):
            r = dict(row)
            if not (rx.search(r.get("user_message") or "")
                    or rx.search(r.get("messages") or "")):
                continue
            hits.append(r)
            if len(hits) >= hard_limit:
                break
        return hits

    @staticmethod
    def _grep_snippet(row: dict, rx: "re.Pattern") -> str:
        """A window of text around the match, for the result's ``context``."""
        for field in ("user_message", "messages"):
            text = row.get(field) or ""
            m = rx.search(text)
            if m is not None:
                start = max(0, m.start() - 40)
                return text[start:start + 160]
        return ""

    async def search_grep(
        self, pattern: str, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """REGEX search over user_message + messages — a real ``grep``.

        ``translat(e|or)`` and ``summ.rize`` match; an invalid pattern raises
        ``re.error`` for the caller to report.  See :meth:`_grep_scan` for why
        the match runs in Python and how the count stays in step.
        """
        rx = re.compile(pattern)
        limit = _clamp_limit(limit)
        rows = await self._grep_scan(rx, since, until, hard_limit=limit)
        results = []
        for row in rows:
            r = dict(row)
            r.pop("messages", None)
            r["context"] = self._grep_snippet(row, rx)
            results.append(r)
        logger.debug("search_grep pattern=%s hits=%s", pattern[:80], len(results))
        return results

    # ── Embedding ───────────────────────────────────────────────────

    async def upsert_embedding(
        self, diary_rowid: int, chunk_index: int,
        summary: str, tags: str, created_at: str,
        turn_embedding: list[float],
    ) -> None:
        """Insert one chunk embedding for a turn.

        Test-only helper — production re-indexing goes through
        ``replace_embedding_chunks`` (delete-then-insert in ONE transaction,
        under the write lock).  Each turn can produce multiple chunks —
        *chunk_index* is 0-based.  Always INSERTs; the caller clears old
        chunks.
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
        """Count distinct turns that have at least one embedding chunk.

        Test-only helper — the live embedding counts come from
        ``count_unembedded`` / the semantic facts in ``__check``.
        """
        if self._embedding_dim <= 0:
            return 0
        cursor = await self._c.execute(
            "SELECT COUNT(DISTINCT diary_rowid) FROM diary_semantic",
        )
        row = await cursor.fetchone()
        return row[0] if row else 0

    async def clear_all_embeddings(self) -> int:
        """Delete all rows from diary_semantic. Returns count deleted.

        Test-only helper — production never nukes the whole semantic table.
        """
        async with self._write_lock:
            cursor = await self._c.execute("SELECT COUNT(*) FROM diary_semantic")
            row = await cursor.fetchone()
            count = row[0] if row else 0
            await self._c.execute("DELETE FROM diary_semantic")
            await self._c.commit()
        logger.info("embeddings_cleared count=%d", count)
        return count

    async def has_embedding(self, diary_rowid: int) -> bool:
        """Test-only helper — production checks the ``NOT IN diary_semantic``
        unembedded query instead of per-row probes."""
        cursor = await self._c.execute(
            "SELECT rowid FROM diary_semantic WHERE diary_rowid = ? LIMIT 1",
            (diary_rowid,),
        )
        return await cursor.fetchone() is not None


# ── Helpers ─────────────────────────────────────────────────────────


def in_placeholders(count: int) -> str:
    """SQL ``IN (?, ?, …)`` placeholder text for *count* items.

    Every store hand-rolled ``",".join("?" * n)`` before this helper —
    one spelling for the four ``WHERE x IN (…)`` call sites.
    """
    return ",".join("?" * count)


def _vec0_create(stmt: str) -> bool:
    """True for an actual vec0 ``CREATE VIRTUAL TABLE`` statement.

    Requires ``CREATE VIRTUAL TABLE``, not just "vec0" anywhere — the schema
    header comment mentions vec0 and would otherwise be wrongly skipped.
    """
    return "CREATE VIRTUAL TABLE" in stmt and "vec0" in stmt


def _not_vec0_create(stmt: str) -> bool:
    """Inverse of :func:`_vec0_create` — everything except vec0 creates."""
    return not _vec0_create(stmt)


async def run_schema(
    conn,
    schema_path: Path,
    embedding_dim: int,
    *,
    keep: Callable[[str], bool] | None = None,
    log_prefix: str = "schema",
) -> None:
    """Execute a store's ``schema.sql`` statement-by-statement.

    ``sqlite3.executescript`` can't be used — vec0 virtual tables hang in
    aiosqlite's executescript — so every statement runs individually, with
    the ``float[1536]`` embedding-dimension placeholder substituted first.
    ``keep`` triages each trimmed statement: a store skips vec0 creates when
    no embedding backend is configured (dim ≤ 0) via :func:`_not_vec0_create`,
    or keeps ONLY them via :func:`_vec0_create` for a migration recreate.

    Shared by memdb and memfiles — the four hand-rolled copies of this loop
    (each schema run + the recreate runs) lived in the two stores.
    """
    schema_sql = schema_path.read_text(encoding="utf-8")
    schema_sql = schema_sql.replace("float[1536]", f"float[{embedding_dim}]")
    for stmt in _split_sql(schema_sql):
        stmt = stmt.strip()
        if not stmt:
            continue
        if keep is not None and not keep(stmt):
            continue
        try:
            await conn.execute(stmt)
        except Exception as e:
            # A failed CREATE TRIGGER / FTS / vec0 statement leaves the index
            # missing with no production signal — log it loudly (DEBUG would
            # silently hide a structurally broken DB).
            logger.error("%s_stmt_error err=%s stmt=%.80s", log_prefix, e, stmt)
    await conn.commit()


def _split_sql(sql_text: str) -> list[str]:
    """Split SQL text on semicolons, respecting quotes and comments.

    Multi-statement constructs (CREATE TRIGGER … BEGIN … END) are kept
    together so SQLite can parse them as a single statement.  Otherwise
    the interior INSERT / DELETE would be split into orphaned fragments.
    Delegated to ``sqlparse`` — the mature, maintained SQL statement
    splitter — instead of a hand-rolled lexer (`sqlparse.split` keeps
    comment blocks attached to the following trigger statement, so the
    ``CREATE TRIGGER`` keyword is never hidden from a build dialect).
    """
    return sqlparse.split(sql_text)


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

    CJK is ~1 char/token; Latin ~4 chars/token in prose — but that density
    is a ceiling, not a floor.  Punctuation- and escape-dense runs (escaped
    JSON, tool dumps) tokenize at 1-2 chars/token, so relying on it let a
    30k-char newline-free JSON line ride as one "fits" chunk, the provider
    rejected it (bge-m3's 8192-token cap), and the drainer stalled on that
    turn forever.  Floor the density at **1 char/token** — the densest any
    BPE gets — so an oversized chunk always hard-splits into pieces that
    fit by construction.  Normal text never pays for this: ``_chunk_text``
    already caps paragraphs at ``CHUNK_SIZE_CHARS`` (well under any limit),
    so this budget only governs pathological single lines.
    """
    if not text:
        return max_tokens
    cjk = sum(1 for ch in text if _contains_cjk(ch))
    other = len(text) - cjk
    est_tokens = cjk + other / 4
    per_char = est_tokens / len(text)
    return max(1, int(max_tokens / max(per_char, 1.0)))


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


#: Characters FTS5 treats as query operators/grouping when unquoted (parens,
#: NOT/leading +/-/colon, column filters …).  A bare `(urgent)` or trailing
#: `foo -` would otherwise be a syntax error inside ``MATCH``.
_FTS_SPECIALS = set("()[]{}:^~+-.,!?")


def _to_fts5_query(query: str) -> str:
    cleaned = query.replace('"', '').replace("'", "").replace("*", "")
    words = cleaned.split()
    if not words:
        return '""'
    quoted: list[str] = []
    for w in words:
        low = w.lower()
        # Quote a word when it is an FTS5 reserved operator (so a literal
        # "and"/"or"/"not"/"near" isn't parsed as an operator) or carries FTS
        # special characters (which would otherwise raise a MATCH syntax
        # error, e.g. a lone '(').  Inside double quotes those characters are
        # literal phrase content, never operators.
        if low in ("and", "or", "not", "near") or any(
            c in _FTS_SPECIALS for c in w
        ):
            # A token that is PURELY operators (e.g. "-" or "(") has no search
            # content — drop it rather than emit an empty phrase.
            inner = w.strip("()[]{}:^~+-,.")
            if not inner:
                continue
            quoted.append(f'"{inner}"')
        else:
            quoted.append(w)
    if not quoted:
        return '""'
    return " AND ".join(quoted)
