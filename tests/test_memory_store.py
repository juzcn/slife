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

        with patch("sqlite_vec.loadable_path", return_value="/path/to/vec"):
            store = SessionStore(Path("/tmp/test.db"))
            await store.setup()

        mock_connect.assert_called_once()
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
            author="testuser",
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
        mock_embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3])

        rowid = await store.save_turn(
            author="testuser",
            user_message="Hello",
            embedder=mock_embedder,
        )

        assert rowid == 1
        mock_embedder.embed_one.assert_called()

    @pytest.mark.asyncio
    async def test_save_turn_embedder_skip_on_overflow(self):
        """When the turn text exceeds the model's token limit, skip embedding."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 1
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        mock_embedder = MagicMock()
        mock_embedder.available = True
        mock_embedder.max_tokens = 1  # Very small limit

        rowid = await store.save_turn(
            author="testuser",
            user_message="Hello world " * 500,  # Way over the limit
            embedder=mock_embedder,
        )

        assert rowid == 1
        # embed_one should NOT have been called — text was too long
        mock_embedder.embed_one.assert_not_called()


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

        result = await store.get_turn(rowid=1, author="user")
        assert result == {"rowid": 1, "user_message": "Hello"}

    @pytest.mark.asyncio
    async def test_get_turn_not_found(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.get_turn(rowid=999, author="user")
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

        result = await store.get_recent_turns(author="user", limit=50)
        assert len(result) == 2
        assert result[0]["user_message"] == "Turn 1"
        assert result[1]["user_message"] == "Turn 2"


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

        result = await store.has_turns(author="user")
        assert result is True

    @pytest.mark.asyncio
    async def test_has_turns_false(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.has_turns(author="user")
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

        result = await store.count_turns(author="user")
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

        result = await store.count_turns(author="user", query="hello", mode="fts5")
        assert result["total"] == 10
        assert result["filtered"] == 3


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

        result = await store.list_recent(author="user", limit=5)
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
            rowid=1, author="user",
            summary="Great conversation", tags="ai,chat",
        )
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_no_fields_skips(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        store._conn = mock_conn

        await store.update_summary(rowid=1, author="user")
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

        result = await store.search_keyword(author="user", query="hello")
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

        result = await store.search_keyword(author="user", query="bad!!query")
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

        result = await store.search_grep(author="user", pattern="Hello")
        assert len(result) == 1


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
            author="user",
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
            author="user",
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
            rowid=1, author="user",
            summary="", tags="", created_at="2024-01-01T00:00:00",
            turn_embedding=[0.1, 0.2, 0.3],
        )
        assert mock_conn.execute.call_count == 2  # SELECT + INSERT
        mock_conn.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_upsert_update_existing(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        # First call: check existing -> found; second: DELETE; third: INSERT
        call_count = [0]

        async def _side_effect(*args, **kwargs):
            call_count[0] += 1
            cursor = AsyncMock()
            if call_count[0] == 1:
                cursor.fetchone = AsyncMock(return_value={"rowid": 1})
            else:
                cursor.fetchone = AsyncMock(return_value=None)
            return cursor

        mock_conn.execute = AsyncMock(side_effect=_side_effect)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.upsert_embedding(
            rowid=1, author="user",
            summary="updated", tags="new", created_at="2024-01-01T00:00:00",
            turn_embedding=[0.4, 0.5, 0.6],
        )
        assert mock_conn.execute.call_count == 3  # SELECT + DELETE + INSERT


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
