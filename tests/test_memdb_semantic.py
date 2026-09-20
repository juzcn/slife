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

from slife import timeouts as _t
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


def _doc(**kw):
    t = {"doc_id": 1, "text": "hello world",
         "summary": "", "tags": "", "created_at": "2026-01-01T00:00:00+00:00"}
    t.update(kw)
    return t


class TestStatePublication:
    """``_set_state`` is the ONE writer of ``_state``/``_reason``, and every
    move notifies the publication hook — a subclass that writes the state
    somewhere else (the catalog publishes it into the shared db) cannot lag
    the transition it describes."""

    @pytest.mark.asyncio
    async def test_a_transition_notifies_once_with_the_new_state(self):
        seen: list[tuple[str, str]] = []

        class _Publishing(SemanticManager):
            async def _publish_state(self) -> None:
                seen.append((self._state, self._reason))

        m = _Publishing(AsyncMock())
        await m._set_state("stalled", "boom")

        assert seen == [("stalled", "boom")]
        assert m.state == "stalled" and m.reason == "boom"

    @pytest.mark.asyncio
    async def test_disable_publishes_through_the_public_path(self):
        seen: list[str] = []

        class _Publishing(SemanticManager):
            async def _publish_state(self) -> None:
                seen.append(self._state)

        m = _Publishing(AsyncMock())
        await m.disable()

        assert seen == ["disabled"]

    @pytest.mark.asyncio
    async def test_a_failing_publication_cannot_break_the_transition(self):
        """The state is already moved when the hook runs, so a store that is
        down must cost the publication, never the transition."""
        published = []

        class _Broken(SemanticManager):
            async def _publish_state(self) -> None:
                published.append(self._state)
                raise RuntimeError("db down")

        m = _Broken(AsyncMock())
        await m._set_state("ready")          # must not raise

        assert m.state == "ready" and published == ["ready"]


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
        store.get_unembedded_docs = AsyncMock(return_value=[_doc()])
        store.replace_embedding_chunks = AsyncMock()
        m = SemanticManager(store)
        m._enabled = True
        m._embedder = _embedder()
        m._drain_task = asyncio.create_task(m._drain_loop())
        try:
            await asyncio.sleep(0.05)
            assert m.semantic_ready is True and m.state == "ready"

            # a saved turn wakes the idle drainer; it drains and re-opens the gate
            m.on_saved()
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
        store.get_unembedded_docs = AsyncMock(return_value=[_doc()])
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
    async def test_stall_parks_alive_and_heals_on_new_work(self):
        """A stall PARKS the drainer; it does not end it.

        ``_enabled`` staying True is what lets ``on_saved()`` deliver the wake
        that ends the stall.  The previous behaviour returned out of the loop
        AND cleared ``_enabled``, so every content-driven wake became a silent
        no-op — a transient embedder failure disabled semantic indexing until
        the next process restart.
        """
        # remaining drops to 0 only when the embed actually commits
        state = {"remaining": 1}
        store = AsyncMock()
        store.count_unembedded = AsyncMock(side_effect=lambda: state["remaining"])
        store.get_unembedded_docs = AsyncMock(
            side_effect=lambda limit=100: [_doc()] if state["remaining"] else [])
        store.replace_embedding_chunks = AsyncMock(
            side_effect=lambda *a, **kw: state.__setitem__("remaining", 0))
        m = SemanticManager(store)
        m._enabled = True
        m._embedder = _embedder(embed_result=None)  # embed fails persistently

        with patch.object(_t.timeouts.ready, "watchdog_backoff_initial", 0.0), \
                patch.object(_t.timeouts.ready, "watchdog_backoff_max", 0.0), \
                patch("slife.plugins.memdb.semantic.MAX_REINDEX_NO_PROGRESS", 3):
            m._drain_task = asyncio.create_task(m._drain_loop())
            try:
                for _ in range(200):          # burn the budget → stall
                    await asyncio.sleep(0.005)
                    if m.state == "stalled":
                        break
                assert m.state == "stalled"
                assert m.semantic_ready is False
                assert m._enabled is True, "a stalled drainer is alive, not finished"
                assert not m._drain_task.done(), "the loop parked; it did not return"

                # Parked means idle: no batch runs while nothing wakes it.
                calls = store.get_unembedded_docs.await_count
                await asyncio.sleep(0.05)
                assert store.get_unembedded_docs.await_count == calls

                # New work wakes it, and the embedder now works → gate reopens.
                m._embedder = _embedder()
                m.on_saved()
                for _ in range(200):
                    await asyncio.sleep(0.005)
                    if m.semantic_ready:
                        break
                assert m.semantic_ready is True, "the stall never healed"
                assert m.state == "ready"
            finally:
                await m.close()

    @pytest.mark.asyncio
    async def test_unavailable_embedder_stalls_instead_of_spinning(self):
        """An unusable embedder must stall, not report ``complete``.

        The loop reads ``complete`` as progress, so that early-out used to
        reset the no-progress bound and spin at ``sleep(0)`` forever — neither
        stalling nor ever opening the gate.  It was the one state
        ``MAX_REINDEX_NO_PROGRESS`` structurally could not catch.
        """
        store = AsyncMock()
        store.count_unembedded = AsyncMock(return_value=1)
        store.get_unembedded_docs = AsyncMock(return_value=[_doc()])
        m = SemanticManager(store)
        m._enabled = True
        m._embedder = None                     # nothing to embed with

        with patch.object(_t.timeouts.ready, "watchdog_backoff_initial", 0.0), \
                patch.object(_t.timeouts.ready, "watchdog_backoff_max", 0.0), \
                patch("slife.plugins.memdb.semantic.MAX_REINDEX_NO_PROGRESS", 3):
            m._drain_task = asyncio.create_task(m._drain_loop())
            try:
                for _ in range(200):
                    await asyncio.sleep(0.005)
                    if m.state == "stalled":
                        break
                assert m.state == "stalled"
                assert m.semantic_ready is False
                assert not m._drain_task.done()
                # It got here by burning the bound rather than spinning: the
                # budget is parked at the limit, and the loop ran a handful of
                # iterations instead of an unbounded stream of them.
                assert m._no_progress == 3
                assert store.count_unembedded.await_count <= 5
            finally:
                await m.close()

    @pytest.mark.asyncio
    async def test_pace_retry_backs_off_and_caps(self):
        """Failing batches are paced.  Only ``sleep(0)`` separated them before,
        so a broken embedder was retried as fast as the loop allowed."""
        m = SemanticManager(AsyncMock())
        fake = MagicMock()
        fake.sleep = AsyncMock()
        with patch("slife.plugins.memdb.semantic.asyncio", fake), \
                patch.object(_t.timeouts.ready, "watchdog_backoff_multiplier", 2.0), \
                patch.object(_t.timeouts.ready, "watchdog_backoff_max", 5.0):
            b = await m._pace_retry(1.0)
            assert b == 2.0
            b = await m._pace_retry(b)
            assert b == 4.0
            b = await m._pace_retry(b)
            assert b == 5.0                    # capped at the registry max
        assert [c.args[0] for c in fake.sleep.await_args_list] == [1.0, 2.0, 4.0]


class TestProcessBatch:
    @pytest.mark.asyncio
    async def test_counts_full_success_and_preserves_summary_tags(self):
        store = AsyncMock()
        store.get_unembedded_docs = AsyncMock(return_value=[
            _doc(doc_id=1, summary="s", tags="t"),
        ])
        store.count_unembedded = AsyncMock(side_effect=[1, 0])
        store.replace_embedding_chunks = AsyncMock()
        m = SemanticManager(store)
        m._embedder = _embedder()

        result = await m._process_batch()

        assert result["indexed"] == 1
        assert result["complete"] is True
        store.replace_embedding_chunks.assert_awaited_once()
        doc, _embeddings = store.replace_embedding_chunks.await_args.args
        assert doc["doc_id"] == 1
        assert doc["summary"] == "s"
        assert doc["tags"] == "t"

    @pytest.mark.asyncio
    async def test_failed_embed_counts_zero_no_commit(self):
        store = AsyncMock()
        store.get_unembedded_docs = AsyncMock(return_value=[_doc()])
        store.count_unembedded = AsyncMock(side_effect=[1, 1])
        store.replace_embedding_chunks = AsyncMock()
        m = SemanticManager(store)
        m._embedder = _embedder(embed_result=None)

        result = await m._process_batch()

        assert result["indexed"] == 0
        assert result["complete"] is False
        store.replace_embedding_chunks.assert_not_awaited()


class TestOnSaved:
    def test_sets_event_only_when_enabled(self):
        m = SemanticManager(AsyncMock())
        m._enabled = True
        m.on_saved()
        assert m._work_event.is_set()

        m._work_event.clear()
        m._enabled = False
        m.on_saved()
        assert not m._work_event.is_set()


