"""Turn store — SQLite database with FTS5 + sqlite-vec hybrid search.

One row = one turn (user message + assistant's complete response).
No sessions, no lifecycle — each turn is independent and immutable.
Restore loads the most recent N turns by rowid.

Agent isolation is at the file level — each agent_id has its own .db file.
"""

import json
import logging
import struct
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_EMBEDDING_DIM = 1536


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _serialize_f32(vector: list[float]) -> bytes:
    return struct.pack(f"{len(vector)}f", *vector)


class SessionStore:
    """Manages the Slife memory database — turn-based, no sessions."""

    def __init__(self, db_path: Path):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._embedding_dim = DEFAULT_EMBEDDING_DIM

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ── Lifecycle ──────────────────────────────────────────────────

    async def setup(self, embedding_dim: int = DEFAULT_EMBEDDING_DIM) -> None:
        self._embedding_dim = embedding_dim
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(str(self._db_path))
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._load_vec_extension()
        await self._run_schema()
        logger.info("store_ready path=%s wal=on vec_dim=%d", self._db_path, self._embedding_dim)

    async def _load_vec_extension(self) -> None:
        import sqlite_vec
        await self._conn.enable_load_extension(True)
        await self._conn.load_extension(sqlite_vec.loadable_path())
        await self._conn.enable_load_extension(False)
        row = await self._conn.execute("SELECT vec_version()")
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
            try:
                await self._conn.execute(stmt)
            except Exception as e:
                logger.debug("schema_stmt_error err=%s stmt=%.80s", e, stmt)
        await self._conn.commit()
        logger.debug("schema_ready")

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("store_closed")

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
        assert self._conn is not None

        now = _now()
        messages_json = json.dumps(messages or [], ensure_ascii=False)

        cursor = await self._conn.execute(
            """INSERT INTO diary (user_message, messages, summary, tags,
                                  channel, created_at, who_helped, what_model, token_count)
               VALUES (?, ?, '', '', ?, ?, ?, ?, ?)""",
            (user_message, messages_json, channel, now, who_helped, what_model, token_count),
        )
        await self._conn.commit()
        rowid = cursor.lastrowid
        logger.debug("turn_saved rowid=%s", rowid)

        # Embed the full turn text. Skip if it exceeds the model's token
        # limit — semantic search misses this turn, but keyword (FTS5/grep)
        # search still works.  No truncation: partial knowledge is misleading.
        if embedder is not None and embedder.available:
            embed_text = _turn_text_for_embedding(user_message, messages or [])
            if embed_text.strip():
                est_tokens = len(embed_text) // 4  # ~4 chars per token
                if est_tokens <= embedder.max_tokens:
                    try:
                        emb = await embedder.embed_one(embed_text)
                        if emb:
                            await self.upsert_embedding(
                                rowid=rowid, summary="", tags="",
                                created_at=now, turn_embedding=emb,
                            )
                    except Exception as e:
                        logger.debug("embedding_save_skipped rowid=%s err=%s", rowid, e)

        return rowid

    async def get_turn(self, rowid: int) -> dict | None:
        """Return a single turn by rowid."""
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT rowid, * FROM diary WHERE rowid = ?",
            (rowid,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def get_recent_turns(self, limit: int = 50) -> list[dict]:
        """Return the most recent N turns, oldest-first for restore."""
        assert self._conn is not None
        cursor = await self._conn.execute(
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
        assert self._conn is not None
        cursor = await self._conn.execute(
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
        assert self._conn is not None

        row = await self._conn.execute("SELECT COUNT(*) FROM diary")
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
                row2 = await self._conn.execute(
                    "SELECT COUNT(*) FROM diary_fts WHERE diary_fts MATCH ?",
                    (fts_query,),
                )
                filtered = (await row2.fetchone())[0]
                return {"total": total, "filtered": filtered,
                        "query": query, "mode": mode,
                        "since": since, "until": until}

            if since:
                where += " AND created_at >= ?"
                params.append(since)
            if until:
                where += " AND created_at <= ?"
                params.append(until)
            row2 = await self._conn.execute(
                f"SELECT COUNT(*) FROM diary WHERE {where}", params,
            )
            filtered = (await row2.fetchone())[0]
        elif since or until:
            clauses: list[str] = []
            params = []
            if since:
                clauses.append("created_at >= ?")
                params.append(since)
            if until:
                clauses.append("created_at <= ?")
                params.append(until)
            where = " AND ".join(clauses)
            row2 = await self._conn.execute(
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
        assert self._conn is not None
        cursor = await self._conn.execute(
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
        assert self._conn is not None
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
        await self._conn.execute(
            f"UPDATE diary SET {', '.join(updates)} WHERE rowid = ?",
            params,
        )
        await self._conn.commit()

    # ── Search ──────────────────────────────────────────────────────

    async def search_keyword(
        self, query: str, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """FTS5 keyword search with snippet highlighting."""
        assert self._conn is not None
        fts_query = _to_fts5_query(query)
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
        """sqlite-vec KNN on turn_embedding."""
        assert self._conn is not None
        vec_blob = _serialize_f32(embedding)
        fetch_limit = limit * 3 if (since or until) else limit
        cursor = await self._conn.execute(
            """SELECT rowid, summary, tags, created_at, distance
               FROM diary_semantic
               WHERE turn_embedding MATCH ? AND k = ?
               ORDER BY distance""",
            (vec_blob, fetch_limit),
        )
        results = [dict(row) for row in await cursor.fetchall()]
        if since:
            results = [r for r in results if r.get("created_at", "") >= since]
        if until:
            results = [r for r in results if r.get("created_at", "") <= until]
        results = results[:limit]
        logger.debug("search_semantic hits=%s", len(results))
        return results

    async def search_time(
        self, limit: int = 20,
        since: str | None = None, until: str | None = None,
    ) -> list[dict]:
        """Time-range browsing of turns."""
        assert self._conn is not None
        clauses: list[str] = []
        params: list[str | int] = []
        if since:
            clauses.append("created_at >= ?")
            params.append(since)
        if until:
            clauses.append("created_at <= ?")
            params.append(until)
        if clauses:
            where = "WHERE " + " AND ".join(clauses)
        else:
            where = ""
        params.append(limit)
        cursor = await self._conn.execute(
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
        assert self._conn is not None
        safe = pattern.replace("%", r"\%").replace("_", r"\_")
        like_pattern = f"%{safe}%"
        time_clauses = ""
        time_params: list[str] = []
        if since:
            time_clauses += " AND created_at >= ?"
            time_params.append(since)
        if until:
            time_clauses += " AND created_at <= ?"
            time_params.append(until)
        cursor = await self._conn.execute(
            f"""SELECT rowid, user_message, summary, tags, created_at,
                      substr(messages, max(0, instr(messages, ?) - 40), 160) AS context
               FROM diary
               WHERE (user_message LIKE ? OR messages LIKE ?){time_clauses}
               ORDER BY rowid DESC LIMIT ?""",
            (pattern, like_pattern, like_pattern, *time_params, limit),
        )
        results = [dict(row) for row in await cursor.fetchall()]
        logger.debug("search_grep pattern=%s hits=%s", pattern[:80], len(results))
        return results

    # ── Embedding ───────────────────────────────────────────────────

    async def upsert_embedding(
        self, rowid: int,
        summary: str, tags: str, created_at: str,
        turn_embedding: list[float],
    ) -> None:
        """Insert or update a turn embedding."""
        assert self._conn is not None
        vec_blob = _serialize_f32(turn_embedding)
        cursor = await self._conn.execute(
            "SELECT rowid FROM diary_semantic WHERE rowid = ?",
            (rowid,),
        )
        if await cursor.fetchone():
            await self._conn.execute(
                "DELETE FROM diary_semantic WHERE rowid = ?",
                (rowid,),
            )
        await self._conn.execute(
            """INSERT INTO diary_semantic (rowid, turn_embedding, summary, tags, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (rowid, vec_blob, summary, tags, created_at),
        )
        await self._conn.commit()
        logger.debug("embedding_upserted rowid=%s", rowid)

    async def has_embedding(self, rowid: int) -> bool:
        assert self._conn is not None
        cursor = await self._conn.execute(
            "SELECT rowid FROM diary_semantic WHERE rowid = ?",
            (rowid,),
        )
        return await cursor.fetchone() is not None


# ── Helpers ─────────────────────────────────────────────────────────


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


def _to_fts5_query(query: str) -> str:
    cleaned = query.replace('"', '').replace("'", "").replace("*", "")
    words = cleaned.split()
    if not words:
        return '""'
    if len(words) == 1:
        return words[0]
    return " AND ".join(words)
