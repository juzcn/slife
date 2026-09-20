"""Catalog wiring tests — seeded snapshot, loaded projection, A4 hints,
and the worker (subagent) sharing semantics at the service level."""

import json

import pytest
import pytest_asyncio

from slife.tools.base import Tool
from slife.tools.catalog import CatalogStore
from slife.tools.catalog_service import ToolCatalogService
from slife.tools.registry import ToolRegistry
from slife.tools.whitelist import META_WHITELIST


class _Native(Tool):
    name = "native_a"
    description = "a native tool"
    parameters = {"type": "object", "properties": {}, "required": []}
    category = "System"

    async def execute(self, **kwargs) -> str:
        return "ok-native_a"


class _NativeB(Tool):
    name = "native_b"
    description = "another native tool"
    parameters = {"type": "object", "properties": {}, "required": []}
    category = "Execution"

    async def execute(self, **kwargs) -> str:
        return "ok-native_b"


class _NativeC(Tool):
    name = "native_c"
    description = "third native tool"
    parameters = {"type": "object", "properties": {}, "required": []}
    category = "Execution"

    async def execute(self, **kwargs) -> str:
        return "ok-native_c"


class _TurnPromptStub(Tool):
    name = "_turn_prompt"
    description = "stub"
    parameters = {"type": "object", "properties": {}, "required": []}
    category = "Models"

    async def execute(self, **kwargs) -> str:
        return "pong"


@pytest_asyncio.fixture
async def db(tmp_path):
    s = CatalogStore(tmp_path / "tools.db")
    await s.open()
    yield s
    await s.close()


# ── Seed / snapshot ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_seed_defaults_new_rows_unloaded_except_whitelist(db):
    """Discovery alone never injects: a NEW row is unloaded unless it is
    always-loaded (the whitelist) — or marked ``autoload: true``."""
    svc = ToolCatalogService(db, write_owner=True, autoload=("native_b",))
    await svc.sync_system_tools([_Native(), _NativeB(), _TurnPromptStub()])

    loaded = set(await db.loaded_names())
    assert loaded == {"native_b", "_turn_prompt"}   # the autoload + the whitelist
    snap = await svc.snapshot_loaded()
    assert "native_b" in snap
    assert "native_a" not in snap                        # not loaded by seeding
    assert META_WHITELIST <= snap                        # always injectable


@pytest.mark.asyncio
async def test_seed_preserves_user_unload_across_re_seed(db):
    svc = ToolCatalogService(db, write_owner=True)
    await svc.sync_system_tools([_Native()])
    ok, _ = await svc.load_tool("native_a")              # the model loads it
    assert ok
    await svc.unload_tool("native_a")                    # …and unloads it again
    # A later seed re-upserts the row but must NOT reset the state: the sync
    # mirrors WHICH tools are registered, never what the model decided.
    await svc.sync_system_tools([_Native()])
    assert await db.get_effective("native_a") == "unloaded"


@pytest.mark.asyncio
async def test_autoload_entry_overrides_the_db_status(db):
    """``autoload: true`` is a CONFIG decision, not just a new-row default:
    the entry says "this tool stays loaded", so a sync puts it back — the one
    place a mirror may overwrite what the model decided."""
    svc = ToolCatalogService(db, write_owner=True, autoload=("native_b",))
    await svc.sync_system_tools([_Native(), _NativeB()])
    assert await db.get_effective("native_b") == "loaded"

    await svc.unload_tool("native_b")                    # the model unloads it
    assert await db.get_effective("native_b") == "unloaded"
    await svc.sync_system_tools([_Native(), _NativeB()])  # config wins again
    assert await db.get_effective("native_b") == "loaded"
    # …and the row carries a recency, so a later sync (or a dropped autoload)
    # does not leave it as the oldest thing in the table.
    assert (await db.get_tool("native_b"))["last_loaded"] is not None


@pytest.mark.asyncio
async def test_autoload_override_writes_nothing_when_already_loaded(db):
    """An override that overrides nothing is not a write — the same rule the
    rest of the sync follows (``tool_au`` re-indexes on any UPDATE)."""
    svc = ToolCatalogService(db, write_owner=True, autoload=("native_b",))
    await svc.sync_system_tools([_Native(), _NativeB()])
    stored = await db.get_tool("native_b")

    # The very same row again — nothing moved, so nothing is written.
    result = await db.reconcile([{
        "name": "native_b", "description": stored["description"],
        "category": "builtin", "schema": stored["schema"],
        "load_status": "loaded", "override_status": True,
    }])
    assert result["skipped"] == 1
    assert (await db.get_tool("native_b"))["last_loaded"] == stored["last_loaded"]


@pytest.mark.asyncio
async def test_a_server_autoload_covers_its_whole_tool_set(db):
    """mcp/rest-api have no per-tool autoload: the flag is on the SERVER entry,
    so one decision owns every row of that server."""
    svc = ToolCatalogService(db, write_owner=True, autoload_servers=("svcA",))
    tools = [{"name": "x", "description": "d",
              "inputSchema": {"type": "object", "properties": {}}}]
    await svc.mirror_external_tools("svcA", tools, category="mcp")
    assert await db.get_effective("svcA__x") == "loaded"

    await svc.unload_tool("svcA__x")
    await svc.mirror_external_tools("svcA", tools, category="mcp")
    assert await db.get_effective("svcA__x") == "loaded"

    # A server without the flag keeps the model's decision.
    await svc.mirror_external_tools("svcB", tools, category="mcp")
    await svc.unload_tool("svcB__x")
    await svc.mirror_external_tools("svcB", tools, category="mcp")
    assert await db.get_effective("svcB__x") == "unloaded"


@pytest.mark.asyncio
async def test_skill_and_cli_rows_have_no_status_to_override(db):
    """Neither family has a load state, so an ``autoload`` flag on one has
    nothing to own: the column stays ``'n/a'`` either way."""
    svc = ToolCatalogService(db, write_owner=True, autoload=("a-skill",))
    await svc.sync_category("skill", {"a-skill": {"description": "d", "schema": "S"}})
    # The row name is namespaced — a skill and a cli may share a bare name.
    row = await db.get_tool("skill:a-skill")
    assert row["load_status"] == "n/a" and row["category"] == "skill"


# ── Load / unload matrix ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_load_unload_refusal_matrix(db):
    svc = ToolCatalogService(db, write_owner=True)
    # unknown
    ok, msg = await svc.load_tool("nope")
    assert not ok and "unknown" in msg
    ok, msg = await svc.unload_tool("nope")
    assert not ok and "unknown" in msg

    await svc.sync_system_tools([_Native()])
    # whitelist refuse on unload
    ok, msg = await svc.unload_tool("_turn_prompt")
    assert not ok and "whitelisted" in msg
    # skill/cli refuse
    await db.upsert_tool("skill-xyz", category="skill")
    ok, msg = await svc.load_tool("skill-xyz")
    assert not ok and "no load/unload state" in msg
    # loaded by the model (seeding left it unloaded)
    ok, msg = await svc.load_tool("native_a")
    assert ok and "Loaded" in msg
    ok, msg = await svc.load_tool("native_a")
    assert ok and "already loaded" in msg
    # unload → reload
    ok, _ = await svc.unload_tool("native_a")
    assert ok
    ok, msg = await svc.unload_tool("native_a")
    assert ok and "already unloaded" in msg
    ok, msg = await svc.load_tool("native_a")
    assert ok


@pytest.mark.asyncio
async def test_load_refuses_disabled_and_error(db):
    svc = ToolCatalogService(db, write_owner=True)
    await db.upsert_tool("native_dis", category="builtin", status="disabled",
                        load_status="unloaded")
    ok, msg = await svc.load_tool("native_dis")
    assert not ok and "disabled" in msg

    # An external tool whose server is down reads `error` — the runtime's own
    # lane of the status column, so the row keeps what the model decided.
    await db.upsert_tool("svcA__x", category="mcp", source_id="svcA", load_status="loaded")
    await db.mark_source_error("svcA")
    ok, msg = await svc.load_tool("svcA__x")
    # One line, and no switch offered: `error` is not a state enable can fix.
    assert not ok and msg.endswith("its status is error.")

    await db.upsert_tool("svcB__x", category="mcp", source_id="svcB", load_status="unloaded")
    ok, msg = await svc.load_tool("svcB__x")
    assert ok


# ── Injection schema reads the CATALOG db (not tool code) ───────────────


@pytest.mark.asyncio
async def test_injected_schema_comes_from_catalog_not_instance(db):
    """The loop builds the LLM tool list from the catalog's ``schema`` column
    — a db row edit is what the model sees, not the instance's own schema."""
    import json
    from types import SimpleNamespace

    from slife.agent.loop import AgentLoop, _function_from_schema
    from slife.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(_Native())  # instance parameters = {} (empty)
    svc = ToolCatalogService(db, write_owner=True)
    await svc.sync_system_tools([_Native()])

    # Overwrite the DB row's schema with a DIFFERENT descriptor.
    await db.upsert_tool(
        "native_a", category="builtin", status="enabled", load_status="loaded",
        schema=json.dumps({"name": "native_a", "description": "a native tool",
                            "inputSchema": {"type": "object",
                                            "properties": {"q": {"type": "string"}}}}),
    )

    loop = SimpleNamespace(
        tool_catalog=svc,
        tool_registry=registry,
        _turn_snapshot=frozenset({"native_a"}),
    )
    tools = await AgentLoop._tools_for_request(loop)
    fn = next(f for f in tools if f["function"]["name"] == "native_a")
    # the injected schema has the db's `q` param — NOT the instance's empty {}
    assert "q" in fn["function"]["parameters"]["properties"]

    # the pure mapper: absent/garbage schema → None (caller falls back)
    assert _function_from_schema("x", "") is None
    assert _function_from_schema("x", "{not json") is None
    ok = _function_from_schema("x", '{"name":"x","description":"d","inputSchema":{"type":"object"}}')
    assert ok["function"]["name"] == "x" and ok["function"]["parameters"] == {"type": "object"}

    # T1b: the passed REGISTRY name wins over a bare descriptor name — an
    # external mcp/rest-api row stores {name: <bare server tool>} while the
    # injected function MUST advertise the full {server}__{tool} key the
    # registry resolves (a bare name made every external call Unknown tool).
    ext = _function_from_schema(
        "svcA__search",
        '{"name":"search","description":"d",'
        '"inputSchema":{"type":"object","properties":{}}}',
    )
    assert ext["function"]["name"] == "svcA__search"


# ── Eviction policy ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_autoload_server_tools_are_born_loaded_and_protected(db):
    """A server entry's ``autoload: true`` covers tools whose names the config
    cannot know: its mirrored rows land loaded, and eviction leaves them be."""
    svc = ToolCatalogService(
        db, threshold=1, write_owner=True, autoload_servers=("eager",),
    )
    await svc.upsert_external_tool("eager__search", server="eager",
                                   description="search",
                                   schema=json.dumps({"name": "search", "description": "search", "inputSchema": {"type": "object", "properties": {}}}))
    await svc.upsert_external_tool("lazy__search", server="lazy",
                                   description="search",
                                   schema=json.dumps({"name": "search", "description": "search", "inputSchema": {"type": "object", "properties": {}}}))
    await svc.sync_system_tools([_Native()])

    assert await db.get_effective("eager__search") == "loaded"
    assert await db.get_effective("lazy__search") == "unloaded"

    # threshold 1 is far exceeded; the autoloaded server's tool is not a victim
    ok, _ = await svc.load_tool("native_a")
    assert ok
    evicted = await svc.evict_to_threshold()
    assert "eager__search" not in evicted


@pytest.mark.asyncio
async def test_evict_to_threshold_respects_whitelist_and_owner(db):
    svc = ToolCatalogService(
        db, threshold=2, write_owner=True, autoload=("native_a",),
    )
    await svc.sync_system_tools([_Native(), _NativeB(), _NativeC()])
    for name in ("native_b", "native_c"):        # the model loads the rest
        ok, _ = await svc.load_tool(name)
        assert ok
    assert len(await db.loaded_names()) == 3

    evicted = await svc.evict_to_threshold()
    assert len(evicted) == 1
    assert not (set(evicted) & META_WHITELIST)
    assert len(await db.loaded_names()) == 2

    # a subagent worker (not the write owner) never squeezes the budget
    svc_worker = ToolCatalogService(db, threshold=1, write_owner=False)
    assert await svc_worker.evict_to_threshold() == []


# ── registry A4 execution hints ────────────────────────────────────

@pytest.mark.asyncio
async def test_registry_execute_hints_with_catalog(db):
    registry = ToolRegistry()
    registry.register(_Native())
    registry.register(_TurnPromptStub())

    svc = ToolCatalogService(db, write_owner=True)
    await svc.sync_system_tools([_Native(), _TurnPromptStub()])
    registry.set_catalog(svc)

    # 1. meta runs even when the gate would object (it stays loaded anyway)
    assert await registry.execute("_turn_prompt") == "pong"
    # 2. in-pool but unloaded → actionable hint, not a silent run
    await svc.unload_tool("native_a")
    assert "not loaded" in await registry.execute("native_a")
    assert "tool_search" in await registry.execute("native_a")
    # 3. catalog-known but not in the pool → different hint
    await db.upsert_tool("svcA__gh", category="mcp", source_id="svcA", load_status="unloaded")
    assert "known but not loaded" in await registry.execute("svcA__gh")
    # 3b. its server is down → the row says `error`, and the gate names that
    # rather than pretending the tool is merely unloaded.
    await db.mark_source_error("svcA")
    assert "status is error" in await registry.execute("svcA__gh")
    # 4. unknown everywhere → historical string
    assert await registry.execute("nope") == "Error: Unknown tool 'nope'"


@pytest.mark.asyncio
async def test_registry_execute_without_catalog_keeps_historical(db):
    registry = ToolRegistry()
    registry.register(_Native())
    assert await registry.execute("nope") == "Error: Unknown tool 'nope'"
    assert await registry.execute("native_a") == "ok-native_a"


# ── Worker shares the same state (subagent = worker, same db) ───────

@pytest.mark.asyncio
async def test_worker_reads_shared_loaded_set_and_can_flip_status(tmp_path):
    path = tmp_path / "tools.db"

    agent_store = CatalogStore(path)
    await agent_store.open()
    agent = ToolCatalogService(agent_store, write_owner=True)
    await agent.sync_system_tools([_Native(), _NativeB()])
    for name in ("native_a", "native_b"):
        ok, _ = await agent.load_tool(name)
        assert ok

    worker_store = CatalogStore(path)  # second process opening the same file
    await worker_store.open()
    worker = ToolCatalogService(worker_store, write_owner=False)

    # worker's snapshot reflects what the agent seeded (inheritance)
    snap = await worker.snapshot_loaded()
    assert "native_a" in snap and "native_b" in snap

    # worker may load/unload (shared mechanism) — but never reseeds/evicts
    ok, _ = await worker.load_tool("native_b")   # flips it in the shared db
    assert ok
    assert await worker.evict_to_threshold() == []  # policy is agent's

    await agent_store.close()
    await worker_store.close()

# ── Non-function row mirror (skill / cli) ──────────────────────────

@pytest.mark.asyncio
async def test_sync_category_mirrors_and_purges(db):
    """skill/cli rows come from their own live sources: upsert every entry,
    purge the vanished one — and no load state, ever."""
    svc = ToolCatalogService(db, write_owner=True)

    purged = await svc.sync_category("skill", {
        "deploy": {"description": "Ship it", "schema": "# Deploy\nrun x", "status": "enabled"},
        "quiet": {"description": "hush", "schema": "# Quiet", "status": "disabled"},
    })
    assert purged == []

    row = await db.get_tool("skill:deploy")
    assert row["category"] == "skill"                     # the category IS the kind
    assert row["load_status"] == "n/a"                    # skills have no load state
    assert "# Deploy" in row["schema"]              # SKILL.md, as stored
    assert (await db.get_tool("skill:quiet"))["status"] == "disabled"
    # discoverable exactly like a function tool, and never injected
    # Searchable by its bare words, stored under its namespaced name.
    assert [r["name"] for r in await db.search_keyword("deploy")] == ["skill:deploy"]
    assert "deploy" not in await svc.snapshot_loaded()

    # a skill that left the dir loses its row (the §8.5 "清理干净" contract)
    purged = await svc.sync_category("skill", {
        "deploy": {"description": "Ship it", "schema": "# Deploy\nrun x", "status": "enabled"},
    })
    assert purged == ["skill:quiet"]      # row names are namespaced
    assert await db.get_tool("skill:quiet") is None
    assert await db.get_tool("skill:deploy") is not None


@pytest.mark.asyncio
async def test_sync_category_cli_rows_have_no_schema(db):
    svc = ToolCatalogService(db, write_owner=True)
    await svc.sync_category("cli", {"gh": {"description": "GitHub CLI", "schema": None}})

    row = await db.get_tool("cli:gh")
    assert row["category"] == "cli"
    assert row["load_status"] == "n/a"
    # "No schema text" is a VALUE in a NOT NULL column, not an absence — the
    # drainer and the search both read it as the sentinel rather than NULL.
    assert row["schema"] == "n/a"


@pytest.mark.asyncio
async def test_sync_category_refuses_load_and_unload(db):
    """A skill row is a row, not a function tool: the load verbs refuse it."""
    svc = ToolCatalogService(db, write_owner=True)
    await svc.sync_category("skill", {"deploy": {"description": "Ship it", "schema": "# Deploy"}})

    ok, msg = await svc.load_tool("skill:deploy")
    assert not ok and "no load/unload state" in msg and "skill" in msg
    ok, msg = await svc.unload_tool("skill:deploy")
    assert not ok and "no load/unload state" in msg
