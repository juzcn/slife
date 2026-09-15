"""CatalogStore unit tests — the single shared tools.db (schema, effective
status, LRU, search, drainer contract, WAL cross-process pragmas)."""

import asyncio
import json

import pytest
import pytest_asyncio

from slife.tools.catalog import (
    CatalogStore,
    EFF_DISABLED,
    EFF_NA,
    EFF_UNAVAILABLE,
    _compact_schema,
    _cosine_distance,
    _deserialize_f32,
    _flatten_schema,
)


@pytest_asyncio.fixture
async def store(tmp_path):
    s = CatalogStore(tmp_path / "tools.db")
    await s.open()
    yield s
    await s.close()


def _tool(name, description="", schema=None):
    tool = {"name": name, "description": description}
    if schema is not None:
        tool["inputSchema"] = schema
    return tool


def _descriptor(name, description, schema):
    return _compact_schema(_tool(name, description, schema))


async def _set_embedding(store, name, vec, *, model="test-model"):
    await store.replace_embedding_chunks({"doc_id": name}, [vec], model=model)


# ── Upserts / effective status ──────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_tool_and_server_effective_truth_table(store):
    # mcp tool: enabled comes from the server join
    await store.upsert_server(
        "svcA", runtime="CONNECTED", enabled=True,
    )
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "full-text search", None),
        status="loaded",
    )
    # rest-api tool on a server that is up but disabled → DISABLED (config wins)
    await store.upsert_server("apiB", runtime="CONNECTED", enabled=False)
    await store.upsert_tool(
        "apiB__users", category="rest-api", source_id="apiB",
        status="loaded",
    )
    # mcp tool on a server that is enabled but NOT connected → UNAVAILABLE
    await store.upsert_server("svcC", runtime="DISCONNECTED", enabled=True)
    await store.upsert_tool(
        "svcC__ping", category="mcp", source_id="svcC", status="loaded",
    )
    # builtin disabled by config → DISABLED; enabled → loaded
    await store.upsert_tool("native_a", category="builtin", enabled=False, status="loaded")
    await store.upsert_tool("native_b", category="builtin", enabled=True, status="unloaded")
    # skill/cli → n/a (no load state)
    await store.upsert_tool("skill-xyz", category="skill")
    await store.upsert_tool("cli-foo", category="cli")

    eff = {r["name"]: r["eff"] for r in await store.scan_effective()}
    assert eff["svcA__search"] == "loaded"
    assert eff["apiB__users"] == EFF_DISABLED
    assert eff["svcC__ping"] == EFF_UNAVAILABLE
    assert eff["native_a"] == EFF_DISABLED
    assert eff["native_b"] == "unloaded"
    assert eff["skill-xyz"] == EFF_NA
    assert eff["cli-foo"] == EFF_NA

    # loaded_names() only yields server-up ∧ enabled locals that are loaded
    # (native_a is config-disabled → eff DISABLED despite status='loaded')
    assert set(await store.loaded_names()) == {"svcA__search"}
    assert await store.count_loaded() == 1


@pytest.mark.asyncio
async def test_upsert_tool_keeps_status_on_reupdate(store):
    await store.upsert_tool("native_a", category="builtin", enabled=True, status="loaded")
    await store.set_status("native_a", "unloaded")
    # a plugin re-register (reconcile upsert) must NOT clobber the unload
    changed = await store.upsert_tool(
        "native_a", category="builtin", enabled=True, status="loaded",
    )
    assert changed is False
    assert (await store.get_tool("native_a"))["status"] == "unloaded"


@pytest.mark.asyncio
async def test_upsert_schema_change_drops_embedding(store):
    s1 = {"type": "object", "properties": {"repo": {"type": "string"}}}
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "full-text search", s1), status="unloaded",
    )
    await _set_embedding(store, "svcA__search", [0.1, 0.2])
    assert await store.count_unembedded() == 0

    # description edit → part of the descriptor → stale vector dropped
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "semantic vector search", s1), status="unloaded",
    )
    assert await store.count_unembedded() == 1

    # unchanged re-upsert (idempotent reconcile) keeps the embedding
    await _set_embedding(store, "svcA__search", [0.3, 0.4])
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "semantic vector search", s1), status="unloaded",
    )
    assert await store.count_unembedded() == 0


@pytest.mark.asyncio
async def test_session_start_snapshots_last_runtime(store):
    await store.upsert_server("svcA", runtime="CONNECTED")
    await store.upsert_server("svcB", runtime="ERROR", error_reason="boom")
    # a new session starts: the CURRENT runtimes become the eager-connect set
    await store.session_start()
    assert (await store.get_server("svcA"))["last_runtime"] == "CONNECTED"
    assert (await store.get_server("svcB"))["last_runtime"] == "ERROR"
    # mid-session runtime flips don't touch the snapshot
    await store.upsert_server("svcA", runtime="DISCONNECTED")
    assert (await store.get_server("svcA"))["last_runtime"] == "CONNECTED"
    assert (await store.get_server("svcA"))["runtime"] == "DISCONNECTED"


@pytest.mark.asyncio
async def test_remove_server_cascades_tools(store):
    await store.upsert_server("svcA", runtime="CONNECTED")
    await store.upsert_tool("svcA__a", category="mcp", source_id="svcA", status="loaded")
    await store.upsert_tool("svcA__b", category="mcp", source_id="svcA", status="loaded")
    assert await store.remove_server("svcA") == 2
    assert await store.get_server("svcA") is None
    assert await store.get_tool("svcA__a") is None
    # FTS row gone too (delete trigger)
    hits = await store.search_keyword("svcA__a")
    assert hits == []


# ── LRU eviction ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evict_lru_orders_by_last_loaded_and_skips_protected(store):
    for idx, name in enumerate(["t1", "t2", "t3", "t4"]):
        await store.upsert_tool(name, category="builtin", enabled=True, status="loaded")
    # bump in a defined order: t2 oldest, then t3, t1, t4 newest
    for name in ("t2", "t3", "t1", "t4"):
        await asyncio.sleep(0)  # ensure distinct second? no — force order below
    await store.set_status("t2", "loaded", bump=True)
    await store.set_status("t3", "loaded", bump=True)
    await store.set_status("t1", "loaded", bump=True)
    await store.set_status("t4", "loaded", bump=True)

    evicted = await store.evict_lru(2, protected=frozenset({"t1"}))
    assert set(evicted) == {"t2", "t3"}
    assert (await store.get_tool("t2"))["status"] == "unloaded"
    assert (await store.get_tool("t1"))["status"] == "loaded"

    # a NULL last_loaded sorts oldest — evict it first
    await store.upsert_tool("t5", category="builtin", enabled=True, status="loaded")
    evicted = await store.evict_lru(1)
    assert evicted == ["t5"]
    assert (await store.get_tool("t5"))["status"] == "unloaded"


@pytest.mark.asyncio
async def test_evict_lru_zero_and_skill_rows_untouchable(store):
    # skill rows carry status NULL — never 'loaded' candidates, evict skips them
    await store.upsert_tool("skill-xyz", category="skill")
    await store.upsert_tool("cli-foo", category="cli")
    assert await store.evict_lru(10) == []
    assert await store.evict_lru(0) == []

    # a loaded function tool is a candidate; limit 0 is a no-op
    await store.upsert_tool("native_a", category="builtin", enabled=True, status="loaded")
    assert await store.evict_lru(0) == []
    assert await store.evict_lru(10) == ["native_a"]
    assert (await store.get_tool("native_a"))["status"] == "unloaded"


# ── Search ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_keyword_and_cjk_fallback(store):
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        description="跨仓库全文搜索工具",
        schema=_descriptor("search", "full-text search across repos", None),
        status="unloaded",
    )
    await store.upsert_tool(
        "native_exec", category="builtin", enabled=True,
        description="run a shell command", status="loaded",
    )
    eng = await store.search_keyword("search")
    assert {r["name"] for r in eng} == {"svcA__search"}
    # CJK routes to the LIKE substring fallback (unicode61 can't segment it)
    cjk = await store.search_keyword("全文搜索")
    assert {r["name"] for r in cjk} == {"svcA__search"}


@pytest.mark.asyncio
async def test_search_grep_and_category_filter(store):
    await store.upsert_tool("svcA__search", category="mcp", source_id="svcA",
                            description="full-text search tool")
    await store.upsert_tool("native_exec", category="builtin", enabled=True,
                            description="run a command")
    await store.upsert_tool("cli-foo", category="cli", description="foo cli help")
    hits = await store.search_grep("foo")
    assert {r["name"] for r in hits} == {"cli-foo"}
    hits = await store.search_grep("search", category="mcp")
    assert {r["name"] for r in hits} == {"svcA__search"}
    hits = await store.search_grep("search", category="cli")
    assert hits == []


@pytest.mark.asyncio
async def test_search_semantic_closest_chunk_per_tool(store):
    await store.upsert_tool("svcA__search", category="mcp", source_id="svcA",
                            schema=_descriptor("search", "search", None), status="unloaded")
    await store.upsert_tool("native_exec", category="builtin", enabled=True,
                            description="exec", status="loaded")
    await _set_embedding(store, "svcA__search", [1.0, 0.0, 0.0])
    await _set_embedding(store, "native_exec", [0.0, 1.0, 0.0])
    hits = await store.search_semantic([0.9, 0.1, 0.0], limit=2)
    assert hits[0]["name"] == "svcA__search"
    assert hits[0]["distance"] < hits[1]["distance"]

    # width mismatch (stale model rows) skipped defensively
    await _set_embedding(store, "svcA__search", [1.0, 0.0])
    hits = await store.search_semantic([0.9, 0.1, 0.0], limit=2)
    assert {r["name"] for r in hits} == {"native_exec"}


# ── Drainer contract ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_drainer_roundtrip_and_model_meta(store):
    await store.upsert_tool("svcA__search", category="mcp", source_id="svcA",
                            schema=_descriptor("search", "search things", None),
                            status="unloaded")
    # skill rows (SKILL.md text) never count as unembedded docs
    await store.upsert_tool("skill-xyz", category="skill", schema="# Skill notes")
    # empty-schema rows don't count either
    await store.upsert_tool("cli-foo", category="cli")

    docs = await store.get_unembedded_docs()
    assert [d["doc_id"] for d in docs] == ["svcA__search"]
    # text is already flattened (name + description + params)
    assert "search things" in docs[0]["text"]
    assert docs[0]["text"].strip()

    await store.replace_embedding_chunks(docs[0], [[0.1, 0.2]], model="api:bge-m3")
    assert await store.count_unembedded() == 0
    assert await store.count_embedded() == 1
    # meta never written by replace — the SemanticManager writes it on model select
    assert (await store.get_meta("embedding_model")) is None

    # model swap contract: drop_embeddings clears the old vector space
    await _set_embedding(store, "svcA__search", [0.5, 0.6], model="old")
    assert await store.drop_embeddings() == 1
    assert await store.count_embedded() == 0
    assert await store.count_unembedded() == 1  # svcA__search needs re-embedding


# ── Pragmas / schema version / helper sanity ────────────────────────

@pytest.mark.asyncio
async def test_wal_pragmas_and_user_version(tmp_path):
    store = CatalogStore(tmp_path / "tools.db")
    await store.open()
    cursor = await store._c.execute("PRAGMA journal_mode")
    assert (await cursor.fetchone())[0].lower() == "wal"
    import slife.timeouts as _timeouts
    cursor = await store._c.execute("PRAGMA busy_timeout")
    expected_ms = int(_timeouts.timeouts.storage.sqlite_busy * 1000)
    assert (await cursor.fetchone())[0] == expected_ms
    cursor = await store._c.execute("PRAGMA user_version")
    assert (await cursor.fetchone())[0] == 1
    await store.close()


def test_helper_sanity():
    desc = _descriptor("search", "find repos", None)
    flat = _flatten_schema(desc)
    assert "name: search" in flat and "find repos" in flat
    assert _flatten_schema("") == ""
    assert _flatten_schema("# just a markdown skill") == ""


def test_cosine_and_f32_roundtrip():
    vec = [1.0, 0.0, 0.5]
    blob = __import__("slife.plugins.memdb.store", fromlist=["_serialize_f32"])._serialize_f32(vec)
    assert _deserialize_f32(blob) == vec
    assert _cosine_distance([1, 0, 0], [1, 0, 0]) < 1e-9
    assert _cosine_distance([1, 0, 0], [0, 1, 0]) > 0.9