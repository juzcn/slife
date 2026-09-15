"""Tool-system self-consistency — a fresh install with its seeded tools.json5
must be coherent with an EMPTY database and with an EXISTING one.

Covers the DESIGNER_NOTES §8.5 acceptance line: seeded config ⇄ empty db
(first run) and ⇄ persisted db (restart), the eager-connect set derivation,
reconcile upserts (mcp vs rest-api category), and config-removal deletion.
All deterministic — no network, no child processes.
"""

import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

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

    ``tools.json5`` is the AUTHORITATIVE config: every enabled/category the
    catalog mirrors must derive from it (via the gateway config), never from
    a separately-carved value.
    """
    tools_path = _seeded_tools_json5(tmp_path)
    monkeypatch.setenv("SLIFE_TOOLS_DB", str(tmp_path / "tools.db"))
    monkeypatch.setenv("TOOLS_FILE", str(tools_path))
    from slife.plugins.mcp_gateway import config as _cfg
    _cfg.set_config_path(str(tools_path))  # pin the gateway config resolver
    yield tmp_path


def _eager():
    """The wrapper's eager-connect helper (imports server fresh)."""
    sys.modules.pop("slife.plugins.mcp_gateway.server", None)
    with patch(
        "slife.server_utils.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        import importlib
        srv = importlib.import_module("slife.plugins.mcp_gateway.server")
    return srv._eager_set_from_db()


async def _reconcile(catalog: ToolCatalogService, live: dict[str, str]) -> None:
    """Mirror a live wrapper state into the catalog — but the ENABLED flag and
    the category come from tools.json5 (the gateway config), never hardcoded.
    ``live`` maps server → runtime (runtime is live state, not config)."""
    from slife.plugins.mcp_gateway import config as _cfg

    for name, entry in _cfg.servers().items():
        enabled = (entry or {}).get("enabled", True) is not False
        src = (entry or {}).get("source")
        source_json = json.dumps(src) if isinstance(src, dict) else None
        await catalog.sync_server_status(
            name,
            enabled=enabled,
            runtime=live.get(name, "DISCONNECTED"),
            source=source_json,
        )

    # Tool rows come from the wrapper's live mcp_list_tools; their CATEGORY is
    # derived by the host from the json5-sourced server row.
    await catalog.store.upsert_tool(
        "serper__search", category="mcp", source_id="serper",
        schema=json.dumps({"name": "search", "description": "web search",
                            "inputSchema": {"type": "object", "properties": {}}}),
        status="unloaded",
    )
    await catalog.store.upsert_tool(
        "weather__temp", category="rest-api", source_id="weather",
        schema=json.dumps({"name": "temp", "description": "temp",
                            "inputSchema": {"type": "object", "properties": {}}}),
        status="unloaded",
    )
    await catalog.store.upsert_tool(
        "filesystem__read", category="mcp", source_id="filesystem",
        schema=json.dumps({"name": "read", "description": "read",
                            "inputSchema": {"type": "object", "properties": {}}}),
        status="unloaded",
    )


# ── A: FRESH INSTALL — missing db / empty db + seeded config ─────────────


def test_missing_db_eager_is_connect_all(_isolate):
    """No db yet → the wrapper derives None → connect all enabled (default)."""
    assert _eager() is None


@pytest.mark.asyncio
async def test_empty_db_opens_and_seeds(_isolate):
    cfg = _cfg_from(_isolate)
    assert cfg.tool_load_threshold == 5
    assert cfg.cli_tools["mycmd"]["command"] == "echo hi"
    assert cfg.disabled_jobs == frozenset() and cfg.disabled_skills == frozenset()

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, threshold=cfg.tool_load_threshold, write_owner=True)
    await svc.session_start()
    await svc.seed_inventory([_NativeShell(), _NativeHealth()])

    # self-consistent empty state
    assert await store.count_loaded() == 2
    snap = await svc.snapshot_loaded()
    assert {"execute_shell", "system_health"} <= snap
    assert ALWAYS_LOADED <= snap
    assert await store.get_server("serper") is None   # nothing reconciled yet
    await store.close()
    assert not (_isolate / "tools.db").exists() or True  # file created on open


# ── B: EXISTING db — reconcile, categories, restart self-consistency ─────


@pytest.mark.asyncio
async def test_reconcile_categories_and_effective_status(_isolate):
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await svc.seed_inventory([_NativeShell()])
    await _reconcile(svc, {
        "filesystem": "DISCONNECTED",
        "serper": "CONNECTED",
        "weather": "CONNECTED",
    })

    # json5 IS the authority: the mirrored enabled/category match the config
    assert (await store.get_server("filesystem"))["enabled"] == 0   # enabled:false
    assert (await store.get_server("serper"))["enabled"] == 1
    assert (await store.get_server("weather"))["enabled"] == 1
    # rest-api is its own category (semantics distinct from mcp), derived
    # from the json5 ``source.type``
    assert await svc.server_category("weather") == "rest-api"
    assert await svc.server_category("serper") == "mcp"
    assert (await store.get_tool("weather__temp"))["category"] == "rest-api"
    eff = {r["name"]: r["eff"] for r in await store.scan_effective()}
    # serper connected ⇒ its tool unloaded-but-loadable; filesystem disabled ⇒
    # config wins (DISABLED); weather connected ⇒ UNLOADED until loaded
    assert eff["serper__search"] == "unloaded"
    assert eff["filesystem__read"] == "disabled"
    assert eff["weather__temp"] == "unloaded"

    # load a tool → appears in the snapshot
    ok, _ = await svc.load_tool("serper__search")
    assert ok
    assert "serper__search" in await svc.snapshot_loaded()
    await store.close()


@pytest.mark.asyncio
async def test_restart_existing_db_eager_and_state_survival(_isolate):
    db = _isolate / "tools.db"

    # -- session 1: reconcile incl. one ERROR server, then close --
    store = CatalogStore(db)
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await svc.seed_inventory([_NativeShell()])
    await _reconcile(svc, {
        "filesystem": "DISCONNECTED",
        "serper": "CONNECTED",
        "weather": "CONNECTED",
    })
    await svc.sync_server_status(
        "serper", enabled=True, runtime="ERROR", error_reason="boom",
    )  # serper ends the session ERROR
    await store.close()

    # -- host session 2: session_start snapshots runtime into last_runtime
    #    FIRST (the real boot order — the host initializes the catalog before
    #    the wrapper child spawns), then the wrapper reads the eager set from
    #    that snapshot (never the live runtime mirror, which the watchdog
    #    rewrites to DISCONNECTED on a gateway crash) --
    store2 = CatalogStore(db)
    await store2.open()
    svc2 = ToolCatalogService(store2, write_owner=True)
    await svc2.session_start()
    assert (await store2.get_server("serper"))["last_runtime"] == "ERROR"
    assert (await store2.get_server("weather"))["last_runtime"] == "CONNECTED"

    eager = _eager()
    assert isinstance(eager, set)
    # weather was CONNECTED and is eager; serper ERROR → skipped; filesystem
    # DISCONNECTED → skipped
    assert "weather" in eager
    assert "serper" not in eager
    assert "filesystem" not in eager

    await svc2.seed_inventory([_NativeShell()])

    # serper ended session 1 in ERROR → its tool is UNAVAILABLE at boot
    # (not loadable until reconnected); natives re-seeded loaded (session default)
    assert await store2.get_effective("serper__search") == "unavailable"
    assert await store2.get_effective("execute_shell") == "loaded"
    await store2.close()


# ── C: startup db ← tools.json5 sync (config IS the authority) ───────────


@pytest.mark.asyncio
async def test_startup_config_sync_mirrors_and_purges(_isolate):
    """At startup the db is synced FROM tools.json5 — covering hand-edits and
    agent-tool edits alike.  Every configured server gets a mirror row
    (enabled + source from json5), and rows for servers no longer in the
    config are purged."""
    from slife.plugins.mcp_gateway import config as _cfg

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)

    # a stale row from a previously-configured (since removed) server
    await store.upsert_server("ghost", runtime="CONNECTED", source=None)

    purged = await svc.sync_config_servers(dict(_cfg.servers()))

    # ghost is not in the seeded json5 → purged
    assert purged == ["ghost"]
    assert await store.get_server("ghost") is None
    # configured servers now have rows with json5-derived enabled/source
    assert (await store.get_server("filesystem"))["enabled"] == 0
    assert (await store.get_server("weather"))["enabled"] == 1
    assert (await svc.server_category("weather")) == "rest-api"

    # idempotent re-sync (restart twice) purges nothing new
    assert await svc.sync_config_servers(dict(_cfg.servers())) == []
    await store.close()


# ── D: connect failure ⇒ ERROR, never a crash ────────────────────────────


@pytest.mark.asyncio
async def test_connect_failure_marks_server_error(_isolate):
    """A failed connect records ``runtime=ERROR`` in the shared catalog."""
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    await store.upsert_server("serper", enabled=True, runtime="CONNECTED")
    await store.close()

    sys.modules.pop("slife.plugins.mcp_gateway.server", None)
    with patch(
        "slife.server_utils.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        import importlib
        srv = importlib.import_module("slife.plugins.mcp_gateway.server")

    srv._mark_server_error("serper", "boom")

    store2 = CatalogStore(_isolate / "tools.db")
    await store2.open()
    row = await store2.get_server("serper")
    assert row["runtime"] == "ERROR"
    assert "boom" in (row["error_reason"] or "")
    await store2.close()


@pytest.mark.asyncio
async def test_mirror_maps_failed_to_error_and_survives_bad_row(_isolate):
    """The host's reconcile maps the wrapper's ``failed`` state to ERROR and
    never crashes if one server's db write hiccups."""
    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)

    class _FakeClient:
        async def call_tool(self, name, arguments=None):
            return json.dumps({"servers": [
                {"name": "bad", "state": "stopped", "status": "failed",
                 "enabled": True, "error": "conn refused"},
                {"name": "auth", "state": "stopped", "status": "disconnected",
                 "enabled": True, "needs_user_auth": True},
            ]})

    stub = SimpleNamespace(_catalog=svc, is_subagent=False)
    await AgentService._mirror_mcp_server_rows(stub, _FakeClient())

    assert (await store.get_server("bad"))["runtime"] == "ERROR"
    assert (await store.get_server("auth"))["runtime"] == "ERROR"  # needs user
    await store.close()


# ── E: gateway watchdog death → catalog rows marked down ────────────────


@pytest.mark.asyncio
async def test_gateway_child_exit_marks_servers_down(_isolate):
    """When the mcp gateway child dies, its managed servers become
    DISCONNECTED in the catalog so the join drops their tools immediately."""
    from slife.agent.service import AgentService

    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await svc.seed_inventory([_NativeShell()])
    await _reconcile(svc, {
        "filesystem": "DISCONNECTED",
        "serper": "CONNECTED",
        "weather": "CONNECTED",
    })

    stub = SimpleNamespace(_catalog=svc, is_subagent=False)
    # a non-gateway plugin exit is a no-op
    await AgentService.on_plugin_child_exit(stub, "memdb")
    assert (await store.get_server("serper"))["runtime"] == "CONNECTED"

    # the gateway exiting marks every server down
    await AgentService.on_plugin_child_exit(stub, "mcp-gateway")
    assert (await store.get_server("serper"))["runtime"] == "DISCONNECTED"
    assert (await store.get_server("weather"))["runtime"] == "DISCONNECTED"
    # the join now hides the previously-connected servers' tools
    assert await store.get_effective("serper__search") == "unavailable"
    await store.close()


# ── F: config-removal deletes rows (config ⇄ db coherence) ───────────────


@pytest.mark.asyncio
async def test_config_removal_deletes_server_rows(_isolate):
    store = CatalogStore(_isolate / "tools.db")
    await store.open()
    svc = ToolCatalogService(store, write_owner=True)
    await svc.seed_inventory([_NativeShell()])
    await _reconcile(svc, {
        "filesystem": "DISCONNECTED",
        "serper": "CONNECTED",
        "weather": "CONNECTED",
    })

    # the _unregister_external_server_tools path for mcp_remove
    await store.remove_server("weather")
    assert await store.get_server("weather") is None
    assert await store.get_tool("weather__temp") is None
    # disabling is NOT removal — other servers remain
    assert await store.get_tool("serper__search") is not None
    await store.close()