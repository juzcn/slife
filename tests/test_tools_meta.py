"""Meta tool tests — tool_search / func-tool-load / _unload_func_tool /
mcp_tool_load delegation."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

from slife.tools.base import Tool
from slife.tools.catalog import CatalogStore
from slife.tools.catalog_service import ToolCatalogService
from slife.tools.mcp import McpToolLoadTool
from slife.tools.meta_tools import (
    FuncToolLoadTool,
    ToolSearchTool,
    UnloadFuncTool,
)
from slife.tools.registry import ToolRegistry


class _NativeA(Tool):
    name = "native_a"
    description = "a native tool"
    parameters = {"type": "object", "properties": {"q": {"type": "string"}}, "required": []}
    category = "System"

    async def execute(self, **kwargs) -> str:
        return "ok-native_a"


class _SemanticStub:
    """A semantic query surface — the three calls ``tool_search`` makes.

    Both real implementations (the drainer's ``SemanticManager`` and the
    drainer-less ``SemanticReader``) answer exactly these, which is what lets
    the hybrid leg stay branch-free; the unit tests that only care about
    scoring use this instead of standing up an endpoint.
    """

    def __init__(self, vector, *, ready=True, reason=""):
        self._vector = vector
        self._ready = ready
        self.reason = reason

    async def query_ready(self) -> bool:
        return self._ready

    async def embed_query(self, text):
        return self._vector


@pytest_asyncio.fixture
async def db(tmp_path):
    s = CatalogStore(tmp_path / "tools.db")
    await s.open()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def ctx(db):
    svc = ToolCatalogService(db, write_owner=True)
    await svc.sync_system_tools([_NativeA()])
    registry = ToolRegistry()
    registry.register(_NativeA())
    registry.set_catalog(svc)
    # a rectangular fake mcp client — create_proxy_tools only touches its
    # minimal protocol at construction.
    fake_mcp = SimpleNamespace(name="mcp", call_tool=__import__("asyncio").sleep)
    return SimpleNamespace(catalog=svc, registry=registry, mcp_client=fake_mcp)


# ── tool_search ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_tool_search_filters_by_columns(db, ctx):
    await db.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA", load_status="unloaded",
        description="full-text search",
        schema=json.dumps({"name": "search", "description": "find things",
                            "inputSchema": {"type": "object", "properties": {}}}),
    )
    await db.upsert_tool("cli-foo", category="cli", description="foo cli")
    await db.upsert_tool("skill-xyz", category="skill", description="a skill")

    tool = ToolSearchTool()
    object.__setattr__(tool, "_ctx", ctx)

    # all categories, keyword query
    payload = json.loads(await tool.execute(query="search"))
    assert payload["count"] == 1
    assert payload["results"][0]["category"] == "mcp"
    assert payload["results"][0]["status"] == "unloaded"

    # category filter across rows (keyword search matches the row's own text)
    payload = json.loads(await tool.execute(query="xyz", category="skill"))
    assert [r["name"] for r in payload["results"]] == ["skill-xyz"]

    # load_status: the column, asked for directly, and read BOTH ways.  The
    # query has to match something — the legs are FTS/semantic, so an empty
    # query is not "browse all".
    payload = json.loads(await tool.execute(query="native", load_status="unloaded"))
    assert [r["name"] for r in payload["results"]] == ["native_a"]
    payload = json.loads(await tool.execute(query="native", load_status="loaded"))
    assert payload["count"] == 0


@pytest.mark.asyncio
async def test_tool_search_grep_mode_and_effective_status(db, ctx):
    await db.upsert_tool("grepme", category="builtin", status="disabled", description="zzz")
    tool = ToolSearchTool()
    object.__setattr__(tool, "_ctx", ctx)
    payload = json.loads(await tool.execute(query="grepme", mode="grep"))
    assert payload["count"] == 1
    assert payload["results"][0]["status"] == "disabled"


# ── func-tool-load / _unload_func_tool ─────────────────────────────

@pytest.mark.asyncio
async def test_tool_load_and_unload_roundtrip_opts(ctx):
    t_load = FuncToolLoadTool()
    object.__setattr__(t_load, "_ctx", ctx)
    # seeding leaves a native unloaded — the first load flips it
    msg = await t_load.execute(full_name="native_a")
    assert "Loaded" in msg or "already loaded" in msg
    # unload it, then reload
    t_unload = UnloadFuncTool()
    object.__setattr__(t_unload, "_ctx", ctx)
    msg = await t_unload.execute(full_name="native_a")
    assert "Unloaded" in msg
    assert await ctx.catalog.effective_status("native_a") == "unloaded"
    msg = await t_load.execute(full_name="native_a")
    assert "Loaded" in msg or "already loaded" in msg
    assert await ctx.catalog.effective_status("native_a") == "loaded"


@pytest.mark.asyncio
async def test_tool_load_refuses_meta_and_error(db, ctx):
    # The server is down: the verdict is the status column's runtime lane,
    # not a load status.
    await db.upsert_tool("svcA__x", category="mcp", source_id="svcA",
                         load_status="unloaded")
    await db.mark_source_error("svcA")
    t_load = FuncToolLoadTool()
    object.__setattr__(t_load, "_ctx", ctx)
    msg = await t_load.execute(full_name="svcA__x")
    # The server's row is in `error` — the refusal says exactly that, and
    # offers no switch (there is none to offer).
    assert msg == "Error: tool 'svcA__x' cannot be loaded — its status is error."
    msg = await t_load.execute(full_name="_turn_prompt")
    assert msg  # meta unknown? actually _turn_prompt is not in catalog → unknown
    # _unload_func_tool refuses a whitelisted tool
    await ctx.catalog.sync_system_tools([_NativeA(), _turn_prompt_stub()])
    t_unload = UnloadFuncTool()
    object.__setattr__(t_unload, "_ctx", ctx)
    msg = await t_unload.execute(full_name="_turn_prompt")
    assert "whitelisted" in msg


def _turn_prompt_stub():
    class _Stub(Tool):
        name = "_turn_prompt"
        description = "stub"
        parameters = {"type": "object", "properties": {}, "required": []}
        category = "Models"

        async def execute(self, **kwargs) -> str:
            return "pong"
    return _Stub()


# ── mcp_tool_load delegation ───────────────────────────────────────

@pytest.mark.asyncio
async def test_mcp_tool_load_delegates_to_func_tool_load(ctx):
    t = McpToolLoadTool()
    object.__setattr__(t, "_ctx", ctx)
    # native path through the delegation flips status like func-tool-load
    t2 = UnloadFuncTool()
    object.__setattr__(t2, "_ctx", ctx)
    await t2.execute(full_name="native_a")
    msg = await t.execute(full_name="native_a")
    assert "Loaded" in msg or "already loaded" in msg
    assert await ctx.catalog.effective_status("native_a") == "loaded"

@pytest.mark.asyncio
async def test_a_column_filter_is_not_truncated_by_the_candidate_cutoff(db, ctx):
    """The regression the column filters exist for.

    A column filter used to run in Python AFTER the ``limit * 2`` candidate
    fetch, so a filtered search silently under-reported: with 14 error rows out
    of 40, ``limit=10`` returned 7.  A filter that is a real SQL predicate runs
    before the LIMIT, so the count is the count.
    """
    for i in range(40):
        await db.upsert_tool(
            f"report_{i:02d}", category="mcp",
            source_id="down" if i % 3 == 0 else "up",
            description="report generator", load_status="unloaded",
        )
    await db.mark_source_error("down")
    tool = ToolSearchTool()
    object.__setattr__(tool, "_ctx", ctx)

    payload = json.loads(await tool.execute(query="report", status="error", limit=10))
    assert payload["count"] == 10          # a full page of qualifying rows
    assert all(r["status"] == "error" for r in payload["results"])


@pytest.mark.asyncio
async def test_the_status_filter_selects_each_state(db, ctx):
    """``status`` is one string filter over the three states the column has —
    which is why the old ``enabled`` / ``unavailable`` boolean pair is gone:
    it asked the same question in two halves."""
    await db.upsert_tool("flag-on", category="builtin", status="enabled", description="flagtest")
    await db.upsert_tool("flag-off", category="builtin", status="disabled", description="flagtest")
    await db.upsert_tool("flag-bad", category="mcp", source_id="down",
                         description="flagtest", load_status="unloaded")
    await db.mark_source_error("down")
    tool = ToolSearchTool()
    object.__setattr__(tool, "_ctx", ctx)

    names = lambda p: {r["name"] for r in json.loads(p)["results"]}
    assert names(await tool.execute(query="flagtest", status="enabled")) == {"flag-on"}
    assert names(await tool.execute(query="flagtest", status="disabled")) == {"flag-off"}
    assert names(await tool.execute(query="flagtest", status="error")) == {"flag-bad"}
    # No filter at all sees every state.
    assert names(await tool.execute(query="flagtest")) == {"flag-on", "flag-off", "flag-bad"}


class TestScoreBands:
    """A semantic leg always returns its k nearest — so the payload must say
    HOW near.  The catalog was the one hybrid path not using the shared
    scoring contract (``annotate_scores`` + ``SCORE_BAND_HINT``), which left
    "nothing matched" and "the nearest neighbours are unrelated" identical on
    the wire: grep-mode returned 0 for a nonsense query while hybrid returned
    five arbitrary tools with nothing to tell them apart.
    """

    @pytest.mark.asyncio
    async def test_a_weak_match_carries_its_similarity_and_the_band_legend(self, monkeypatch):
        store = AsyncMock()
        store.search_keyword.return_value = []
        # Cosine distance ~1.0 = unrelated; the annotator maps it to ~0.0.
        store.search_semantic.return_value = [{
            "name": "playwright__browser_close", "description": "close the browser",
            "category": "mcp", "source_id": "playwright", "schema": "{}",
            "status": "enabled", "load_status": "unloaded", "distance": 0.99,
        }]
        catalog = MagicMock(store=store)
        tool = ToolSearchTool()
        object.__setattr__(tool, "_ctx", SimpleNamespace(catalog=catalog))
        catalog.semantic_query = _SemanticStub([0.1, 0.2])

        payload = json.loads(await tool.execute(query="zzqx-nonexistent-thing-7788"))
        row = payload["results"][0]
        # The number is what distinguishes this from "no match at all"…
        assert row["similarity"] == 0.01
        # …and the legend is what makes the number readable.
        assert "0.1–0.5 weak" in payload["hint"]

    @pytest.mark.asyncio
    async def test_a_keyword_only_hit_carries_no_similarity(self, db, ctx):
        """A hit the semantic leg did not score gets no number — inventing one
        would be a lie about a match nothing measured."""
        await db.upsert_tool("translate_tool", category="builtin",
                             description="translate text")
        tool = ToolSearchTool()
        object.__setattr__(tool, "_ctx", ctx)
        payload = json.loads(await tool.execute(query="translate"))
        assert "similarity" not in payload["results"][0]
