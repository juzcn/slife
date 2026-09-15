"""Tool-system self-consistency — a fresh install with its seeded tools.json5
must be coherent with an EMPTY database and with an EXISTING one.

Covers the DESIGNER_NOTES §8.5 acceptance line under the post-``server``-table
model: seeded config ⇄ empty db (first run) and ⇄ persisted db (restart), the
category derivation from tools.json5, and the connectivity verdict projected
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
from slife.tools.catalog import CatalogStore
from slife.tools.catalog_service import ToolCatalogService
from slife.tools.whitelist import ALWAYS_LOADED


class _NativeShell(Tool):
    name = "execute_shell"
    description = "run a shell command"
    parameters = {"type": "object", "properties": {}, "required": []}
    category = "Execution"

    async def execute(self, **kwargs) -> str:
        return "ok"


class _NativeHealth(Tool):
    name = "system_health"
    description = "one-call health report"
    parameters = {"type": "object", "properties": {}, "required": []}
    category = "System"

    async def execute(self, **kwargs) -> str:
        return "ok"


def _seeded_tools_json5(tmp: Path) -> Path:
    """A representative seed: builtin + mcp servers + a rest-api entry."""
    path = tmp / "tools.json5"
    path.write_text(
        """
        {
          builtin: [{ name: "install_python_package", enabled: false }],
          mcp: { servers: {
            filesystem: {
              command: "npx",
              args: ["-y", "@modelcontextprotocol/server-filesystem", "."],
              description: "Local filesystem operations.",
              enabled: false
            },
            serper: {
              command: "npx",
              args: ["-y", "serper-search-scrape-mcp-server"],
              env: { SERPER_API_KEY: "${SERPER_API_KEY}" },
              description: "Google web search via Serper."
            }
          }},
          "rest-api": {
            weather: {
              command: "uvx",
              args: ["--from", "mcp-openapi-proxy", "--", "https://example/api.json"],
              description: "Weather API.",
              source: { type: "rest_api" }
            }
          },
          cli: { mycmd: { command: "echo hi", description: "hi" } },
          job: [],
          skill: [],
          tool_load: { threshold: 5 }
        }
        """,
        encoding="utf-8",
    )
    return path


def _cfg_from(tmp: Path) -> Config:
    tools = _seeded_tools_json5(tmp)
    slife = tmp / "slife.json5"
    slife.write_text(
        "{ models: [{ ref: 'm', provider: 'p', model: 'm' }], active_model: 'm' }",
        encoding="utf-8",
    )
    return Config.from_json5(slife, agent_name="slife")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Per-test db + config files, wrapper helper isolated.

    ``tools.json5`` is the AUTHORITATIVE config: every category the catalog
    mirrors derives from it (via the gateway config), never from a separately
    carved value.
    """
    tools_path = _seeded_tools_json5(tmp_path)
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

    The category comes from tools.json5 (``_server_category``), never from a
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
    await svc.seed_inventory([_NativeShell(), _NativeHealth()])

    # self-consistent empty state: rows exist, but seeding alone injects
    # NOTHING except the always-loaded whitelist
    assert await store.count_loaded() == 0
    snap = await svc.snapshot_loaded()
    assert snap == set(ALWAYS_LOADED)
    assert (await store.get_tool("execute_shell"))["status"] == "unloaded"
    assert await store.list_source_ids() == set()     # no server has connected
    await store.close()


@pytest.mark.asyncio
async def test_nothing_external_is_usable_before_a_connect(_isolate):
    """Startup marks every external row ``error`` — no server is up yet.

    Rows from a previous session must not keep injecting until the server
    they belong to actually connects.
    """
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await store.upsert_tool("serper__search", category="mcp", source_id="serper",
                            status="loaded")

    await svc.mark_all_external_error()

    assert (await store.get_tool("serper__search"))["status"] == "error"
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
    assert (await store.get_tool("serper__search"))["status"] == "unloaded"
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
    assert (await store.get_tool("serper__search"))["status"] == "error"
    # the OTHER server is untouched by its neighbour's outage
    assert (await store.get_tool("weather__temp"))["status"] == "unloaded"

    # reconnect clears the error mark (the row is available again, unloaded)
    await svc.mark_server_connected("serper")
    assert (await store.get_tool("serper__search"))["status"] == "unloaded"
    assert await svc.effective_status("serper__search") == "unloaded"
    await store.close()


@pytest.mark.asyncio
async def test_gateway_death_marks_every_external_tool_error(_isolate):
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await svc.seed_inventory([_NativeShell()])
    await _mirror_server(svc, "serper", ["search"])
    await _mirror_server(svc, "weather", ["temp"])
    ok, _ = await svc.load_tool("serper__search")
    assert ok

    marked = await svc.mark_all_external_error()

    assert marked == 2
    assert (await store.get_tool("serper__search"))["status"] == "error"
    assert (await store.get_tool("weather__temp"))["status"] == "error"
    # only external rows were touched
    assert (await store.get_tool("execute_shell"))["status"] == "unloaded"
    await store.close()


# ── C: tools.json5 is the authority — removal purges ────────────────────


@pytest.mark.asyncio
async def test_purge_unconfigured_sources(_isolate):
    """A server that left tools.json5 loses its rows; configured ones stay."""
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await _mirror_server(svc, "serper", ["search"])
    await store.upsert_tool("ghost__x", category="mcp", source_id="ghost",
                            schema=_descriptor("x", "x"), status="unloaded")

    from slife.plugins.mcp_gateway import config as _cfg
    purged = await svc.purge_unconfigured_sources(set(_cfg.servers()))

    assert purged == {"ghost"}
    assert await store.get_tool("ghost__x") is None
    assert await store.get_tool("serper__search") is not None
    await store.close()


# ── D: restart — the row state survives, no snapshot involved ───────────


@pytest.mark.asyncio
async def test_row_state_survives_a_restart(_isolate):
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
    # The error verdict persisted; a fresh session starts by re-asserting it.
    assert (await store2.get_tool("serper__search"))["status"] == "error"
    await svc2.mark_all_external_error()
    assert await svc2.snapshot_loaded() >= ALWAYS_LOADED
    assert "serper__search" not in await svc2.snapshot_loaded()

    # The server connects again → available, and only the error mark is gone.
    await svc2.mark_server_connected("serper")
    assert (await store2.get_tool("serper__search"))["status"] == "unloaded"
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

        states = {"serper": "connected", "weather": "failed"}

        async def _check(*_a, **_kw):
            return json.dumps({"servers": [
                {"name": name, "status": status} for name, status in states.items()
            ]})

        client.call_tool = _check

        await service._mark_server_connectivity(client, {"serper", "weather"})

        assert (await store.get_tool("serper__search"))["status"] == "unloaded"
        assert (await store.get_tool("weather__temp"))["status"] == "error"

        # weather comes up on the next pass → its mark clears
        states["weather"] = "connected"
        await service._mark_server_connectivity(client, {"serper", "weather"})
        assert (await store.get_tool("weather__temp"))["status"] == "unloaded"
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

        assert (await store.get_tool("serper__search"))["status"] == "loaded"
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
        await svc.seed_inventory([_NativeShell()])
        await _mirror_server(svc, "serper", ["search"])
        ok, _ = await svc.load_tool("serper__search")
        assert ok

        stub = SimpleNamespace(_catalog=svc, is_subagent=False)
        # a non-gateway plugin exit is a no-op
        await AgentService.on_plugin_child_exit(stub, "memdb")
        assert (await store.get_tool("serper__search"))["status"] == "loaded"

        await AgentService.on_plugin_child_exit(stub, "mcp-gateway")
        assert (await store.get_tool("serper__search"))["status"] == "error"
        assert "serper__search" not in await svc.snapshot_loaded()
    finally:
        await store.close()
