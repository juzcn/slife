"""ToolStore unit tests — catalog sync, search, semantic cosine, drainer."""

import pytest
import pytest_asyncio

from mcp_plugin.store import ToolStore, _cosine_distance, _deserialize_f32, _serialize_f32


@pytest_asyncio.fixture
async def store():
    s = ToolStore()
    await s.open()
    yield s
    await s.close()


def _tool(name, description=""):
    return {"name": name, "description": description}


@pytest.mark.asyncio
async def test_sync_server_upserts_and_preserves_enabled(store):
    await store.sync_server("svcA", [_tool("search", "full-text search"), _tool("fetch")])
    assert await store.count_by_server("svcA") == 2
    assert await store.get_tool("svcA__search") is not None

    # Disable one tool, then re-sync the same server — the flag must survive.
    assert await store.set_tool_enabled("svcA__search", False)
    await store.sync_server("svcA", [_tool("search", "updated description"), _tool("fetch")])
    row = await store.get_tool("svcA__search")
    assert row["enabled"] == 0
    assert row["description"] == "updated description"


@pytest.mark.asyncio
async def test_sync_server_deletes_absent_tools(store):
    await store.sync_server("svcA", [_tool("search"), _tool("fetch")])
    await store.sync_server("svcA", [_tool("search")])
    assert await store.count_by_server("svcA") == 1
    assert await store.get_tool("svcA__fetch") is None


@pytest.mark.asyncio
async def test_remove_server(store):
    await store.sync_server("svcA", [_tool("search")])
    await store.sync_server("svcB", [_tool("list")])
    assert await store.remove_server("svcA") == 1
    assert await store.count_by_server("svcA") == 0
    assert await store.count_by_server("svcB") == 1


@pytest.mark.asyncio
async def test_list_tools_by_server(store):
    await store.sync_server("svcA", [_tool("search", "find files"), _tool("fetch")])
    await store.sync_server("svcB", [_tool("list")])
    await store.set_tool_enabled("svcA__search", False)

    rows = await store.list_tools_by_server("svcA")
    assert [r["name"] for r in rows] == ["search", "fetch"]
    search = {r["name"]: r for r in rows}["search"]
    assert search["full_name"] == "svcA__search"
    assert search["description"] == "find files"
    assert search["enabled"] == 0
    assert await store.list_tools_by_server("svcB") == [
        {"full_name": "svcB__list", "server": "svcB", "name": "list",
         "description": "", "enabled": 1},
    ]
    assert await store.list_tools_by_server("svcNope") == []


@pytest.mark.asyncio
async def test_set_tool_enabled_unknown(store):
    assert await store.set_tool_enabled("nope__x", True) is False


@pytest.mark.asyncio
async def test_disable_server_tools(store):
    await store.sync_server("svcA", [_tool("search"), _tool("fetch")])
    await store.sync_server("svcB", [_tool("list")])
    # Enabled-by-default tools of svcB stay enabled; svcA's all go disabled.
    assert await store.disable_server_tools("svcA") == 2
    assert (await store.get_tool("svcA__search"))["enabled"] == 0
    assert (await store.get_tool("svcA__fetch"))["enabled"] == 0
    assert (await store.get_tool("svcB__list"))["enabled"] == 1
    # Re-enabling the server flips all its tools back to enabled.
    assert await store.enable_server_tools("svcA") == 2
    assert (await store.get_tool("svcA__search"))["enabled"] == 1
    assert (await store.get_tool("svcB__list"))["enabled"] == 1


@pytest.mark.asyncio
async def test_search_keyword_fts5(store):
    await store.sync_server("svcA", [_tool("search", "find files by content")])
    await store.sync_server("svcB", [_tool("search", "look up contacts")])
    hits = await store.search_keyword("files")
    assert [h["full_name"] for h in hits] == ["svcA__search"]
    assert hits[0]["snippet"]


@pytest.mark.asyncio
async def test_search_keyword_cjk_routes_to_like(store):
    await store.sync_server("svcA", [_tool("search", "文件搜索工具")])
    hits = await store.search_keyword("文件")
    assert len(hits) == 1 and hits[0]["full_name"] == "svcA__search"


@pytest.mark.asyncio
async def test_search_keyword_server_and_disabled_filters(store):
    await store.sync_server("svcA", [_tool("search", "find files")])
    await store.sync_server("svcB", [_tool("search", "find files")])
    await store.set_tool_enabled("svcA__search", False)

    hits = await store.search_keyword("find", server="svcB")
    assert [h["full_name"] for h in hits] == ["svcB__search"]

    hits = await store.search_keyword("find", include_disabled=False)
    assert [h["full_name"] for h in hits] == ["svcB__search"]

    hits = await store.search_keyword("find", include_disabled=True)
    assert len(hits) == 2


@pytest.mark.asyncio
async def test_search_grep_escapes(store):
    await store.sync_server("svcA", [_tool("search", "100% match")])
    await store.sync_server("svcB", [_tool("search", "100 matches")])
    # '%' must match literally, not as a wildcard.
    hits = await store.search_grep("100%")
    assert [h["full_name"] for h in hits] == ["svcA__search"]


@pytest.mark.asyncio
async def test_search_semantic_cosine_ordering(store):
    await store.sync_server("svcA", [_tool("search", "github api")])
    await store.sync_server("svcB", [_tool("list", "todo list")])
    # Deliberately give svcB__list a different dim to exercise the dim guard.
    await store.replace_embedding("svcA__search", [1.0, 0.0, 0.0], "test-model")
    await store.replace_embedding("svcB__list", [0.0, 1.0, 0.0, 1.0], "test-model")

    hits = await store.search_semantic([1.0, 0.5, 0.0])
    assert [h["full_name"] for h in hits] == ["svcA__search"]
    assert "distance" in hits[0]


@pytest.mark.asyncio
async def test_semantic_server_filter(store):
    await store.sync_server("svcA", [_tool("search")])
    await store.sync_server("svcB", [_tool("search")])
    await store.replace_embedding("svcA__search", [1.0, 0.0], "test-model")
    await store.replace_embedding("svcB__search", [1.0, 0.0], "test-model")
    hits = await store.search_semantic([1.0, 0.0], server="svcB")
    assert [h["full_name"] for h in hits] == ["svcB__search"]


@pytest.mark.asyncio
async def test_drainer_contract(store):
    await store.sync_server("svcA", [_tool("search", "github api"), _tool("list")])
    assert await store.count_unembedded() == 2
    docs = await store.get_unembedded_docs()
    assert len(docs) == 2
    assert {d["doc_id"] for d in docs} == {"svcA__search", "svcA__list"}
    texts = {d["text"] for d in docs}
    assert any(t.startswith("svcA search\n") for t in texts)

    await store.replace_embedding("svcA__search", [1.0, 0.0], "test-model")
    assert await store.count_unembedded() == 1
    assert await store.count_embedded() == 1

    assert await store.drop_embeddings() == 1
    assert await store.count_unembedded() == 2


@pytest.mark.asyncio
async def test_meta_roundtrip(store):
    assert await store.get_meta("embedding_model") is None
    await store.set_meta("embedding_model", "api:bge-m3")
    assert await store.get_meta("embedding_model") == "api:bge-m3"


def test_serialize_roundtrip():
    vec = [0.1, 0.2, 0.3]
    # f32 storage — compare with tolerance for pack/unpack precision loss.
    assert _deserialize_f32(_serialize_f32(vec)) == pytest.approx(vec, abs=1e-6)


def test_cosine_distance():
    assert _cosine_distance([1.0, 0.0], [1.0, 0.0]) == 0.0
    assert _cosine_distance([1.0, 0.0], [-1.0, 0.0]) == pytest.approx(2.0)
    assert _cosine_distance([1.0, 0.0], [0.0, 1.0]) == pytest.approx(1.0)
