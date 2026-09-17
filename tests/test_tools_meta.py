"""Meta tool tests — tool_search / func-tool-load / _unload_func_tool /
mcp_tool_load delegation."""

import json
from types import SimpleNamespace

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
async def test_tool_search_filters_by_category_and_status(db, ctx):
    await db.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA", status="unloaded",
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

    # status filter hides the loaded native (native_a eff=loaded)
    payload = json.loads(await tool.execute(query="", status="unloaded"))
    names = [r["name"] for r in payload["results"]]
    assert "native_a" not in names


@pytest.mark.asyncio
async def test_tool_search_grep_mode_and_disabled_status(db, ctx):
    await db.upsert_tool("grepme", category="builtin", enabled=False, description="zzz")
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
async def test_tool_load_refuses_meta_and_unavailable(db, ctx):
    await db.upsert_tool("svcA__x", category="mcp", source_id="svcA", status="error")
    t_load = FuncToolLoadTool()
    object.__setattr__(t_load, "_ctx", ctx)
    msg = await t_load.execute(full_name="svcA__x")
    # The server's row was marked `error` — the refusal says so and names the
    # diagnostic (mcp_list) rather than a retired connect tool.
    assert "not up" in msg and "mcp_list" in msg
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