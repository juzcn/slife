"""Tool-system self-consistency — a fresh install with its seeded tools.yaml
must be coherent with an EMPTY database and with an EXISTING one.

Covers the DESIGNER_NOTES §8.5 acceptance line under the post-``server``-table
model: seeded config ⇄ empty db (first run) and ⇄ persisted db (restart), the
category derivation from tools.yaml, and the connectivity verdict projected
onto the tool rows (`error` when a server is unusable, cleared when it
connects).  All deterministic — no network, no child processes.
"""

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from slife.config import Config
from slife.tools.base import Tool
from slife.tools.catalog import CatalogStore, effective_from_row
from slife.tools.catalog_service import ToolCatalogService
from slife.tools.whitelist import ALWAYS_LOADED


class _NativeShell(Tool):
    name = "execute_shell"
    description = "run a shell command"
    parameters = {"type": "object", "properties": {}, "required": []}
    category = "Execution"

    async def execute(self, **kwargs) -> str:
        return "ok"


class _NativeToolList(Tool):
    name = "system_tools_list"
    description = "list the registered native tools"
    parameters = {"type": "object", "properties": {}, "required": []}
    category = "System"

    async def execute(self, **kwargs) -> str:
        return "ok"


def _seeded_tools_yaml(tmp: Path) -> Path:
    """A representative seed: builtin + mcp servers + a rest-api entry."""
    path = tmp / "tools.yaml"
    path.write_text(
        """
        builtin:
          - name: install_python_package
            enabled: false
        mcp:
          servers:
            filesystem:
              command: npx
              args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
              description: Local filesystem operations.
              enabled: false
            serper:
              command: npx
              args: ["-y", "serper-search-scrape-mcp-server"]
              env:
                SERPER_API_KEY: ${SERPER_API_KEY}
              description: Google web search via Serper.
        rest-api:
          weather:
            command: uvx
            args: ["--from", "mcp-openapi-proxy", "--", "https://example/api.json"]
            description: Weather API.
            source:
              type: rest_api
        cli:
          mycmd:
            command: echo hi
            description: hi
        job: []
        skill: []
        tool_load:
          threshold: 5
        """,
        encoding="utf-8",
    )
    return path


def _cfg_from(tmp: Path) -> Config:
    tools = _seeded_tools_yaml(tmp)
    slife = tmp / "slife.yaml"
    slife.write_text(
        "models:\n  - ref: m\n    provider: p\n    model: m\nactive_model: m\n",
        encoding="utf-8",
    )
    return Config.from_yaml(slife, agent_name="slife")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test db + config files, wrapper helper isolated.

    ``tools.yaml`` is the AUTHORITATIVE config: every category the catalog
    mirrors derives from it (via the gateway config), never from a separately
    carved value.
    """
    tools_path = _seeded_tools_yaml(tmp_path)
    monkeypatch.setenv("SLIFE_TOOLS_DB", str(tmp_path / "tools.db"))
    monkeypatch.setenv("TOOLS_FILE", str(tools_path))
    from slife.plugins.mcp_gateway import config as _cfg
    _cfg.set_config_path(str(tools_path))  # pin the gateway config resolver
    yield tmp_path


def _descriptor(name: str, description: str) -> str:
    return json.dumps({
        "name": name, "description": description,
        "inputSchema": {"type": "object", "properties": {}},
    })


async def _mirror_server(catalog: ToolCatalogService, server: str, tools: list[str]) -> None:
    """What the reconcile does when a server connects: mirror its tool rows.

    The category comes from tools.yaml (``_server_category``), never from a
    mirrored provenance row — there is no server table.
    """
    from slife.agent.service import _server_category

    for tool in tools:
        await catalog.upsert_external_tool(
            f"{server}__{tool}",
            server=server,
            description=f"{tool} desc",
            schema=_descriptor(tool, f"{tool} desc"),
            category=_server_category(server),
        )


# ── A: FRESH INSTALL — empty db + seeded config ─────────────────────────


@pytest.mark.asyncio
async def test_empty_db_opens_and_seeds(_isolate):
    cfg = _cfg_from(_isolate)
    assert cfg.tool_load_threshold == 5
    assert cfg.cli_tools["mycmd"]["command"] == "echo hi"
    assert cfg.disabled_jobs == frozenset() and cfg.disabled_skills == frozenset()

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, threshold=cfg.tool_load_threshold, write_owner=True)
    await svc.sync_system_tools([_NativeShell(), _NativeToolList()])

    # self-consistent empty state: rows exist, but seeding alone injects
    # NOTHING except the always-loaded whitelist
    assert await store.count_loaded() == 0
    snap = await svc.snapshot_loaded()
    assert snap == set(ALWAYS_LOADED)
    assert (await store.get_tool("execute_shell"))["load_status"] == "unloaded"
    assert await store.list_source_ids() == set()     # no server has connected
    await store.close()


@pytest.mark.asyncio
async def test_a_hand_edited_cli_entry_lands_without_a_restart(_isolate, sample_config):
    """tools.yaml stays the authority mid-session.

    A cli entry added by hand (not through ``cli_set``, which re-mirrors by
    itself) reaches the db on the next reconcile pass — the mtimes are what
    make that cheap enough to do there.
    """
    import os

    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    try:
        svc = ToolCatalogService(store, write_owner=True)
        service = AgentService(sample_config)
        service._catalog = svc
        service._tool_ctx.catalog = svc      # the mirrors write through the ctx

        # First pass primes the mtimes (and mirrors the seeded `mycmd`).
        await service._refresh_local_rows_if_changed(svc)
        assert "cli:mycmd" in await store.names_by_category("cli")

        # A hand-edit: a new cli entry, written straight to the file.
        tools_path = _isolate / "tools.yaml"
        text = tools_path.read_text(encoding="utf-8")
        tools_path.write_text(
            text.replace(
                "description: hi\n",
                "description: hi\n"
                "          byhand:\n"
                "            command: echo byhand\n"
                "            description: edited by hand\n",
            ),
            encoding="utf-8",
        )
        stamp = tools_path.stat().st_mtime + 10
        os.utime(tools_path, (stamp, stamp))   # a human edit is never instant

        await service._refresh_local_rows_if_changed(svc)

        assert "cli:byhand" in await store.names_by_category("cli")
        # …and an unchanged file is a no-op (two stats, nothing rewritten).
        before = [
            dict(r) for r in await store.scan_effective()
            if r["category"] == "cli"
        ]
        await service._refresh_local_rows_if_changed(svc)
        after = [
            dict(r) for r in await store.scan_effective()
            if r["category"] == "cli"
        ]
        assert before == after
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_a_config_disabled_builtin_gets_a_row_marked_disabled(_isolate):
    """config declaring a tool the db had never heard of is a disagreement.

    A disabled builtin is never REGISTERED, so the registry cannot seed it —
    the seed hands its instance over explicitly, and the row reports
    ``disabled`` (the model can see the tool exists and is switched off,
    instead of it being absent with no explanation).
    """

    class _DisabledNative(Tool):
        name = "native_off"
        description = "a builtin the config switched off"
        parameters = {"type": "object", "properties": {}, "required": []}
        category = "System"

        async def execute(self, **kwargs) -> str:
            return "never called"

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    try:
        svc = ToolCatalogService(
            store, write_owner=True, disabled_builtins=("native_off",),
        )
        await svc.sync_system_tools([_NativeShell(), _DisabledNative()])

        row = await store.get_tool("native_off")
        assert row is not None                    # yaml names it → the db has it
        assert row["enabled"] == 0
        assert await svc.effective_status("native_off") == "disabled"
        assert "native_off" not in await svc.snapshot_loaded()
        # …and it is not loadable: the row is a fact, not an offer.
        ok, msg = await svc.load_tool("native_off")
        assert not ok and "disabled" in msg
        # the enabled neighbour is unaffected
        assert await svc.effective_status("execute_shell") == "unloaded"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_nothing_external_is_usable_before_a_connect(_isolate):
    """Startup flags every external row unavailable — no server is up yet.

    Rows from a previous session must not keep injecting until the server
    they belong to actually connects — and the flag is what stops them, so the
    ``loaded`` the last session persisted is still there to come back to.
    """
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await store.upsert_tool("serper__search", category="mcp", source_id="serper",
                            load_status="loaded")

    await svc.mark_all_external_error()

    assert (await store.get_tool("serper__search"))["unavailable"] == 1
    assert (await store.get_tool("serper__search"))["load_status"] == "loaded"
    assert await svc.snapshot_loaded() >= ALWAYS_LOADED
    assert "serper__search" not in await svc.snapshot_loaded()
    await store.close()


# ── B: connect / disconnect as the rows see it ──────────────────────────


@pytest.mark.asyncio
async def test_connect_mirrors_unloaded_and_disconnect_marks_error(_isolate):
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)

    await _mirror_server(svc, "serper", ["search", "scrape"])
    await _mirror_server(svc, "weather", ["temp"])

    # registered tools land UNLOADED and carry the config-derived category
    assert (await store.get_tool("serper__search"))["load_status"] == "unloaded"
    assert (await store.get_tool("serper__search"))["category"] == "mcp"
    assert (await store.get_tool("weather__temp"))["category"] == "rest-api"
    assert await store.get_tool("weather__temp") is not None

    # the model loads one; then serper goes down
    ok, _ = await svc.load_tool("serper__search")
    assert ok
    assert "serper__search" in await svc.snapshot_loaded()

    await svc.mark_source_error("serper")
    snap = await svc.snapshot_loaded()
    assert "serper__search" not in snap            # gone from the tool list
    assert await svc.effective_status("serper__search") == "unavailable"
    assert (await store.get_tool("serper__search"))["load_status"] == "loaded"
    # the OTHER server is untouched by its neighbour's outage
    assert await svc.effective_status("weather__temp") == "unloaded"

    # the reconnect clears the verdict — and the loaded row is loaded again
    await svc.mark_server_connected("serper")
    assert await svc.effective_status("serper__search") == "loaded"
    assert "serper__search" in await svc.snapshot_loaded()
    await store.close()


@pytest.mark.asyncio
async def test_gateway_death_marks_every_external_tool_error(_isolate):
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await svc.sync_system_tools([_NativeShell()])
    await _mirror_server(svc, "serper", ["search"])
    await _mirror_server(svc, "weather", ["temp"])
    ok, _ = await svc.load_tool("serper__search")
    assert ok

    marked = await svc.mark_all_external_error()

    assert marked == 2
    assert await svc.effective_status("serper__search") == "unavailable"
    assert await svc.effective_status("weather__temp") == "unavailable"
    # only the verdict moved — the load state the model set is still there
    assert (await store.get_tool("serper__search"))["load_status"] == "loaded"
    # and only external rows were flagged
    assert await svc.effective_status("execute_shell") == "unloaded"
    await store.close()


# ── C: tools.yaml is the authority — removal purges ────────────────────


@pytest.mark.asyncio
async def test_purge_unconfigured_sources(_isolate):
    """A server that left tools.yaml loses its rows; configured ones stay."""
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await _mirror_server(svc, "serper", ["search"])
    await store.upsert_tool("ghost__x", category="mcp", source_id="ghost",
                            schema=_descriptor("x", "x"), load_status="unloaded")

    from slife.plugins.mcp_gateway import config as _cfg
    purged = await svc.purge_unconfigured_sources(set(_cfg.servers()))

    assert purged == {"ghost"}
    assert await store.get_tool("ghost__x") is None
    assert await store.get_tool("serper__search") is not None
    await store.close()


@pytest.mark.asyncio
async def test_a_tool_a_server_stopped_publishing_loses_its_row(_isolate):
    """Upsert-then-purge over the server's whole set — the contract every other
    family's mirror already follows (a plugin's source-scoped sync, a skill/cli
    ``sync_category``).

    A tool the registry already dropped (its proxy went with it) must not keep a
    catalog row, or ``tool_search`` goes on offering it and ``func-tool-load``
    materializes a proxy with nothing behind it."""
    from types import SimpleNamespace

    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    try:
        svc = ToolCatalogService(store, write_owner=True)
        stub = SimpleNamespace(_catalog=svc)
        listed = [
            {"name": t, "description": f"{t} desc",
             "inputSchema": {"type": "object", "properties": {}}}
            for t in ("search", "scrape")
        ]
        await AgentService._upsert_external_catalog_rows(
            stub, "serper", listed, category="mcp")
        assert await store.get_tool("serper__scrape") is not None
        await store.replace_embedding_chunks(
            {"doc_id": "serper__scrape"}, [[0.1, 0.2]], model="test-model")
        assert await store.count_embedded() == 1

        # the server republishes only `search`
        await AgentService._upsert_external_catalog_rows(
            stub, "serper", listed[:1], category="mcp")

        assert await store.get_tool("serper__scrape") is None
        assert await svc.effective_status("serper__scrape") is None
        assert await store.count_embedded() == 0        # vectors went too
        # …and the survivors are untouched: a sibling's removal is not a reason
        # to reset what the model loaded
        assert await store.get_tool("serper__search") is not None
        ok, _ = await svc.load_tool("serper__search")
        assert ok
        assert "serper__search" in await svc.snapshot_loaded()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_an_empty_listing_never_purges_a_servers_rows(_isolate):
    """The "not ready yet" guard — an empty tool list means the server has not
    listed yet, NEVER that it owns nothing, so it must not wipe its rows.

    Both real callers already return early on an empty listing; this pins the
    second line of defence, so a future caller cannot turn the purge into a
    wipe by handing it an empty set."""
    from types import SimpleNamespace

    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    try:
        svc = ToolCatalogService(store, write_owner=True)
        stub = SimpleNamespace(_catalog=svc)
        await _mirror_server(svc, "serper", ["search", "scrape"])

        await AgentService._upsert_external_catalog_rows(
            stub, "serper", [], category="mcp")

        assert await store.get_tool("serper__search") is not None
        assert await store.get_tool("serper__scrape") is not None
    finally:
        await store.close()


# ── D: restart — the row state survives, no snapshot involved ───────────


@pytest.mark.asyncio
async def test_row_state_survives_a_restart(_isolate):
    """The load state is what the db exists to persist — so a restart, a
    startup sweep and a dead server must not cost the model its decisions.

    This used to end with ``unloaded``: the verdict was written into
    ``status``, so "user intent: loaded" was destroyed by the outage and again
    by the sweep on the next boot.
    """
    db = _isolate / "tools.db"

    store = CatalogStore(db)
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await _mirror_server(svc, "serper", ["search", "scrape"])
    await svc.load_tool("serper__search")            # user intent: loaded
    await svc.mark_source_error("serper")            # …then the server died
    await store.close()

    store2 = CatalogStore(db)
    await store2.open()
    svc2 = ToolCatalogService(store2, write_owner=True)
    # The intent persisted, and the fresh session re-asserts the verdict.
    assert (await store2.get_tool("serper__search"))["load_status"] == "loaded"
    await svc2.mark_all_external_error()
    assert await svc2.snapshot_loaded() >= ALWAYS_LOADED
    assert "serper__search" not in await svc2.snapshot_loaded()

    # The server connects again → the verdict clears, and what the model had
    # loaded is injected again.
    await svc2.mark_server_connected("serper")
    assert await svc2.effective_status("serper__search") == "loaded"
    assert "serper__search" in await svc2.snapshot_loaded()
    await store2.close()


# ── E: the reconcile's connectivity projection (host side) ──────────────


@pytest.mark.asyncio
async def test_connectivity_projection_follows_check(_isolate, sample_config):
    """``__check`` is the liveness input; each pass projects it onto the rows."""
    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    try:
        svc = ToolCatalogService(store, write_owner=True)
        await _mirror_server(svc, "serper", ["search"])
        await _mirror_server(svc, "weather", ["temp"])

        service = AgentService(sample_config)
        service._catalog = svc
        service._catalog_semantic = None
        client = AsyncMock()
        client.is_connected = True

        # The verdict is ``tools_ok`` — a server that answered tools/list and
        # whose result is still held.  A "failed" server is simply one with no
        # working tool list.
        states = {"serper": True, "weather": False}

        async def _check(*_a, **_kw):
            return json.dumps({"servers": [
                {"name": name, "tools_ok": ok} for name, ok in states.items()
            ]})

        client.call_tool = _check

        await service._mark_server_connectivity(client, {"serper", "weather"})

        assert await svc.effective_status("serper__search") == "unloaded"
        assert await svc.effective_status("weather__temp") == "unavailable"
        assert (await store.get_tool("weather__temp"))["unavailable"] == 1

        # weather comes up on the next pass → its mark clears
        states["weather"] = True
        await service._mark_server_connectivity(client, {"serper", "weather"})
        assert await svc.effective_status("weather__temp") == "unloaded"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_switching_a_server_off_keeps_its_rows_and_the_load_state(
    _isolate, sample_config,
):
    """The reconcile's two columns move independently.

    ``enabled`` mirrors tools.yaml's switch; ``unavailable`` carries the
    liveness verdict.  A switched-off server is NOT a down one — calling it
    ``error`` would be a lie the model could not tell from the real thing — and
    its rows stay put, so re-enabling restores a tool set that remembers what
    was loaded.
    """
    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    try:
        svc = ToolCatalogService(store, write_owner=True)
        await _mirror_server(svc, "serper", ["search"])
        await store.set_load_status("serper__search", "loaded")

        service = AgentService(sample_config)
        service._catalog = svc
        service._catalog_semantic = None
        client = AsyncMock()
        client.is_connected = True

        async def _check(*_a, **_kw):
            return json.dumps({"servers": [{"name": "serper", "tools_ok": False}]})

        client.call_tool = _check

        await service._mark_server_connectivity(client, {"serper"}, {"serper": False})

        row = await store.get_tool("serper__search")
        assert row["enabled"] == 0            # the switch
        assert row["load_status"] == "loaded"      # the model's decision, untouched
        assert effective_from_row(row) == "disabled"

        # Switched back on while still down: now it IS the liveness verdict
        # — and the row still remembers that the model had loaded it.
        await service._mark_server_connectivity(client, {"serper"}, {"serper": True})
        row = await store.get_tool("serper__search")
        assert row["enabled"] == 1
        assert row["unavailable"] == 1
        assert effective_from_row(row) == "unavailable"
        assert row["load_status"] == "loaded"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_connectivity_probe_failure_is_not_a_verdict(_isolate, sample_config):
    """A failed ``__check`` must not mark every server broken."""
    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    try:
        svc = ToolCatalogService(store, write_owner=True)
        await _mirror_server(svc, "serper", ["search"])
        ok, _ = await svc.load_tool("serper__search")
        assert ok

        service = AgentService(sample_config)
        service._catalog = svc
        client = AsyncMock()
        client.call_tool = AsyncMock(side_effect=RuntimeError("gateway restarting"))

        await service._mark_server_connectivity(client, {"serper"})

        assert (await store.get_tool("serper__search"))["load_status"] == "loaded"
        assert "serper__search" in await svc.snapshot_loaded()
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_gateway_child_exit_marks_external_tools_error(_isolate):
    """The watchdog's exit hook is the crash path for the connectivity mark."""
    from types import SimpleNamespace

    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    try:
        svc = ToolCatalogService(store, write_owner=True)
        await svc.sync_system_tools([_NativeShell()])
        await _mirror_server(svc, "serper", ["search"])
        ok, _ = await svc.load_tool("serper__search")
        assert ok

        stub = SimpleNamespace(_catalog=svc, is_subagent=False)
        # a non-gateway plugin exit is a no-op
        await AgentService.on_plugin_child_exit(stub, "memdb")
        assert await svc.effective_status("serper__search") == "loaded"

        await AgentService.on_plugin_child_exit(stub, "mcp-gateway")
        assert await svc.effective_status("serper__search") == "unavailable"
        assert "serper__search" not in await svc.snapshot_loaded()
        # the crash flags the row; what the model loaded is still on it, so the
        # gateway's restart brings it back rather than resetting it
        assert (await store.get_tool("serper__search"))["load_status"] == "loaded"
    finally:
        await store.close()
