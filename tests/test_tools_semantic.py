"""The drainer-less semantic surface — a worker querying its parent's index.

A subagent owns no drainer, so before this it held no semantic surface at all
and every ``tool_search`` in every worker was keyword-only, against an index
its parent was maintaining in the same database.  The reader fixes that, and
the interesting part is what it REFUSES: an index that is still filling, or one
built by a different embedding model, must report keyword-only with a reason
rather than return a plausible-looking result set.  Each refusal below is a
case where guessing would have been worse than degrading.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from slife.config import EmbeddingsConfig
from slife.tools.catalog import CatalogStore
from slife.tools.semantic import (
    SEMANTIC_STATE_KEY,
    SemanticManager,
    SemanticReader,
    read_published_state,
)

ENDPOINT = {
    "providers": {
        "local": {
            "base_url": "http://127.0.0.1:1/v1",
            "api_key": "k",
            "model": "bge-m3",
        },
    },
    "active_model": "local",
}


def _transport(dim=3, fail: bool = False):
    """MockTransport answering /models + /embeddings (OpenAI-compatible)."""

    def handler(request: httpx2.Request) -> httpx2.Response:
        if fail:
            return httpx2.Response(500, json={"error": "boom"})
        if request.url.path.endswith("/models"):
            return httpx2.Response(200, json={"data": [{"id": "bge-m3", "dimension": dim}]})
        if request.url.path.endswith("/embeddings"):
            return httpx2.Response(200, json={
                "data": [{"object": "embedding", "index": 0,
                          "embedding": [0.25] * dim}],
            })
        return httpx2.Response(404, json={"error": "not found"})

    return httpx2.MockTransport(handler)


@pytest.fixture
def config():
    return EmbeddingsConfig.from_dict(ENDPOINT)


async def _store(tmp_path) -> CatalogStore:
    store = CatalogStore(tmp_path / "tools.db")
    await store.open()
    return store


async def _publish(store, **facts) -> None:
    """Write the state row the index's owner publishes (as the drainer does)."""
    state = {
        "configured": True, "available": True, "semantic_ready": True,
        "state": "ready", "reason": "", "model": "bge-m3", "dimension": 3,
    }
    state.update(facts)
    await store.set_meta(SEMANTIC_STATE_KEY, json.dumps(state))


# ── ready: the reader searches what the owner embedded ──────────────────


@pytest.mark.asyncio
async def test_a_ready_index_is_queryable_from_a_process_that_owns_no_drainer(
    tmp_path, config,
):
    store = await _store(tmp_path)
    try:
        await _publish(store)
        reader = SemanticReader(store, config, transport=_transport())

        assert await reader.query_ready() is True
        assert reader.reason == ""
        vec = await reader.embed_query("find me a search tool")
        assert vec and len(vec) == 3
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_the_model_that_built_the_index_is_what_makes_it_comparable(
    tmp_path, config,
):
    """Same width is not the same vector space — identity is the check.

    A reader whose config names a different model would compare its query
    vector against vectors from another space: the widths can match and every
    similarity would still be meaningless, so this refuses instead of scoring.
    """
    store = await _store(tmp_path)
    try:
        await _publish(store, model="BAAI/bge-m3")
        reader = SemanticReader(store, config, transport=_transport())

        assert await reader.query_ready() is False
        assert "different embedding model" in reader.reason
        assert "BAAI/bge-m3" in reader.reason
    finally:
        await store.close()


# ── not ready: refuse, and say which refusal it was ─────────────────────


@pytest.mark.asyncio
async def test_an_index_still_filling_is_not_a_result_set(tmp_path, config):
    """A partial index answers "not yet" — the owner's own gate, honoured here."""
    store = await _store(tmp_path)
    try:
        await _publish(
            store, semantic_ready=False, state="indexing",
            reason="indexing 12/40 tools",
        )
        reader = SemanticReader(store, config, transport=_transport())

        assert await reader.query_ready() is False
        assert reader.reason == "indexing 12/40 tools"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_published_state_names_that_fact(tmp_path, config):
    """``unknown`` is its own diagnosis, not a generic "unavailable".

    A db no drainer has ever run in is a different situation from a drainer
    that failed — and the reader is the process that cannot tell them apart by
    looking at itself, so it asks the db and reports what it found.
    """
    store = await _store(tmp_path)
    try:
        reader = SemanticReader(store, config, transport=_transport())

        assert await reader.query_ready() is False
        assert "no published state" in reader.reason
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_no_endpoint_configured_is_reported_as_such(tmp_path):
    """With no embeddings section the reader says so — it does not guess."""
    store = await _store(tmp_path)
    try:
        await _publish(store)
        reader = SemanticReader(store, EmbeddingsConfig.from_dict({}))

        assert await reader.query_ready() is False
        assert "no embedding endpoint configured" in reader.reason
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a_disabled_embeddings_section_disables_the_reader(tmp_path, config):
    """``embeddings: {enabled: false}`` is the user switching semantic search
    off — a reader may no more ignore it than the drainer may, and the reason
    says "switched off", not "no endpoint" (there IS one; it is off).
    """
    store = await _store(tmp_path)
    try:
        await _publish(store)
        off = EmbeddingsConfig.from_dict({**ENDPOINT, "enabled": False})
        reader = SemanticReader(store, off, transport=_transport())

        assert await reader.query_ready() is False
        assert "switched off" in reader.reason
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a_dead_endpoint_is_probed_once_not_once_per_search(
    tmp_path, config,
):
    """A worker is long-lived: a per-search retry would stall every task.

    The load is attempted once and its failure is remembered with a reason
    that names the endpoint, so ``tool_search`` degrades to keyword-only at the
    cost of one bounded probe rather than a transport timeout per call.
    """
    store = await _store(tmp_path)
    try:
        await _publish(store)
        reader = SemanticReader(store, config, transport=_transport(fail=True))

        assert await reader.query_ready() is False
        assert "did not answer" in reader.reason
        assert reader._load_attempted is True

        # Second call: already known bad, no new attempt, same verdict.
        assert await reader.query_ready() is False
        assert reader._load_attempted is True
    finally:
        await store.close()


# ── the parser both the health block and the reader share ───────────────


@pytest.mark.asyncio
async def test_read_published_state_is_the_one_parser(tmp_path):
    store = await _store(tmp_path)
    try:
        assert await read_published_state(store) == {"state": "unknown"}
        await _publish(store, state="stalled", reason="embedder gave up")
        assert (await read_published_state(store))["state"] == "stalled"

        for garbage in ("", "not json", "[1,2,3]"):
            await store.set_meta(SEMANTIC_STATE_KEY, garbage)
            assert await read_published_state(store) == {"state": "unknown"}
    finally:
        await store.close()


# ── the owner's own surface answers the same contract ───────────────────


@pytest.mark.asyncio
async def test_the_manager_reports_the_gate_before_the_index_is_ready(
    tmp_path, config,
):
    """The drainer's manager is not "ready" merely because it exists.

    Both surfaces expose ``query_ready``/``reason``/``embed_query`` so the
    hybrid leg never branches; for the manager, ready-ness is the drain gate
    (``semantic_ready``), which is False until the index drains.
    """
    store = await _store(tmp_path)
    try:
        manager = SemanticManager(store, config)
        assert await manager.query_ready() is False
        assert await manager.embed_query("x") is None
    finally:
        await store.close()


# ── the owner re-points its index when the section changes ──────────────


@pytest.mark.asyncio
async def test_reload_repoints_the_index_at_a_freshly_written_section(
    tmp_path, monkeypatch,
):
    """Switching providers must move the index with it, in-process.

    The manager was built from the config this process started with, so a
    section written later has to be handed over; ``enable()`` then rebuilds the
    embedder from it and — when the model identity changed — drops the vectors
    that live in the previous model's space, which is the whole reason the
    index can be re-pointed safely at all.
    """
    from slife.tools.semantic import EmbeddingClient

    store = await _store(tmp_path)
    try:
        # No I/O: this test is about which section the manager adopts, not
        # about reaching an endpoint.
        async def _fake_load(self):
            self._loaded = True
            return True

        monkeypatch.setattr(EmbeddingClient, "load", _fake_load)

        manager = SemanticManager(store, EmbeddingsConfig.from_dict(ENDPOINT))
        assert manager._new_embedder().base_url == "http://127.0.0.1:1/v1"

        # The provider the index was built with is recorded in the store.
        await store.set_meta("embedding_model", "api:old-model")
        await manager.reload(EmbeddingsConfig.from_dict({
            "providers": {"remote": {
                "base_url": "https://api.example/v1", "api_key": "k",
                "model": "bge-m3",
            }},
            "active_model": "remote",
        }))
        assert manager._new_embedder().base_url == "https://api.example/v1"
        # A different model identity drops the old vectors (the store's meta
        # is the record of what the index holds).
        assert await store.get_meta("embedding_model") == "api:bge-m3"

        await manager.close()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_reloading_onto_an_unusable_section_disables_the_index(
    tmp_path, monkeypatch,
):
    """Removing the last provider must degrade the index, not leave it running
    against a section that no longer names an endpoint."""
    from slife.tools.semantic import EmbeddingClient

    store = await _store(tmp_path)
    try:
        async def _fake_load(self):
            self._loaded = True
            return True

        monkeypatch.setattr(EmbeddingClient, "load", _fake_load)
        manager = SemanticManager(store, EmbeddingsConfig.from_dict(ENDPOINT))
        await manager.reload(EmbeddingsConfig.from_dict({}))

        assert manager.state == "disabled"
        assert manager.reason
        assert await manager.query_ready() is False
        await manager.close()
    finally:
        await store.close()
