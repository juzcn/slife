"""Tests for slife_memory.store — SessionStore and helpers."""

import struct
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife_memory.store import (
    SessionStore,
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
        assert "+" in result or result.endswith("Z")


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
    @patch("slife_memory.store.aiosqlite.connect")
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

        # Patch sqlite_vec inside _load_vec_extension
        with patch("sqlite_vec.loadable_path", return_value="/path/to/vec"):
            store = SessionStore(Path("/tmp/test.db"))
            await store.setup()

        mock_connect.assert_called_once()
        mock_conn.executescript.assert_called_once()
        mock_conn.commit.assert_called()


    @pytest.mark.asyncio
    @patch("pathlib.Path.mkdir")
    @patch("slife_memory.store.aiosqlite.connect")
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
            # Second setup should not raise — schema uses IF NOT EXISTS
            await store.setup()

        assert mock_connect.call_count == 2
        assert mock_conn.executescript.call_count == 2


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


class TestSessionStoreOpenDiary:
    """Tests for open_diary."""

    @pytest.mark.asyncio
    async def test_open_diary_no_interrupted(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        # 1st execute: find_interrupted → returns None
        # 2nd execute: INSERT → returns cursor with lastrowid
        # 3rd execute: _find_last_diary → returns None (no prior sessions)
        mock_cursor_interrupted = AsyncMock()

        async def _fetchone_none():
            return None
        mock_cursor_interrupted.fetchone = _fetchone_none

        mock_cursor_last = AsyncMock()
        mock_cursor_last.fetchone = _fetchone_none

        mock_cursor_insert = MagicMock()
        mock_cursor_insert.lastrowid = 42

        async def _execute_side_effect(*args, **kwargs):
            call_count = mock_conn.execute.call_count
            if call_count == 1:
                return mock_cursor_interrupted
            elif call_count == 2:
                return mock_cursor_insert
            else:
                return mock_cursor_last

        mock_conn.execute = AsyncMock(side_effect=_execute_side_effect)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        result = await store.open_diary(
            author="testuser",
            who_helped="assistant",
            what_model="deepseek/flash",
            system_prompt="You are helpful.",
        )

        assert result["rowid"] == 42
        assert result["interrupted"] is False

    @pytest.mark.asyncio
    async def test_open_diary_with_interrupted(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()

        interrupted_row = {
            "rowid": 99, "author": "testuser", "title": "Previous",
            "created_at": "2024-01-01T00:00:00", "updated_at": "2024-01-01T01:00:00",
            "status": "进行中", "how_many_turns": 3, "how_many_tokens": 500,
            "who_helped": "assistant", "what_model": "deepseek/flash",
        }
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=interrupted_row)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.open_diary(author="testuser")

        assert result["rowid"] == 99
        assert result["interrupted"] is True
        assert result["title"] == "Previous"


class TestSessionStoreUpdateDiary:
    """Tests for update_diary."""

    @pytest.mark.asyncio
    async def test_update_with_messages_and_counts(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.update_diary(
            rowid=1, author="user",
            messages=[{"role": "user", "content": "hello"}],
            turn_count=2, token_count=100,
        )

        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_messages_only(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.update_diary(
            rowid=1, author="user",
            messages=[{"role": "user", "content": "hi"}],
        )

        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_counters_only(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.update_diary(
            rowid=1, author="user",
            turn_count=5, token_count=200,
        )

        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_timestamp_only(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.update_diary(rowid=1, author="user")
        mock_conn.commit.assert_called_once()


class TestSessionStoreCloseDiary:
    """Tests for close_diary."""

    @pytest.mark.asyncio
    async def test_close_diary(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.close_diary(rowid=1, author="user")

        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()


class TestSessionStoreMarkCrashed:
    """Tests for mark_crashed."""

    @pytest.mark.asyncio
    async def test_mark_crashed(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.mark_crashed(rowid=1, author="user")

        mock_conn.execute.assert_called_once()
        mock_conn.commit.assert_called_once()


class TestSessionStoreGetDiary:
    """Tests for get_diary."""

    @pytest.mark.asyncio
    async def test_get_diary_found(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value={"rowid": 1, "title": "Test"})
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.get_diary(rowid=1, author="user")
        assert result == {"rowid": 1, "title": "Test"}

    @pytest.mark.asyncio
    async def test_get_diary_not_found(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.get_diary(rowid=999, author="user")
        assert result is None


class TestSessionStoreListRecent:
    """Tests for list_recent."""

    @pytest.mark.asyncio
    async def test_list_recent(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "title": "Chat 1"},
            {"rowid": 2, "title": "Chat 2"},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.list_recent(author="user", limit=5)
        assert len(result) == 2


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
            rowid=1, author="user",
            title="My Chat", summary="Great conversation",
            tags="ai,chat", key_moments="Found bug",
        )
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_no_fields_skips(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        store._conn = mock_conn

        await store.update_summary(rowid=1, author="user")
        mock_conn.execute.assert_not_called()




class TestSessionStoreFindInterrupted:
    """Tests for find_interrupted."""

    @pytest.mark.asyncio
    async def test_find_interrupted_found(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value={
            "rowid": 5, "author": "user", "title": "Crashed",
        })
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.find_interrupted(author="user")
        assert result is not None
        assert result["rowid"] == 5

    @pytest.mark.asyncio
    async def test_find_interrupted_none(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.find_interrupted(author="user")
        assert result is None


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

        result = await store.has_embedding(rowid=1, author="user")
        assert result is True

    @pytest.mark.asyncio
    async def test_has_embedding_false(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.has_embedding(rowid=1, author="user")
        assert result is False
