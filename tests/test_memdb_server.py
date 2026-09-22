"""Tests for the memdb plugin server tool wiring.

The semantic lifecycle (gate, embedder, index drainer) lives in
``SemanticManager`` (semantic.py) and is covered by ``test_memdb_semantic.py``.
These tests cover the FastMCP tool layer: how ``turn_recall`` reads the gate,
and how ``__memory_save_turn`` wakes the drainer.
"""

import pytest; pytestmark = pytest.mark.unit


import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from slife.agent.message_history import TokenizerUnavailable
from slife.plugins.memdb.recall import RecallPolicy

import pytest


@pytest.fixture
def restore_root_logger():
    """Importing the server reconfigures logging — restore it afterwards."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)


def _import_memdb_server():
    """Import the memdb server fresh, stubbing the logging side-effect."""
    sys.modules.pop("slife.plugins.memdb.server", None)
    with patch(
        "slife.server_utils.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        return importlib.import_module("slife.plugins.memdb.server")


def _fake_manager(*, semantic_ready: bool = False, reason: str = "") -> MagicMock:
    """A stand-in SemanticManager: gate read by turn_recall."""
    m = MagicMock()
    m.semantic_ready = semantic_ready
    m.reason = reason
    m.embedder = MagicMock() if semantic_ready else None
    return m


class TestRecallDegradation:
    """The hybrid→fts5 degradation is reported on the recall answer itself —
    even when nothing survives it.  An empty selection is exactly when a silent
    fallback would mislead (REVIEW: silent degradation), and it is also the
    answer that *clears* the context, so "nothing matched" and "the semantic
    leg is down" must never read the same."""

    def _server(self, keyword_hits, manager, turns=None, semantic_hits=None):
        srv = _import_memdb_server()
        store = AsyncMock()
        store.search_keyword = AsyncMock(return_value=keyword_hits)
        store.search_semantic = AsyncMock(return_value=semantic_hits or [])
        store.get_turns_by_ids = AsyncMock(return_value=turns or [])
        srv._store = store
        srv._manager = manager
        srv._recall_policy = lambda: RecallPolicy()  # skip the config read
        return srv, store

    @staticmethod
    def _turns(*rowids):
        """The stored rows for a selection: the tool reads their fields for the
        answer and their size for the token budget."""
        return [
            {"rowid": r, "created_at": "2026-08-01T10:00:00+08:00",
             "user_message": f"turn {r}", "summary": "", "messages": "[]"}
            for r in rowids
        ]

    @pytest.mark.asyncio
    async def test_empty_recall_still_reports_degradation(self, restore_root_logger):
        import json

        srv, store = self._server(
            keyword_hits=[],
            manager=_fake_manager(semantic_ready=False,
                                  reason="hybrid degraded to fts5 — semantic index is building"),
        )

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await srv.turn_recall(query="北京天气怎么样")

        data = json.loads(out)
        assert data["turns"] == []
        # The degradation reason, not just "no matching memories found".
        assert "semantic index is building" in data["degraded"]

    @pytest.mark.asyncio
    async def test_keyword_hits_with_gate_off_also_report(self, restore_root_logger):
        import json

        srv, store = self._server(
            keyword_hits=[
                {"turn_id": 1, "user_message": "微信登录", "snippet": "…", "rank": -1.0},
            ],
            manager=_fake_manager(semantic_ready=False,
                                  reason="hybrid degraded to fts5 — semantic index is building"),
            turns=self._turns(1),
        )

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await srv.turn_recall(query="微信登录")

        data = json.loads(out)
        assert [t["turn_id"] for t in data["turns"]] == [1]
        assert "semantic index is building" in data["degraded"]

    @pytest.mark.asyncio
    async def test_tokenizer_failure_is_fatal_not_an_empty_answer(
        self, restore_root_logger,
    ):
        """An unusable tokenizer is an environment failure, not an answer.

        Every row's cost comes from it, so without one there is no budget and
        no selection — but the empty selection this used to return is the one
        shape the caller reads as "clear the context", and `degraded` was
        empty with it, so a wiped context looked exactly like a thin recall.
        Fatal instead: the MCP layer renders the raise as the error string
        that makes the caller keep the context it has.
        """
        srv, store = self._server(
            keyword_hits=[
                {"turn_id": 1, "user_message": "微信登录", "snippet": "…", "rank": -1.0},
            ],
            manager=_fake_manager(semantic_ready=False, reason=""),
            turns=self._turns(1),
        )

        with patch.object(
            srv, "_ensure_store_locked", AsyncMock(return_value=store),
        ), patch.object(
            srv, "estimate_turn_tokens",
            side_effect=TokenizerUnavailable("no vocabulary"),
        ):
            with pytest.raises(TokenizerUnavailable):
                await srv.turn_recall(query="微信登录")

    @pytest.mark.asyncio
    async def test_gate_on_but_query_embed_fails(self, restore_root_logger):
        """semantic_ready True but the query embed returns None → the
        degradation names the query-embed failure, not the index."""
        import json

        manager = _fake_manager(semantic_ready=True, reason="")
        # embed_one returns None → no semantic hits
        manager.embedder.embed_one = AsyncMock(return_value=None)
        srv, store = self._server(keyword_hits=[], manager=manager)

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await srv.turn_recall(query="北京天气怎么样")

        data = json.loads(out)
        assert "query embedding generation failed" in data["degraded"]


class TestRecallRowsCarryTurnId:
    """The store keys search hits on the internal ``rowid``, while
    ``merge_hybrid`` aligns on ``turn_id`` — and every row the answer carries
    must expose ``turn_id``, because the rebuild reads its ids straight out of
    these rows.  The real hit shape is rowid-keyed (regression: hybrid returned
    `keyword=6 semantic=6 merged=0`)."""

    def _server(self, keyword_hits, semantic_hits):
        srv = _import_memdb_server()
        store = AsyncMock()
        store.search_keyword = AsyncMock(return_value=keyword_hits)
        store.search_semantic = AsyncMock(return_value=semantic_hits)
        store.get_turns_by_ids = AsyncMock(return_value=[
            {"rowid": r, "created_at": "2026-08-01T10:00:00+08:00",
             "user_message": f"turn {r}", "summary": "", "messages": "[]"}
            for r in (1, 2, 5)
        ])
        manager = _fake_manager(semantic_ready=True, reason="")
        manager.embedder.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3])
        srv._store = store
        srv._manager = manager
        srv._recall_policy = lambda: RecallPolicy()  # skip the config read
        return srv, store

    @pytest.mark.asyncio
    async def test_store_shaped_hits_merge_and_carry_turn_id(self, restore_root_logger):
        import json

        srv, store = self._server(
            keyword_hits=[
                {"rowid": 1, "user_message": "微信登录", "summary": "s1",
                 "tags": "", "created_at": "2026-08-01", "snippet": "…", "rank": -1.0},
                {"rowid": 2, "user_message": "other", "summary": "s2",
                 "tags": "", "created_at": "2026-08-02", "snippet": "…", "rank": -0.5},
            ],
            semantic_hits=[
                {"rowid": 5, "diary_rowid": 5, "summary": "s5", "tags": "",
                 "created_at": "2026-08-01", "distance": 0.5},
            ],
        )

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await srv.turn_recall(query="微信登录")

        data = json.loads(out)
        assert [t["turn_id"] for t in data["turns"]] == [1, 2, 5], (
            "merged hits, ordered by turn id"
        )
        for row in data["turns"]:
            assert "rowid" not in row          # internal key never exposed
            assert "diary_rowid" not in row    # semantic dedup key not exposed
            assert "messages" not in row       # turn_read is for those
        # Only the semantic hit carried a measured similarity.
        assert data["turns"][2]["score"] == 0.5
        assert "score" not in data["turns"][0]


class TestStoreLifecycleLocking:
    """The per-turn save holds the lifecycle lock, and wakes the drainer."""

    @pytest.mark.asyncio
    async def test_save_turn_holds_lifecycle_lock(self, restore_root_logger):
        """__memory_save_turn holds the lock during save_turn."""
        import json

        srv = _import_memdb_server()
        lock_held = []

        async def _fake_save_turn(**kwargs):
            lock_held.append(srv._get_init_lock().locked())
            return 1

        store = AsyncMock()
        store.save_turn = _fake_save_turn
        srv._ensure_store_locked = AsyncMock(return_value=store)
        srv._manager = _fake_manager()

        out = await getattr(srv, "__memory_save_turn")(user_message="hi")

        assert lock_held == [True]
        assert json.loads(out)["turn_id"] == 1

    @pytest.mark.asyncio
    async def test_save_turn_forwarded_and_wakes_drainer(self, restore_root_logger):
        """__memory_save_turn forwards the turn args and calls on_saved()
        (the drainer wake) — no reindex side-effect on the save path, and no
        separate images channel (image blocks ride the history; the
        ``images`` column is gone)."""
        import json

        srv = _import_memdb_server()
        captured: dict = {}

        async def _fake_save_turn(**kwargs):
            captured.update(kwargs)
            return 7

        store = AsyncMock()
        store.save_turn = _fake_save_turn
        srv._ensure_store_locked = AsyncMock(return_value=store)
        manager = _fake_manager()
        srv._manager = manager

        out = await getattr(srv, "__memory_save_turn")(
            user_message="hi",
        )

        assert json.loads(out)["turn_id"] == 7
        assert captured["user_message"] == "hi"
        assert "images" not in captured
        # save_turn is insert-only (embedding is internal/reindex); the
        # harness's 10s save timeout can no longer be tripped by a slow embed.
        assert "embedder" not in captured
        manager.on_saved.assert_called_once()


class TestTurnSummarize:
    """turn_summarize — explicit turn_id annotates a past turn;
    turn_id=None captures the current (in-flight) turn for save time."""

    def _server(self):
        srv = _import_memdb_server()
        store = AsyncMock()
        store.update_summary = AsyncMock()
        srv._store = store
        return srv, store

    @pytest.mark.asyncio
    async def test_rowid_none_captures_current_turn(self, restore_root_logger):
        import json

        srv, store = self._server()
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_summarize(summary="sum", tags="a,b")

        data = json.loads(out)
        assert data["status"] == "captured"
        assert data["turn_id"] is None
        # No write and no latest_rowid lookup — applied at save time instead.
        store.update_summary.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_rowid_updates_immediately(self, restore_root_logger):
        import json

        srv, store = self._server()
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_summarize(turn_id=3, summary="sum")

        data = json.loads(out)
        assert data["status"] == "updated"
        assert data["turn_id"] == 3
        store.update_summary.assert_awaited_once_with(
            rowid=3, summary="sum", tags=None,
        )

    @pytest.mark.asyncio
    async def test_save_applies_captured_annotation(self, restore_root_logger):
        """__memory_save_turn applies summary/tags to the row it just wrote."""
        import json

        srv, store = self._server()
        store.save_turn = AsyncMock(return_value=9)
        with patch.object(
            srv, "_ensure_store_locked", AsyncMock(return_value=store),
        ):
            out = await getattr(srv, "__memory_save_turn")(
                user_message="hi", summary="sum", tags="a,b",
            )

        assert json.loads(out)["turn_id"] == 9
        store.update_summary.assert_awaited_once_with(
            rowid=9, summary="sum", tags="a,b",
        )

    @pytest.mark.asyncio
    async def test_save_without_captured_annotation_writes_nothing_extra(
        self, restore_root_logger,
    ):
        import json

        srv, store = self._server()
        store.save_turn = AsyncMock(return_value=9)
        with patch.object(
            srv, "_ensure_store_locked", AsyncMock(return_value=store),
        ):
            out = await getattr(srv, "__memory_save_turn")(user_message="hi")

        assert json.loads(out)["turn_id"] == 9
        store.update_summary.assert_not_awaited()


class TestTokenUsage:
    """turn_token_usage — per-turn token consumption."""

    @pytest.mark.asyncio
    async def test_passes_filters_to_store(self, restore_root_logger):
        import json

        srv = _import_memdb_server()
        store = AsyncMock()
        store.token_usage = AsyncMock(return_value={
            "turns": [{"rowid": 1, "token_count": 100}],
            "summary": {"count": 1},
            "filters": {},
        })
        srv._store = store
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_token_usage(
                turn_id=3, since="2026-01-01", until="2026-02-01", limit=10,
            )

        store.token_usage.assert_awaited_once_with(
            rowid=3, since="2026-01-01", until="2026-02-01", limit=10,
        )
        assert json.loads(out)["summary"]["count"] == 1


class _QueryCursor:
    """An aiosqlite-like cursor: async context manager with async fetchone."""

    async def __aenter__(self) -> "_QueryCursor":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def fetchone(self) -> tuple:
        return (1,)


class TestMemdbLifespan:
    """Readiness (MCP plugin contract): the lifespan gates ``initialize``.

    The store is the plugin's serving requirement, now encoded in
    initialization — an unusable store raises in the lifespan (the port
    signal never fires, so the harness reports FAILED) instead of answering
    a ``__ready`` tool with ``ready: false``.
    """

    @staticmethod
    def _ok_store():
        """A store whose SELECT 1 succeeds — the store can serve."""
        store = AsyncMock()
        # aiosqlite's ``Connection.execute`` returns a cursor that supports
        # ``async with`` — a MagicMock (not AsyncMock) models that.
        store._c.execute = MagicMock(return_value=_QueryCursor())
        return store

    @pytest.mark.asyncio
    async def test_store_failure_raises(self, restore_root_logger):
        """Store cannot be established → the lifespan raises on enter."""
        srv = _import_memdb_server()
        with patch.object(
            srv, "_ensure_store",
            AsyncMock(side_effect=RuntimeError("db locked")),
        ):
            with pytest.raises(RuntimeError, match="db locked"):
                async with srv._memdb_lifespan(None):
                    pass

    @pytest.mark.asyncio
    async def test_store_query_failure_raises(self, restore_root_logger):
        """Connection open but the verification query fails → lifespan raises."""
        srv = _import_memdb_server()
        store = AsyncMock()
        store._c.execute = MagicMock(side_effect=RuntimeError("query boom"))
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            with pytest.raises(RuntimeError, match="query boom"):
                async with srv._memdb_lifespan(None):
                    pass

    @pytest.mark.asyncio
    async def test_store_ok_yields_then_closes(self, restore_root_logger):
        """Store can serve → lifespan yields, and teardown closes it."""
        srv = _import_memdb_server()
        store = self._ok_store()
        srv._store = store
        srv._manager = None
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            entered = False
            async with srv._memdb_lifespan(None):
                entered = True
        assert entered
        store.close.assert_awaited_once()


class TestTurnRecallContract:
    """``turn_recall`` has no error return — only a store failure is
    fatal.

    The recall answer **is** the caller's context, so an empty selection is a
    real answer that overrides it, while an "error" would force the caller to
    keep what it has.  Collapsing a rejected time bound into an empty list is
    therefore the correct degradation; collapsing a *store* failure into one
    would wipe the context from a database that cannot be trusted.
    """

    @staticmethod
    def _server():
        from slife.plugins.memdb.recall import RecallPolicy

        srv = _import_memdb_server()
        store = AsyncMock()
        srv._store = store
        srv._manager = None
        # Skip the config read (the real one is config-driven; the caps are
        # not what these tests are about).
        srv._recall_policy = lambda: RecallPolicy()
        return srv, store

    @pytest.mark.asyncio
    async def test_a_rejected_time_bound_is_an_empty_selection(
        self, restore_root_logger,
    ):
        import json

        from slife.timeutil import InvalidTimeBound

        srv, store = self._server()
        store.search_time = AsyncMock(
            side_effect=InvalidTimeBound("invalid since bound '下周'")
        )
        recall = getattr(srv, "turn_recall")

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await recall(query="", since="下周")

        assert json.loads(out) == {"turns": [], "degraded": ""}

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_is_also_an_empty_selection(
        self, restore_root_logger,
    ):
        import json

        srv, store = self._server()
        store.get_turns_by_ids = AsyncMock(side_effect=RuntimeError("pipeline bug"))
        store.search_time = AsyncMock(return_value=[{"rowid": 1}])
        recall = getattr(srv, "turn_recall")

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await recall(query="", since="today")

        assert json.loads(out) == {"turns": [], "degraded": ""}

    @pytest.mark.asyncio
    async def test_a_store_failure_propagates(self, restore_root_logger):
        import sqlite3

        srv, store = self._server()
        store.search_time = AsyncMock(
            side_effect=sqlite3.DatabaseError("database disk image is malformed")
        )
        recall = getattr(srv, "turn_recall")

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            with pytest.raises(sqlite3.DatabaseError):
                await recall(query="", since="today")


class TestTurnRecallSchema:
    """``turn_recall``'s own description states every retrieval mode.

    It is the **only** statement of the parameter surface: the harness
    discriminator's instruction renders this schema, so a mode the description
    does not name is unreachable however good the model is — and the tool's
    callers read the same words.
    """

    @pytest.mark.asyncio
    async def test_description_names_every_mode(self, restore_root_logger):
        srv = _import_memdb_server()

        tools = await srv.mcp.list_tools()
        tool = next(t for t in tools if t.name == "turn_recall")
        desc = tool.description

        assert "A query searches the whole history" in desc, "query, no range"
        assert "a time range narrows it" in desc, "query + range"
        assert "a time range without a query browses that period" in desc, (
            "no query + range — time-only retrieval"
        )
        assert "With neither there is nothing to recall" in desc, (
            "no parameters — an empty call recalls nothing"
        )
        assert "ascending turn-id order" in desc, "the order contract"

    @pytest.mark.asyncio
    async def test_an_empty_call_recalls_nothing(self, restore_root_logger):
        """No query and no range is **not** "the most recent turns" — there is
        no default selection at all, and the store is not even queried.  A
        default that hands back N turns would replace a context the caller
        never asked to change."""
        import json

        srv = _import_memdb_server()
        store = AsyncMock()
        srv._store = store
        srv._manager = None
        srv._recall_policy = lambda: RecallPolicy()
        recall = getattr(srv, "turn_recall")

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await recall()

        assert json.loads(out) == {"turns": [], "degraded": ""}
        store.search_time.assert_not_awaited()
        store.get_turns_by_ids.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_schema_parameters_are_the_three(self, restore_root_logger):
        """The discriminator fills exactly these in — no caps, no mode knob."""
        srv = _import_memdb_server()

        tools = await srv.mcp.list_tools()
        tool = next(t for t in tools if t.name == "turn_recall")

        props = tool.parameters["properties"]
        assert set(props) == {"query", "since", "until"}
        assert tool.parameters.get("required", []) == [], "all three are optional"
        # The per-parameter how-to-use text rides the schema (FastMCP lifts it
        # from the docstring's Args block) — and the discriminator's instruction
        # IS this schema, so a missing description is a missing instruction.
        for name in ("query", "since", "until"):
            assert props[name].get("description"), f"{name} has no description"
