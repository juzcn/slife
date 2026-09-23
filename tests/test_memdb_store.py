"""Tests for slife.plugins.memdb.store — SessionStore and helpers."""

import json
import re
import pytest; pytestmark = pytest.mark.unit


import struct
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite

import pytest

from slife.plugins.memdb.store import (
    SessionStore,
    INDEX_TEXT_VERSION,
    TOOL_ARG_CHARS,
    _char_limit_for_tokens,
    _now,
    _serialize_f32,
    _split_chunks_to_token_limit,
    _split_sql,
    _to_fts5_query,
    _turn_text_for_embedding,
    DEFAULT_EMBEDDING_DIM,
)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _create_diary_table(conn) -> None:
    """Create the real diary schema (all columns non-NULL defaults) on an
    already-open aiosqlite connection.

    Matches ``schema.sql``; the full column list is required because
    ``get_turns_by_ids`` SELECTs every column.
    """
    await conn.execute("""\
        CREATE TABLE IF NOT EXISTS diary (
            user_message   TEXT NOT NULL DEFAULT '',
            messages       TEXT NOT NULL DEFAULT '[]',
            summary        TEXT DEFAULT '',
            tags           TEXT DEFAULT '',
            created_at     TEXT NOT NULL,
            completed_at   TEXT,
            channel        TEXT DEFAULT '',
            who_helped     TEXT DEFAULT '',
            what_model     TEXT DEFAULT '',
            token_count    INTEGER NOT NULL DEFAULT 0,
            context_tokens  INTEGER NOT NULL DEFAULT 0
        )""")
    await conn.execute("""\
        CREATE TABLE IF NOT EXISTS turn_channel (
            turn_id  INTEGER PRIMARY KEY,
            data     TEXT NOT NULL DEFAULT '{}'
        )""")
    # save_turn appends the new rowid to the live-context list inside the
    # same transaction, so the meta table is part of what it needs.
    await conn.execute("""\
        CREATE TABLE IF NOT EXISTS diary_meta (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
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

    def test_dense_single_line_splits_inside_token_budget(self):
        """A newline-free escaped-JSON dump (density ~1-2 chars/token) must
        hard-split so every piece stays within *max_tokens* chars — the old
        Latin-4-chars/token estimate let a 30k-char dump ride as one chunk,
        the provider rejected it (400 invalid parameter), and the drainer
        stalled on that turn forever."""
        line = '{\\"messages\\": \\"{\\\\\\"{a\\\\\\"]\\", ' * 3000
        out = _split_chunks_to_token_limit([line], max_tokens=8192)
        assert out, "oversized line must split, never be dropped"
        assert all(len(c) <= 8192 for c in out)
        assert "".join(out) == line  # nothing lost

    def test_char_limit_floors_density_at_one_char_per_token(self):
        """The char budget never assumes a better density than 1 char/token,
        so a piece cannot exceed the provider's real token limit no matter
        how dense the text is."""
        assert _char_limit_for_tokens(8192, '{"a":1}' * 2000) <= 8192
        assert _char_limit_for_tokens(8192, "x" * 100) <= 8192
        assert _char_limit_for_tokens(8192, "中文" * 100) <= 8192


class TestTurnTextForEmbedding:
    """What a turn's vector is a vector OF: the conversation, not the dumps.

    Tool results were 56–99% of a real turn's text, which made every turn
    read as "an agent ran tools" and left the recall's similarity cap nothing
    to separate a relevant turn from an irrelevant one.  They stay in
    ``messages``, which is what the keyword leg and turn_read read.
    """

    @staticmethod
    def _turn(user_message="查一下首经贸新闻", assistant="查到了，首经贸 70 周年校庆在 10 月 18 日。",
              tool_calls=None, tool_content="<html>…a fetched page, 40KB of markup…</html>"):
        messages = []
        if tool_calls:
            messages.append({"role": "assistant", "content": "",
                             "tool_calls": tool_calls})
        messages.append({"role": "assistant", "content": assistant})
        if tool_content is not None:
            messages.append({"role": "tool", "content": tool_content})
        return user_message, messages

    def test_tool_results_are_not_embedded(self):
        text = _turn_text_for_embedding(*self._turn())
        assert "40KB of markup" not in text

    def test_the_conversation_is_embedded(self):
        text = _turn_text_for_embedding(*self._turn())
        assert "查一下首经贸新闻" in text       # what was asked
        assert "70 周年校庆" in text            # what was answered

    def test_tool_requests_are_embedded(self):
        """The request says what the turn was about — the search string, the
        URL, the command — so a tool-heavy turn keeps a topic even when its
        prose is one line (or "(Turn interrupted)")."""
        calls = [{"id": "1", "type": "function",
                  "function": {"name": "duckduckgo-search__search",
                               "arguments": '{"query": "首经贸 校庆"}'}}]
        text = _turn_text_for_embedding(
            *self._turn(assistant="(Turn interrupted)", tool_calls=calls),
        )
        assert "duckduckgo-search__search" in text
        assert "首经贸 校庆" in text

    def test_tool_arguments_are_bounded(self):
        """One call's arguments cannot own the chunk: a rendered page or a
        file body is unbounded, and the request is what carries the topic."""
        calls = [{"id": "1", "type": "function",
                  "function": {"name": "fetch__fetch",
                               "arguments": "x" * (TOOL_ARG_CHARS * 5)}}]
        text = _turn_text_for_embedding(*self._turn(tool_calls=calls))
        assert text.count("x") == TOOL_ARG_CHARS

    def test_a_turn_with_nothing_but_tool_results_has_no_text(self):
        """No user text, no request, no prose — nothing to embed, and the
        caller's empty-text skip is what keeps the drainer from stalling."""
        assert _turn_text_for_embedding("", [
            {"role": "tool", "content": "a result nobody asked for in prose"},
        ]) == ""


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

    def test_grouping_parens_quoted_not_syntax(self):
        """D3 regression: a lone '(' / ')'(…) must be quoted into a literal
        phrase — FTS5 would otherwise raise a MATCH syntax error."""
        assert _to_fts5_query("(urgent)") == '"urgent"'
        assert "(" not in _to_fts5_query("( urgent )")

    def test_trailing_minus_dropped(self):
        """D3 regression: a trailing bare '-' (FTS5 NOT) is dropped, not left
        as a syntax error."""
        assert _to_fts5_query("foo -") == "foo"

    def test_pure_operator_token_skipped(self):
        assert _to_fts5_query("(") == '""'
        assert _to_fts5_query("-") == '""'


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
        mock_cursor = AsyncMock()
        mock_cursor.lastrowid = 42
        # The save also reads the live-context list inside the same
        # transaction; no row means an empty list.
        mock_cursor.fetchone = AsyncMock(return_value=None)
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
    async def test_save_turn_appends_to_context_list(self):
        """The new rowid joins the live-context list in the same commit."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.lastrowid = 42
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        await store.save_turn(user_message="Hello")

        writes = [
            call[0] for call in mock_conn.execute.call_args_list
            if "diary_meta" in call[0][0] and "INSERT OR REPLACE" in call[0][0]
        ]
        assert writes, "save_turn must append to the live-context list"
        assert json.loads(writes[0][1][1]) == [42]

    @pytest.mark.asyncio
    async def test_save_turn_honors_created_at(self):
        """save_turn persists created_at (user input) and completed_at
        (assistant completion) when both are threaded from the harness."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.lastrowid = 7
        mock_cursor.fetchone = AsyncMock(return_value=None)
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        mock_conn.commit = AsyncMock()
        store._conn = mock_conn

        rowid = await store.save_turn(
            user_message="Hello",
            created_at="2026-08-12T14:32:09+08:00",
            completed_at="2026-08-12T14:35:40+08:00",
        )

        assert rowid == 7
        # The save writes several statements (diary INSERT, context-list
        # append) — pick the diary row's.
        insert = next(
            call for call in mock_conn.execute.call_args_list
            if "INSERT INTO diary " in call[0][0]
        )
        args = insert[0][1]
        # INSERT tuple order: (user_message, messages_json, channel,
        #                      created_at, completed_at,
        #                      who_helped, what_model, token_count)
        assert args[3] == "2026-08-12T14:32:09+08:00"
        assert args[4] == "2026-08-12T14:35:40+08:00"

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
        assert result == {"rowid": 1, "user_message": "Hello"}  # store stays internal

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


class TestSessionStoreGetTurnsByIds:
    """Tests for get_turns_by_ids — the ordered live-context read."""

    @pytest.mark.asyncio
    async def test_returns_turns_in_caller_order(self):
        """The id list is authoritative: the result follows it, and is
        never re-sorted by rowid."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "Turn 1"},
            {"rowid": 2, "user_message": "Turn 2"},
        ])
        # A second query merges the turn_channel payload rows — no rows →
        # every turn falls back to "{}".
        channel_cursor = AsyncMock()
        channel_cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(
            side_effect=[mock_cursor, channel_cursor],
        )
        store._conn = mock_conn

        result = await store.get_turns_by_ids([2, 1])
        assert [t["user_message"] for t in result] == ["Turn 2", "Turn 1"]
        assert result[0]["channel_data"] == "{}"
        assert result[1]["channel_data"] == "{}"

    @pytest.mark.asyncio
    async def test_empty_list_reads_nothing(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_conn.execute = AsyncMock()
        store._conn = mock_conn

        assert await store.get_turns_by_ids([]) == []
        mock_conn.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_turns_by_ids_real_db(self, tmp_path):
        """Integration test: the list drives the read, order included."""
        db_path = tmp_path / "memory.db"

        # ── Set up schema directly (bypass setup() to avoid sqlite_vec) ──
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
                context_tokens  INTEGER NOT NULL DEFAULT 0
            )""")
        await conn.execute("""\
            CREATE TABLE IF NOT EXISTS turn_channel (
                turn_id  INTEGER PRIMARY KEY,
                data     TEXT NOT NULL DEFAULT '{}'
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

        # A contiguous slice, oldest-first as the list spells it.
        result = await store.get_turns_by_ids([3, 4, 5])
        assert [t["user_message"] for t in result] == [
            "User message 3", "User message 4", "User message 5",
        ]

        # All columns should be present in each row
        for turn in result:
            assert "rowid" in turn  # restore's turn_header reads it
            assert "user_message" in turn
            assert "messages" in turn
            assert "created_at" in turn
            assert "completed_at" in turn
            assert "who_helped" in turn
            assert "what_model" in turn
            assert "token_count" in turn

        # Verify messages are parseable JSON
        assert json.loads(result[2]["messages"])[0]["content"] == "Reply 5"

        # NON-CONTIGUOUS — the point of the list.  A rowid boundary could
        # never express this, and the order is the caller's, not rowid's.
        sparse = await store.get_turns_by_ids([5, 1, 3])
        assert [t["user_message"] for t in sparse] == [
            "User message 5", "User message 1", "User message 3",
        ]

        # Unknown ids are skipped, the rest keep their given order.
        assert [t["user_message"] for t in await store.get_turns_by_ids([2, 999])] \
            == ["User message 2"]
        assert await store.get_turns_by_ids([]) == []

        await store._conn.close()

        await store._conn.close()

    @pytest.mark.asyncio
    async def test_chunks_past_the_sql_variable_limit(self, tmp_path):
        """A long list must not blow SQLite's bound-variable limit — the
        read chunks, and the caller's order still wins across chunks."""
        from slife.plugins.memdb.store import _MAX_SQL_VARS

        store = SessionStore(tmp_path / "memory.db")
        store._conn = await aiosqlite.connect(str(tmp_path / "memory.db"))
        store._conn.row_factory = aiosqlite.Row
        await _create_diary_table(store._conn)

        n = _MAX_SQL_VARS + 5
        for i in range(n):
            await store._conn.execute(
                "INSERT INTO diary (user_message, created_at) VALUES (?, ?)",
                (f"msg {i + 1}", "2026-08-12T00:00:00+08:00"),
            )
        await store._conn.commit()

        ids = list(range(n, 0, -1))  # descending — deliberately not rowid order
        turns = await store.get_turns_by_ids(ids)

        assert len(turns) == n
        assert [t["rowid"] for t in turns] == ids
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_duplicate_input_ids_yield_one_turn(self, tmp_path):
        store = SessionStore(tmp_path / "memory.db")
        store._conn = await aiosqlite.connect(str(tmp_path / "memory.db"))
        store._conn.row_factory = aiosqlite.Row
        await _create_diary_table(store._conn)
        await store._conn.execute(
            "INSERT INTO diary (user_message, created_at) VALUES ('a', '2026-08-12T00:00:00+08:00')"
        )
        await store._conn.commit()

        turns = await store.get_turns_by_ids([1, 1, 1])

        assert len(turns) == 1, "one id, one turn — not the same dict aliased"
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_channel_data_round_trip(self, tmp_path):
        """The per-channel payload survives save → reload (A2A peer name)."""
        import aiosqlite

        db_path = tmp_path / "memory.db"
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await _create_diary_table(conn)
        await conn.close()

        store = SessionStore(db_path)
        store._conn = await aiosqlite.connect(str(db_path))
        store._conn.row_factory = aiosqlite.Row

        # A2A turn with a peer-name payload → sibling row written.
        a2a_rowid = await store.save_turn(
            user_message="GO",
            channel="Jack",
            channel_data='{"agent_name": "Jack"}',
        )
        # Built-in turn with no payload → no sibling row.
        await store.save_turn(user_message="hi", channel="human", channel_data="{}")

        cur = await store._conn.execute(
            "SELECT turn_id, data FROM turn_channel WHERE turn_id = ?",
            (a2a_rowid,),
        )
        row = await cur.fetchone()
        assert row is not None
        assert row["data"] == '{"agent_name": "Jack"}'

        turns = await store.get_turns_by_ids([a2a_rowid, a2a_rowid + 1])
        by_user = {t["user_message"]: t for t in turns}
        assert by_user["GO"]["channel_data"] == '{"agent_name": "Jack"}'
        assert by_user["hi"]["channel_data"] == "{}"

        await store._conn.close()


class TestSessionStoreContextTurns:
    """Live-context id list on diary_meta — the ordered list of turns that
    makes restore rebuild the exit-time context."""

    @staticmethod
    async def _store_with_meta(tmp_path, diary=True):
        store = SessionStore(tmp_path / "memory.db")
        store._conn = await aiosqlite.connect(str(tmp_path / "memory.db"))
        store._conn.row_factory = aiosqlite.Row
        await store._conn.execute(
            "CREATE TABLE diary_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        if diary:
            await _create_diary_table(store._conn)
        return store

    @pytest.mark.asyncio
    async def test_fresh_db_is_empty(self, tmp_path):
        """No meta row → empty list → nothing is in context.

        (Under the old scalar boundary an absent row meant ``0`` =
        "replay everything"; an empty list means the opposite, which is why
        the existing databases are being thrown away rather than migrated.)
        """
        store = await self._store_with_meta(tmp_path)
        assert await store.get_context_turns() == []
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_set_and_get_roundtrip_preserves_order(self, tmp_path):
        store = await self._store_with_meta(tmp_path)
        await store.set_context_turns([7, 3, 11])
        assert await store.get_context_turns() == [7, 3, 11]
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_unparseable_value_reads_empty(self, tmp_path):
        """A corrupt value degrades to "nothing in context", never a crash."""
        store = await self._store_with_meta(tmp_path)
        await store._conn.execute(
            "INSERT INTO diary_meta (key, value) VALUES ('context_turns', ?)",
            ("not json at all",),
        )
        await store._conn.commit()
        assert await store.get_context_turns() == []
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_duplicate_ids_collapse_to_first_position(self, tmp_path):
        store = await self._store_with_meta(tmp_path)
        await store._conn.execute(
            "INSERT INTO diary_meta (key, value) VALUES ('context_turns', ?)",
            (json.dumps([4, 2, 4, 9, 2]),),
        )
        await store._conn.commit()
        assert await store.get_context_turns() == [4, 2, 9]
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_drop_removes_only_the_given_ids(self, tmp_path):
        """A trim drops exactly the evicted turns, keeping the survivors'
        relative positions — a non-contiguous list stays non-contiguous."""
        store = await self._store_with_meta(tmp_path)
        await store.set_context_turns([1, 4, 7, 9])

        remaining = await store.drop_context_turns([4, 9])

        assert remaining == [1, 7]
        assert await store.get_context_turns() == [1, 7]
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_drop_unknown_ids_is_a_noop(self, tmp_path):
        store = await self._store_with_meta(tmp_path)
        await store.set_context_turns([2, 5])
        assert await store.drop_context_turns([99]) == [2, 5]
        assert await store.drop_context_turns([]) == [2, 5]
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_clear_empties_the_list(self, tmp_path):
        store = await self._store_with_meta(tmp_path)
        await store.set_context_turns([1, 2, 3])
        await store.clear_context_turns()
        assert await store.get_context_turns() == []
        await store._conn.close()

    @pytest.mark.asyncio
    async def test_save_appends_and_restore_reads_it_back(self, tmp_path):
        """The end-to-end shape: saving builds the list, and that list is
        what restore replays."""
        store = await self._store_with_meta(tmp_path)
        first = await store.save_turn(user_message="a")
        second = await store.save_turn(user_message="b")

        assert await store.get_context_turns() == [first, second]

        # A trim of the oldest turn leaves only the newer one.
        await store.drop_context_turns([first])
        assert await store.get_context_turns() == [second]
        turns = await store.get_turns_by_ids(await store.get_context_turns())
        assert [t["user_message"] for t in turns] == ["b"]

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
    async def test_count_and_search_share_one_like_clause(self):
        """Regression: the count and the search each built their own LIKE clause
        and drifted twice over.  The count LIKE'd the WHOLE query as a single
        pattern over two columns, while ``_search_like`` ANDed each word over
        four — so ``"子agent 委托"`` matched rows in turn_search and none in
        turn_count, and a hit living only in ``summary``/``tags`` was invisible
        to the count.  Both now come from ``_like_terms``."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(3,))
        cursor.fetchall = AsyncMock(return_value=[])
        mock_conn.execute = AsyncMock(return_value=cursor)
        store._conn = mock_conn

        await store.count_turns(query="子agent 委托", mode="fts5")
        count_sql, count_params = mock_conn.execute.call_args_list[1][0]

        mock_conn.execute.reset_mock()
        await store.search_keyword(query="子agent 委托")
        search_sql, search_params = mock_conn.execute.call_args[0]

        # One predicate per word, one ? per column, ANDed.
        assert count_params == ["%子agent%"] * 4 + ["%委托%"] * 4
        # The search carries the instr snippet anchor first, the limit last.
        assert search_params[0] == "子agent"
        assert search_params[1:-1] == count_params
        for sql in (count_sql, search_sql):
            assert sql.count("LIKE") == 8
            for col in ("user_message", "messages", "summary", "tags"):
                assert col in sql
        # The whole-query pattern that used to make the count miss is gone.
        assert "%子agent 委托%" not in count_params

    @pytest.mark.asyncio
    async def test_count_matches_search_on_a_real_db(self, tmp_path):
        """The tests above assert SQL *shape*; this one asserts BEHAVIOR against
        a real SQLite file — the layer the divergence actually lived in, and the
        one a mock cannot see.

        Both halves of the old bug are staged here: two query words that land in
        DIFFERENT columns (the old whole-query ``LIKE "%子agent 委托%"`` could
        never match them), and a hit that lives only in ``tags`` (the old count
        searched two columns, not four).  Each returned 1 hit from the search
        and 0 from the count."""
        import aiosqlite

        db_path = tmp_path / "memory.db"
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("""\
            CREATE TABLE diary (
                user_message   TEXT NOT NULL DEFAULT '',
                messages       TEXT NOT NULL DEFAULT '[]',
                summary        TEXT DEFAULT '',
                tags           TEXT DEFAULT '',
                created_at     TEXT NOT NULL
            )""")
        await conn.execute(
            "INSERT INTO diary (user_message, messages, created_at) VALUES (?, ?, ?)",
            ("让子agent 去处理这个任务",
             '[{"role": "assistant", "content": "已经委托给子进程了"}]',
             "2026-07-22T10:00:00"),
        )
        await conn.execute(
            "INSERT INTO diary (user_message, messages, tags, created_at)"
            " VALUES (?, ?, ?, ?)",
            ("无关内容", "[]", "重构", "2026-07-22T11:00:00"),
        )
        await conn.commit()

        store = SessionStore(db_path)
        store._conn = conn
        try:
            for query in ("子agent 委托", "重构"):
                hits = await store.search_keyword(query)
                counted = await store.count_turns(query=query, mode="fts5")
                assert len(hits) == 1, query
                assert counted["filtered"] == len(hits), query
        finally:
            await conn.close()

    @pytest.mark.asyncio
    async def test_time_window_windows_both_keyword_paths(self, tmp_path):
        """A bound must narrow the keyword search on BOTH SQL paths — FTS5 for
        an ASCII query, the CJK LIKE fallback for a Chinese one — and the two
        must agree about it.  ``turn_search`` picks the path from the QUERY text
        while the window comes from the bound, so a window that reached only one
        path would make one bound mean two different things.

        Absolute ISO bounds, deliberately not relative words: a relative bound
        would make these expectations move with the wall clock.  The grammar
        itself is pinned in ``tests/test_timeutil.py``."""
        import aiosqlite

        db_path = tmp_path / "memory.db"
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("""\
            CREATE TABLE diary (
                user_message   TEXT NOT NULL DEFAULT '',
                messages       TEXT NOT NULL DEFAULT '[]',
                summary        TEXT DEFAULT '',
                tags           TEXT DEFAULT '',
                channel        TEXT DEFAULT '',
                created_at     TEXT NOT NULL
            )""")
        await conn.execute("""\
            CREATE VIRTUAL TABLE diary_fts USING fts5(
                user_message, messages, summary, tags, channel,
                content='diary', content_rowid='rowid')""")
        await conn.execute(
            "INSERT INTO diary (user_message, messages, created_at) VALUES (?, ?, ?)",
            ("asyncio 并发笔记", '[{"role": "assistant", "content": "关于 asyncio"}]',
             "2026-09-21T10:00:00+08:00"),
        )
        await conn.execute("INSERT INTO diary_fts(diary_fts) VALUES('rebuild')")
        await conn.commit()

        store = SessionStore(db_path)
        store._conn = conn
        try:
            # "asyncio" is ASCII → FTS5 MATCH; "并发" is CJK → the LIKE fallback.
            for query in ("asyncio", "并发"):
                assert len(await store.search_keyword(
                    query, since="2026-09-01")) == 1, query
                # Closed before the row, and open after it — both exclude it.
                assert await store.search_keyword(
                    query, until="2026-09-01") == [], query
                assert await store.search_keyword(
                    query, since="2026-10-01") == [], query
                # A window on both sides keeps it.
                assert len(await store.search_keyword(
                    query, since="2026-09-01", until="2026-09-30")) == 1, query
        finally:
            await conn.close()

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


class TestSessionStoreTokenUsage:
    """Tests for token_usage — per-turn billing / context-size query."""

    @pytest.mark.asyncio
    async def test_aggregates_sums(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 2, "token_count": 300, "context_tokens": 200},
            {"rowid": 1, "token_count": 100, "context_tokens": 80},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.token_usage(limit=50)
        assert result["summary"]["count"] == 2
        assert result["summary"]["total_token_count"] == 400
        assert result["summary"]["total_context_tokens"] == 280
        assert result["summary"]["avg_token_count"] == 200
        assert result["summary"]["avg_context_tokens"] == 140

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
        # Stats-only: the query must never pull message text into the dump.
        assert "user_message" not in sql

    @pytest.mark.asyncio
    async def test_real_db_rows_are_usage_only(self, tmp_path):
        """token_usage rows carry usage figures only — no user_message.

        The tool is a stats report; dragging message text into the LLM-facing
        dump would burn tokens for nothing (the turn_id is enough to read the
        message on demand).
        """
        import json

        import aiosqlite

        db_path = tmp_path / "usage.db"
        conn = await aiosqlite.connect(str(db_path))
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await _create_diary_table(conn)
        await conn.execute(
            "INSERT INTO diary (user_message, messages, created_at, "
            "token_count, context_tokens) "
            "VALUES ('a long user message', '[]', '2026-08-01T10:00:00', 100, 200)"
        )
        await conn.commit()
        await conn.close()

        store = SessionStore(db_path)
        store._conn = await aiosqlite.connect(str(db_path))
        store._conn.row_factory = aiosqlite.Row

        result = await store.token_usage(limit=10)
        assert len(result["turns"]) == 1
        turn = result["turns"][0]
        assert "user_message" not in turn
        assert turn["context_tokens"] == 200
        assert turn["token_count"] == 100
        assert "created_at" in turn

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
        assert result[0]["rowid"] == 1  # store layer keeps the internal rowid

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
            {"turn_id": 1, "user_message": "今天北京天气怎么样？", "snippet": "…", "rank": 0},
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


class TestSessionStoreTurnList:
    """Tests for turn_list — the paged browse behind the ``turn_list`` tool."""

    @staticmethod
    def _store(count: int, rows: list[dict]):
        """A store whose two reads (the count, then the page) are scripted."""
        store = SessionStore(Path("/tmp/test.db"))
        count_cursor = AsyncMock()
        count_cursor.fetchone = AsyncMock(return_value=(count,))
        rows_cursor = AsyncMock()
        rows_cursor.fetchall = AsyncMock(return_value=rows)
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=[count_cursor, rows_cursor])
        store._conn = conn
        return store, conn

    @pytest.mark.asyncio
    async def test_envelope_windows_the_page_and_the_count_alike(self):
        store, conn = self._store(7, [
            {"rowid": 2, "user_message": "newer"},
            {"rowid": 1, "user_message": "older"},
        ])

        out = await store.turn_list(
            since="2026-08-01", until="2026-08-02", limit=2,
        )

        assert out["total"] == 7
        assert [e["rowid"] for e in out["entries"]] == [2, 1], "newest first"
        count_sql, count_params = conn.execute.call_args_list[0].args
        page_sql, page_params = conn.execute.call_args_list[1].args
        # `total` must count the SAME window the page is drawn from, or the
        # caller pages past the end of a set that was never that big.
        assert count_sql.startswith("SELECT COUNT(*) FROM diary WHERE")
        assert "created_at >= ?" in count_sql and "created_at <= ?" in count_sql
        assert count_params == ["2026-08-01", "2026-08-02"]
        # Ordered by the turn id, which is monotonic — a page boundary can
        # never fall inside a group of turns sharing a timestamp.
        assert "ORDER BY rowid DESC" in page_sql
        assert page_params == ["2026-08-01", "2026-08-02", 2, 0]

    @pytest.mark.asyncio
    async def test_no_window_means_no_where(self):
        store, conn = self._store(0, [])

        out = await store.turn_list()

        count_sql, count_params = conn.execute.call_args_list[0].args
        assert "WHERE" not in count_sql
        assert count_params == []
        assert out == {"entries": [], "total": 0}

    @pytest.mark.asyncio
    async def test_offset_pages_and_a_negative_one_is_clamped(self):
        store, conn = self._store(0, [])
        await store.turn_list(limit=5, offset=10)
        assert conn.execute.call_args_list[1].args[1] == [5, 10]

        store, conn = self._store(0, [])
        await store.turn_list(limit=5, offset=-3)
        assert conn.execute.call_args_list[1].args[1] == [5, 0], "no negative OFFSET"

    @pytest.mark.asyncio
    async def test_an_unusable_bound_raises_for_the_caller(self):
        """A bound in no known grammar is the caller's to report — the tool
        layer turns it into an error payload.  The store never guesses."""
        from slife.timeutil import InvalidTimeBound

        store, _ = self._store(0, [])
        with pytest.raises(InvalidTimeBound):
            await store.turn_list(since="下周")


class TestSessionStoreSearchTime:
    """search_time — the recall selector's window, delegated to turn_list.

    The selector and the browse window one axis, so there is one
    implementation of it: a second copy is how the two would come to disagree
    about what a bound means.
    """

    @pytest.mark.asyncio
    async def test_it_returns_turn_list_entries(self):
        store = SessionStore(Path("/tmp/test.db"))
        rows = [{"rowid": 2, "user_message": "Newer"},
                {"rowid": 1, "user_message": "Older"}]
        count_cursor = AsyncMock()
        count_cursor.fetchone = AsyncMock(return_value=(2,))
        rows_cursor = AsyncMock()
        rows_cursor.fetchall = AsyncMock(return_value=rows)
        conn = AsyncMock()
        conn.execute = AsyncMock(side_effect=[count_cursor, rows_cursor])
        store._conn = conn

        result = await store.search_time(since="2024-01-01", until="2024-12-31")

        assert result == rows
        sql, params = conn.execute.call_args_list[1].args
        assert "created_at >= ?" in sql and "created_at <= ?" in sql
        assert params == ["2024-01-01", "2024-12-31", 20, 0], "limit default 20"


class TestSessionStoreSearchGrep:
    """Tests for search_grep."""

    @pytest.mark.asyncio
    async def test_search_grep(self):
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"turn_id": 1, "user_message": "Hello", "context": "Hello world"},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        result = await store.search_grep(pattern="Hello")
        assert len(result) == 1

    @pytest.mark.asyncio
    async def test_search_grep_is_regex_not_like(self):
        """``grep`` is a real grep: the pattern is a regex (``re.search``), so
        alternation and wildcards work — and `%`/`_`, which were LIKE
        metacharacters needing an ESCAPE clause, are simply literals now."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "please summarize this",
             "summary": "", "tags": "", "created_at": "x", "messages": ""},
            {"rowid": 2, "user_message": "unrelated turn",
             "summary": "", "tags": "", "created_at": "x", "messages": ""},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        # `summ.rize` and alternation — the two things LIKE could not do.
        assert [r["rowid"] for r in await store.search_grep("summ.rize")] == [1]
        assert {r["rowid"] for r in await store.search_grep("summarize|unrelated")} == {1, 2}
        # `%` and `_` are ordinary characters in a regex, not wildcards — the
        # LIKE predicate and its ESCAPE clause are gone.
        sql, _ = mock_conn.execute.call_args[0]
        assert "LIKE" not in sql and "ESCAPE" not in sql
        # Recency ordering is the SQL's job (the mock returns a fixed order).
        assert "ORDER BY rowid DESC" in sql

    @pytest.mark.asyncio
    async def test_search_grep_swaps_messages_for_the_match_window(self):
        """``_grep_scan`` needs the heavy ``messages`` column to match on, but
        a result can only afford the text around the hit."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "please summarize this", "summary": "",
             "tags": "", "created_at": "x",
             "messages": '[{"role": "tool", "content": "summarize the file"}]'},
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        (hit,) = await store.search_grep("summ.rize")
        assert "messages" not in hit
        assert "summarize" in hit["context"]

    @pytest.mark.asyncio
    async def test_search_grep_rejects_an_invalid_pattern(self):
        """A bad pattern is the caller's to report — never a silent no-match."""
        store = SessionStore(Path("/tmp/test.db"))
        with pytest.raises(re.error):
            await store.search_grep("a(b")

    @pytest.mark.asyncio
    async def test_search_clamps_negative_limit(self):
        """REVIEW M6 — a negative limit is clamped (it would otherwise slice
        from the tail)."""
        store = SessionStore(Path("/tmp/test.db"))
        mock_conn = AsyncMock()
        mock_cursor = AsyncMock()
        mock_cursor.fetchall = AsyncMock(return_value=[
            {"rowid": i, "user_message": "hit", "summary": "", "tags": "",
             "created_at": "x", "messages": ""} for i in range(30)
        ])
        mock_conn.execute = AsyncMock(return_value=mock_cursor)
        store._conn = mock_conn

        assert len(await store.search_grep("hit", limit=-5)) == 20


async def _vec_store(tmp_path, dim: int = 8) -> SessionStore:
    """A real store with sqlite-vec loaded — or a skip.

    sqlite-vec cannot load on every platform (a macOS Python built without
    ``enable_load_extension`` is the case CI hits): the store degrades to
    keyword-only, no vec0 table exists, and a test of the vec metric has
    nothing to measure.
    """
    store = SessionStore(tmp_path / "t.db")
    await store.setup(embedding_dim=dim)
    if not store._vec_available:
        await store.close()
        pytest.skip("sqlite-vec unavailable on this platform (vec_dim=0)")
    return store


class TestVecStoreMetric:
    """The vec0 tables measure COSINE — the metric the 0–1 ``similarity`` reads.

    A vec0 table's metric is baked into its DDL, and ``1 - distance`` is only
    the cosine when that metric IS cosine.  The tables used to take sqlite-vec's
    L2 default and the search converted with ``1 - d²/2`` — the identity that
    holds for unit-norm vectors, which nothing here establishes: llama.cpp's
    raw output (local-embed's gguf path) is not normalized, so distances ran
    past the [0,2] that identity allows and every strong hit clamped to 0.0.
    """

    @staticmethod
    def _cosine(a, b) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        na = sum(x * x for x in a) ** 0.5
        nb = sum(x * x for x in b) ** 0.5
        return dot / (na * nb)

    @pytest.mark.asyncio
    async def test_the_vec0_tables_declare_the_cosine_metric(self, tmp_path):
        store = await _vec_store(tmp_path)
        try:
            cursor = await store._c.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'diary_semantic'",
            )
            ddl = (await cursor.fetchone())[0]
            assert "distance_metric=cosine" in ddl
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_an_extension_less_interpreter_is_recorded_as_such(self, tmp_path):
        """The macOS case: sqlite3 built without loadable extensions has no
        ``enable_load_extension`` at all.  The store must degrade to
        keyword-only and say WHY — the wheel ships a binary for the OS
        regardless, so "the platform has no sqlite-vec" would be the wrong
        story, and the remedy (a different Python) is not findable from it."""
        from unittest.mock import MagicMock

        store = SessionStore(tmp_path / "t.db")
        store._conn = None
        conn = MagicMock(spec=[])          # no attributes at all, as on macOS
        with patch.object(
            SessionStore, "_c", new_callable=lambda: property(lambda self: conn),
        ):
            store._vec_available = True
            await store._load_vec_extension()

        assert store._vec_available is False
        assert "cannot load extensions" in store._vec_reason
        assert "enable_load_extension" in store._vec_reason

    @pytest.mark.asyncio
    async def test_a_table_on_the_old_metric_is_rebuilt(self, tmp_path):
        """A pre-existing L2 table must not survive: the reading would be
        plausible and wrong rather than visibly broken."""
        db = tmp_path / "t.db"
        store = await _vec_store(tmp_path)
        # Hand-build the pre-change table on the store's own connection (the
        # extension is loaded there; a macOS Python cannot load it at all,
        # which is why _vec_store skipped us out otherwise).
        await store._c.execute("DROP TABLE IF EXISTS diary_semantic")
        await store._c.execute(
            "CREATE VIRTUAL TABLE diary_semantic USING vec0("
            "turn_embedding float[8], +diary_rowid INTEGER)",
        )
        await store._c.commit()
        await store.close()

        store = SessionStore(db)          # the fresh start that must rebuild
        await store.setup(embedding_dim=8)
        try:
            assert store._vec_available, "skip guard should have caught this"
            cursor = await store._c.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'diary_semantic'",
            )
            assert "distance_metric=cosine" in (await cursor.fetchone())[0]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_an_unnormalized_pair_reads_as_its_true_cosine(self, tmp_path):
        """The regression, at the level where the metric lives: non-unit
        vectors — the shape the reported turn hit had (distance 18.3) — now
        report their cosine instead of clamping to 0.0."""
        from slife.plugins.memdb.search import annotate_scores

        store = await _vec_store(tmp_path)
        try:
            a = [0.9, 1.4, -0.7, 2.1, 0.3, 0.3, 0.3, 0.3]
            b = [1.1, 1.2, -0.5, 1.9, 0.4, 0.4, 0.4, 0.4]
            await store._c.execute(
                "INSERT INTO diary_semantic(turn_embedding, diary_rowid, "
                "chunk_index) VALUES (?, 1, 0)",
                (_serialize_f32(a),),
            )
            await store._c.commit()
            cursor = await store._c.execute(
                "SELECT distance FROM diary_semantic "
                "WHERE turn_embedding MATCH ? AND k = 1",
                (_serialize_f32(b),),
            )
            distance = (await cursor.fetchone())[0]

            similarity = annotate_scores([{"distance": distance}])[0]["similarity"]
            assert similarity == round(self._cosine(a, b), 4)
            # …where the old L2 reading of the same pair was a clamped 0.0.
            assert similarity > 0.9
        finally:
            await store.close()


class TestIndexTextContract:
    """Vectors measure the text they were built from, so that text is half of
    what makes them comparable — the same way the model is.  A builder change
    that left the old vectors in place would be silently wrong: nothing in the
    numbers says the text behind them changed.
    """

    @staticmethod
    async def _seed_one_vector(store) -> None:
        await store._c.execute(
            "INSERT INTO diary_semantic(turn_embedding, diary_rowid, "
            "chunk_index) VALUES (?, 1, 0)",
            (_serialize_f32([0.1] * 8),),
        )
        await store._c.commit()

    @staticmethod
    async def _indexed_rows(store) -> int:
        cursor = await store._c.execute("SELECT COUNT(*) FROM diary_semantic")
        return (await cursor.fetchone())[0]

    @pytest.mark.asyncio
    async def test_a_current_text_contract_keeps_the_index(self, tmp_path):
        """The check must not rebuild on every start — that would throw the
        index away and re-embed the whole diary on each launch."""
        db = tmp_path / "t.db"
        store = await _vec_store(tmp_path)
        try:
            await self._seed_one_vector(store)
            assert await self._indexed_rows(store) == 1
        finally:
            await store.close()

        store = SessionStore(db)
        await store.setup(embedding_dim=8)
        try:
            assert store._vec_available, "skip guard should have caught this"
            assert await self._indexed_rows(store) == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_an_unversioned_index_is_rebuilt(self, tmp_path):
        """An index written before the contract was versioned has no key at
        all, and its text predates the current builder — absent is a
        mismatch, not an unknown."""
        db = tmp_path / "t.db"
        store = await _vec_store(tmp_path)
        try:
            await self._seed_one_vector(store)
            await store._c.execute(
                "DELETE FROM diary_meta WHERE key = 'embedding_text_version'",
            )
            await store._c.commit()
        finally:
            await store.close()

        store = SessionStore(db)
        await store.setup(embedding_dim=8)
        try:
            assert store._vec_available, "skip guard should have caught this"
            assert await self._indexed_rows(store) == 0       # dropped
            cursor = await store._c.execute(
                "SELECT value FROM diary_meta "
                "WHERE key = 'embedding_text_version'",
            )
            assert (await cursor.fetchone())[0] == INDEX_TEXT_VERSION
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_an_older_text_contract_is_rebuilt(self, tmp_path):
        db = tmp_path / "t.db"
        store = await _vec_store(tmp_path)
        try:
            await self._seed_one_vector(store)
            await store._c.execute(
                "UPDATE diary_meta SET value = 'older' "
                "WHERE key = 'embedding_text_version'",
            )
            await store._c.commit()
        finally:
            await store.close()

        store = SessionStore(db)
        await store.setup(embedding_dim=8)
        try:
            assert store._vec_available, "skip guard should have caught this"
            assert await self._indexed_rows(store) == 0
        finally:
            await store.close()


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
