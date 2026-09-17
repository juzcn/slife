"""CatalogStore unit tests — the single shared tools.db (schema, effective
status, LRU, search, drainer contract, WAL cross-process pragmas)."""

import asyncio

import pytest
import pytest_asyncio

from slife.tools.catalog import (
    SCHEMA_VERSION,
    CatalogStore,
    EFF_DISABLED,
    EFF_ERROR,
    EFF_NA,
    TYPE_CLI,
    TYPE_FUNC,
    TYPE_SKILL,
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
async def test_upsert_tool_and_effective_truth_table(store):
    # An external tool's effective status is its OWN status — a connected
    # server's row is `loaded` until something marks it otherwise.
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "full-text search", None),
        status="loaded",
    )
    # …and an unusable server's rows carry `error` (written by the reconcile,
    # never derived: there is no server table left to join).
    await store.upsert_tool(
        "svcC__ping", category="mcp", source_id="svcC", status="loaded",
    )
    await store.mark_source_error("svcC")
    # builtin disabled by config → DISABLED; enabled → loaded/unloaded
    await store.upsert_tool("native_a", category="builtin", enabled=False, status="loaded")
    await store.upsert_tool("native_b", category="builtin", enabled=True, status="unloaded")
    # skill/cli → n/a (no load state)
    await store.upsert_tool("skill-xyz", category="skill")
    await store.upsert_tool("cli-foo", category="cli")

    eff = {r["name"]: r["eff"] for r in await store.scan_effective()}
    assert eff["svcA__search"] == "loaded"
    assert eff["svcC__ping"] == EFF_ERROR
    assert eff["native_a"] == EFF_DISABLED
    assert eff["native_b"] == "unloaded"
    assert eff["skill-xyz"] == EFF_NA
    assert eff["cli-foo"] == EFF_NA

    # loaded_names() yields exactly the loaded rows that are not config-disabled
    # (native_a is disabled despite status='loaded'; svcC__ping is `error`)
    assert set(await store.loaded_names()) == {"svcA__search"}
    assert await store.count_loaded() == 1


@pytest.mark.asyncio
async def test_mark_and_reset_source_error(store):
    """The connect/disconnect cycle as the store sees it."""
    for name, status in (("svcA__x", "loaded"), ("svcA__y", "unloaded")):
        await store.upsert_tool(name, category="mcp", source_id="svcA", status=status)

    marked = await store.mark_source_error("svcA")
    assert marked == 2
    assert await store.loaded_names() == []

    # A reconnect clears the mark ONLY on error rows — the unloaded one keeps
    # its state, and a user-loaded row would keep `loaded`.
    cleared = await store.reset_source_status("svcA", "unloaded")
    assert cleared == 2
    assert (await store.get_tool("svcA__y"))["status"] == "unloaded"

    # A row the user loaded keeps its state across a blip.
    await store.set_status("svcA__x", "loaded")
    await store.mark_source_error("svcA")
    assert await store.loaded_names() == []
    await store.reset_source_status("svcA", "unloaded")
    assert (await store.get_tool("svcA__x"))["status"] == "unloaded"   # was error → reset


@pytest.mark.asyncio
async def test_mark_all_external_error_spares_local_rows(store):
    await store.upsert_tool("svcA__x", category="mcp", source_id="svcA", status="loaded")
    await store.upsert_tool("native", category="builtin", enabled=True, status="loaded")

    marked = await store.mark_all_external_error()

    assert marked == 1
    assert (await store.get_tool("svcA__x"))["status"] == "error"
    assert (await store.get_tool("native"))["status"] == "loaded"
    assert set(await store.loaded_names()) == {"native"}


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


# ── Reconcile: the boot/mirror delta contract ───────────────────────
#
# The sync must write the DELTA and nothing else.  `total_changes` is the
# probe: it counts row changes on this connection, so a steady-state
# reconcile that touches no row leaves it flat — which is the whole point
# (the old unconditional upsert rewrote every row AND fired tool_au, which
# re-indexed every row into tool_fts on every boot).

def _row(name, *, description="", category="builtin", schema=None,
         source_id=None, enabled=None, status=None):
    return {
        "name": name, "description": description, "category": category,
        "source_id": source_id, "schema": schema, "enabled": enabled,
        "status": status,
    }


@pytest.mark.asyncio
async def test_reconcile_noop_writes_nothing(store):
    rows = [
        _row("native_a", description="A", schema="schema-a", enabled=True,
             status="unloaded"),
        _row("native_b", description="B", schema="schema-b", enabled=True,
             status="unloaded"),
    ]
    first = await store.reconcile(rows)
    assert sorted(first["inserted"]) == ["native_a", "native_b"]

    before = store._c.total_changes
    second = await store.reconcile(rows)
    assert store._c.total_changes == before
    assert second["inserted"] == [] and second["updated"] == []
    assert second["skipped"] == 2


@pytest.mark.asyncio
async def test_reconcile_writes_only_the_column_that_moved(store):
    """An enabled-only flip must not rewrite the schema blob — so the FTS
    update trigger does not fire — and must not invalidate the embedding."""
    await store.reconcile([_row("native_a", description="A", schema="schema-a",
                                enabled=True)])
    await _set_embedding(store, "native_a", [0.1, 0.2])

    result = await store.reconcile([_row("native_a", description="A",
                                         schema="schema-a", enabled=False)])
    assert result["updated"] == ["native_a"]
    assert result["schema_changed"] == []
    assert await store.count_unembedded() == 0
    assert (await store.get_tool("native_a"))["enabled"] == 0


@pytest.mark.asyncio
async def test_reconcile_invalidates_only_the_tools_whose_schema_moved(store):
    await store.reconcile([_row("a", schema="schema-a"), _row("b", schema="schema-b")])
    await _set_embedding(store, "a", [0.1, 0.2])
    await _set_embedding(store, "b", [0.3, 0.4])

    result = await store.reconcile([_row("a", schema="schema-a2"),
                                    _row("b", schema="schema-b")])
    assert result["schema_changed"] == ["a"]
    assert await store.count_unembedded() == 1
    assert await store.count_embedded() == 1


@pytest.mark.asyncio
async def test_reconcile_invalidates_on_the_embedded_text_not_the_raw_schema(store):
    """The drainer embeds ``_flatten_schema(schema)``, which keeps only name,
    description, each param's type/required/description and a return line.

    So the invalidation comparator must be that text, not the raw schema
    column: a change only in a field the flattener drops (enum, default,
    nesting past one level) otherwise deleted the vectors and re-embedded to a
    byte-identical vector."""
    base = {"type": "object", "properties": {"repo": {"type": "string"}}}
    await store.reconcile([_row("svc__t", category="mcp", source_id="svc",
                                schema=_descriptor("t", "search repos", base))])
    await _set_embedding(store, "svc__t", [0.1, 0.2])

    # ``enum`` is dropped by the flattener → the embedded text is unchanged
    with_enum = {"type": "object", "properties": {
        "repo": {"type": "string", "enum": ["a", "b"]}}}
    dropped = await store.reconcile([_row(
        "svc__t", category="mcp", source_id="svc",
        schema=_descriptor("t", "search repos", with_enum))])
    assert dropped["updated"] == ["svc__t"]        # the row (and FTS) moved…
    assert dropped["schema_changed"] == []         # …the embedding did not
    assert await store.count_unembedded() == 0

    # a param description IS part of the embedded text → invalidated
    described = {"type": "object", "properties": {
        "repo": {"type": "string", "description": "the repository"}}}
    moved = await store.reconcile([_row(
        "svc__t", category="mcp", source_id="svc",
        schema=_descriptor("t", "search repos", described))])
    assert moved["schema_changed"] == ["svc__t"]
    assert await store.count_unembedded() == 1


@pytest.mark.asyncio
async def test_reconcile_enabled_none_is_no_opinion_and_status_survives(store):
    """`enabled=None` leaves the column alone (the mcp/rest-api contract),
    and a re-reconcile never clobbers the model's load state."""
    await store.reconcile([_row("svc__t", category="mcp", source_id="svc",
                                schema="s", enabled=None, status="loaded")])
    row = await store.get_tool("svc__t")
    assert row["enabled"] is None and row["status"] == "loaded"

    # An explicit value DOES land, even over a NULL column — the behaviour the
    # old COALESCE(excluded.enabled, tool.enabled) provided.
    result = await store.reconcile([_row("svc__t", category="mcp", source_id="svc",
                                         schema="s", enabled=False, status="unloaded")])
    assert result["updated"] == ["svc__t"]
    row = await store.get_tool("svc__t")
    assert row["enabled"] == 0
    assert row["status"] == "loaded"      # untouched by an update


@pytest.mark.asyncio
async def test_reconcile_purge_is_scoped_to_its_category(store):
    await store.reconcile([_row("keep", category="skill", schema="s")],
                          category="skill", purge=True)
    await store.reconcile([_row("other", category="cli")],
                          category="cli", purge=True)
    await _set_embedding(store, "keep", [0.1, 0.2])

    result = await store.reconcile([], category="skill", purge=True)
    assert result["purged"] == ["keep"]
    assert await store.get_tool("keep") is None
    assert await store.get_tool("other") is not None     # another category
    assert await store.count_embedded() == 0             # vectors went too


@pytest.mark.asyncio
async def test_reconcile_without_purge_keeps_vanished_rows(store):
    """The boot seed runs without purge: a name missing from the registered
    set may simply be an mcp row whose server has not connected yet."""
    await store.reconcile([_row("gone")])
    result = await store.reconcile([])
    assert result["purged"] == []
    assert await store.get_tool("gone") is not None


@pytest.mark.asyncio
async def test_reconcile_ignores_nameless_rows(store):
    result = await store.reconcile([_row(""), _row("real", schema="s")])
    assert result["inserted"] == ["real"]


@pytest.mark.asyncio
async def test_purge_source_drops_its_tools(store):
    """A server that left the config owns no rows."""
    await store.upsert_tool("svcA__a", category="mcp", source_id="svcA", status="loaded")
    await store.upsert_tool("svcA__b", category="mcp", source_id="svcA", status="loaded")
    await store.upsert_tool("svcB__c", category="mcp", source_id="svcB", status="loaded")

    assert await store.purge_source("svcA") == 2

    assert await store.get_tool("svcA__a") is None
    assert await store.get_tool("svcB__c") is not None      # other servers untouched
    # FTS row gone too (delete trigger)
    assert await store.search_keyword("svcA__a") == []
    assert await store.list_source_ids() == {"svcB"}


@pytest.mark.asyncio
async def test_purge_missing_sources_keeps_the_configured_set(store):
    for sid in ("keep", "gone"):
        await store.upsert_tool(f"{sid}__x", category="mcp", source_id=sid, status="unloaded")

    purged = await store.purge_missing_sources({"keep"})

    assert purged == {"gone"}
    assert await store.list_source_ids() == {"keep"}


@pytest.mark.asyncio
async def test_migration_drops_the_retired_server_table(tmp_path):
    """An existing db from the previous schema loses `server` on open."""
    import aiosqlite

    path = tmp_path / "tools.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute("CREATE TABLE server (name TEXT PRIMARY KEY, runtime TEXT)")
    await conn.execute("INSERT INTO server(name, runtime) VALUES ('svcA', 'CONNECTED')")
    await conn.execute("PRAGMA user_version = 1")
    await conn.commit()
    await conn.close()

    store = CatalogStore(path)
    await store.open()
    cur = await store._c.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='server'",
    )
    assert await cur.fetchone() is None                    # table is gone
    cur = await store._c.execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == SCHEMA_VERSION
    await store.close()


@pytest.mark.asyncio
async def test_migration_adds_and_backfills_tool_type(tmp_path):
    """A v2 db's rows gain ``type`` on open — derived from ``category``, so an
    old file answers the same questions the new schema does."""
    import aiosqlite

    path = tmp_path / "tools.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(
        """CREATE TABLE tool (
               name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
               category TEXT NOT NULL, source_id TEXT, schema TEXT,
               enabled INTEGER, status TEXT, last_loaded TEXT)""",
    )
    for name, category in (
        ("execute_shell", "builtin"), ("job_x", "job"),
        ("svc__search", "mcp"), ("readme", "skill"), ("mycmd", "cli"),
    ):
        await conn.execute(
            "INSERT INTO tool(name, category) VALUES (?, ?)", (name, category),
        )
    await conn.execute("PRAGMA user_version = 2")
    await conn.commit()
    await conn.close()

    store = CatalogStore(path)
    await store.open()
    types = {
        name: (await store.get_tool(name))["type"]
        for name in ("execute_shell", "job_x", "svc__search", "readme", "mycmd")
    }
    assert types == {
        "execute_shell": TYPE_FUNC, "job_x": TYPE_FUNC, "svc__search": TYPE_FUNC,
        "readme": TYPE_SKILL, "mycmd": TYPE_CLI,
    }
    cur = await store._c.execute("PRAGMA user_version")
    assert (await cur.fetchone())[0] == SCHEMA_VERSION
    await store.close()


@pytest.mark.asyncio
async def test_type_is_derived_from_category(store):
    """``type`` is written wherever a row is, from the category — the two
    columns cannot drift, and a non-function row refuses load/unload."""
    await store.upsert_tool("execute_shell", category="builtin")
    await store.upsert_tool("svc__search", category="mcp", source_id="svc")
    await store.upsert_tool("turn_search", category="plugin", source_id="memdb")
    await store.upsert_tool("readme", category="skill")
    await store.upsert_tool("mycmd", category="cli")

    assert (await store.get_tool("execute_shell"))["type"] == TYPE_FUNC
    assert (await store.get_tool("svc__search"))["type"] == TYPE_FUNC
    # A plugin's own tool is a function tool too — it carries a load state and
    # its plugin as the source.
    plugin_row = await store.get_tool("turn_search")
    assert plugin_row["type"] == TYPE_FUNC
    assert plugin_row["source_id"] == "memdb"
    assert (await store.get_tool("readme"))["type"] == TYPE_SKILL
    assert (await store.get_tool("mycmd"))["type"] == TYPE_CLI
    # A re-upsert under another category moves the type with it.
    await store.upsert_tool("readme", category="builtin")
    assert (await store.get_tool("readme"))["type"] == TYPE_FUNC
    # Search rows carry it too (one row shape for every read path).
    hits = await store.search_keyword("execute_shell")
    assert [h["type"] for h in hits] == [TYPE_FUNC]


# ── The stale-CHECK guard (no in-place migration by policy) ──────────

#: The ``tool`` table as v3 created it: same columns, category CHECK without
#: ``plugin``.  ``CREATE TABLE IF NOT EXISTS`` never touches such a table, and
#: widening a CHECK needs a rebuild the project does not do — so the file is
#: meant to be deleted, and this is the guard that says so.
_V3_TOOL_DDL = """
CREATE TABLE tool (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL
                CHECK (category IN ('builtin','job','mcp','rest-api','skill','cli')),
    type        TEXT NOT NULL DEFAULT 'func'
                CHECK (type IN ('func','skill','cli')),
    source_id   TEXT, schema TEXT, enabled INTEGER, status TEXT, last_loaded TEXT)
"""


def test_category_check_values_reads_only_the_category_clause():
    """The parser is scoped to the category CHECK on purpose: ``'skill'`` and
    ``'cli'`` also appear in the TYPE check, so a whole-statement substring
    test would report a category list missing them as complete."""
    from slife.tools.catalog import _category_check_values

    values = _category_check_values(_V3_TOOL_DDL)
    assert values == {"builtin", "job", "mcp", "rest-api", "skill", "cli"}
    assert "plugin" not in values
    assert _category_check_values("CREATE TABLE tool (name TEXT PRIMARY KEY)") is None


@pytest.mark.asyncio
async def test_stale_category_check_is_reported_not_silently_broken(tmp_path):
    """An old file keeps its old CHECK, so every ``plugin`` write fails — and
    the mirror is best-effort, so the tools would just stay uncatalogued.  The
    open-time probe must say so (the file is deleted, not migrated)."""
    import aiosqlite
    from slife.health import clear, get_report

    clear()
    path = tmp_path / "tools.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(_V3_TOOL_DDL)
    await conn.execute("INSERT INTO tool(name, category) VALUES ('execute_shell', 'builtin')")
    await conn.execute("PRAGMA user_version = 3")
    await conn.commit()
    await conn.close()

    store = CatalogStore(path)
    await store.open()

    entry = next(e for e in get_report() if e.get("component") == "tool_catalog")
    assert entry["level"] == "warning"
    assert entry["value"] == "stale (no plugin category)"
    assert "Delete" in entry["hint"] and str(path) in entry["hint"]
    # …and the reason it matters, verified rather than asserted in prose: the
    # write the plugin mirror would make is refused by the old constraint.
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        await store.upsert_tool("turn_search", category="plugin", source_id="memdb")
    # The pre-existing row is untouched and still readable.
    assert (await store.get_tool("execute_shell"))["category"] == "builtin"
    await store.close()
    clear()


@pytest.mark.asyncio
async def test_remove_tool_deletes_single_row(store):
    """A vanished non-server tool (a removed job, a dropped plugin tool) must
    lose its row — the mirror is upsert-only, so without this it lingers and
    tool_search keeps returning a tool that no longer exists."""
    await store.upsert_tool("job_a", category="job", status="loaded")
    await store.upsert_tool("job_b", category="job", status="loaded")

    await store.remove_tool("job_a")

    assert await store.get_tool("job_a") is None
    assert await store.get_tool("job_b") is not None   # only the one removed
    assert await store.search_keyword("job_a") == []   # FTS trigger fired


# ── LRU eviction ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evict_lru_orders_by_last_loaded_and_skips_protected(store):
    for name in ("t1", "t2", "t3", "t4"):
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
    # a skill row's doc IS its SKILL.md — a playbook is text worth searching
    await store.upsert_tool("skill-xyz", category="skill", schema="# Skill notes")
    # a cli row holds no tool def, so it has nothing to embed
    await store.upsert_tool("cli-foo", category="cli")

    docs = await store.get_unembedded_docs()
    assert [d["doc_id"] for d in docs] == ["skill-xyz", "svcA__search"]
    texts = {d["doc_id"]: d["text"] for d in docs}
    assert texts["skill-xyz"] == "# Skill notes"          # verbatim, not parsed
    assert "search things" in texts["svcA__search"]        # flattened descriptor
    assert "cli-foo" not in texts

    await store.replace_embedding_chunks(docs[0], [[0.1, 0.2]], model="api:bge-m3")
    assert await store.count_unembedded() == 1
    assert await store.count_embedded() == 1
    # meta never written by replace — the SemanticManager writes it on model select
    assert (await store.get_meta("embedding_model")) is None

    # model swap contract: drop_embeddings clears the old vector space
    await _set_embedding(store, "svcA__search", [0.5, 0.6], model="old")
    assert await store.drop_embeddings() == 2
    assert await store.count_embedded() == 0
    assert await store.count_unembedded() == 2  # both need re-embedding


# ── Chunking: one shared chunker for tool schemas too ───────────────

def test_tool_schema_chunks_through_the_shared_chunker():
    """A tool schema is embedded through the SAME chunker as memdb turns and
    memfiles docs, so an oversized schema is hard-split rather than riding as
    one chunk the provider rejects — a rejection that left the tool
    permanently unembedded and the semantic gate locked off."""
    from slife.plugins.memdb.store import (
        _chunk_text, _split_chunks_to_token_limit,
    )

    # _flatten_schema's shape for a big tool: a long, newline-free,
    # escape-dense params line (the worst case for token density).
    huge = "name: big__tool\nA big tool\nparams: " + "; ".join(
        f'p{i}: {{"type":"object","description":"param number {i}"}}'
        for i in range(600)
    )
    chunks = _split_chunks_to_token_limit(_chunk_text(huge), 8192)
    assert len(chunks) > 1                        # split, not kept whole
    assert all(len(c) <= 8192 for c in chunks)    # 1 char/token floor
    assert "param number 599" in "".join(chunks)  # nothing dropped


def test_catalog_semantic_inherits_the_memdb_embed_path():
    """The catalog's SemanticManager must not reimplement embedding — it
    inherits memdb's ``_embed_doc`` (the one chunker), so the tool catalog
    and the memdb/memfiles indexes can never drift apart."""
    from slife.plugins.memdb.semantic import SemanticManager as MemdbSM
    from slife.tools.semantic import SemanticManager as CatalogSM

    assert CatalogSM._embed_doc is MemdbSM._embed_doc


# ── Pragmas / schema version / helper sanity ────────────────────────

@pytest.mark.asyncio
async def test_wal_pragmas_and_user_version(tmp_path):
    store = CatalogStore(tmp_path / "tools.db")
    await store.open()
    cursor = await store._c.execute("PRAGMA journal_mode")
    row = await cursor.fetchone()
    assert row is not None and row[0].lower() == "wal"
    import slife.timeouts as _timeouts
    cursor = await store._c.execute("PRAGMA busy_timeout")
    expected_ms = int(_timeouts.timeouts.storage.sqlite_busy * 1000)
    row = await cursor.fetchone()
    assert row is not None and row[0] == expected_ms
    cursor = await store._c.execute("PRAGMA user_version")
    row = await cursor.fetchone()
    assert row is not None and row[0] == SCHEMA_VERSION
    await store.close()


def test_helper_sanity():
    desc = _descriptor("search", "find repos", None)
    flat = _flatten_schema(desc)
    assert "name: search" in flat and "find repos" in flat
    assert _flatten_schema("") == ""
    # A non-JSON schema is a skill's SKILL.md: the text IS the doc.
    assert _flatten_schema("# just a markdown skill") == "# just a markdown skill"


def test_cosine_and_f32_roundtrip():
    vec = [1.0, 0.0, 0.5]
    blob = __import__("slife.plugins.memdb.store", fromlist=["_serialize_f32"])._serialize_f32(vec)
    assert _deserialize_f32(blob) == vec
    assert _cosine_distance([1, 0, 0], [1, 0, 0]) < 1e-9
    assert _cosine_distance([1, 0, 0], [0, 1, 0]) > 0.9

# ── Embedding drainer contract (count ⟺ docs) ───────────────────────


@pytest.mark.asyncio
async def test_schemaless_rows_do_not_starve_the_drainer(store):
    """Regression: schema-less rows must not hide the embeddable ones.

    ``cli`` rows carry no schema and can never be embedded, so the exclusion
    has to happen IN SQL.  When it ran in Python *after* ``LIMIT``, a batch
    consisting of exactly those rows returned no docs while
    ``count_unembedded()`` still reported one — the drainer burnt its
    no-progress bound, gave up, and the semantic gate stayed shut with 1537 of
    1538 tools already embedded.
    """
    # Five schema-less rows sort before the embeddable tool and
    # REINDEX_BATCH_LIMIT is 5 — the exact shape that starved the drainer.
    for name in ("browser-harness", "npm", "npx", "uv", "uvx"):
        await store.upsert_tool(name, category="cli")
    await store.upsert_tool(
        "wait_minutes", category="builtin", status="loaded",
        schema=_descriptor("wait_minutes", "pause and resume later", None),
    )

    assert await store.count_unembedded() == 1
    docs = await store.get_unembedded_docs(limit=5)
    assert [d["doc_id"] for d in docs] == ["wait_minutes"]
    assert docs[0]["text"].strip()


@pytest.mark.asyncio
async def test_unembedded_count_and_docs_agree(store):
    """The invariant the gate rides on: a non-zero count always yields at
    least one doc; zero yields none."""
    await store.upsert_tool("a", category="builtin",
                            schema=_descriptor("a", "first tool", None))
    await store.upsert_tool("b", category="cli")          # never embeddable
    assert await store.count_unembedded() == 1
    assert [d["doc_id"] for d in await store.get_unembedded_docs()] == ["a"]

    await _set_embedding(store, "a", [0.1] * 4)
    assert await store.count_unembedded() == 0
    assert await store.get_unembedded_docs() == []
