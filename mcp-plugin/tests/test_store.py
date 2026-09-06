"""ToolStore unit tests — catalog sync, search, semantic cosine, drainer."""

import json

import pytest
import pytest_asyncio

from mcp_plugin.store import ToolStore, _cosine_distance, _deserialize_f32, _serialize_f32


@pytest_asyncio.fixture
async def store():
    s = ToolStore()
    await s.open()
    yield s
    await s.close()


def _tool(name, description="", schema=None):
    tool = {"name": name, "description": description}
    if schema is not None:
        tool["inputSchema"] = schema
    return tool


@pytest.mark.asyncio
async def test_sync_server_upserts_tools_and_server_meta(store):
    await store.sync_server("svcA", [_tool("search", "full-text search"), _tool("fetch")])
    assert await store.count_by_server("svcA") == 2
    assert await store.get_tool("svcA__search") is not None
    srv = await store.get_server("svcA")
    assert srv["enabled"] == 1 and srv["auto_load"] == 0

    # Per-mcp: a disabled server stays disabled across a re-sync (enabled is
    # preserved); auto_load is refreshed from the connection config.
    await store.set_server_enabled("svcA", False)
    await store.sync_server(
        "svcA", [_tool("search", "updated description"), _tool("fetch")],
        auto_load=True,
    )
    srv = await store.get_server("svcA")
    assert srv["enabled"] == 0
    assert srv["auto_load"] == 1
    assert (await store.get_tool("svcA__search"))["description"] == "updated description"


@pytest.mark.asyncio
async def test_sync_server_invalidates_stale_embedding(store):
    # The semantic vector is sourced from the full tool-descriptor column
    # ({name, description, inputSchema}) — ANY change to that text drops the
    # tool's old vector (a stale vector would otherwise keep matching forever,
    # since count_unembedded only sees rows with no embedding row).
    s1 = {"type": "object", "properties": {"repo": {"type": "string", "description": "repo name"}}}
    s2 = {"type": "object", "properties": {"user": {"type": "string", "description": "user name"}}}
    await store.sync_server("svcA", [_tool("search", "full-text search", schema=s1)])
    await store.replace_embedding("svcA__search", [0.1, 0.2, 0.3], "api:bge-m3")
    assert await store.count_unembedded() == 0

    # description edit → part of the descriptor text → stale vector dropped
    await store.sync_server("svcA", [_tool("search", "semantic vector search", schema=s1)])
    assert await store.count_unembedded() == 1

    # schema edit → stale vector dropped too
    await store.replace_embedding("svcA__search", [0.1, 0.2, 0.3], "api:bge-m3")
    await store.sync_server("svcA", [_tool("search", "semantic vector search", schema=s2)])
    assert await store.count_unembedded() == 1


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

    rows = await store.list_tools_by_server("svcA")
    assert [r["name"] for r in rows] == ["search", "fetch"]
    search = {r["name"]: r for r in rows}["search"]
    assert search["full_name"] == "svcA__search"
    assert search["description"] == "find files"
    assert "enabled" not in search  # per-mcp only — no per-tool state
    assert await store.list_tools_by_server("svcB") == [
        {"full_name": "svcB__list", "server": "svcB", "name": "list",
         "description": "",
         "input_schema": json.dumps({"name": "list", "description": ""},
                                    ensure_ascii=False, separators=(",", ":"))},
    ]
    assert await store.list_tools_by_server("svcNope") == []


@pytest.mark.asyncio
async def test_sync_server_persists_full_schema(store):
    schema = {"type": "object", "properties": {"repo": {"type": "string"}}}
    await store.sync_server("svcA", [_tool("search", "find repos", schema=schema)])
    row = await store.get_tool("svcA__search")
    # The column holds the COMPLETE tools/list descriptor — name, description
    # and inputSchema, not just the inputSchema sub-object.
    assert json.loads(row["input_schema"]) == {
        "name": "search", "description": "find repos", "inputSchema": schema,
    }
    rows = await store.list_tools_by_server("svcA")
    assert json.loads(rows[0]["input_schema"]) == {
        "name": "search", "description": "find repos", "inputSchema": schema,
    }


@pytest.mark.asyncio
async def test_set_server_enabled(store):
    await store.sync_server("svcA", [_tool("search"), _tool("fetch")])
    await store.sync_server("svcB", [_tool("list")])
    assert await store.set_server_enabled("svcA", False) == 1
    assert (await store.get_server("svcA"))["enabled"] == 0
    assert (await store.get_server("svcB"))["enabled"] == 1
    assert await store.set_server_enabled("svcA", True) == 1
    assert (await store.get_server("svcA"))["enabled"] == 1
    # Unknown server → 0 rows changed, no server row.
    assert await store.set_server_enabled("nope", True) == 0
    assert await store.get_server("nope") is None


@pytest.mark.asyncio
async def test_search_hides_disabled_and_auto_load_servers(store):
    # Per-mcp visibility: search only surfaces tools of enabled, non-auto_load
    # servers — auto_load tools are already in the toolset, disabled servers
    # cannot be loaded.
    await store.sync_server("svcA", [_tool("search", "find files")])
    await store.sync_server("svcB", [_tool("search", "find files")])
    await store.sync_server("svcC", [_tool("search", "find files")], auto_load=True)

    hits = await store.search_keyword("find", server="svcB")
    assert [h["full_name"] for h in hits] == ["svcB__search"]

    hits = await store.search_keyword("find")
    assert {h["full_name"] for h in hits} == {"svcA__search", "svcB__search"}

    # Auto_load server's tools are never discoverable.
    assert await store.search_keyword("find", server="svcC") == []
    # Disabled server's tools disappear from discovery too.
    await store.set_server_enabled("svcA", False)
    hits = await store.search_keyword("find")
    assert [h["full_name"] for h in hits] == ["svcB__search"]


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
async def test_search_grep_escapes(store):
    await store.sync_server("svcA", [_tool("search", "100% match")])
    await store.sync_server("svcB", [_tool("search", "100 matches")])
    # '%' must match literally, not as a wildcard.
    hits = await store.search_grep("100%")
    assert [h["full_name"] for h in hits] == ["svcA__search"]


@pytest.mark.asyncio
async def test_search_keyword_matches_schema_content(store):
    # The schema column is FTS5-indexed — a query hitting only a parameter name
    # or its description inside the schema surfaces the tool (bm25 weights put
    # input_schema last, but exact param terms still match).
    schema = {"type": "object", "properties": {
        "startTime": {"type": "string", "description": "log window start, ISO-8601"},
    }}
    await store.sync_server("svcA", [_tool("search", "generic file tool", schema=schema)])
    hits = await store.search_keyword("startTime")
    assert [h["full_name"] for h in hits] == ["svcA__search"]
    hits = await store.search_keyword("window")
    assert [h["full_name"] for h in hits] == ["svcA__search"]
    hits = await store.search_grep("startTime")
    assert [h["full_name"] for h in hits] == ["svcA__search"]


@pytest.mark.asyncio
async def test_search_keyword_cjk_matches_schema(store):
    # CJK query → LIKE fallback, which now also scans the schema column.
    schema = {"type": "object", "properties": {
        "路径": {"type": "string", "description": "要读取的文件路径"},
    }}
    await store.sync_server("svcA", [_tool("search", "generic tool", schema=schema)])
    hits = await store.search_keyword("文件")
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
    schema = {"type": "object", "properties": {"repo": {"type": "string"}}}
    await store.sync_server("svcA", [
        _tool("search", "github api", schema=schema),
        _tool("list"),  # no inputSchema — the descriptor still has name/desc
    ])
    assert await store.count_unembedded() == 2
    docs = await store.get_unembedded_docs()
    assert {d["doc_id"] for d in docs} == {"svcA__search", "svcA__list"}
    by_id = {d["doc_id"]: d["text"] for d in docs}
    assert "name: search" in by_id["svcA__search"]
    assert "github api" in by_id["svcA__search"]
    assert "params: repo (string)" in by_id["svcA__search"]
    assert by_id["svcA__list"] == "name: list"

    await store.replace_embedding("svcA__search", [1.0, 0.0], "test-model")
    assert await store.count_unembedded() == 1
    assert await store.count_embedded() == 1

    assert await store.drop_embeddings() == 1
    assert await store.count_unembedded() == 2


@pytest.mark.asyncio
async def test_semantic_doc_text_is_full_descriptor(store):
    # The embedding vector is sourced from the input_schema column ALONE,
    # which stores the complete tools/list descriptor — the tools row's
    # description arrives via the schema text (name + description + params).
    schema = {
        "type": "object",
        "properties": {
            "repo": {"type": "string", "description": "repository name"},
            "filters": {"type": "object",
                        "properties": {"archived": {"type": "boolean"}}},
        },
        "required": ["repo"],
    }
    await store.sync_server("svcA", [_tool("search", "Search repositories on GitHub", schema=schema)])
    docs = await store.get_unembedded_docs()
    assert len(docs) == 1
    text = docs[0]["text"]
    assert "name: search" in text
    assert "Search repositories on GitHub" in text
    assert "repo (string, required): repository name" in text
    assert "filters (object): archived (boolean)" in text
    assert "name: search" in text.splitlines()[0]  # name-first, then description


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
