"""CatalogStore unit tests — the single shared tools.db (schema, effective
status, LRU, search, drainer contract, WAL cross-process pragmas)."""

import asyncio
import json

import pytest
import pytest_asyncio

from slife.config import EmbeddingsConfig
from slife.tools.catalog import (
    SCHEMA_VERSION,
    STATUS_DISABLED,
    STATUS_ENABLED,
    STATUS_ERROR,
    CatalogStore,
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


async def count_tool_vectors(store) -> int:
    """Distinct tools with at least one embedding row.

    The store's own read surface has no count for this (production reads
    ``count_unembedded`` / the semantic facts instead), so the tests that need
    to observe vector presence ask the table directly.
    """
    cursor = await store._c.execute(
        "SELECT COUNT(DISTINCT name) FROM tool_embeddings",
    )
    row = await cursor.fetchone()
    return row[0] if row else 0




def _tool(name, description="", schema=None):
    tool = {"name": name, "description": description}
    if schema is not None:
        tool["inputSchema"] = schema
    return tool


def _descriptor(name, description, schema):
    """Compact JSON of a tool descriptor — the catalog ``schema`` column.

    Mirrors ``catalog_service.descriptor_json``, except that a ``None``
    schema omits the key rather than emitting ``inputSchema: null`` (the
    fixtures below use ``None`` to mean "no schema at all").
    """
    return json.dumps(
        _tool(name, description, schema), ensure_ascii=False, separators=(",", ":"),
    )


async def _set_embedding(store, name, vec):
    await store.replace_embedding_chunks({"doc_id": name}, [vec])


# ── Upserts / effective status ──────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_tool_and_effective_truth_table(store):
    # An external tool's effective status is its OWN status — a connected
    # server's row is `loaded` until something marks it otherwise.
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "full-text search", None),
        load_status="loaded",
    )
    # …and an unusable server's rows read `error`: the runtime's lane of the
    # status column, written by the reconcile (never derived — there is no
    # server table to join) and never written into the load state.
    await store.upsert_tool(
        "svcC__ping", category="mcp", source_id="svcC", load_status="loaded",
    )
    await store.mark_source_error("svcC")
    # builtin disabled by config → DISABLED; enabled → loaded/unloaded
    await store.upsert_tool("native_a", category="builtin", status=STATUS_DISABLED,
                            load_status="loaded")
    await store.upsert_tool("native_b", category="builtin", status=STATUS_ENABLED,
                            load_status="unloaded")
    # skill/cli have no load state, so their own status IS the answer
    await store.upsert_tool("skill-xyz", category="skill")
    await store.upsert_tool("cli-foo", category="cli")

    eff = {r["name"]: r["eff"] for r in await store.scan_effective()}
    assert eff["svcA__search"] == "loaded"
    assert eff["svcC__ping"] == STATUS_ERROR
    assert eff["native_a"] == STATUS_DISABLED
    assert eff["native_b"] == "unloaded"
    assert eff["skill-xyz"] == STATUS_ENABLED
    assert eff["cli-foo"] == STATUS_ENABLED

    # the verdict did not cost svcC__ping its load state — it is still loaded
    assert (await store.get_tool("svcC__ping"))["load_status"] == "loaded"

    # loaded_names() yields exactly the rows that are loaded and enabled
    # (native_a is switched off despite status='loaded'; svcC__ping is loaded
    # but its server is not up)
    assert set(await store.loaded_names()) == {"svcA__search"}
    assert await store.count_loaded() == 1


@pytest.mark.asyncio
async def test_set_source_enabled_moves_the_flag_and_nothing_else(store):
    """An enable/disable flip crosses the switch line — the model's column and
    the runtime's lane are not part of it.

    The row survives being switched off (a disabled server keeps its tools),
    it reports ``disabled`` rather than looking merely down, and a tool the
    model had loaded is still loaded when the server comes back.
    """
    await store.upsert_tool("svcA__x", category="mcp", source_id="svcA", load_status="loaded")
    await store.upsert_tool("svcA__y", category="mcp", source_id="svcA", load_status="unloaded")

    moved = await store.set_source_enabled("svcA", False)
    assert moved == 2
    x = await store.get_tool("svcA__x")
    assert x["status"] == STATUS_DISABLED
    assert x["load_status"] == "loaded"                      # untouched
    eff = {r["name"]: r["eff"] for r in await store.scan_effective()}
    assert eff["svcA__x"] == STATUS_DISABLED            # switched off…
    assert eff["svcA__y"] == STATUS_DISABLED
    assert await store.loaded_names() == []             # …so not injectable

    await store.set_source_enabled("svcA", True)
    x = await store.get_tool("svcA__x")
    assert x["status"] == STATUS_ENABLED
    assert x["load_status"] == "loaded"                      # the round trip cost nothing
    assert set(await store.loaded_names()) == {"svcA__x"}

    # An override that overrides nothing is not a write: the sync re-projects
    # every configured server on every pass, and ``tool_au`` fires on any
    # UPDATE — re-indexing the server's rows into tool_fts each time.
    assert await store.set_source_enabled("svcA", True) == 0
    # A row born with no opinion is ENABLED — the column has a default, so "no
    # opinion" is a fact about the caller (it asked for no write), never a
    # sentinel in the table.  Pinning it to 'enabled' therefore moves nothing;
    # the config-wins rule that remains is the explicit False above.
    await store.upsert_tool("svcA__z", category="mcp", source_id="svcA")
    assert await store.set_source_enabled("svcA", True) == 0


@pytest.mark.asyncio
async def test_config_and_runtime_never_overwrite_each_other(store):
    """One column, two lanes: config crosses the switch line, the runtime
    crosses the liveness line, and neither writes in the other's.

    So a server that was down when it was switched off is ``disabled`` — off
    is not down, and the model must be able to tell the two apart — and coming
    back up does not resurrect a tool the config switched off.
    """
    await store.upsert_tool("svcA__x", category="mcp", source_id="svcA", load_status="loaded")
    await store.mark_source_error("svcA")
    await store.set_source_enabled("svcA", False)

    eff = {r["name"]: r["eff"] for r in await store.scan_effective()}
    assert eff["svcA__x"] == STATUS_DISABLED
    assert (await store.get_tool("svcA__x"))["status"] == STATUS_DISABLED
    assert (await store.get_tool("svcA__x"))["load_status"] == "loaded"

    # The runtime's mark does not touch the switch, either way round.
    assert await store.mark_source_error("svcA") == 0        # already disabled
    # …and a reconnect cannot switch a config-disabled server back on.
    assert await store.mark_source_connected("svcA") == 0
    assert (await store.get_tool("svcA__x"))["status"] == STATUS_DISABLED


@pytest.mark.asyncio
async def test_mark_and_clear_source_error(store):
    """The connect/disconnect cycle as the store sees it — and what it costs."""
    for name, status in (("svcA__x", "loaded"), ("svcA__y", "unloaded")):
        await store.upsert_tool(name, category="mcp", source_id="svcA", load_status=status)

    marked = await store.mark_source_error("svcA")
    assert marked == 2
    # Every reconcile pass re-projects a still-unreachable server: a row that
    # is already flagged is not written (and not re-indexed) again.
    assert await store.mark_source_error("svcA") == 0
    assert await store.loaded_names() == []          # out of the injected set
    assert await store.get_effective("svcA__x") == STATUS_ERROR

    cleared = await store.mark_source_connected("svcA")
    assert cleared == 2
    # The point of the separate lane: the reconnect gives back exactly what
    # the server blip took — the load state was never written over.
    assert (await store.get_tool("svcA__x"))["load_status"] == "loaded"
    assert (await store.get_tool("svcA__y"))["load_status"] == "unloaded"
    assert set(await store.loaded_names()) == {"svcA__x"}

    # A second pass over an available source is a no-op.
    assert await store.mark_source_connected("svcA") == 0


@pytest.mark.asyncio
async def test_mark_all_external_error_spares_local_rows(store):
    await store.upsert_tool("svcA__x", category="mcp", source_id="svcA", load_status="loaded")
    await store.upsert_tool("native", category="builtin", status=STATUS_ENABLED,
                            load_status="loaded")

    marked = await store.mark_all_external_error()

    assert marked == 1
    # A second gateway death must not re-flag the whole external set.
    assert await store.mark_all_external_error() == 0
    assert (await store.get_tool("svcA__x"))["status"] == STATUS_ERROR
    # The startup sweep is what a restart does to every external row: it must
    # leave the load state alone, or a restart would cost the model its set.
    assert (await store.get_tool("svcA__x"))["load_status"] == "loaded"
    assert (await store.get_tool("native"))["load_status"] == "loaded"
    assert set(await store.loaded_names()) == {"native"}


@pytest.mark.asyncio
async def test_upsert_tool_keeps_status_on_reupdate(store):
    await store.upsert_tool("native_a", category="builtin", status=STATUS_ENABLED,
                            load_status="loaded")
    await store.set_load_status("native_a", "unloaded")
    # a plugin re-register (reconcile upsert) must NOT clobber the unload
    changed = await store.upsert_tool(
        "native_a", category="builtin", status=STATUS_ENABLED, load_status="loaded",
    )
    assert changed is False
    assert (await store.get_tool("native_a"))["load_status"] == "unloaded"


@pytest.mark.asyncio
async def test_upsert_schema_change_drops_embedding(store):
    s1 = {"type": "object", "properties": {"repo": {"type": "string"}}}
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "full-text search", s1), load_status="unloaded",
    )
    await _set_embedding(store, "svcA__search", [0.1, 0.2])
    assert await store.count_unembedded() == 0

    # description edit → part of the descriptor → stale vector dropped
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "semantic vector search", s1), load_status="unloaded",
    )
    assert await store.count_unembedded() == 1

    # unchanged re-upsert (idempotent reconcile) keeps the embedding
    await _set_embedding(store, "svcA__search", [0.3, 0.4])
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        schema=_descriptor("search", "semantic vector search", s1), load_status="unloaded",
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
         source_id=None, status=None, load_status=None):
    return {
        "name": name, "description": description, "category": category,
        "source_id": source_id, "schema": schema, "status": status,
        "load_status": load_status,
    }


@pytest.mark.asyncio
async def test_reconcile_noop_writes_nothing(store):
    rows = [
        _row("native_a", description="A", schema="schema-a", status=STATUS_ENABLED,
             load_status="unloaded"),
        _row("native_b", description="B", schema="schema-b", status=STATUS_ENABLED,
             load_status="unloaded"),
    ]
    first = await store.reconcile(rows)
    assert sorted(first["inserted"]) == ["native_a", "native_b"]

    before = store._c.total_changes
    second = await store.reconcile(rows)
    assert store._c.total_changes == before
    assert second["inserted"] == [] and second["updated"] == []
    assert second["skipped"] == 2


# ── Op accounting: what a window of writes did ─────────────────────

@pytest.mark.asyncio
async def test_op_delta_counts_the_row_operations(store):
    """The tool-sync report's delta is what a window WROTE, by operation.

    Counted inside the store so every write path is covered, whichever
    service method drives it: a name that was not there is ``added``, one
    whose config-derived columns moved is ``updated``, a delete is
    ``removed``.
    """
    delta = store.begin_ops()

    await store.reconcile([
        _row("native_a", description="A", schema="schema-a"),
        _row("native_b", description="B", schema="schema-b"),
    ])
    assert delta.added == 2

    # The no-op contract holds for the count too: an unchanged pass writes
    # nothing, so it counts nothing.
    await store.reconcile([
        _row("native_a", description="A", schema="schema-a"),
        _row("native_b", description="B", schema="schema-b"),
    ])
    assert (delta.added, delta.updated, delta.removed) == (2, 0, 0)

    await store.reconcile([_row("native_a", description="A!", schema="schema-a")])
    assert delta.updated == 1

    await store.reconcile([_row("svc__tool", category="mcp", source_id="svc")])
    assert await store.purge_source_except("svc", set()) == ["svc__tool"]
    assert delta.removed == 1

    await store.remove_tool("native_b")
    assert delta.removed == 2


@pytest.mark.asyncio
async def test_op_delta_does_not_count_a_row_born_switched_off(store):
    """A disabled row is WRITTEN, but it is not a tool the user gained.

    The tool-set line prints ``added`` beside ``count_usable()``, and both ask
    the same question of the same ``status`` column — so the row the config
    declares switched off (written all the same: yaml names it, so the db
    carries it) must book a gain on neither side.  Booking it on one is what
    made a cold start read ``新增 1586 … 1575 个工具可用``.
    """
    delta = store.begin_ops()

    await store.reconcile([
        _row("native_a", description="A", schema="schema-a"),
        _row("off_tool", description="Off", status=STATUS_DISABLED),
    ])

    assert delta.added == 1
    assert await store.count_usable() == 1
    assert (await store.get_tool("off_tool"))["status"] == STATUS_DISABLED


@pytest.mark.asyncio
async def test_count_usable_is_the_whole_catalog_not_the_function_families(store):
    """``count_usable`` asks "is it callable", never "does it have a load state".

    ``FUNCTION_CATEGORIES`` splits on load/unload, and a skill / cli row is a
    usable tool that simply has none — so the count has to come off the rows,
    where only a registry (which holds no instance for those families) would
    miss them.  ``error`` is the other half: a tool whose server is down is not
    usable, so an enabled count must not include it.
    """
    await store.reconcile([
        _row("native_a", description="A", schema="schema-a"),
        _row("browser-use", description="Skill", category="skill",
             source_id="skill", schema="# browser-use"),
        _row("gh", description="Cli", category="cli", source_id="cli"),
        _row("off_tool", description="Off", status=STATUS_DISABLED),
        _row("down__tool", description="Down", category="mcp",
             source_id="down", status=STATUS_ERROR),
    ])

    assert await store.count_usable() == 3


@pytest.mark.asyncio
async def test_op_delta_counts_a_switched_off_server(store):
    """``status``'s config arm IS config-derived — a server switched off in
    tools.yaml changes those tools — while a switch that moves nothing is not
    a write, and the runtime's own writes are never counted."""
    await store.reconcile([
        _row("svc__tool", category="mcp", source_id="svc", status=STATUS_ENABLED),
    ])
    delta = store.begin_ops()

    assert await store.set_source_enabled("svc", False) == 1
    assert delta.updated == 1

    assert await store.set_source_enabled("svc", False) == 0
    assert delta.updated == 1


@pytest.mark.asyncio
async def test_op_delta_ignores_runtime_state_writes(store):
    """A load, a touch, a connectivity mark, an eviction — runtime state, not
    a change to the tool set.  Without this a server coming up would report
    every one of its tools as "modified"."""
    await store.reconcile([
        _row("native_a", description="A", schema="schema-a",
             load_status="unloaded"),
        _row("svc__tool", category="mcp", source_id="svc",
             load_status="unloaded"),
    ])
    delta = store.begin_ops()

    await store.set_load_status("native_a", "loaded", bump=True)
    await store.touch("native_a")
    await store.mark_source_error("svc")
    await store.mark_source_connected("svc")
    await store.mark_all_external_error()
    await store.evict_lru(1, protected=frozenset())

    assert (delta.added, delta.updated, delta.removed) == (0, 0, 0)


@pytest.mark.asyncio
async def test_reconcile_splits_content_from_status_updates(store):
    """``override_status`` re-states a row's load state: runtime, so it is
    reported as ``status_updated`` and never as a modified tool."""
    await store.reconcile([_row("native_a", description="A", schema="schema-a",
                                load_status="unloaded")])

    result = await store.reconcile([
        {**_row("native_a", description="A", schema="schema-a",
                load_status="loaded"), "override_status": True},
    ])

    assert result["updated"] == []
    assert result["status_updated"] == ["native_a"]
    assert (await store.get_tool("native_a"))["load_status"] == "loaded"


@pytest.mark.asyncio
async def test_the_boot_comparison_is_these_five_columns(store):
    """What a startup pass compares a row on: ``description``, ``category``,
    ``source_id``, ``schema`` and ``status``.

    Each one on its own makes the row an update, and nothing else does — the
    runtime columns (``load_status`` / ``last_loaded``) are not part of it, or
    a server coming up would report every one of its tools as modified.  This
    is the whole test of "same record or not": the pass writes exactly the
    difference and nothing else.
    """
    base = _row("svc__tool", description="D", category="mcp", source_id="svc",
                schema="schema-a", status=STATUS_ENABLED, load_status="unloaded")
    await store.reconcile([base])
    await store.reconcile([base])
    assert await store.reconcile([base]) == {
        "inserted": [], "updated": [], "status_updated": [], "skipped": 1,
        "purged": [], "schema_changed": [],
    }

    moved = {
        "description": {**base, "description": "D2"},
        "category": {**base, "category": "rest-api"},
        "source_id": {**base, "source_id": "other"},
        "schema": {**base, "schema": "schema-b"},
        "status": {**base, "status": STATUS_DISABLED},
    }
    for column, row in moved.items():
        result = await store.reconcile([row])
        assert result["updated"] == ["svc__tool"], column
        assert result["status_updated"] == [], column
        await store.reconcile([base])          # put it back for the next one


@pytest.mark.asyncio
async def test_reconcile_writes_only_the_column_that_moved(store):
    """A switch-only flip must not rewrite the schema blob — so the FTS
    update trigger does not fire — and must not invalidate the embedding."""
    await store.reconcile([_row("native_a", description="A", schema="schema-a",
                                status=STATUS_ENABLED)])
    await _set_embedding(store, "native_a", [0.1, 0.2])

    result = await store.reconcile([_row("native_a", description="A",
                                         schema="schema-a", status=STATUS_DISABLED)])
    assert result["updated"] == ["native_a"]
    assert result["schema_changed"] == []
    assert await store.count_unembedded() == 0
    assert (await store.get_tool("native_a"))["status"] == STATUS_DISABLED


@pytest.mark.asyncio
async def test_reconcile_invalidates_only_the_tools_whose_schema_moved(store):
    await store.reconcile([_row("a", schema="schema-a"), _row("b", schema="schema-b")])
    await _set_embedding(store, "a", [0.1, 0.2])
    await _set_embedding(store, "b", [0.3, 0.4])

    result = await store.reconcile([_row("a", schema="schema-a2"),
                                    _row("b", schema="schema-b")])
    assert result["schema_changed"] == ["a"]
    assert await store.count_unembedded() == 1
    assert await count_tool_vectors(store) == 1


@pytest.mark.asyncio
async def test_the_only_embedding_trigger_is_a_schema_move(store):
    """One rule, one trigger: the schema text changed → the vectors are stale.

    The drainer embeds ``_flatten_schema(schema)``, which drops
    enum/default/nesting, so this re-embeds a tool whose edit only touched a
    dropped field — a redundant embedding call, and the price of the invariant
    that makes the whole index self-describing: **a row has vectors if and only
    if its schema has not moved since they were made**.  A second, cleverer
    comparator (compare the flattened text, not the column) bought that one
    call back at the cost of a rule of its own, and it had to stay in step with
    the drainer's own predicate forever.
    """
    base = {"type": "object", "properties": {"repo": {"type": "string"}}}
    await store.reconcile([_row("svc__t", category="mcp", source_id="svc",
                                schema=_descriptor("t", "search repos", base))])
    await _set_embedding(store, "svc__t", [0.1, 0.2])
    assert await store.count_unembedded() == 0

    # Any schema edit invalidates — a dropped-field one included.
    with_enum = {"type": "object", "properties": {
        "repo": {"type": "string", "enum": ["a", "b"]}}}
    moved = await store.reconcile([_row(
        "svc__t", category="mcp", source_id="svc",
        schema=_descriptor("t", "search repos", with_enum))])
    assert moved["updated"] == ["svc__t"]
    assert moved["schema_changed"] == ["svc__t"]
    assert await store.count_unembedded() == 1
    assert await count_tool_vectors(store) == 0

    # …and a NON-schema move never does: the vectors are the schema's, so a
    # switch, a description or a rename leaves them alone.
    await _set_embedding(store, "svc__t", [0.3, 0.4])
    for row in (
        {**_row("svc__t", category="mcp", source_id="svc",
                schema=_descriptor("t", "search repos", with_enum)),
         "status": STATUS_DISABLED},
    ):
        result = await store.reconcile([row])
        assert result["schema_changed"] == []
        assert await store.count_unembedded() == 0

    # A brand-new row with an embeddable schema is handed over too — that is
    # how its first vector ever gets made.
    fresh = await store.reconcile([
        _row("svc__fresh", category="mcp", source_id="svc", schema="schema-x"),
    ])
    assert fresh["schema_changed"] == ["svc__fresh"]
    # A row with no schema text (the sentinel) is never handed over: the
    # drainer's own predicate would refuse it, and a hand-over it refuses is
    # what starves the drainer.
    bare = await store.reconcile([
        _row("svc__bare", category="mcp", source_id="svc", schema=None),
    ])
    assert bare["schema_changed"] == []


@pytest.mark.asyncio
async def test_reconcile_status_none_is_no_opinion(store):
    """`status=None` leaves the column alone (the mcp/rest-api contract), and
    a re-reconcile never clobbers the model's load state."""
    await store.reconcile([_row("svc__t", category="mcp", source_id="svc",
                                schema="s", status=None, load_status="loaded")])
    row = await store.get_tool("svc__t")
    # "No opinion" is about the CALLER not writing, not about a sentinel value:
    # a fresh row takes the column's default.
    assert row["status"] == STATUS_ENABLED and row["load_status"] == "loaded"

    # An explicit value still lands — the behaviour the old
    # COALESCE(excluded.enabled, tool.enabled) provided.
    result = await store.reconcile([_row("svc__t", category="mcp", source_id="svc",
                                         schema="s", status=STATUS_DISABLED,
                                         load_status="unloaded")])
    assert result["updated"] == ["svc__t"]
    row = await store.get_tool("svc__t")
    assert row["status"] == STATUS_DISABLED
    assert row["load_status"] == "loaded"      # untouched by an update


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
    assert await count_tool_vectors(store) == 0             # vectors went too


@pytest.mark.asyncio
async def test_reconcile_without_purge_keeps_vanished_rows(store):
    """The store-level default: a reconcile without ``purge`` keeps vanished rows.

    The membership sweeps are the SERVICE's calls, one per authority — the
    boot seed's builtin sweep (``own_builtins``) for the registry's own rows, a
    source-scoped mirror for a plugin, a category-scoped mirror for skill/cli —
    and each keys on a list that is authoritative for what it sweeps.  The
    store itself never guesses, because a name missing from one caller's list
    may simply be an mcp row whose server has not connected yet.
    """
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
    await store.upsert_tool("svcA__a", category="mcp", source_id="svcA", load_status="loaded")
    await store.upsert_tool("svcA__b", category="mcp", source_id="svcA", load_status="loaded")
    await store.upsert_tool("svcB__c", category="mcp", source_id="svcB", load_status="loaded")

    assert await store.purge_source("svcA") == 2

    assert await store.get_tool("svcA__a") is None
    assert await store.get_tool("svcB__c") is not None      # other servers untouched
    # FTS row gone too (delete trigger)
    assert await store.search_keyword("svcA__a") == []
    assert await store.list_source_ids() == {"svcB"}


@pytest.mark.asyncio
async def test_purge_missing_sources_keeps_the_configured_set(store):
    for sid in ("keep", "gone"):
        await store.upsert_tool(f"{sid}__x", category="mcp", source_id=sid, load_status="unloaded")

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
async def test_the_category_is_the_only_thing_that_says_what_a_row_is(tmp_path):
    """There is no ``type`` column to keep in sync.

    A row's kind is its ``category``, and every question the retired
    projection answered ("does this row have a load state?") is a membership
    test over ``FUNCTION_CATEGORIES`` — so a re-upsert under another category
    moves the whole answer with it, and no writer can leave a second column
    stale.
    """
    import aiosqlite

    path = tmp_path / "tools.db"
    conn = await aiosqlite.connect(str(path))
    # The v8 shape, one revision back: same columns PLUS the dropped `type`.
    await conn.execute(
        """CREATE TABLE tool (
               name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
               category TEXT NOT NULL, type TEXT NOT NULL DEFAULT 'func',
               source_id TEXT, schema TEXT, status TEXT, load_status TEXT,
               last_loaded TEXT)""",
    )
    await conn.commit()
    await conn.close()

    store = CatalogStore(path)
    await store.open()
    from slife.health import get_report
    try:
        # An extra column is a stale file, not a harmless leftover: nothing
        # maintains it any more, which is the burden dropping it removed.
        entry = next(e for e in get_report() if e.get("component") == "tool_catalog")
        assert entry["value"] == "stale (unknown type column)"
        assert "Delete" in entry["hint"]
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_load_state_is_decided_by_the_category(store):
    """A non-function category has no load state — the refusal the service
    makes, and the rule the injection predicate is built from."""
    from slife.tools.catalog import FUNCTION_CATEGORIES, is_function_category

    await store.upsert_tool("execute_shell", category="builtin")
    await store.upsert_tool("svc__search", category="mcp", source_id="svc")
    await store.upsert_tool("turn_search", category="plugin", source_id="memdb")
    await store.upsert_tool("job_x", category="job", source_id="job-coding")
    await store.upsert_tool("api__x", category="rest-api", source_id="api")
    await store.upsert_tool("readme", category="skill")
    await store.upsert_tool("mycmd", category="cli")

    assert FUNCTION_CATEGORIES == {
        "builtin", "job", "plugin", "mcp", "rest-api",
    }
    for category in FUNCTION_CATEGORIES:
        assert is_function_category(category)
    assert not is_function_category("skill") and not is_function_category("cli")
    assert not is_function_category("")
    # A load state can be set on a function row and simply stays 'n/a' on the
    # other two — the rule above is what the service and the injection
    # predicate read, with no second column to consult.
    await store.set_load_status("execute_shell", "loaded")
    assert (await store.get_tool("execute_shell"))["load_status"] == "loaded"
    await store.set_load_status("readme", "loaded")
    assert (await store.get_tool("readme"))["load_status"] == "loaded"   # store is lenient
    assert await store.loaded_names() == ["execute_shell"]               # …the gate is not
    # One row shape for every read path — the scan/search rows carry category.
    hits = await store.search_keyword("execute_shell")
    assert [h["category"] for h in hits] == ["builtin"]


# ── The stale-CHECK guard (no in-place migration by policy) ──────────

#: The ``tool`` table before ``plugin`` joined the category CHECK.
#: ``CREATE TABLE IF NOT EXISTS`` never touches such a table, and widening a
#: CHECK needs a rebuild the project does not do — so the file is meant to be
#: deleted, and this is the guard that says so.  Only that one clause is
#: deliberately old: the columns are current, so the drift under test is the
#: CHECK and nothing else.
_STALE_CATEGORY_CHECK_DDL = """
CREATE TABLE tool (
    name        TEXT PRIMARY KEY,
    description TEXT NOT NULL DEFAULT '',
    category    TEXT NOT NULL
                CHECK (category IN ('builtin','job','mcp','rest-api','skill','cli')),
    type        TEXT NOT NULL DEFAULT 'func'
                CHECK (type IN ('func','skill','cli')),
    source_id   TEXT, schema TEXT, status TEXT, load_status TEXT, last_loaded TEXT)
"""


def test_category_check_values_reads_only_the_category_clause():
    """The parser is scoped to the category CHECK on purpose: ``'skill'`` and
    ``'cli'`` also appear in the TYPE check, so a whole-statement substring
    test would report a category list missing them as complete."""
    from slife.tools.catalog import _category_check_values

    values = _category_check_values(_STALE_CATEGORY_CHECK_DDL)
    assert values == {"builtin", "job", "mcp", "rest-api", "skill", "cli"}
    assert "plugin" not in values
    assert _category_check_values("CREATE TABLE tool (name TEXT PRIMARY KEY)") is None


@pytest.mark.asyncio
async def test_stale_category_check_is_reported_not_silently_broken(tmp_path):
    """An old file keeps its old CHECK, so every ``plugin`` write fails — and
    the mirror is best-effort, so the tools would just stay uncatalogued.  The
    open-time probe must say so (the file is deleted, not migrated)."""
    import aiosqlite
    from slife.health import get_report

    path = tmp_path / "tools.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(_STALE_CATEGORY_CHECK_DDL)
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


@pytest.mark.asyncio
async def test_missing_column_is_reported_not_left_as_a_query_error(tmp_path):
    """A file from before a column was added is stale — and this revision has
    no migration for it, by design (the catalog is derived data).

    So the open-time probe must say which column is missing and what to do,
    because the alternative is every scan failing with ``no such column``,
    which reads as a code bug rather than "delete the derived file".

    The file here is the current shape minus one column (``status``), so the
    ONLY drift is the missing one — a file with columns of an older revision
    as well has its own test below.
    """
    import aiosqlite
    from slife.health import get_report

    path = tmp_path / "tools.db"
    conn = await aiosqlite.connect(str(path))
    await conn.execute(
        """CREATE TABLE tool (
               name TEXT PRIMARY KEY, description TEXT NOT NULL DEFAULT '',
               category TEXT NOT NULL, source_id TEXT, schema TEXT,
               load_status TEXT NOT NULL DEFAULT 'n/a',
               last_loaded TEXT NOT NULL DEFAULT '')""",
    )
    await conn.execute("PRAGMA user_version = 7")
    await conn.commit()
    await conn.close()

    store = CatalogStore(path)
    await store.open()
    try:
        entry = next(e for e in get_report() if e.get("component") == "tool_catalog")
        assert entry["level"] == "warning"
        assert entry["value"] == "stale (no status column)"
        assert "Delete" in entry["hint"] and str(path) in entry["hint"]

        # …and why it has to be loud: the file cannot answer a scan at all.
        import sqlite3
        with pytest.raises(sqlite3.OperationalError, match="status"):
            await store.get_tool("anything")
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_remove_tool_deletes_single_row(store):
    """A vanished non-server tool (a removed job, a dropped plugin tool) must
    lose its row — the mirror is upsert-only, so without this it lingers and
    tool_search keeps returning a tool that no longer exists."""
    await store.upsert_tool("job_a", category="job", load_status="loaded")
    await store.upsert_tool("job_b", category="job", load_status="loaded")

    await store.remove_tool("job_a")

    assert await store.get_tool("job_a") is None
    assert await store.get_tool("job_b") is not None   # only the one removed
    assert await store.search_keyword("job_a") == []   # FTS trigger fired


# ── LRU eviction ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_evict_lru_orders_by_last_loaded_and_skips_protected(store):
    for name in ("t1", "t2", "t3", "t4"):
        await store.upsert_tool(name, category="builtin", status=STATUS_ENABLED, load_status="loaded")
    # bump in a defined order: t2 oldest, then t3, t1, t4 newest
    for name in ("t2", "t3", "t1", "t4"):
        await asyncio.sleep(0)  # ensure distinct second? no — force order below
    await store.set_load_status("t2", "loaded", bump=True)
    await store.set_load_status("t3", "loaded", bump=True)
    await store.set_load_status("t1", "loaded", bump=True)
    await store.set_load_status("t4", "loaded", bump=True)

    evicted = await store.evict_lru(2, protected=frozenset({"t1"}))
    assert set(evicted) == {"t2", "t3"}
    assert (await store.get_tool("t2"))["load_status"] == "unloaded"
    assert (await store.get_tool("t1"))["load_status"] == "loaded"

    # a NULL last_loaded sorts oldest — evict it first
    await store.upsert_tool("t5", category="builtin", status=STATUS_ENABLED, load_status="loaded")
    evicted = await store.evict_lru(1)
    assert evicted == ["t5"]
    assert (await store.get_tool("t5"))["load_status"] == "unloaded"


@pytest.mark.asyncio
async def test_evict_lru_zero_and_skill_rows_untouchable(store):
    # skill rows carry status NULL — never 'loaded' candidates, evict skips them
    await store.upsert_tool("skill-xyz", category="skill")
    await store.upsert_tool("cli-foo", category="cli")
    assert await store.evict_lru(10) == []
    assert await store.evict_lru(0) == []

    # a loaded function tool is a candidate; limit 0 is a no-op
    await store.upsert_tool("native_a", category="builtin", status=STATUS_ENABLED, load_status="loaded")
    assert await store.evict_lru(0) == []
    assert await store.evict_lru(10) == ["native_a"]
    assert (await store.get_tool("native_a"))["load_status"] == "unloaded"


# ── Search ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_keyword_and_cjk_fallback(store):
    await store.upsert_tool(
        "svcA__search", category="mcp", source_id="svcA",
        description="跨仓库全文搜索工具",
        schema=_descriptor("search", "full-text search across repos", None),
        load_status="unloaded",
    )
    await store.upsert_tool(
        "native_exec", category="builtin", status=STATUS_ENABLED,
        description="run a shell command", load_status="loaded",
    )
    eng = await store.search_keyword("search")
    assert {r["name"] for r in eng} == {"svcA__search"}
    # CJK routes to the LIKE substring fallback (unicode61 can't segment it)
    cjk = await store.search_keyword("全文搜索")
    assert {r["name"] for r in cjk} == {"svcA__search"}


@pytest.mark.asyncio
async def test_cjk_fallback_reads_every_text_column(store):
    """The LIKE fallback must read the SAME columns the FTS index and grep do.

    It read four of five — ``source_id`` was missing — so a row matching only
    there was findable by ``grep`` and by an ASCII ``search_keyword``, and
    invisible to the CJK fallback: one corpus, two answers.

    The distinguishing text has to be CJK *and* sit in ``source_id``, because
    only a CJK query reaches this path.  A Chinese ``source_id`` is unrealistic;
    the column coverage is what is under test, not the data."""
    await store.upsert_tool(
        "plain_tool", category="builtin", status=STATUS_ENABLED,
        source_id="内置插件", description="nothing to see here",
        load_status="unloaded",
    )
    # The CJK fallback, reaching the row through source_id alone.
    assert {r["name"] for r in await store.search_keyword("内置插件")} == {"plain_tool"}
    # …and the other two text modes agree that this row is a match, which is what
    # makes the missing column a divergence rather than a scope choice.
    assert {r["name"] for r in await store.search_keyword("builtin")} == {"plain_tool"}
    assert {r["name"] for r in await store.search_grep("内置")} == {"plain_tool"}


@pytest.mark.asyncio
async def test_search_grep_is_a_real_regex(store):
    """``grep`` matches like grep: alternation, wildcards, and a pattern that
    LIKE could not express.  It was SQL ``LIKE %pattern%`` — a literal
    substring — which made the name a misnomer (a ``|`` was a literal pipe).
    """
    await store.upsert_tool("translate_tool", category="builtin", status=STATUS_ENABLED,
                            description="translate text")
    await store.upsert_tool("summarize_tool", category="builtin", status=STATUS_ENABLED,
                            description="summarize it")
    await store.upsert_tool("mcp_set", category="plugin",
                            description="configure servers")

    names = lambda hits: sorted(r["name"] for r in hits)
    assert names(await store.search_grep("translat(e|or)")) == ["translate_tool"]
    assert names(await store.search_grep("summ.rize")) == ["summarize_tool"]
    assert names(await store.search_grep("translate|summarize")) == [
        "summarize_tool", "translate_tool"]
    # `_` is an ordinary character in a regex, not a LIKE wildcard.
    assert names(await store.search_grep("mcp_set")) == ["mcp_set"]


@pytest.mark.asyncio
async def test_search_grep_rejects_an_invalid_pattern(store):
    """A bad pattern is the caller's to report — never a silent no-match."""
    import re as _re
    with pytest.raises(_re.error):
        await store.search_grep("a(b")


@pytest.mark.asyncio
async def test_search_grep_and_category_filter(store):
    await store.upsert_tool("svcA__search", category="mcp", source_id="svcA",
                            description="full-text search tool")
    await store.upsert_tool("native_exec", category="builtin", status=STATUS_ENABLED,
                            description="run a command")
    await store.upsert_tool("cli-foo", category="cli", description="foo cli help")
    hits = await store.search_grep("foo")
    assert {r["name"] for r in hits} == {"cli-foo"}
    hits = await store.search_grep("search", filters={"category": "mcp"})
    assert {r["name"] for r in hits} == {"svcA__search"}
    hits = await store.search_grep("search", filters={"category": "cli"})
    assert hits == []


@pytest.mark.asyncio
async def test_search_semantic_closest_chunk_per_tool(store):
    await store.upsert_tool("svcA__search", category="mcp", source_id="svcA",
                            schema=_descriptor("search", "search", None), load_status="unloaded")
    await store.upsert_tool("native_exec", category="builtin", status=STATUS_ENABLED,
                            description="exec", load_status="loaded")
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
                            load_status="unloaded")
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

    await store.replace_embedding_chunks(docs[0], [[0.1, 0.2]])
    assert await store.count_unembedded() == 1
    assert await count_tool_vectors(store) == 1
    # meta never written by replace — the SemanticManager writes it on model select
    assert (await store.get_meta("embedding_model")) is None

    # model swap contract: drop_embeddings clears the old vector space
    await _set_embedding(store, "svcA__search", [0.5, 0.6])
    assert await store.drop_embeddings() == 2
    assert await count_tool_vectors(store) == 0
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


@pytest.mark.asyncio
async def test_catalog_semantic_publishes_its_state_for_other_processes(tmp_path):
    """The drainer's state is published into the shared db, on every transition.

    The state lives in the main process's manager, but the index it describes
    (``tool_embeddings``) is shared — a subagent runs no drainer, so the only
    way its ``system_health`` can report a degraded index is to read the row
    its owner wrote.  Publishing rides ``_set_state``, which is the only writer
    of the state, so the row cannot lag the transition it describes.
    """
    import json

    from slife.tools.semantic import SEMANTIC_STATE_KEY
    from slife.tools.semantic import SemanticManager as CatalogSM

    store = CatalogStore(tmp_path / "tools.db")
    await store.open()
    try:
        m = CatalogSM(store, EmbeddingsConfig())
        assert await store.get_meta(SEMANTIC_STATE_KEY) is None  # nothing yet

        await m._set_state("stalled", "the embedder gave up this round")
        published = json.loads(await store.get_meta(SEMANTIC_STATE_KEY))
        assert published["state"] == "stalled"
        assert published["reason"] == "the embedder gave up this round"
        assert published["semantic_ready"] is False
        assert published["model"] == "" and published["dimension"] == 0

        # One row, rewritten — not one per transition.
        await m._set_state("ready")
        published = json.loads(await store.get_meta(SEMANTIC_STATE_KEY))
        assert published["state"] == "ready" and published["reason"] == ""
    finally:
        await store.close()


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
        "wait_minutes", category="builtin", load_status="loaded",
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
