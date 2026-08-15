"""Tests for slife.plugins.memdb.semantic — SemanticManager.

Covers the binary gate (only writer), blocking enable/disable, the
event-driven index drainer (no polling), and the atomic batch commit.
"""

import pytest; pytestmark = pytest.mark.unit


import asyncio
from collections import deque
from itertools import cycle
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.plugins.memdb.semantic import SemanticManager


_DEFAULT = object()  # sentinel: distinguish "no override" from "embed returns None"


def _embedder(*, available=True, backend="gguf", model="bge-m3", dim=1024,
              load_ok=True, embed_result=_DEFAULT, embed_sleep=0.0):
    """A fake EmbeddingClient.

    ``embed_result=None`` means embed() returns None (a failing embedder);
    anything else (incl. the default vector) is returned as-is. ``embed_sleep``
    paces embed() so an infinite-work drainer test can't busy-spin and balloon
    the mocks' call-records (memory safety).
    """
    e = MagicMock()
    e.available = available
    e.backend = backend
    e._model = model
    e.dimension = dim
    e.loaded = False
    e.max_tokens = 1000
    e.load = AsyncMock(return_value=load_ok)

    async def _embed(chunks):
        if embed_sleep:
            await asyncio.sleep(embed_sleep)
        return [[0.1, 0.2, 0.3]] if embed_result is _DEFAULT else embed_result

    e.embed = _embed
    e.embed_one = AsyncMock(return_value=[0.1, 0.2, 0.3])
    return e


def _turn(**kw):
    t = {"rowid": 1, "user_message": "hello world", "messages": "[]",
         "summary": "", "tags": "", "created_at": "2026-01-01T00:00:00+00:00"}
    t.update(kw)
    return t


class TestEnable:
    @pytest.mark.asyncio
    async def test_enable_loads_migrates_starts_drainer(self):
        store = AsyncMock()
        store.reconfigure_for_embedding = AsyncMock()
        m = SemanticManager(store)
        emb = _embedder()
        with patch("slife.plugins.memdb.semantic.EmbeddingClient.from_config",
                   return_value=emb):
            status = await m.enable()

        assert status["status"] == "ok"
        assert status["backend"] == "gguf"
        assert status["semantic_ready"] is False  # gate stays OFF until drained
        assert status["state"] == "indexing"
        emb.load.assert_awaited_once()
        store.reconfigure_for_embedding.assert_awaited_once()
        assert m._drain_task is not None  # drainer started (not awaited)
        await m.close()  # stop the drainer

    @pytest.mark.asyncio
    async def test_enable_backend_unavailable(self):
        store = AsyncMock()
        m = SemanticManager(store)
        emb = _embedder(available=False)
        with patch("slife.plugins.memdb.semantic.EmbeddingClient.from_config",
                   return_value=emb):
            status = await m.enable()

        assert status["status"] == "degraded"
        assert status["state"] == "disabled"
        assert status["semantic_ready"] is False
        assert m._drain_task is None
        emb.load.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_enable_model_load_failure_stalls(self):
        store = AsyncMock()
        m = SemanticManager(store)
        emb = _embedder(load_ok=False)
        with patch("slife.plugins.memdb.semantic.EmbeddingClient.from_config",
                   return_value=emb):
            status = await m.enable()

        assert status["status"] == "degraded"
        assert status["state"] == "stalled"
        assert status["semantic_ready"] is False
        assert m._drain_task is None

    @pytest.mark.asyncio
    async def test_enable_replaces_existing_drainer(self):
        """A second enable (model change) cancels + replaces the drainer."""
        store = AsyncMock()
        store.reconfigure_for_embedding = AsyncMock()
        m = SemanticManager(store)
        emb = _embedder()
        with patch("slife.plugins.memdb.semantic.EmbeddingClient.from_config",
                   return_value=emb):
            await m.enable()
        first_task = m._drain_task
        assert first_task is not None

        with patch("slife.plugins.memdb.semantic.EmbeddingClient.from_config",
                   return_value=emb):
            await m.enable()
        assert m._drain_task is not None
        assert m._drain_task is not first_task  # replaced, not stacked
        await m.close()


class TestDisable:
    @pytest.mark.asyncio
    async def test_disable_stops_drainer_keeps_embeddings(self):
        store = AsyncMock()
        store.clear_all_embeddings = AsyncMock()
        m = SemanticManager(store)
        emb = _embedder()
        with patch("slife.plugins.memdb.semantic.EmbeddingClient.from_config",
                   return_value=emb):
            await m.enable()
        assert m._drain_task is not None

        status = await m.disable()

        assert status["state"] == "disabled"
        assert status["semantic_ready"] is False
        assert m._embedder is None
        assert m._drain_task is None
        store.clear_all_embeddings.assert_not_awaited()  # embeddings preserved


class TestDrainerGate:
    """The drainer is the only gate writer: opens at count==0, closes while >0."""

    @pytest.mark.asyncio
    async def test_gate_opens_and_wakes_on_save(self):
        # drainer check 0 → ready+wait; save wakes → check 1 → process (total 1,
        # remaining 0) → complete → check 0 → ready+wait
        q = deque([0, 1, 1, 0, 0, 0])
        store = AsyncMock()
        store.count_unembedded = AsyncMock(side_effect=lambda: q.popleft())
        store.get_unembedded_turns = AsyncMock(return_value=[_turn()])
        store.replace_embedding_chunks = AsyncMock()
        m = SemanticManager(store)
        m._enabled = True
        m._embedder = _embedder()
        m._drain_task = asyncio.create_task(m._drain_loop())
        try:
            await asyncio.sleep(0.05)
            assert m.semantic_ready is True and m.state == "ready"

            # a saved turn wakes the idle drainer; it drains and re-opens the gate
            m.on_turn_saved()
            await asyncio.sleep(0.1)
            assert m.semantic_ready is True and m.state == "ready"
        finally:
            await m.close()  # cancel the drainer task even on assertion failure

    @pytest.mark.asyncio
    async def test_gate_closes_while_draining(self):
        # every batch: check 1, total 1, remaining 1 — never completes. The
        # embed is paced (embed_sleep) so the drainer can't busy-spin and
        # balloon the mocks' call-records while we observe the state.
        store = AsyncMock()
        store.count_unembedded = AsyncMock(
            side_effect=lambda: next(cycle([1, 1, 1])))
        store.get_unembedded_turns = AsyncMock(return_value=[_turn()])
        store.replace_embedding_chunks = AsyncMock()
        m = SemanticManager(store)
        m._enabled = True
        m._embedder = _embedder(embed_sleep=0.02)
        m._drain_task = asyncio.create_task(m._drain_loop())
        try:
            await asyncio.sleep(0.05)
            # drainer set gate OFF while indexing; batch never completes
            assert m.semantic_ready is False
            assert m.state == "indexing"
        finally:
            await m.close()  # cancel the drainer task even on assertion failure

    @pytest.mark.asyncio
    async def test_stalls_after_no_progress(self):
        # every batch: check 1, total 1, remaining 1 — embed returns None
        store = AsyncMock()
        store.count_unembedded = AsyncMock(
            side_effect=lambda: next(cycle([1, 1, 1])))
        store.get_unembedded_turns = AsyncMock(return_value=[_turn()])
        m = SemanticManager(store)
        m._enabled = True
        m._embedder = _embedder(embed_result=None)  # embed fails persistently

        with patch("slife.plugins.memdb.semantic.MAX_REINDEX_NO_PROGRESS", 3):
            await m._drain_loop()  # returns on its own (stalled)

        assert m.state == "stalled"
        assert m.semantic_ready is False
        assert m._enabled is False  # only enable() restarts


class TestProcessBatch:
    @pytest.mark.asyncio
    async def test_counts_full_success_and_preserves_summary_tags(self):
        store = AsyncMock()
        store.get_unembedded_turns = AsyncMock(return_value=[
            _turn(rowid=1, summary="s", tags="t"),
        ])
        store.count_unembedded = AsyncMock(side_effect=[1, 0])
        store.replace_embedding_chunks = AsyncMock()
        m = SemanticManager(store)
        m._embedder = _embedder()

        result = await m._process_batch()

        assert result["indexed"] == 1
        assert result["complete"] is True
        store.replace_embedding_chunks.assert_awaited_once()
        kwargs = store.replace_embedding_chunks.await_args.kwargs
        assert kwargs["summary"] == "s"
        assert kwargs["tags"] == "t"
        assert kwargs["diary_rowid"] == 1

    @pytest.mark.asyncio
    async def test_failed_embed_counts_zero_no_commit(self):
        store = AsyncMock()
        store.get_unembedded_turns = AsyncMock(return_value=[_turn()])
        store.count_unembedded = AsyncMock(side_effect=[1, 1])
        store.replace_embedding_chunks = AsyncMock()
        m = SemanticManager(store)
        m._embedder = _embedder(embed_result=None)

        result = await m._process_batch()

        assert result["indexed"] == 0
        assert result["complete"] is False
        store.replace_embedding_chunks.assert_not_awaited()


class TestOnTurnSaved:
    def test_sets_event_only_when_enabled(self):
        m = SemanticManager(AsyncMock())
        m._enabled = True
        m.on_turn_saved()
        assert m._work_event.is_set()

        m._work_event.clear()
        m._enabled = False
        m.on_turn_saved()
        assert not m._work_event.is_set()


