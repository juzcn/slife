"""Tests for the memdb plugin server tool wiring.

The semantic lifecycle (gate, embedder, index drainer) lives in
``SemanticManager`` (semantic.py) and is covered by ``test_memdb_semantic.py``.
These tests cover the FastMCP tool layer: how ``memory_search`` reads the gate,
and how ``__memory_save_turn`` wakes the drainer.
"""

import pytest; pytestmark = pytest.mark.unit


import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
    """A stand-in SemanticManager: gate read by memory_search."""
    m = MagicMock()
    m.semantic_ready = semantic_ready
    m.reason = reason
    m.embedder = MagicMock() if semantic_ready else None
    return m


class TestHybridDegradationHint:
    """The hybrid→fts5 degradation must be surfaced even when no keyword
    hits survive — an empty result is exactly when a silent fallback
    would mislead (REVIEW: silent degradation)."""

    def _server(self, keyword_hits: list[dict], manager: MagicMock):
        srv = _import_memdb_server()
        store = AsyncMock()
        store.search_keyword = AsyncMock(return_value=keyword_hits)
        store.search_semantic = AsyncMock(return_value=[])
        srv._store = store
        srv._manager = manager
        return srv, store

    @pytest.mark.asyncio
    async def test_empty_result_still_reports_degradation(self, restore_root_logger):
        import json

        srv, store = self._server(
            keyword_hits=[],
            manager=_fake_manager(semantic_ready=False,
                                  reason="hybrid degraded to fts5 — semantic index is building"),
        )

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.memory_search(query="北京天气怎么样", mode="hybrid")

        data = json.loads(out)
        assert data["mode"] == "fts5"
        # The degradation reason, not just "no matching memories found".
        assert "degraded" in data["hint"]

    @pytest.mark.asyncio
    async def test_keyword_hits_with_gate_off_also_report(self, restore_root_logger):
        import json

        srv, store = self._server(
            keyword_hits=[
                {"rowid": 1, "user_message": "微信登录", "snippet": "…", "rank": -1.0},
            ],
            manager=_fake_manager(semantic_ready=False,
                                  reason="hybrid degraded to fts5 — semantic index is building"),
        )

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.memory_search(query="微信登录", mode="hybrid")

        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert data["results"]
        assert "degraded" in data["hint"]

    @pytest.mark.asyncio
    async def test_gate_on_but_query_embed_fails(self, restore_root_logger):
        """semantic_ready True but the query embed returns None → the
        fallback names the query-embed failure, not the index."""
        import json

        manager = _fake_manager(semantic_ready=True, reason="")
        # embed_one returns None → no semantic hits
        manager.embedder.embed_one = AsyncMock(return_value=None)
        srv, store = self._server(keyword_hits=[], manager=manager)

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.memory_search(query="北京天气怎么样", mode="hybrid")

        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert "query embedding generation failed" in data["hint"]


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
        assert json.loads(out)["rowid"] == 1

    @pytest.mark.asyncio
    async def test_save_turn_forwarded_and_wakes_drainer(self, restore_root_logger):
        """__memory_save_turn forwards the turn args and calls on_saved()
        (the drainer wake) — no reindex side-effect on the save path, and no
        separate images channel (image blocks ride the conversation; the
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

        assert json.loads(out)["rowid"] == 7
        assert captured["user_message"] == "hi"
        assert "images" not in captured
        # save_turn is insert-only (embedding is internal/reindex); the
        # harness's 10s save timeout can no longer be tripped by a slow embed.
        assert "embedder" not in captured
        manager.on_saved.assert_called_once()


class TestTurnSummarize:
    """memory_turn_summarize — explicit rowid annotates a past turn;
    rowid=None captures the current (in-flight) turn for save time."""

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
            out = await srv.memory_turn_summarize(summary="sum", tags="a,b")

        data = json.loads(out)
        assert data["status"] == "captured"
        assert data["rowid"] is None
        # No write and no latest_rowid lookup — applied at save time instead.
        store.update_summary.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_explicit_rowid_updates_immediately(self, restore_root_logger):
        import json

        srv, store = self._server()
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.memory_turn_summarize(rowid=3, summary="sum")

        data = json.loads(out)
        assert data["status"] == "updated"
        assert data["rowid"] == 3
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

        assert json.loads(out)["rowid"] == 9
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

        assert json.loads(out)["rowid"] == 9
        store.update_summary.assert_not_awaited()


class TestListTurns:
    """memory_list_turns — rowid-anchored lightweight listing."""

    @pytest.mark.asyncio
    async def test_passes_rowid_window_to_store(self, restore_root_logger):
        import json

        srv = _import_memdb_server()
        store = AsyncMock()
        store.list_recent = AsyncMock(return_value=[
            {"rowid": 2, "user_message": "x"},
        ])
        srv._store = store
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            out = await srv.memory_list_turns(before_rowid=10, limit=5)

        store.list_recent.assert_awaited_once_with(
            limit=5, before_rowid=10, after_rowid=None,
        )
        assert json.loads(out)[0]["rowid"] == 2


class TestTokenUsage:
    """memory_token_usage — per-turn token consumption."""

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
            out = await srv.memory_token_usage(
                rowid=3, since="2026-01-01", until="2026-02-01", limit=10,
            )

        store.token_usage.assert_awaited_once_with(
            rowid=3, since="2026-01-01", until="2026-02-01", limit=10,
        )
        assert json.loads(out)["summary"]["count"] == 1
