"""Tests for slife.plugins.memdb.store — SessionStore and helpers."""

import pytest; pytestmark = pytest.mark.unit


import struct
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.plugins.memdb.store import (
    SessionStore,
    _normalize_time_param,
    _now,
    _serialize_f32,
    _split_sql,
    _to_fts5_query,
    DEFAULT_EMBEDDING_DIM,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


class TestNow:
    """Tests for _now()."""

    def test_returns_iso_format(self):
        result = _now()
        assert "T" in result


class TestSerializeF32:
    """Tests for _serialize_f32."""

    def test_packs_floats_to_bytes(self):
        vec = [1.0, 2.0, 3.0]
        result = _serialize_f32(vec)
        # 3 floats * 4 bytes each = 12 bytes
        assert len(result) == 12
        unpacked = struct.unpack("3f", result)
        assert unpacked == pytest.approx((1.0, 2.0, 3.0))

    def test_empty_vector(self):
        result = _serialize_f32([])
        assert len(result) == 0


class TestToFts5Query:
    """Tests for _to_fts5_query."""

    def test_single_word(self):
        assert _to_fts5_query("hello") == "hello"

    def test_multi_word(self):
        result = _to_fts5_query("hello world")
        assert "hello" in result
        assert "world" in result
        assert " AND " in result

    def test_strips_special_chars(self):
        result = _to_fts5_query('"hello" world*')
        assert '"' not in result
        assert "*" not in result

    def test_empty_string(self):
        assert _to_fts5_query("") == '""'


class TestNormalizeTimeParam:
    """Tests for _normalize_time_param."""

    def test_relative_yesterday(self):
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        assert _normalize_time_param("yesterday", "since") == yesterday

    def test_relative_today(self):
        today = date.today().isoformat()
        assert _normalize_time_param("today", "since") == today

    def test_relative_tomorrow(self):
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        assert _normalize_time_param("tomorrow", "since") == tomorrow

    def test_relative_now(self):
        result = _normalize_time_param("now", "since")
        assert "T" in result  # Full ISO datetime

    def test_case_insensitive(self):
        today = date.today().isoformat()
        assert _normalize_time_param("TODAY", "since") == today
        assert _normalize_time_param("Yesterday", "since") == (date.today() - timedelta(days=1)).isoformat()

    def test_whitespace_stripped(self):
        today = date.today().isoformat()
        assert _normalize_time_param("  today  ", "since") == today

    def test_iso_string_passthrough_since(self):
        assert _normalize_time_param("2026-07-20T14:39:19+08:00", "since") == "2026-07-20T14:39:19+08:00"

    def test_date_only_since_passthrough(self):
        """Bare-date since works: created_at >= '2026-07-20' includes all records on that day."""
        assert _normalize_time_param("2026-07-20", "since") == "2026-07-20"

    def test_date_only_until_advances_day(self):
        """Bare-date until must advance one day so records on that day are included."""
        assert _normalize_time_param("2026-07-20", "until") == "2026-07-21"

    def test_datetime_until_passthrough(self):
        """Full datetime until is left alone — the caller specified the time explicitly."""
        assert _normalize_time_param("2026-07-20T23:59:59", "until") == "2026-07-20T23:59:59"

    def test_invalid_date_passthrough(self):
        """Garbage input passes through unchanged so the SQL can reject it."""
        assert _normalize_time_param("not-a-date", "since") == "not-a-date"


class TestSplitSql:
    """Tests for _split_sql."""

    def test_single_statement(self):
        result = _split_sql("CREATE TABLE foo (id INTEGER PRIMARY KEY);")
        assert len(result) == 1
        assert "CREATE TABLE" in result[0]

    def test_multiple_statements(self):
        sql = "CREATE TABLE foo (id INTEGER);\nCREATE TABLE bar (id INTEGER);"
        result = _split_sql(sql)
        assert len(result) == 2

    def test_ignores_semicolons_in_strings(self):
        sql = "INSERT INTO foo VALUES ('hello;world');"
        result = _split_sql(sql)
        assert len(result) == 1

    def test_ignores_line_comments(self):
        sql = "-- this is a comment;\nCREATE TABLE t (id INT);"
        result = _split_sql(sql)
        # Single effective statement after comment
        assert len(result) == 1
        assert "CREATE TABLE" in result[0]

    def test_no_trailing_semicolon(self):
        sql = "SELECT * FROM foo"
        result = _split_sql(sql)
        assert len(result) == 1
        assert result[0] == "SELECT * FROM foo"


# ── SessionStore ────────────────────────────────────────────────────────────


class TestSessionStoreInit:
    """Tests for SessionStore initialization."""

    def test_store_creation(self):
        store = SessionStore(Path("/tmp/test.db"))
        assert store.db_path == Path("/tmp/test.db")
        assert store._conn is None


class TestSessionStoreSetup:
    """Tests for setup."""

    @pytest.mark.asyncio
    @patch("pathlib.Path.mkdir")
    @patch("slife.plugins.memdb.store.aiosqlite.connect")
    async def test_setup_initializes_db(self, mock_connect, mock_mkdir):
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.executescript = AsyncMock()
        mock_conn.commit = AsyncMock()
        mock_conn.enable_load_extension = AsyncMock()
        mock_conn.load_extension = AsyncMock()

        async def _connect(*args, **kwargs):
            return mock_conn

        mock_connect.side_effect = _connect

        with patch("sqlite_vec.loadable_path", return_value="/path/to/vec"):
            store = SessionStore(Path("/tmp/test.db"))
            await store.setup()

        mock_connect.assert_called_once()
        mock_conn.commit.assert_called()

    @pytest.mark.asyncio
    @patch("pathlib.Path.mkdir")
    @patch("slife.plugins.memdb.store.aiosqlite.connect")
    async def test_setup_is_idempotent(self, mock_connect, mock_mkdir):
        """Calling setup() twice on the same DB file should not fail."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.executescript = AsyncMock()
        mock_conn.commit = AsyncMock()
        mock_conn.enable_load_extension = AsyncMock()
        mock_conn.load_extension = AsyncMock()

        async def _connect(*args, **kwargs):
            return mock_conn

        mock_connect.side_effect = _connect

        with patch("sqlite_vec.loadable_path", return_value="/path/to/vec"):
            store = SessionStore(Path("/tmp/test_idem.db"))
            await store.setup()
            await store.setup()

        assert mock_connect.call_count == 2


class TestSessionStoreClose:
    """Tests for close."""

    @pytest.mark.asyncio
    async def test_close_no_connection(self):
        store = SessionStore(Path("/tmp/test.db"))
        await store.close()  # Should not raise

    @pytest.mark.asyncio
    async def test_close_with_connection(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        store._conn = mock_conn

        await store.close()
        mock_conn.close.assert_called_once()
        assert store._conn is None


class TestSessionStoreSaveTurn:
    """Tests for save_turn."""

    @pytest.mark.asyncio
    async def test_save_turn_basic(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 42
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        rowid = await store.save_turn(
            user_message="Hello",
            token_count=10,
            who_helped="assistant",
            what_model="deepseek/flash",
        )

        assert rowid == 42
        mock_conn.execute.assert_called()
        mock_conn.commit.assert_called()

    @pytest.mark.asyncio
    async def test_save_turn_with_embedder(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 1
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        mock_embedder = MagicMock()
        mock_embedder.available = True
        mock_embedder.max_tokens = 8192
        mock_embedder.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

        rowid = await store.save_turn(
            user_message="Hello",
            embedder=mock_embedder,
        )

        assert rowid == 1
        mock_embedder.embed.assert_called()

    @pytest.mark.asyncio
    async def test_save_turn_chunks_long_text(self):
        """Long turns are chunked — embedder.embed() receives multiple chunks."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 1
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        mock_embedder = MagicMock()
        mock_embedder.available = True
        mock_embedder.max_tokens = 8192
        # Return one embedding per chunk
        mock_embedder.embed = AsyncMock(return_value=[[0.1] * 1024] * 3)

        long_msg = "\n".join([f"line {i}" for i in range(500)])
        rowid = await store.save_turn(
            user_message=long_msg,
            embedder=mock_embedder,
        )

        assert rowid == 1
        # embed() should have been called with multiple chunks
        mock_embedder.embed.assert_called_once()
        chunks = mock_embedder.embed.call_args[0][0]
        assert len(chunks) > 1  # Long text → multiple chunks


class TestSessionStoreGetTurn:
    """Tests for get_turn."""

    @pytest.mark.asyncio
    async def test_get_turn_found(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value={"rowid": 1, "user_message": "Hello"})
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.get_turn(rowid=1)
        assert result == {"rowid": 1, "user_message": "Hello"}

    @pytest.mark.asyncio
    async def test_get_turn_not_found(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.get_turn(rowid=999)
        assert result is None


class TestSessionStoreGetRecentTurns:
    """Tests for get_recent_turns."""

    @pytest.mark.asyncio
    async def test_get_recent_turns(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "Turn 1"},
            {"rowid": 2, "user_message": "Turn 2"},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.get_recent_turns(limit=50)
        assert len(result) == 2
        assert result[0]["user_message"] == "Turn 1"
        assert result[1]["user_message"] == "Turn 2"

    @pytest.mark.asyncio
    async def test_get_recent_turns_real_db(self, tmp_path):
        """Integration test: read recent turns from a real SQLite DB."""
        import json

        db_path = tmp_path / "memory.db"

        # ── Set up schema directly (bypass setup() to avoid sqlite_vec) ──
        import aiosqlite

        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("""\
            CREATE TABLE IF NOT EXISTS diary (
                user_message   TEXT NOT NULL DEFAULT '',
                messages       TEXT NOT NULL DEFAULT '[]',
                summary        TEXT DEFAULT '',
                tags           TEXT DEFAULT '',
                created_at     TEXT NOT NULL,
                channel        TEXT DEFAULT '',
                who_helped     TEXT DEFAULT '',
                what_model     TEXT DEFAULT '',
                token_count    INTEGER NOT NULL DEFAULT 0
            )""")
        await conn.commit()

        # ── Insert 5 turns ──
        for i in range(1, 6):
            await conn.execute(
                """INSERT INTO diary
                   (user_message, messages, created_at, who_helped, what_model, token_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    f"User message {i}",
                    json.dumps([{"role": "assistant", "content": f"Reply {i}"}]),
                    f"2026-07-22T10:0{i}:00",
                    "deepseek-v4-flash",
                    "deepseek/deepseek-v4-flash",
                    100 + i,
                ),
            )
        await conn.commit()
        await conn.close()

        # ── Read back via SessionStore ──
        store = SessionStore(db_path)
        store._conn = await aiosqlite.connect(str(db_path))
        store._conn.row_factory = aiosqlite.Row

        # With limit=3, should return the 3 most recent (rows 3,4,5), newest-first
        result = await store.get_recent_turns(limit=3)
        assert len(result) == 3
        assert result[0]["user_message"] == "User message 5"
        assert result[1]["user_message"] == "User message 4"
        assert result[2]["user_message"] == "User message 3"

        # All columns should be present in each row
        for turn in result:
            assert "rowid" in turn
            assert "user_message" in turn
            assert "messages" in turn
            assert "created_at" in turn
            assert "who_helped" in turn
            assert "what_model" in turn
            assert "token_count" in turn

        # Verify messages are parseable JSON
        assert json.loads(result[0]["messages"])[0]["content"] == "Reply 5"

        # No limit: should return all 5, newest-first
        all_result = await store.get_recent_turns(limit=50)
        assert len(all_result) == 5
        assert all_result[0]["user_message"] == "User message 5"
        assert all_result[4]["user_message"] == "User message 1"

        await store._conn.close()


class TestSessionStoreHasTurns:
    """Tests for has_turns."""

    @pytest.mark.asyncio
    async def test_has_turns_true(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=(1,))
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.has_turns()
        assert result is True

    @pytest.mark.asyncio
    async def test_has_turns_false(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.has_turns()
        assert result is False


class TestSessionStoreCountTurns:
    """Tests for count_turns."""

    @pytest.mark.asyncio
    async def test_count_no_filter(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()

        total_cursor = AsyncMock()
        total_cursor.fetchone = AsyncMock(return_value=(42,))

        mock_conn.execute = AsyncMock(return_value=total_cursor)
        store._conn = mock_conn

        result = await store.count_turns()
        assert result["total"] == 42
        assert result["filtered"] == 42

    @pytest.mark.asyncio
    async def test_count_with_fts5_query(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()

        call_count = [0]

        async def _execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            cursor = AsyncMock()
            if call_count[0] == 1:
                cursor.fetchone = AsyncMock(return_value=(10,))
            else:
                cursor.fetchone = AsyncMock(return_value=(3,))
            return cursor

        mock_conn.execute = AsyncMock(side_effect=_execute_side_effect)
        store._conn = mock_conn

        result = await store.count_turns(query="hello", mode="fts5")
        assert result["total"] == 10
        assert result["filtered"] == 3

    @pytest.mark.asyncio
    async def test_count_fts5_honors_since_until(self):
        """REVIEW M6 — the fts5 count joins diary and applies since/until
        (previously they were silently ignored for fts5 mode)."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        call_count = [0]

        async def _execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            cursor = AsyncMock()
            cursor.fetchone = AsyncMock(return_value=(5,))
            return cursor

        mock_conn.execute = AsyncMock(side_effect=_execute_side_effect)
        store._conn = mock_conn

        await store.count_turns(
            query="hello", mode="fts5",
            since="2026-01-01", until="2026-02-01",
        )

        # Second execute = the fts5 count — must JOIN diary and carry the
        # time clauses + params.
        sql, params = mock_conn.execute.call_args_list[1][0]
        assert "JOIN diary" in sql
        assert "d.created_at >=" in sql and "d.created_at <=" in sql
        assert len(params) == 3  # fts_query + since + until


class TestSessionStoreListRecent:
    """Tests for list_recent."""

    @pytest.mark.asyncio
    async def test_list_recent(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 2, "user_message": "Chat 2"},
            {"rowid": 1, "user_message": "Chat 1"},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.list_recent(limit=5)
        assert len(result) == 2
        # Newest first
        assert result[0]["rowid"] == 2


class TestSessionStoreUpdateSummary:
    """Tests for update_summary."""

    @pytest.mark.asyncio
    async def test_update_all_fields(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.update_summary(
            rowid=1,
            summary="Great conversation", tags="ai,chat",
        )
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_no_fields_skips(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        store._conn = mock_conn

        await store.update_summary(rowid=1)
        mock_conn.execute.assert_not_called()


class TestSessionStoreSearchKeyword:
    """Tests for search_keyword."""

    @pytest.mark.asyncio
    async def test_search_keyword(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "Hello world", "snippet": "Hello…", "rank": 0.1},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.search_keyword(query="hello")
        assert len(result) == 1
        assert result[0]["rowid"] == 1

    @pytest.mark.asyncio
    async def test_search_keyword_handles_parse_error(self):
        store = SessionStore(Path("/tmp/test.db"))
        import aiosqlite as aiosqlite_mod

        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(
            side_effect=aiosqlite_mod.OperationalError("malformed MATCH expression")
        )
        store._conn = mock_conn

        result = await store.search_keyword(query="bad!!query")
        assert result == []


class TestSessionStoreSearchGrep:
    """Tests for search_grep."""

    @pytest.mark.asyncio
    async def test_search_grep(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "Hello", "context": "Hello world"},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.search_grep(pattern="Hello")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_search_grep_escapes_like_metachars(self):
        """REVIEW M5 — %/_ in the pattern are escaped and the LIKE carries an
        ESCAPE '\\' clause, so they match literally instead of acting as
        wildcards (previously the escape was a no-op and '50%' matched nothing)."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        await store.search_grep(pattern="50%_done")

        sql, params = mock_conn.execute.call_args[0]
        assert "ESCAPE '\\'" in sql
        # instr context uses the raw pattern (literal); the LIKEs use the
        # escaped pattern so %/_ are literal.
        assert params[0] == "50%_done"
        assert params[1] == "%50\\%\\_done%"
        assert params[2] == "%50\\%\\_done%"

    @pytest.mark.asyncio
    async def test_search_clamps_negative_limit(self):
        """REVIEW M6 — a negative limit is clamped (SQLite LIMIT -1 = unlimited)."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        await store.search_grep(pattern="x", limit=-5)

        sql, params = mock_conn.execute.call_args[0]
        assert params[-1] == 20  # clamped to the default, not -5


class TestSessionStoreSearchTime:
    """Tests for search_time."""

    @pytest.mark.asyncio
    async def test_search_time(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "Old turn"},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.search_time(
            since="2024-01-01",
            until="2024-12-31",
        )
        assert len(result) == 1


class TestSessionStoreSearchSemantic:
    """Tests for search_semantic."""

    @pytest.mark.asyncio
    async def test_search_semantic(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "summary": "A chat", "distance": 0.5},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.search_semantic(
            embedding=[0.1, 0.2, 0.3],
        )
        assert len(result) == 1


class TestSessionStoreUpsertEmbedding:
    """Tests for upsert_embedding."""

    @pytest.mark.asyncio
    async def test_upsert_insert(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)  # No existing
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.upsert_embedding(
            diary_rowid=1, chunk_index=0,
            summary="", tags="", created_at="2024-01-01T00:00:00",
            turn_embedding=[0.1, 0.2, 0.3],
        )
        assert mock_conn.execute.call_count == 1  # INSERT only (no SELECT needed)
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_update_existing(self):
        """upsert_embedding always INSERTs — caller handles clearing old chunks."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=AsyncMock())
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.upsert_embedding(
            diary_rowid=1, chunk_index=0,
            summary="updated", tags="new", created_at="2024-01-01T00:00:00",
            turn_embedding=[0.4, 0.5, 0.6],
        )
        # Single INSERT — _clear_chunks() is called separately by the caller
        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()


class TestSessionStoreHasEmbedding:
    """Tests for has_embedding."""

    @pytest.mark.asyncio
    async def test_has_embedding_true(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value={"rowid": 1})
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.has_embedding(diary_rowid=1)
        assert result is True

    @pytest.mark.asyncio
    async def test_has_embedding_false(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.has_embedding(diary_rowid=1)
        assert result is False
