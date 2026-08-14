"""Tests for the memdb plugin server — background reindex bounding (REVIEW M7)
and the index-completeness gate (semantic search only when the index is full)."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
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


class TestBackgroundReindex:
    """The background reindex loop must not spin forever on a persistently
    failing embedder — it bounds consecutive no-progress batches."""

    @pytest.mark.asyncio
    async def test_gives_up_on_persistent_failure(self, restore_root_logger):
        """REVIEW M7 — indexed stays 0 forever → the loop aborts after the
        threshold instead of re-embedding the same turns forever."""
        srv = _import_memdb_server()
        srv._reindex_impl = AsyncMock(return_value={
            "indexed": 0, "remaining": 5, "complete": False,
        })

        with patch.object(srv, "_MAX_REINDEX_NO_PROGRESS", 3):
            await srv._background_reindex()

        assert srv._reindex_impl.await_count == 3  # gave up after the threshold

    @pytest.mark.asyncio
    async def test_progress_resets_and_completion_exits(self, restore_root_logger):
        """Progress resets the no-progress counter; complete ends the loop."""
        srv = _import_memdb_server()
        calls = [0]

        async def _impl(**kwargs):
            calls[0] += 1
            if calls[0] == 1:
                return {"indexed": 5, "remaining": 3, "complete": False}
            return {"indexed": 3, "remaining": 0, "complete": True}

        srv._reindex_impl = _impl

        with patch.object(srv, "_MAX_REINDEX_NO_PROGRESS", 3):
            await srv._background_reindex()

        assert calls[0] == 2  # completed on the second batch

    @pytest.mark.asyncio
    async def test_reindex_impl_counts_only_stored(self, restore_root_logger):
        """_reindex_impl must count turns that STORED embeddings, not attempts.

        embed() swallows backend errors and returns None, so a failing
        embedder yields indexed=0 — which is what makes the background loop's
        no-progress bound trip. Previously indexed incremented per attempt,
        defeating the M7 bound (REVIEW re-opening)."""
        srv = _import_memdb_server()

        store = AsyncMock()
        store.get_unembedded_turns.return_value = [
            {"rowid": 1, "user_message": "hello world", "messages": "[]",
             "created_at": "2026-01-01T00:00:00+00:00"},
        ]
        store.count_unembedded.return_value = 1
        store.upsert_embedding = AsyncMock()

        failing = MagicMock()
        failing.available = True
        failing.max_tokens = 1000
        failing.embed = AsyncMock(return_value=None)  # backend failure → None

        srv._embedder = failing
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            result = await srv._reindex_impl(reset=False, batch_limit=5)

        assert result["indexed"] == 0
        assert result["remaining"] == 1
        assert result["complete"] is False
        store.upsert_embedding.assert_not_called()

    @pytest.mark.asyncio
    async def test_reindex_impl_counts_stored_embeddings(self, restore_root_logger):
        """A healthy embedder that returns vectors IS counted as indexed."""
        srv = _import_memdb_server()

        store = AsyncMock()
        store.get_unembedded_turns.return_value = [
            {"rowid": 2, "user_message": "hello world", "messages": "[]",
             "created_at": "2026-01-01T00:00:00+00:00"},
        ]
        # 1 unembedded turn before processing, 0 after.
        store.count_unembedded = AsyncMock(side_effect=[1, 0])
        store.upsert_embedding = AsyncMock()

        healthy = MagicMock()
        healthy.available = True
        healthy.max_tokens = 1000
        healthy.embed = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

        srv._embedder = healthy
        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)):
            result = await srv._reindex_impl(reset=False, batch_limit=5)

        assert result["indexed"] == 1
        assert result["remaining"] == 0
        assert result["complete"] is True
        store.upsert_embedding.assert_awaited_once()


class TestIndexCompletenessGate:
    """Semantic search is gated OFF while the index is incomplete, ON when
    every turn is embedded — no partial semantic results are served."""

    def _ready_server(self, unembedded: int):
        srv = _import_memdb_server()
        store = AsyncMock()
        store.count_unembedded = AsyncMock(return_value=unembedded)
        srv._store = store
        srv._embedder = MagicMock()
        srv._embedder.available = True
        srv._reindex_task = None
        srv._semantic_ready = False
        return srv

    @pytest.mark.asyncio
    async def test_gate_on_when_index_complete(self, restore_root_logger):
        """count_unembedded() == 0 → semantic gate ON, no reindex started."""
        srv = self._ready_server(unembedded=0)

        with patch.object(srv, "_background_reindex", AsyncMock()) as mock_reindex:
            await srv._ensure_index_complete()

        assert srv._semantic_ready is True
        mock_reindex.assert_not_called()
        assert srv._reindex_task is None

    @pytest.mark.asyncio
    async def test_gate_off_and_reindex_started(self, restore_root_logger):
        """Unembedded turns → gate OFF + a background reindex starts."""
        srv = self._ready_server(unembedded=3)

        with patch.object(srv, "_background_reindex", AsyncMock()) as mock_reindex:
            await srv._ensure_index_complete()
            assert srv._reindex_task is not None
            await srv._reindex_task  # let the mock reindex task finish

        assert srv._semantic_ready is False
        mock_reindex.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_double_start_when_reindex_running(self, restore_root_logger):
        """A reindex already running → _ensure_index_complete does not start another."""
        srv = self._ready_server(unembedded=3)
        srv._reindex_task = asyncio.create_task(asyncio.sleep(0.01))

        with patch.object(srv, "_background_reindex", AsyncMock()) as mock_reindex:
            await srv._ensure_index_complete()
        mock_reindex.assert_not_called()
        assert srv._semantic_ready is False  # still incomplete

        srv._reindex_task.cancel()
        try:
            await srv._reindex_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_background_reindex_flips_gate_on_complete(self, restore_root_logger):
        """When the reindex finishes (remaining == 0), the gate flips ON."""
        srv = _import_memdb_server()
        srv._reindex_impl = AsyncMock(return_value={
            "indexed": 5, "remaining": 0, "complete": True,
        })
        srv._semantic_ready = False

        await srv._background_reindex()

        assert srv._semantic_ready is True


class TestGateReopenAfterReload:
    """A reloaded embedder (loaded=False) with a complete index must still
    open the gate — the gate triggers the model load itself (deadlock fix)."""

    def _server(self, load_result: bool):
        srv = _import_memdb_server()
        store = AsyncMock()
        store.count_unembedded = AsyncMock(return_value=0)
        srv._store = store
        emb = MagicMock()
        emb.available = True
        emb.loaded = False
        emb.load = AsyncMock(return_value=load_result)
        srv._embedder = emb
        srv._semantic_ready = False
        srv._reindex_task = None
        return srv, emb

    @pytest.mark.asyncio
    async def test_gate_loads_model_and_opens(self, restore_root_logger):
        srv, emb = self._server(load_result=True)

        await srv._ensure_index_complete()

        assert srv._semantic_ready is True
        emb.load.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_gate_stays_off_when_model_load_fails(self, restore_root_logger):
        srv, emb = self._server(load_result=False)

        await srv._ensure_index_complete()

        assert srv._semantic_ready is False
        emb.load.assert_awaited_once()


class TestHybridDegradationHint:
    """The hybrid→fts5 degradation must be surfaced even when no keyword
    hits survive — an empty result is exactly when a silent fallback
    would mislead (REVIEW: silent degradation)."""

    def _server(self, keyword_hits: list[dict]):
        import json  # noqa: F401

        srv = _import_memdb_server()
        store = AsyncMock()
        store.search_keyword = AsyncMock(return_value=keyword_hits)
        store.search_semantic = AsyncMock(return_value=[])
        srv._store = store
        srv._embedder = MagicMock()
        srv._embedder.available = True
        srv._semantic_ready = False  # gate off → semantic unavailable
        return srv, store

    @pytest.mark.asyncio
    async def test_empty_result_still_reports_degradation(self, restore_root_logger):
        import json

        srv, store = self._server(keyword_hits=[])

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(srv, "_ensure_index_complete", AsyncMock()):
            out = await srv.memory_search(query="北京天气怎么样", mode="hybrid")

        data = json.loads(out)
        assert data["mode"] == "fts5"
        # The degradation reason, not just "no matching memories found".
        assert "degraded" in data["hint"]

    @pytest.mark.asyncio
    async def test_keyword_hits_with_gate_off_also_report(self, restore_root_logger):
        import json

        srv, store = self._server(keyword_hits=[
            {"rowid": 1, "user_message": "微信登录", "snippet": "…", "rank": -1.0},
        ])

        with patch.object(srv, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(srv, "_ensure_index_complete", AsyncMock()):
            out = await srv.memory_search(query="微信登录", mode="hybrid")

        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert data["results"]
        assert "degraded" in data["hint"]


class TestStoreLifecycleLocking:
    """The post-model-change rebuild and the per-turn save must both run
    under the lifecycle lock — a rebuild that races a save closes the old
    connection mid-save and loses the turn; a rebuild that races
    _ensure_store orphans the store it builds (REVIEW #2)."""

    @pytest.mark.asyncio
    async def test_save_turn_holds_lifecycle_lock(self, restore_root_logger):
        """__memory_save_turn holds the lock during save_turn, so a rebuild
        cannot close the connection mid-save."""
        import json

        srv = _import_memdb_server()
        lock_held = []

        async def _fake_save_turn(**kwargs):
            lock_held.append(srv._get_init_lock().locked())
            return 1

        store = AsyncMock()
        store.save_turn = _fake_save_turn
        srv._ensure_store_locked = AsyncMock(return_value=store)
        srv._embedder = MagicMock()

        out = await getattr(srv, "__memory_save_turn")(user_message="hi")

        assert lock_held == [True]
        assert json.loads(out)["rowid"] == 1

    @pytest.mark.asyncio
    async def test_save_turn_forwards_images(self, restore_root_logger):
        """__memory_save_turn must accept and forward ``images`` to
        store.save_turn.

        Regression: save_to_memory passes ``images`` (the diary column added
        for TUI image restore), but the tool signature lacked it — FastMCP
        rejected the call as an unexpected keyword argument and every save
        failed silently.
        """
        import json

        srv = _import_memdb_server()
        captured: dict = {}

        async def _fake_save_turn(**kwargs):
            captured.update(kwargs)
            return 7

        store = AsyncMock()
        store.save_turn = _fake_save_turn
        srv._ensure_store_locked = AsyncMock(return_value=store)
        srv._embedder = MagicMock()
        reindex = AsyncMock()
        with patch.object(srv, "_ensure_index_complete", reindex):
            out = await getattr(srv, "__memory_save_turn")(
                user_message="hi", images=[r"C:\cache\a.png"],
            )

        assert json.loads(out)["rowid"] == 7
        assert captured["images"] == [r"C:\cache\a.png"]
        # save_turn is insert-only (embedding is internal/reindex); the
        # harness's 10s save timeout can no longer be tripped by a slow embed.
        assert "embedder" not in captured
        reindex.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reinit_holds_lock_during_swap(self, restore_root_logger):
        """_reinit_store_after_model_change swaps the store under the lock —
        the old connection is closed while the lock is held."""
        srv = _import_memdb_server()

        old_store = AsyncMock()
        new_store = AsyncMock()
        srv._store = old_store
        emb = MagicMock()
        emb.available = True
        emb.dimension = 768
        emb.backend = "api"
        emb._model = "text-embedding-3-small"
        srv._embedder = emb

        orig_close = old_store.close
        close_lock_states = []

        async def _close_and_check():
            close_lock_states.append(srv._get_init_lock().locked())
            await orig_close()

        old_store.close = _close_and_check

        with patch.object(srv, "SessionStore", return_value=new_store), \
             patch.object(srv, "_ensure_index_complete", AsyncMock()):
            await srv._reinit_store_after_model_change()

        assert close_lock_states == [True]
        assert srv._store is new_store
