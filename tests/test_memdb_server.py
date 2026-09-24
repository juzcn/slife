"""Tests for the memdb plugin server tool wiring.

The semantic lifecycle (gate, embedder, index drainer) lives in
``SemanticManager`` (semantic.py) and is covered by ``test_memdb_semantic.py``.
These tests cover the FastMCP tool layer: how ``__memory_turn_recall`` reads
the gate, how the LLM-visible ``turn_search`` / ``turn_list`` answer, and how
``__memory_save_turn`` wakes the drainer.
"""

import pytest; pytestmark = pytest.mark.unit


import importlib
import logging
import re
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


def _recall(srv):
    """The internal selector, fetched by NAME.

    ``srv.__memory_turn_recall`` written inside a test class would be
    name-mangled to ``srv._ClassName__memory_turn_recall`` and miss — the
    leading double underscore is the plugin's internal-tool marker, not a
    private attribute.
    """
    return getattr(srv, "__memory_turn_recall")


def _fake_manager(*, semantic_ready: bool = False, reason: str = "") -> MagicMock:
    """A stand-in SemanticManager: gate read by the recall selector."""
    m = MagicMock()
    m.semantic_ready = semantic_ready
    m.reason = reason
    m.embedder = MagicMock() if semantic_ready else None
    return m


class TestRecallDegradation:
    """The hybrid→fts5 degradation is logged, even when nothing survives it.

    It used to ride the answer, back when the answer was rows the model also
    read.  The selector now answers ids to the harness alone, and the
    selection stands whether or not the semantic leg ran — so the reason is
    kept as the operator's trace (``recall_degraded``) rather than put in a
    payload nothing would branch on."""

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

    @staticmethod
    def _degradation_logged(srv: MagicMock) -> str:
        """The ``recall_degraded`` line's reason, or "" when none was logged."""
        for call in srv.logger.info.call_args_list:
            if call.args and call.args[0] == "recall_degraded reason=%.120s":
                return str(call.args[1])
        return ""

    @pytest.mark.asyncio
    async def test_empty_recall_still_logs_degradation(self, restore_root_logger):
        import json

        srv, store = self._server(
            keyword_hits=[],
            manager=_fake_manager(semantic_ready=False,
                                  reason="hybrid degraded to fts5 — semantic index is building"),
        )
        srv.logger = MagicMock()

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await _recall(srv)(query="北京天气怎么样")

        assert json.loads(out) == {"turns": []}
        # The degradation reason, not just "no matching memories found".
        assert "semantic index is building" in self._degradation_logged(srv)

    @pytest.mark.asyncio
    async def test_keyword_hits_with_gate_off_also_log(self, restore_root_logger):
        import json

        srv, store = self._server(
            keyword_hits=[
                {"turn_id": 1, "user_message": "微信登录", "snippet": "…", "rank": -1.0},
            ],
            manager=_fake_manager(semantic_ready=False,
                                  reason="hybrid degraded to fts5 — semantic index is building"),
            turns=self._turns(1),
        )
        srv.logger = MagicMock()

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await _recall(srv)(query="微信登录")

        assert json.loads(out) == {"turns": [1]}
        assert "semantic index is building" in self._degradation_logged(srv)

    @pytest.mark.asyncio
    async def test_tokenizer_failure_is_fatal_not_an_empty_answer(
        self, restore_root_logger,
    ):
        """An unusable tokenizer is an environment failure, not an answer.

        Every turn's cost comes from it, so without one there is no budget and
        no selection — but the empty selection this used to return is the one
        shape the caller reads as "clear the context", so a wiped context
        looked exactly like a thin recall.  Fatal instead: the MCP layer
        renders the raise as the error string that makes the caller keep the
        context it has.
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
                await _recall(srv)(query="微信登录")

    @pytest.mark.asyncio
    async def test_gate_on_but_query_embed_fails(self, restore_root_logger):
        """semantic_ready True but the query embed returns None → the
        degradation names the query-embed failure, not the index."""
        import json

        manager = _fake_manager(semantic_ready=True, reason="")
        # embed_one returns None → no semantic hits
        manager.embedder.embed_one = AsyncMock(return_value=None)
        srv, store = self._server(keyword_hits=[], manager=manager)
        srv.logger = MagicMock()

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await _recall(srv)(query="北京天气怎么样")

        assert json.loads(out) == {"turns": []}
        assert "query embedding generation failed" in self._degradation_logged(srv)


class TestRecallAnswerIsIds:
    """The selector answers turn ids and nothing else.

    The store keys search hits on the internal ``rowid`` and ``merge_hybrid``
    aligns on ``turn_id``; the ids that survive gating and the token budget are
    what the answer carries.  Nothing model-facing rides here any more — the
    model reads ``turn_search`` / ``turn_list`` for that — so the answer is
    exactly the list the rebuild reads (regression: hybrid returned
    `keyword=6 semantic=6 merged=0`, an empty selection that cleared the
    context)."""

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
    async def test_store_shaped_hits_merge_into_ids(self, restore_root_logger):
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
            out = await _recall(srv)(query="微信登录")

        assert json.loads(out) == {"turns": [1, 2, 5]}, (
            "merged hits, ordered by turn id"
        )


class TestRecallHeadroom:
    """``reserved_tokens`` **narrows** the token budget — it never sets it.

    The union is `kept + recalled`, so the bound has to hold on the *sum*.
    The floor alone cannot be that bound: the trim compacts *to* the floor, so
    a live context sits at or above it for most of a session and subtracting
    it would grant no headroom at all — making "keep this and add that"
    unreachable exactly when a context is worth keeping.  The headroom below
    the **ceiling** is what is actually left to spend.

    Both existing knobs keep their jobs: the floor stays the selection's own
    size (nothing is reserved ⇒ nothing changes), and the ceiling is the
    total's valve.
    """

    def _server(self, count: int = 5):
        srv = _import_memdb_server()
        rows = [
            {"rowid": r, "created_at": f"2026-08-0{r}", "summary": "",
             "user_message": "x" * 400, "messages": "[]"}
            for r in range(1, count + 1)
        ]
        store = AsyncMock()
        store.search_keyword = AsyncMock(return_value=[
            {**row, "tags": "", "snippet": "…", "rank": -1.0} for row in rows
        ])
        store.search_semantic = AsyncMock(return_value=[])
        store.get_turns_by_ids = AsyncMock(return_value=rows)
        srv._store = store
        # No embeddings: the keyword leg decides, which is enough to test the
        # budget — the cap runs over whatever the legs kept.
        srv._manager = _fake_manager(semantic_ready=False, reason="off")
        return srv, store, rows

    @pytest.mark.asyncio
    async def test_nothing_reserved_is_exactly_todays_budget(
        self, restore_root_logger,
    ):
        """The floor still sizes the selection when a decision keeps nothing
        (``"clear"`` + recall) — the overriding behaviour, unchanged."""
        import json

        from slife.agent.message_history import estimate_turn_tokens

        srv, store, rows = self._server()
        cost = estimate_turn_tokens(rows[0])
        # Room for three turns, under a ceiling with plenty of room left.
        srv._recall_policy = lambda: RecallPolicy(
            token_budget=cost * 3, ceiling_tokens=cost * 100,
        )

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = json.loads(await _recall(srv)(query="x", reserved_tokens=0))

        assert out["turns"] == [1, 2, 3], (
            "the floor caps it, and the ceiling is not the binding constraint"
        )

    @pytest.mark.asyncio
    async def test_a_reservation_narrows_the_budget(self, restore_root_logger):
        import json

        from slife.agent.message_history import estimate_turn_tokens

        srv, store, rows = self._server()
        cost = estimate_turn_tokens(rows[0])
        # Floor: three turns.  Ceiling: five.  A decision keeping four turns
        # worth of context leaves room for one more.
        srv._recall_policy = lambda: RecallPolicy(
            token_budget=cost * 3, ceiling_tokens=cost * 5,
        )

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            narrowed = json.loads(
                await _recall(srv)(query="x", reserved_tokens=cost * 4)
            )

        assert narrowed["turns"] == [1], (
            "the kept context is already four turns deep, and the ceiling is "
            "five — so one turn is all that fits, not the floor's three"
        )

    @pytest.mark.asyncio
    async def test_the_ceiling_is_what_the_reservation_is_measured_from(
        self, restore_root_logger,
    ):
        """The bug this replaced: measured against the *floor*, a context that
        had reached it (the state a trim leaves behind, and so most of a
        session) could recall nothing at all."""
        import json

        from slife.agent.message_history import estimate_turn_tokens

        srv, store, rows = self._server()
        cost = estimate_turn_tokens(rows[0])
        srv._recall_policy = lambda: RecallPolicy(
            token_budget=cost * 3, ceiling_tokens=cost * 12,
        )

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            at_floor = json.loads(
                await _recall(srv)(query="x", reserved_tokens=cost * 3)
            )

        assert at_floor["turns"] == [1, 2, 3], (
            "a base sitting exactly at the floor still has the floor's worth "
            "of headroom below the ceiling to add into"
        )

    @pytest.mark.asyncio
    async def test_a_base_at_the_ceiling_recalls_nothing(
        self, restore_root_logger,
    ):
        """Headroom is never negative: at the ceiling the answer is empty
        rather than a budget that inverts into a bigger one."""
        import json

        from slife.agent.message_history import estimate_turn_tokens

        srv, store, rows = self._server()
        cost = estimate_turn_tokens(rows[0])
        srv._recall_policy = lambda: RecallPolicy(
            token_budget=cost * 3, ceiling_tokens=cost * 5,
        )

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = json.loads(
                await _recall(srv)(query="x", reserved_tokens=cost * 5)
            )

        assert out == {"turns": []}

    @pytest.mark.asyncio
    async def test_an_unset_ceiling_leaves_the_budget_alone(
        self, restore_root_logger,
    ):
        """``ceiling_tokens = 0`` is "unset", not "no room" — a policy built by
        hand (the default in tests) keeps its old meaning."""
        import json

        from slife.agent.message_history import estimate_turn_tokens

        srv, store, rows = self._server()
        cost = estimate_turn_tokens(rows[0])
        srv._recall_policy = lambda: RecallPolicy(token_budget=cost * 3)

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = json.loads(
                await _recall(srv)(query="x", reserved_tokens=cost * 99)
            )

        assert out["turns"] == [1, 2, 3]


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
    """``__memory_turn_recall`` has no error return — only a store failure is
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
        recall = _recall(srv)

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await recall(query="", since="下周")

        assert json.loads(out) == {"turns": []}

    @pytest.mark.asyncio
    async def test_an_unexpected_failure_is_also_an_empty_selection(
        self, restore_root_logger,
    ):
        import json

        srv, store = self._server()
        store.get_turns_by_ids = AsyncMock(side_effect=RuntimeError("pipeline bug"))
        store.search_time = AsyncMock(return_value=[{"rowid": 1}])
        recall = _recall(srv)

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await recall(query="", since="today")

        assert json.loads(out) == {"turns": []}

    @pytest.mark.asyncio
    async def test_a_store_failure_propagates(self, restore_root_logger):
        import sqlite3

        srv, store = self._server()
        store.search_time = AsyncMock(
            side_effect=sqlite3.DatabaseError("database disk image is malformed")
        )
        recall = _recall(srv)

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            with pytest.raises(sqlite3.DatabaseError):
                await recall(query="", since="today")


class TestTurnRecallIsInternal:
    """The selector is an **internal** tool.

    The ``__`` prefix is the whole hiding mechanism (``is_internal_tool``), so
    it never reaches the LLM's registry — the model's own reading of the Turns
    DB is ``turn_search`` / ``turn_list``, and neither of those touches the
    context.  Three of its parameters are the ones the discriminator fills in
    — the caps are recall's configuration, and the surface is stated once, in
    ``system_prompt.RECALL_REPLY`` — and ``reserved_tokens`` is the harness's
    own: the tokens the kept turns already spend, which *narrows* the token
    budget rather than setting it.
    """

    @pytest.mark.asyncio
    async def test_it_is_registered_under_the_internal_prefix(
        self, restore_root_logger,
    ):
        srv = _import_memdb_server()

        tools = await srv.mcp.list_tools()
        names = {t.name for t in tools}
        assert "__memory_turn_recall" in names
        assert "turn_recall" not in names, "the selector is not an LLM tool"

    @pytest.mark.asyncio
    async def test_schema_parameters_are_the_three_and_the_headroom(
        self, restore_root_logger,
    ):
        """The discriminator fills exactly three of these in — no caps, no mode
        knob.  The fourth is the *caller's*: the headroom below the context
        floor, which the harness derives from the config and the turns the
        decision kept, and which a model can neither see nor set."""
        srv = _import_memdb_server()

        tools = await srv.mcp.list_tools()
        tool = next(t for t in tools if t.name == "__memory_turn_recall")

        props = tool.parameters["properties"]
        assert set(props) == {"query", "since", "until", "reserved_tokens"}
        assert tool.parameters.get("required", []) == [], "all four are optional"
        # The per-parameter how-to-use text rides the schema (FastMCP lifts it
        # from the docstring's Args block), and the discriminator is asked for
        # the same three keys the loop whitelists.
        for name in ("query", "since", "until", "reserved_tokens"):
            assert props[name].get("description"), f"{name} has no description"

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
        recall = _recall(srv)

        with patch.object(srv, "_ensure_store_locked", AsyncMock(return_value=store)):
            out = await recall()

        assert json.loads(out) == {"turns": []}
        store.search_time.assert_not_awaited()
        store.get_turns_by_ids.assert_not_awaited()


class TestTurnSearch:
    """``turn_search`` is the model's own reading of the Turns DB — it answers
    with hits and never touches the context.

    Its shape mirrors memfiles' ``cabinet_search``: a
    mode/query/results/hint envelope whose ``mode`` names what actually ran,
    and an empty query refused rather than quietly browsed.
    """

    @staticmethod
    def _server(store):
        srv = _import_memdb_server()
        srv._store = store
        srv._manager = None  # no semantic leg
        return srv

    @pytest.mark.asyncio
    async def test_an_empty_query_is_refused_not_browsed(self, restore_root_logger):
        """grep compiles the empty pattern, which matches EVERY string — so an
        empty query would silently become a second browse path.  ``turn_list``
        is the browse, which is why this one says so."""
        import json

        store = AsyncMock()
        srv = self._server(store)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_search(query="")

        assert "turn_list" in json.loads(out)["error"]
        store.search_keyword.assert_not_awaited()
        store.search_grep.assert_not_awaited()
        store.search_time.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fts5_renames_rowid_to_turn_id(self, restore_root_logger):
        import json

        store = AsyncMock()
        store.search_keyword = AsyncMock(return_value=[
            {"rowid": 3, "user_message": "微信登录", "snippet": "…", "rank": -1.0},
        ])
        srv = self._server(store)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_search(query="微信登录", mode="fts5")

        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert [r["turn_id"] for r in data["results"]] == [3]
        assert "rowid" not in data["results"][0], "internal key never exposed"

    @pytest.mark.asyncio
    async def test_grep_reports_an_unusable_pattern(self, restore_root_logger):
        import json

        store = AsyncMock()
        store.search_grep = AsyncMock(
            side_effect=re.error("missing ), unterminated subpattern")
        )
        srv = self._server(store)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_search(query="a(b", mode="grep")

        assert "invalid regex" in json.loads(out)["error"]

    @pytest.mark.asyncio
    async def test_a_rejected_time_bound_is_an_error(self, restore_root_logger):
        """A search is the model's own reading, not the context — so an
        unusable bound is the caller's to fix, exactly as in ``cabinet_search``.
        (The *selector* collapses the same failure into an empty selection,
        because there an error would force the caller to keep a context it was
        told to replace.)"""
        import json

        from slife.timeutil import InvalidTimeBound

        store = AsyncMock()
        store.search_keyword = AsyncMock(
            side_effect=InvalidTimeBound("invalid since bound '下周'")
        )
        srv = self._server(store)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_search(query="天气", since="下周")

        assert "invalid since bound" in json.loads(out)["error"]

    @pytest.mark.asyncio
    async def test_hybrid_reports_the_mode_that_ran(self, restore_root_logger):
        """A degraded hybrid is an fts5 result — answering "hybrid" would
        misreport what was searched."""
        import json

        store = AsyncMock()
        store.search_keyword = AsyncMock(return_value=[
            {"rowid": 1, "user_message": "微信登录", "summary": "", "tags": "",
             "created_at": "2026-08-01", "snippet": "…", "rank": -1.0},
        ])
        srv = self._server(store)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_search(query="微信登录")

        data = json.loads(out)
        assert data["mode"] == "fts5", "no manager → the semantic leg cannot run"
        assert [r["turn_id"] for r in data["results"]] == [1]
        assert "degraded to fts5" in data["hint"]

    @pytest.mark.asyncio
    async def test_a_failed_search_is_an_error_not_a_silent_empty(
        self, restore_root_logger,
    ):
        import json

        store = AsyncMock()
        store.search_keyword = AsyncMock(side_effect=RuntimeError("pipeline bug"))
        srv = self._server(store)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_search(query="微信登录", mode="fts5")

        assert "pipeline bug" in json.loads(out)["error"]


class TestTurnList:
    """``turn_list`` is the browse to ``turn_search``'s search.

    It carries the memfiles ``*_list`` envelope — ``total``/``limit``/
    ``offset``/``entries`` — so a caller can tell whether more remain and page
    for them, and the store's ``rowid`` is exposed as ``turn_id``.
    """

    @staticmethod
    def _server(store):
        srv = _import_memdb_server()
        srv._store = store
        return srv

    @pytest.mark.asyncio
    async def test_envelope_paging_and_truncation(self, restore_root_logger):
        import json

        store = AsyncMock()
        store.turn_list = AsyncMock(return_value={
            "total": 7,
            "entries": [
                {"rowid": 2, "user_message": "x" * 250, "summary": "s",
                 "tags": "t", "created_at": "2026-08-02", "token_count": 5},
                {"rowid": 1, "user_message": "short", "summary": "",
                 "tags": "", "created_at": "2026-08-01", "token_count": 3},
            ],
        })
        srv = self._server(store)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_list(limit=2, offset=1)

        store.turn_list.assert_awaited_once_with(
            since=None, until=None, limit=2, offset=1,
        )
        data = json.loads(out)
        assert (data["total"], data["limit"], data["offset"]) == (7, 2, 1)
        assert [e["turn_id"] for e in data["entries"]] == [2, 1]
        assert "rowid" not in data["entries"][0]
        # The ellipsis is how a cut message reads as cut — without it a model
        # has no reason to call turn_read.
        assert data["entries"][0]["user_message"] == "x" * 200 + "…"
        assert data["entries"][1]["user_message"] == "short"

    @pytest.mark.asyncio
    async def test_a_rejected_time_bound_is_an_error(self, restore_root_logger):
        import json

        from slife.timeutil import InvalidTimeBound

        store = AsyncMock()
        store.turn_list = AsyncMock(
            side_effect=InvalidTimeBound("invalid until bound '下周'")
        )
        srv = self._server(store)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.turn_list(until="下周")

        assert "invalid until bound" in json.loads(out)["error"]
