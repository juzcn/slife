"""Tests for slife.plugins.memdb.store — SessionStore and helpers."""

import pytest; pytestmark = pytest.mark.unit


import struct
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite

import pytest

from slife.plugins.memdb.store import (
    SessionStore,
    _normalize_time_param,
    _now,
    _serialize_f32,
    _split_chunks_to_token_limit,
    _split_sql,
    _to_fts5_query,
    DEFAULT_EMBEDDING_DIM,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _create_diary_table(conn) -> None:
    """Create the real diary schema (all columns non-NULL defaults) on an
    already-open aiosqlite connection.

    Matches ``schema.sql``; the full column list is required because
    ``get_recent_turns`` SELECTs every column.
    """
    await conn.execute("""\
        CREATE TABLE IF NOT EXISTS diary (
            user_message   TEXT NOT NULL DEFAULT '',
            messages       TEXT NOT NULL DEFAULT '[]',
            summary        TEXT DEFAULT '',
            tags           TEXT DEFAULT '',
            images         TEXT NOT NULL DEFAULT '',
            created_at     TEXT NOT NULL,
            completed_at   TEXT,
            channel        TEXT DEFAULT '',
            who_helped     TEXT DEFAULT '',
            what_model     TEXT DEFAULT '',
            token_count    INTEGER NOT NULL DEFAULT 0,
            prompt_tokens  INTEGER NOT NULL DEFAULT 0
        )""")


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


class TestSplitChunksToTokenLimit:
    """Oversized chunks must be hard-split, never dropped — a dropped chunk
    leaves the turn unembedded and locks the semantic-search gate off."""

    def test_splits_oversized_chunk_without_dropping(self):
        chunks = ["x" * 100]
        out = _split_chunks_to_token_limit(chunks, max_tokens=10)  # 40-char limit
        assert all(len(c) <= 40 for c in out)
        assert "".join(out) == "x" * 100  # nothing lost

    def test_leaves_small_chunks_untouched(self):
        chunks = ["hello", "world"]
        assert _split_chunks_to_token_limit(chunks, max_tokens=100) == chunks

    def test_empty_list(self):
        assert _split_chunks_to_token_limit([], max_tokens=100) == []

    def test_nonpositive_limit_returns_unchanged(self):
        chunks = ["abc"]
        assert _split_chunks_to_token_limit(chunks, max_tokens=0) == chunks


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
        """Offset-aware datetimes are normalized to the local offset (same
        instant) so they compare correctly against local created_at."""
        import datetime as _dt
        expected = _dt.datetime.fromisoformat(
            "2026-07-20T14:39:19+08:00"
        ).astimezone().isoformat(timespec="seconds")
        assert _normalize_time_param("2026-07-20T14:39:19+08:00", "since") == expected

    def test_utc_z_datetime_normalized_to_local(self):
        """A UTC ('Z') datetime is converted to local so a lexicographic
        comparison against local created_at doesn't misorder the offset."""
        import datetime as _dt
        result = _normalize_time_param("2026-07-20T06:39:19Z", "since")
        assert result != "2026-07-20T06:39:19Z"
        assert _dt.datetime.fromisoformat(result) == _dt.datetime.fromisoformat(
            "2026-07-20T06:39:19Z"
        )

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

    def test_trigger_after_comment_block_is_kept_together(self):
        """A CREATE TRIGGER preceded by -- comments must not be split.

        Regression: the diary_au trigger (two interior INSERTs) follows a
        comment block; the comments accumulate into the same fragment and
        used to hide the CREATE TRIGGER keyword, so the trigger body was
        split into orphan fragments and never created.
        """
        sql = (
            "-- memory_turn_summarize writes summary/tags via UPDATE\n"
            "-- index must track those updates\n"
            "CREATE TRIGGER IF NOT EXISTS diary_au AFTER UPDATE ON diary BEGIN\n"
            "    INSERT INTO diary_fts(diary_fts, rowid) VALUES ('delete', old.rowid);\n"
            "    INSERT INTO diary_fts(rowid) VALUES (new.rowid);\n"
            "END;\n"
        )
        result = _split_sql(sql)
        assert len(result) == 1
        assert "CREATE TRIGGER" in result[0]
        assert result[0].rstrip().rstrip(";").strip().upper().endswith("END")


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

    @pytest.mark.asyncio
    @patch("pathlib.Path.mkdir")
    @patch("slife.plugins.memdb.store.aiosqlite.connect")
    async def test_setup_vec_load_failure_degrades(self, mock_connect, mock_mkdir):
        """When sqlite-vec can't load (e.g. no bundled extension on macOS
        CI), setup must NOT fail — the store degrades to keyword-only
        (dim 0) so session restore / keyword search still work."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.executescript = AsyncMock()
        mock_conn.commit = AsyncMock()
        mock_conn.enable_load_extension = AsyncMock()
        mock_conn.load_extension = AsyncMock(
            side_effect=RuntimeError("vec unavailable on this platform")
        )

        async def _connect(*args, **kwargs):
            return mock_conn

        mock_connect.side_effect = _connect

        store = SessionStore(Path("/tmp/test.db"))
        await store.setup(embedding_dim=1024)  # must not raise

        assert store._vec_available is False
        assert store._embedding_dim == 0  # degraded to keyword-only

    @pytest.mark.asyncio
    @patch("pathlib.Path.mkdir")
    @patch("slife.plugins.memdb.store.aiosqlite.connect")
    async def test_reconfigure_for_embedding_reuses_connection(self, mock_connect, mock_mkdir):
        """reconfigure_for_embedding must upgrade the live connection in
        place, not reconnect — a second connect leaked the old handle and
        opened a commit race (save_turn's execute/commit split across two
        connections)."""
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        mock_conn.commit = AsyncMock()
        mock_conn.enable_load_extension = AsyncMock()
        mock_conn.load_extension = AsyncMock()

        async def _connect(*args, **kwargs):
            return mock_conn

        mock_connect.side_effect = _connect

        with patch("sqlite_vec.loadable_path", return_value="/path/to/vec"):
            store = SessionStore(Path("/tmp/test.db"))
            await store.setup(embedding_dim=0)
            conn_before = store._conn
            await store.reconfigure_for_embedding(
                embedding_dim=768, embedding_model="transformer:bge-m3",
            )

        assert mock_connect.call_count == 1
        assert store._conn is conn_before
        assert store._embedding_dim == 768


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
    async def test_save_turn_honors_created_at(self):
        """save_turn persists created_at (user input) and completed_at
        (assistant completion) when both are threaded from the harness."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = MagicMock()
        mock_cursor.lastrowid = 7
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        rowid = await store.save_turn(
            user_message="Hello",
            created_at="2026-08-12T14:32:09+08:00",
            completed_at="2026-08-12T14:35:40+08:00",
        )

        assert rowid == 7
        args = mock_conn.execute.call_args[0][1]
        # INSERT tuple order: (user_message, messages_json, images_json,
        #                      channel, created_at, completed_at,
        #                      who_helped, what_model, token_count)
        assert args[4] == "2026-08-12T14:32:09+08:00"
        assert args[5] == "2026-08-12T14:35:40+08:00"

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
                images         TEXT NOT NULL DEFAULT '',
                created_at     TEXT NOT NULL,
                completed_at   TEXT,
                channel        TEXT DEFAULT '',
                who_helped     TEXT DEFAULT '',
                what_model     TEXT DEFAULT '',
                token_count    INTEGER NOT NULL DEFAULT 0,
                prompt_tokens  INTEGER NOT NULL DEFAULT 0
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
            assert "completed_at" in turn
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

        # after_rowid (persisted live-context boundary) — only newer rows
        boundary_result = await store.get_recent_turns(limit=50, after_rowid=2)
        assert len(boundary_result) == 3
        assert boundary_result[0]["user_message"] == "User message 5"
        assert boundary_result[-1]["user_message"] == "User message 3"

        await store._conn.close()


class TestSessionStoreContextStart:
    """Live-context boundary on diary_meta — the exclusive-start rowid
    that makes restore rebuild the exit-time context."""

    @pytest.mark.asyncio
    async def test_fresh_db_defaults_to_zero(self, tmp_path):
        """No meta row → boundary 0 → restore everything."""
        db_path = tmp_path / "memory.db"
        store = SessionStore(db_path)
        store._conn = await aiosqlite.connect(str(db_path))
        store._conn.row_factory = aiosqlite.Row
        # diary_meta exists via schema.sql — just create the table bare.
        await store._conn.execute(
            "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await store._conn.execute("CREATE TABLE diary (user_message TEXT)")

        assert await store.get_context_start() == 0
        assert not await store.latest_rowid(), "empty diary → no latest row"

        await store._conn.close()

    @pytest.mark.asyncio
    async def test_set_and_get_roundtrip(self, tmp_path):
        db_path = tmp_path / "memory.db"
        store = SessionStore(db_path)
        store._conn = await aiosqlite.connect(str(db_path))
        store._conn.row_factory = aiosqlite.Row
        await store._conn.execute(
            "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )

        await store.set_context_start(7)
        assert await store.get_context_start() == 7

        await store._conn.close()

    @pytest.mark.asyncio
    async def test_advance_skips_count_rows(self, tmp_path):
        """advance(count) moves the exclusive boundary past count rows."""
        db_path = tmp_path / "memory.db"
        store = SessionStore(db_path)
        store._conn = await aiosqlite.connect(str(db_path))
        store._conn.row_factory = aiosqlite.Row
        await store._conn.execute(
            "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await _create_diary_table(store._conn)
        await store._conn.execute(
            "INSERT INTO diary (user_message, created_at) VALUES "
            "('a','2026-08-12T00:00:00+08:00'),"
            "('b','2026-08-12T00:01:00+08:00'),"
            "('c','2026-08-12T00:02:00+08:00'),"
            "('d','2026-08-12T00:03:00+08:00'),"
            "('e','2026-08-12T00:04:00+08:00')"
        )
        await store._conn.commit()
        await store.set_context_start(1)  # everything from rowid 2 on is in-context

        boundary = await store.advance_context_start(2)  # trim turns 2,3

        assert boundary == 3          # exclusive: rows 2,3 out, 4+ in
        assert await store.get_context_start() == 3
        # get_recent_turns sees only the in-context suffix
        turns = await store.get_recent_turns(after_rowid=boundary)
        assert [t["rowid"] for t in turns] == [5, 4], "newest-first, only after boundary"

        await store._conn.close()

    @pytest.mark.asyncio
    async def test_advance_clamps_to_latest_when_short(self, tmp_path):
        """Fewer than count rows remain → clamp to the latest row, never
        overshoot — a restore can under-restore, never skip forward."""
        db_path = tmp_path / "memory.db"
        store = SessionStore(db_path)
        store._conn = await aiosqlite.connect(str(db_path))
        store._conn.row_factory = aiosqlite.Row
        await store._conn.execute(
            "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await store._conn.execute(
            "CREATE TABLE diary (user_message TEXT)"
        )
        await store._conn.execute(
            "INSERT INTO diary (user_message) VALUES ('a'),('b')"
        )

        boundary = await store.advance_context_start(50)

        assert boundary == 2, "clamped at the latest row"

        await store._conn.close()

    @pytest.mark.asyncio
    async def test_set_context_start_latest_flushes_history(self, tmp_path):
        """clear_context semantics: move the boundary to the latest row so
        only turns saved afterwards come back on restore."""
        db_path = tmp_path / "memory.db"
        store = SessionStore(db_path)
        store._conn = await aiosqlite.connect(str(db_path))
        store._conn.row_factory = aiosqlite.Row
        await store._conn.execute(
            "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        await _create_diary_table(store._conn)
        await store._conn.execute(
            "INSERT INTO diary (user_message, created_at) VALUES "
            "('a','2026-08-12T00:00:00+08:00'),"
            "('b','2026-08-12T00:01:00+08:00'),"
            "('c','2026-08-12T00:02:00+08:00')"
        )

        assert await store.set_context_start_latest() == 3
        # a turn saved afterwards (rowid 4) is the only in-context content
        await store._conn.execute(
            "INSERT INTO diary (user_message, created_at) "
            "VALUES ('d', '2026-08-12T00:03:00+08:00')"
        )
        await store._conn.commit()
        turns = await store.get_recent_turns(after_rowid=3)
        assert [t["rowid"] for t in turns] == [4]

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

    @pytest.mark.asyncio
    async def test_count_fts5_with_cjk_routes_to_like(self):
        """Regression: FTS5 unicode61 can't match whole-sentence CJK, so an
        fts5-mode count must route CJK to the LIKE path — otherwise count and
        search disagree (memory_count=0 while memory_search returns hits)."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        call_count = [0]

        async def _execute_side_effect(*args, **kwargs):
            call_count[0] += 1
            cursor = AsyncMock()
            cursor.fetchone = AsyncMock(return_value=(3,))
            return cursor

        mock_conn.execute = AsyncMock(side_effect=_execute_side_effect)
        store._conn = mock_conn

        await store.count_turns(query="今天天气怎么样", mode="fts5")

        sql = mock_conn.execute.call_args_list[1][0][0]
        assert "MATCH" not in sql
        assert "LIKE" in sql
        assert "ESCAPE" in sql

    @pytest.mark.asyncio
    async def test_count_unembedded_excludes_empty_turns(self):
        """Regression: a turn with no user text and no messages can never be
        embedded — it must not count as unembedded or the semantic gate stalls
        forever on the same zero-text rows."""
        store = SessionStore(Path("/tmp/test.db"))
        store._embedding_dim = 1536
        mock_conn = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(2,))
        mock_conn.execute = AsyncMock(return_value=cursor)
        store._conn = mock_conn

        count = await store.count_unembedded()

        sql = mock_conn.execute.call_args[0][0]
        assert "NOT IN" in sql
        assert "trim(COALESCE(d.user_message, ''))" in sql
        assert count == 2


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

    @pytest.mark.asyncio
    async def test_list_recent_windowed_by_rowid(self):
        """before_rowid / after_rowid anchor the window (exclusive)."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        await store.list_recent(limit=5, before_rowid=3)
        sql, params = mock_conn.execute.await_args.args
        assert "rowid < ?" in sql and "rowid > ?" not in sql
        assert 3 in params

        await store.list_recent(limit=5, after_rowid=1)
        sql, params = mock_conn.execute.await_args.args
        assert "rowid > ?" in sql and "rowid < ?" not in sql
        assert 1 in params

        await store.list_recent(limit=5, before_rowid=5, after_rowid=1)
        sql, params = mock_conn.execute.await_args.args
        assert "rowid < ?" in sql and "rowid > ?" in sql
        assert params[:2] == [5, 1]

        # No anchors → plain query, no WHERE.
        await store.list_recent(limit=5)
        sql, params = mock_conn.execute.await_args.args
        assert "WHERE" not in sql
        assert params == [5]


class TestSessionStoreTokenUsage:
    """Tests for token_usage — per-turn billing / context-size query."""

    @pytest.mark.asyncio
    async def test_aggregates_sums(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 2, "token_count": 300, "prompt_tokens": 200},
            {"rowid": 1, "token_count": 100, "prompt_tokens": 80},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.token_usage(limit=50)
        assert result["summary"]["count"] == 2
        assert result["summary"]["total_token_count"] == 400
        assert result["summary"]["total_prompt_tokens"] == 280
        assert result["summary"]["avg_token_count"] == 200

    @pytest.mark.asyncio
    async def test_rowid_filters(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        await store.token_usage(rowid=7, limit=10)
        sql, params = mock_conn.execute.await_args.args
        assert "rowid = ?" in sql
        assert 7 in params
        assert 10 in params

    @pytest.mark.asyncio
    async def test_time_window_filters(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        await store.token_usage(since="2026-01-01", until="2026-02-01", limit=5)
        sql, params = mock_conn.execute.await_args.args
        assert "created_at >= ?" in sql
        assert "created_at <= ?" in sql
        # A date-only `until` is advanced a day so records on that day are
        # included (the same normalisation every time-filtered query uses).
        assert params[:2] == ["2026-01-01", "2026-02-02"]


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


class TestSessionStoreLatestRowid:
    """Tests for latest_rowid (the memory_turn_summarize default)."""

    @pytest.mark.asyncio
    async def test_returns_newest_rowid(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=(42,))
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        assert await store.latest_rowid() == 42

    @pytest.mark.asyncio
    async def test_empty_diary_returns_none(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        assert await store.latest_rowid() is None


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

    @pytest.mark.asyncio
    async def test_search_keyword_cjk_falls_back_to_like(self):
        """A whole-sentence CJK query routes to LIKE (FTS5 unicode61 cannot
        segment Chinese — it returns nothing for a longer turn)."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "今天北京天气怎么样？", "snippet": "…", "rank": 0},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.search_keyword(query="北京天气怎么样")
        assert len(result) == 1

        sql, params = mock_conn.execute.call_args[0]
        assert "LIKE" in sql
        assert "MATCH" not in sql
        assert params[1] == "%北京天气怎么样%"  # escaped LIKE pattern

    @pytest.mark.asyncio
    async def test_search_keyword_ascii_keeps_fts5(self):
        """ASCII queries still go through the FTS5 MATCH path."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        await store.search_keyword(query="hello world")

        sql, _ = mock_conn.execute.call_args[0]
        assert "MATCH" in sql

    @pytest.mark.asyncio
    async def test_search_keyword_cjk_multiword_ands(self):
        """Space-separated CJK words AND together (each word must appear),
        matching FTS5's space-splitting — a single LIKE on the whole phrase
        would return nothing because stored text has no spaces."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        await store.search_keyword(query="子agent 委托 测试")

        sql, params = mock_conn.execute.call_args[0]
        assert "LIKE" in sql
        # 3 words × 4 columns each = 12 LIKE predicates, ANDed together.
        assert sql.count("LIKE") == 12
        assert "%子agent%" in params
        assert "%委托%" in params
        assert "%测试%" in params


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
        # First fetchall = KNN rows, second = diary lookup rows.
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [{"rowid": 1, "diary_rowid": 7, "summary": "A chat", "distance": 0.5,
              "tags": "", "created_at": "2026-01-01"}],
            [{"rowid": 7, "user_message": "北京天气怎么样"}],
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.search_semantic(
            embedding=[0.1, 0.2, 0.3],
        )
        assert len(result) == 1
        assert result[0]["rowid"] == 7  # rowid == diary_rowid (merge_hybrid keys on it)
        assert result[0]["user_message"] == "北京天气怎么样"

    @pytest.mark.asyncio
    async def test_search_semantic_knn_has_no_join(self):
        """sqlite-vec forbids auxiliary-column constraints (including JOIN ON)
        inside a KNN query — the KNN runs alone and the diary lookup is a
        second query."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(side_effect=[
            [{"rowid": 1, "diary_rowid": 7, "summary": "s", "tags": "",
              "created_at": "2026-01-01", "distance": 0.1}],
            [{"rowid": 7, "user_message": "北京天气怎么样"}],
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        await store.search_semantic(embedding=[0.1, 0.2], limit=5)

        # First execute is the KNN query — no JOIN, bare k.
        knn_sql = mock_conn.execute.call_args_list[0].args[0]
        assert "JOIN" not in knn_sql
        assert "turn_embedding MATCH ? AND k = ?" in knn_sql
        # Second execute fetches user_message for the surviving diary_rowids.
        assert mock_conn.execute.await_count == 2
        second_sql = mock_conn.execute.call_args_list[1].args[0]
        assert "user_message FROM diary" in second_sql


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


class TestSessionStoreReplaceEmbeddingChunks:
    """replace_embedding_chunks — atomic per-turn chunk replace.

    A turn's chunks must be replaced in ONE transaction: a crash mid-way
    rolls back to NO chunks (turn fully unembedded, re-indexed next pass),
    never a half-indexed turn that the NOT-IN-unembedded query would
    mistake for complete.
    """

    @pytest.mark.asyncio
    async def test_replaces_all_chunks_in_one_transaction(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(return_value=AsyncMock())
        mock_conn.commit = AsyncMock()
        mock_conn.rollback = AsyncMock()
        store._conn = mock_conn

        await store.replace_embedding_chunks(
            {"doc_id": 7, "summary": "s", "tags": "t",
             "created_at": "2024-01-01T00:00:00"},
            [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
        )
        # DELETE (1) + 3 INSERTs = 4 executes, exactly one commit, no rollback
        assert mock_conn.execute.call_count == 4
        mock_conn.commit.assert_awaited_once()
        mock_conn.rollback.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rolls_back_on_mid_insert_failure(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock(side_effect=[
            AsyncMock(),              # DELETE
            AsyncMock(),              # INSERT chunk 0
            RuntimeError("boom"),     # INSERT chunk 1 → fails mid-way
            AsyncMock(),              # INSERT chunk 2 (not reached)
        ])
        mock_conn.commit = AsyncMock()
        mock_conn.rollback = AsyncMock()
        store._conn = mock_conn

        with pytest.raises(RuntimeError):
            await store.replace_embedding_chunks(
                {"doc_id": 7, "summary": "", "tags": "",
                 "created_at": "2024-01-01T00:00:00"},
                [[0.1, 0.2], [0.3, 0.4], [0.5, 0.6]],
            )
        mock_conn.rollback.assert_awaited_once()
        mock_conn.commit.assert_not_awaited()


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


class TestSessionStoreCountEmbedded:
    """count_embedded — distinct turns that have ≥1 embedding chunk."""

    @pytest.mark.asyncio
    async def test_counts_distinct_diary_rowids(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=(7,))  # row[0] index access
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        assert await store.count_embedded() == 7

    @pytest.mark.asyncio
    async def test_zero_when_no_rows(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        assert await store.count_embedded() == 0


class TestSessionStoreGetUnembeddedDocs:
    """get_unembedded_docs must return doc_id/text/summary/tags so a rebuild
    preserves them via replace_embedding_chunks."""

    @pytest.mark.asyncio
    async def test_selects_summary_and_tags(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        row = {
            "doc_id": 3, "user_message": "hi", "messages": "[]",
            "summary": "sum", "tags": "tag", "created_at": "2026-01-01T00:00:00+00:00",
        }
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[row])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        docs = await store.get_unembedded_docs(limit=10)

        assert len(docs) == 1
        assert docs[0]["doc_id"] == 3
        assert docs[0]["text"] == "hi"  # embed-ready turn text
        assert docs[0]["summary"] == "sum"
        assert docs[0]["tags"] == "tag"
        sql = mock_conn.execute.await_args.args[0]
        assert "summary" in sql and "tags" in sql
