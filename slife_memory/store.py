"""Session store — SQLite database with FTS5 + sqlite-vec hybrid search.

Treats conversations like a diary: one row = one complete chat session.
All queries are scoped by author for user isolation.
"""

import json
import logging
import struct
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Embedding dimension — matches the configured model.
# text-embedding-3-small → 1536
# text-embedding-3-large → 3072
DEFAULT_EMBEDDING_DIM = 1536


def _now() -> str:
    """ISO 8601 timestamp in local timezone."""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _serialize_f32(vector: list[float]) -> bytes:
    """Pack a list of floats into a little-endian binary blob for sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


class SessionStore:
    """Manages the slife memory database.

    Usage::

        store = SessionStore(Path("~/.slife/slife.db"))
        await store.setup()

        # Start a new conversation
        rowid = await store.open_diary(
            author="alice",
            who_helped="assistant",
            what_model="deepseek/deepseek-chat",
            system_prompt="You are slife...",
        )

        # Save after each turn
        await store.update_diary(
            rowid=rowid, author="alice",
            messages=[...], turn_count=5, token_count=1200,
        )

        # Clean exit
        await store.close_diary(rowid=rowid, author="alice")
    """

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._embedding_dim = DEFAULT_EMBEDDING_DIM

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── Lifecycle ──────────────────────────────────────────────────

    async def setup(self, embedding_dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        """Open database, enable WAL, create schema, load sqlite-vec."""
        self._embedding_dim = embedding_dim

        # Ensure parent directory exists
        self._db_path.parent.mkdir(parents=True, exist_ok=True)

        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")

        # Load sqlite-vec extension
        await self._load_vec_extension()

        # Run schema
        await self._run_schema()

        logger.info(
            "store_ready path=%s wal=on vec_dim=%d",
            self._db_path, self._embedding_dim,
        )

    async def _load_vec_extension(self) -> None:
        """Load the sqlite-vec extension."""
        import sqlite_vec

        await self._conn.enable_load_extension(True)
        await self._conn.load_extension(sqlite_vec.loadable_path())
        await self._conn.enable_load_extension(False)

        # Verify
        row = await self._conn.execute("SELECT vec_version()")
        version = await row.fetchone()
        logger.info("vec_loaded version=%s", version[0] if version else "unknown")

    async def _run_schema(self) -> None:
        """Create tables if they don't exist."""
        schema_path = Path(__file__).parent / "schema.sql"
        schema_sql = schema_path.read_text(encoding="utf-8")

        # Replace embedding dimension placeholder
        schema_sql = schema_sql.replace("float[1536]", f"float[{self._embedding_dim}]")

        # Use executescript for DDL — it handles multi-statement SQL natively.
        # aiosqlite's executescript runs in the worker thread.
        await self._conn.executescript(schema_sql)
        await self._conn.commit()

        # Migrate existing databases that don't have trim_count yet.
        # In development — no backward compatibility needed later.
        try:
            await self._conn.execute(
                "ALTER TABLE diary ADD COLUMN trim_count INTEGER NOT NULL DEFAULT 0",
            )
            await self._conn.commit()
            logger.debug("schema_migrated added trim_count")
        except Exception:
            pass  # column already exists

        logger.debug("schema_ready")

    async def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("store_closed")

    # ── Diary CRUD ──────────────────────────────────────────────────
    # Each diary row = one complete conversation (from startup to exit).

    async def open_diary(
        self,
        author: str = "default",
        who_helped: str = "",
        what_model: str = "",
        system_prompt: str = "",
    ) -> dict:
        """Start a new diary entry or detect an interrupted one.

        Returns a dict with:
          - rowid: the new (or interrupted) diary row id
          - interrupted: True if a previous session was interrupted
          - interrupted_info: details about the interrupted session (or None)
        """
        assert self._conn is not None

        # Check for interrupted session
        interrupted = await self.find_interrupted(author)

        if interrupted:
            # An interrupted session exists — return it so the caller
            # can decide whether to restore or discard.
            return {
                "rowid": interrupted["rowid"],
                "interrupted": True,
                "title": interrupted["title"],
                "created_at": interrupted["created_at"],
                "updated_at": interrupted["updated_at"],
                "how_many_turns": interrupted["how_many_turns"],
                "how_many_tokens": interrupted["how_many_tokens"],
                "who_helped": interrupted["who_helped"],
                "what_model": interrupted["what_model"],
                "trim_count": interrupted.get("trim_count", 0),
            }

        # No interrupted session — create a fresh one
        now = _now()
        messages_json = json.dumps(
            [{"role": "system", "content": system_prompt}]
            if system_prompt else [],
            ensure_ascii=False,
        )

        cursor = await self._conn.execute(
            """INSERT INTO diary (
                   author, title, created_at, updated_at, status,
                   messages, summary, tags, key_moments,
                   who_helped, what_model, how_many_turns, how_many_tokens
               ) VALUES (?, ?, ?, ?, '进行中', ?, '', '', '', ?, ?, 0, 0)""",
            (author, "", now, now, messages_json, who_helped, what_model),
        )
        await self._conn.commit()

        rowid = cursor.lastrowid
        logger.info(
            "diary_opened author=%s rowid=%s", author, rowid,
        )

        # Always check for a restorable last session — slife is a
        # permanent-memory agent, so every restart should offer to
        # continue the previous conversation.
        last_diary = await self._find_last_diary(author, exclude_rowid=rowid)
        if last_diary and last_diary.get("how_many_turns", 0) > 0:
            return {
                "rowid": rowid,
                "interrupted": False,
                "last_diary": {
                    "rowid": last_diary["rowid"],
                    "title": last_diary["title"],
                    "created_at": last_diary["created_at"],
                    "updated_at": last_diary["updated_at"],
                    "status": last_diary["status"],
                    "how_many_turns": last_diary["how_many_turns"],
                    "how_many_tokens": last_diary["how_many_tokens"],
                    "who_helped": last_diary["who_helped"],
                    "what_model": last_diary["what_model"],
                    "trim_count": last_diary.get("trim_count", 0),
                },
                "title": "",
                "created_at": now,
                "updated_at": now,
                "how_many_turns": 0,
                "how_many_tokens": 0,
                "who_helped": who_helped,
                "what_model": what_model,
            }

        return {
            "rowid": rowid,
            "interrupted": False,
            "title": "",
            "created_at": now,
            "updated_at": now,
            "how_many_turns": 0,
            "how_many_tokens": 0,
            "who_helped": who_helped,
            "what_model": what_model,
        }

    async def update_diary(
        self,
        rowid: int,
        author: str = "default",
        messages: list[dict] | None = None,
        turn_count: int | None = None,
        token_count: int | None = None,
        trim_count: int = 0,
    ) -> None:
        """Update a diary entry after each conversation turn.

        Overwrites the messages JSON and updates counters + timestamp.
        Called after each completed turn (user message + assistant response).

        *trim_count* is the cumulative number of messages trimmed from the
        front of the conversation (after the system prompt).  It is used
        on restore to recover the exact working context.
        """
        assert self._conn is not None

        now = _now()

        if messages is not None:
            messages_json = json.dumps(messages, ensure_ascii=False)
            await self._conn.execute(
                """UPDATE diary
                   SET messages = ?, updated_at = ?,
                       how_many_turns = ?, how_many_tokens = ?,
                       trim_count = ?
                   WHERE rowid = ? AND author = ?""",
                (messages_json, now,
                 turn_count or 0, token_count or 0, trim_count,
                 rowid, author),
            )
        else:
            # Just bump the timestamp and optional counters
            if turn_count is not None and token_count is not None:
                await self._conn.execute(
                    """UPDATE diary
                       SET updated_at = ?, how_many_turns = ?, how_many_tokens = ?
                       WHERE rowid = ? AND author = ?""",
                    (now, turn_count, token_count, rowid, author),
                )
            else:
                await self._conn.execute(
                    "UPDATE diary SET updated_at = ? WHERE rowid = ? AND author = ?",
                    (now, rowid, author),
                )

        await self._conn.commit()

    async def close_diary(self, rowid: int, author: str = "default") -> None:
        """Mark a diary entry as cleanly closed."""
        assert self._conn is not None
        now = _now()
        await self._conn.execute(
            "UPDATE diary SET status = '已完成', updated_at = ? WHERE rowid = ? AND author = ?",
            (now, rowid, author),
        )
        await self._conn.commit()
        logger.info("diary_closed author=%s rowid=%s", author, rowid)

    async def mark_crashed(self, rowid: int, author: str = "default") -> None:
        """Mark a diary entry as crashed (unexpected exit)."""
        assert self._conn is not None
        now = _now()
        await self._conn.execute(
            "UPDATE diary SET status = '意外中断', updated_at = ? WHERE rowid = ? AND author = ?",
            (now, rowid, author),
        )
        await self._conn.commit()
        logger.info("diary_crashed author=%s rowid=%s", author, rowid)

    async def get_diary(self, rowid: int, author: str = "default") -> dict | None:
        """Retrieve a single diary entry by rowid and author.

        Returns a dict with all columns, or None if not found.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT rowid, * FROM diary WHERE rowid = ? AND author = ?",
            (rowid, author),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def find_interrupted(self, author: str = "default") -> dict | None:
        """Find the most recent diary entry with status '进行中'.

        Returns a dict or None if no interrupted session exists.
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT rowid, author, title, created_at, updated_at, status,
                      how_many_turns, how_many_tokens, who_helped, what_model,
                      trim_count
               FROM diary
               WHERE author = ? AND status = '进行中'
               ORDER BY updated_at DESC
               LIMIT 1""",
            (author,),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def _find_last_diary(
        self, author: str = "default", exclude_rowid: int | None = None,
    ) -> dict | None:
        """Find the most recent diary entry regardless of status.

        Excludes *exclude_rowid* (the diary we just created).
        Returns a dict or None if the diary is empty (no prior sessions).
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT rowid, author, title, created_at, updated_at, status,
                      how_many_turns, how_many_tokens, who_helped, what_model,
                      trim_count
               FROM diary
               WHERE author = ? AND rowid != ?
               ORDER BY updated_at DESC
               LIMIT 1""",
            (author, exclude_rowid or 0),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return dict(row)

    async def list_recent(
        self, author: str = "default", limit: int = 20,
    ) -> list[dict]:
        """List recent diary entries for an author, newest first.

        Returns lightweight summaries (no full messages).
        """
        assert self._conn is not None
        cursor = await self._conn.execute(
            """SELECT rowid, title, summary, tags, created_at, updated_at,
                      status, how_many_turns, how_many_tokens, who_helped, what_model
               FROM diary
               WHERE author = ?
               ORDER BY updated_at DESC
               LIMIT ?""",
            (author, limit),
        )
        return [dict(row) for row in await cursor.fetchall()]

    async def update_summary(
        self,
        rowid: int,
        author: str = "default",
        title: str | None = None,
        summary: str | None = None,
        tags: str | None = None,
        key_moments: str | None = None,
    ) -> None:
        """Write a summary, title, tags, or key moments for a diary entry.

        Only updates the provided fields (partial update).
        """
        assert self._conn is not None

        updates = []
        params: list = []

        if title is not None:
            updates.append("title = ?")
            params.append(title)
        if summary is not None:
            updates.append("summary = ?")
            params.append(summary)
        if tags is not None:
            updates.append("tags = ?")
            params.append(tags)
        if key_moments is not None:
            updates.append("key_moments = ?")
            params.append(key_moments)

        if not updates:
            return

        updates.append("updated_at = ?")
        params.append(_now())
        params.extend([rowid, author])

        await self._conn.execute(
            f"UPDATE diary SET {', '.join(updates)} WHERE rowid = ? AND author = ?",
            params,
        )
        await self._conn.commit()

    # ── Search ──────────────────────────────────────────────────────

    async def search_keyword(
        self, author: str, query: str, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Full-text keyword search via FTS5.

        Each result includes a snippet highlighting matching text.
        *since* / *until* are ISO datetime strings for time-range filtering.
        """
        assert self._conn is not None

        # Escape FTS5 special characters and format for FTS5 query
        fts_query = _to_fts5_query(query)

        # Build time filter clauses
        time_clauses = ""
        time_params: list[str] = []
        if since:
            time_clauses += " AND d.created_at >= ?"
            time_params.append(since)
        if until:
            time_clauses += " AND d.created_at <= ?"
            time_params.append(until)

        try:
            cursor = await self._conn.execute(
                f"""SELECT d.rowid, d.title, d.summary, d.tags, d.created_at,
                          d.how_many_turns,
                          snippet(diary_fts, 4, '…', '…', '…', 60) AS snippet,
                          rank
                   FROM diary_fts fts
                   JOIN diary d ON fts.rowid = d.rowid
                   WHERE diary_fts MATCH ?
                     AND fts.author = ?
                     AND d.status != '进行中'{time_clauses}
                   ORDER BY rank
                   LIMIT ?""",
                (fts_query, author, *time_params, limit),
            )
            results = [dict(row) for row in await cursor.fetchall()]
            logger.debug(
                "search_keyword author=%s query=%s hits=%s",
                author, query, len(results),
            )
            return results
        except aiosqlite.OperationalError as e:
            # FTS5 syntax error (e.g. unmatched quotes) → return empty
            logger.debug("search_keyword_parse_error query=%s err=%s", query, e)
            return []

    async def search_semantic(
        self, author: str, embedding: list[float], limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Semantic search via sqlite-vec KNN.

        Returns diary entries ranked by cosine distance to the query embedding.
        *since* / *until* are ISO datetime strings — vec0 doesn't support
        WHERE on auxiliary columns, so we post-filter in Python.
        """
        assert self._conn is not None

        vec_blob = _serialize_f32(embedding)

        # Fetch more than needed to account for post-filtering
        fetch_limit = limit * 3 if (since or until) else limit

        cursor = await self._conn.execute(
            """SELECT rowid, title, summary, tags, created_at, distance
               FROM diary_semantic
               WHERE summary_embedding MATCH ?
                 AND author = ?
                 AND k = ?
               ORDER BY distance""",
            (vec_blob, author, fetch_limit),
        )

        results = [dict(row) for row in await cursor.fetchall()]

        # Post-filter time range (vec0 doesn't support WHERE on +columns)
        if since:
            results = [r for r in results if r.get("created_at", "") >= since]
        if until:
            results = [r for r in results if r.get("created_at", "") <= until]
        results = results[:limit]

        logger.debug(
            "search_semantic author=%s hits=%s", author, len(results),
        )
        return results

    async def search_time(
        self, author: str, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Time-range browsing — list diary entries in a time window.

        Returns lightweight summaries ordered by *created_at* DESC.
        At least one of *since* / *until* should be provided.
        """
        assert self._conn is not None

        clauses = ["author = ?", "status != '进行中'"]
        params: list[str | int] = [author]

        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)

        where = " AND ".join(clauses)
        params.append(limit)

        cursor = await self._conn.execute(
            f"""SELECT rowid, title, summary, tags, created_at,
                      how_many_turns, how_many_tokens
               FROM diary
               WHERE {where}
               ORDER BY created_at DESC
               LIMIT ?""",
            params,
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug(
            "search_time author=%s since=%s until=%s hits=%s",
            author, since, until, len(results),
        )
        return results

    async def search_grep(
        self, author: str, pattern: str, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Exact substring search over the full messages JSON.

        Like grep — finds literal matches anywhere in the stored
        conversation text. Best for error messages, code snippets,
        exact strings, and tool outputs. Not ranked — returns results
        in reverse chronological order.

        Uses SQLite LIKE which does a full scan — appropriate for
        ad-hoc precise searches, not for ranking.

        *since* / *until* are ISO datetime strings for time-range filtering.
        """
        assert self._conn is not None

        # Escape LIKE wildcards in the pattern
        safe = pattern.replace("%", r"\%").replace("_", r"\_")
        like_pattern = f"%{safe}%"

        # Build time filter clauses
        time_clauses = ""
        time_params: list[str] = []
        if since:
            time_clauses += " AND created_at >= ?"
            time_params.append(since)
        if until:
            time_clauses += " AND created_at <= ?"
            time_params.append(until)

        cursor = await self._conn.execute(
            f"""SELECT rowid, title, summary, tags, created_at,
                      how_many_turns, updated_at,
                      -- Extract a window around the match
                      substr(messages,
                             max(0, instr(messages, ?) - 60),
                             200) AS context
               FROM diary
               WHERE author = ?
                 AND status != '进行中'
                 AND messages LIKE ?{time_clauses}
               ORDER BY updated_at DESC
               LIMIT ?""",
            (pattern, author, like_pattern, *time_params, limit),
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug(
            "search_grep author=%s pattern=%s hits=%s",
            author, pattern[:80], len(results),
        )
        return results

    # ── Embedding storage ───────────────────────────────────────────

    async def upsert_embedding(
        self,
        rowid: int,
        author: str,
        summary: str,
        summary_embedding: list[float],
    ) -> None:
        """Insert or update a summary embedding for semantic search.

        Since vec0 doesn't support UPDATE directly, we delete-then-insert
        when the rowid already has an embedding.
        """
        assert self._conn is not None

        vec_blob = _serialize_f32(summary_embedding)

        # Check for existing embedding
        cursor = await self._conn.execute(
            "SELECT rowid FROM diary_semantic WHERE rowid = ? AND author = ?",
            (rowid, author),
        )
        existing = await cursor.fetchone()

        if existing:
            # vec0 doesn't support direct UPDATE — delete first
            await self._conn.execute(
                "DELETE FROM diary_semantic WHERE rowid = ? AND author = ?",
                (rowid, author),
            )

        await self._conn.execute(
            """INSERT INTO diary_semantic
               (rowid, author, summary_embedding, title, summary, tags, created_at)
               SELECT rowid, author, ?, ?, ?, ?, created_at
               FROM diary WHERE rowid = ? AND author = ?""",
            (vec_blob, summary, summary, "", rowid, author),
        )
        await self._conn.commit()
        logger.debug("embedding_upserted author=%s rowid=%s", author, rowid)

    async def has_embedding(self, rowid: int, author: str) -> bool:
        """Check if a diary entry already has an embedding."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT rowid FROM diary_semantic WHERE rowid = ? AND author = ?",
            (rowid, author),
        )
        return await cursor.fetchone() is not None


# ── Helpers ─────────────────────────────────────────────────────────


def _split_sql(sql_text: str) -> list[str]:
    """Split a SQL file into individual statements.

    Splits on semicolons, ignoring those inside strings or comments.
    """
    statements = []
    current: list[str] = []
    in_single_quote = False
    in_double_quote = False
    in_line_comment = False
    in_block_comment = False

    i = 0
    chars = list(sql_text)
    while i < len(chars):
        ch = chars[i]
        next_ch = chars[i + 1] if i + 1 < len(chars) else ""

        if in_line_comment:
            if ch == "\n":
                in_line_comment = False
            current.append(ch)
            i += 1
            continue

        if in_block_comment:
            if ch == "*" and next_ch == "/":
                in_block_comment = False
                current.append(ch)
                current.append(next_ch)
                i += 2
                continue
            current.append(ch)
            i += 1
            continue

        if ch == "-" and next_ch == "-":
            in_line_comment = True
            current.append(ch)
            i += 1
            continue

        if ch == "/" and next_ch == "*":
            in_block_comment = True
            current.append(ch)
            current.append(next_ch)
            i += 2
            continue

        if ch == "'" and not in_double_quote:
            in_single_quote = not in_single_quote
        elif ch == '"' and not in_single_quote:
            in_double_quote = not in_double_quote
        elif ch == ";" and not in_single_quote and not in_double_quote:
            current.append(ch)
            statements.append("".join(current))
            current = []
            i += 1
            continue

        current.append(ch)
        i += 1

    remainder = "".join(current).strip()
    if remainder:
        statements.append(remainder)

    return statements


def _to_fts5_query(query: str) -> str:
    """Convert a user query to a safe FTS5 query string.

    Escapes special FTS5 characters and wraps multi-word queries.
    """
    # Strip special FTS5 operators
    cleaned = query.replace('"', '').replace("'", "").replace("*", "")

    words = cleaned.split()
    if not words:
        return '""'

    if len(words) == 1:
        return words[0]

    # Multi-word: each term with implicit AND, plus a phrase match
    terms = " AND ".join(words)
    return terms
