"""Built-in plugin tools in the shared catalog — the ``plugin`` / ``job``
categories, ``source_id`` = the owning plugin, and the load/error semantics a
plugin row shares with every other function tool.

A plugin tool used to have NO row at all on the main agent (the mirror was
wired to the subagent HTTP-connect path only), and a row is what makes a tool
searchable and injectable — so the tools were invisible to the model.  These
tests pin the row shape and the edges that keeps honest: a skipped plugin
owns nothing, a dead plugin's rows say ``error``, and the unconfigured-source
purge never touches a plugin's rows.
"""

import json
from unittest.mock import MagicMock

import pytest
import pytest_asyncio

from slife.tools.catalog import CatalogStore, SERVER_CATEGORIES
from slife.tools.catalog_service import (
    JOB_PLUGIN_NAME,
    JOB_PLUGIN_OWN_TOOLS,
    JOB_TOOL_PREFIX,
    ToolCatalogService,
    plugin_category,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    s = CatalogStore(tmp_path / "tools.db")
    await s.open()
    yield s
    await s.close()


@pytest_asyncio.fixture
async def svc(db):
    return ToolCatalogService(db, write_owner=True)


def _plugin_rows(plugin, tools, *, status="unloaded", enabled=True):
    """The rows ``_mirror_plugin_tools_catalog`` builds for one plugin."""
    return [{
        "name": name,
        "description": "",
        "category": plugin_category(plugin, name),
        "source_id": plugin,
        "schema": json.dumps({"name": name, "description": "",
                              "inputSchema": {"type": "object", "properties": {}}}),
        "enabled": enabled,
        "status": status,
    } for name in tools]


# ── Category: a job vs the plugin's own tool ────────────────────────

def test_plugin_category_splits_jobs_from_the_plugins_own_tools():
    assert plugin_category(JOB_PLUGIN_NAME, "job-translate") == "job"
    assert plugin_category(JOB_PLUGIN_NAME, "job-summarize") == "job"
    # The four the plugin owns share the job- namespace with the jobs — the
    # prefix alone cannot separate them, the reserved set does.
    for own in sorted(JOB_PLUGIN_OWN_TOOLS):
        assert plugin_category(JOB_PLUGIN_NAME, own) == "plugin"
    # Every other plugin's tools are ``plugin``, whatever they are named.
    assert plugin_category("memdb", "turn_search") == "plugin"
    assert plugin_category("wechat", "wechat_login") == "plugin"
    assert plugin_category("mcp-gateway", "mcp_set") == "plugin"
    # …and only job-coding owns jobs.
    assert plugin_category("memdb", "job-translate") == "plugin"


def test_job_prefix_contract_matches_the_plugin():
    """The harness's copy of the naming and the plugin's own must agree — it is
    what tells a job row from the plugin's tool."""
    from slife.plugins.job_coding import registry, server

    assert JOB_TOOL_PREFIX == registry.JOB_TOOL_PREFIX
    assert registry.tool_name("translate") == "job-translate"
    assert registry.bare_name("job-translate") == "translate"
    # Every name the plugin reserves is one this side knows is not a job, and
    # the plugin reserves it against the EXPOSED name too (a job called
    # ``write`` would be exposed as ``job-write``).
    assert JOB_PLUGIN_OWN_TOOLS <= server._RESERVED_NAMES
    assert server._is_reserved("write") is True
    assert server._is_reserved("translate") is False


def test_catalog_category_routes_a_plugin_proxy():
    """The registered-instance twin (the boot seed's) agrees with the mirror."""
    from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute
    from slife.tools.catalog_service import catalog_category

    def proxy(name, server, route):
        tool = MagicMock(spec=MCPProxyTool)
        tool.name = name
        tool.server = server
        tool._route = route
        return tool

    assert catalog_category(proxy("turn_search", "memdb", ProxyRoute.DIRECT)) == "plugin"
    assert catalog_category(proxy("job-translate", JOB_PLUGIN_NAME, ProxyRoute.DIRECT)) == "job"
    assert catalog_category(proxy("job-list", JOB_PLUGIN_NAME, ProxyRoute.DIRECT)) == "plugin"
    assert catalog_category(proxy("gh__search", "gh", ProxyRoute.EXTERNAL)) == "mcp"


# ── Row shape and load semantics ────────────────────────────────────

@pytest.mark.asyncio
async def test_plugin_rows_are_searchable_born_unloaded(db, svc):
    """The point of the row: ``tool_search`` finds the plugin's tool, and it
    stays out of the tool list until the model loads it."""
    await db.reconcile(_plugin_rows("memdb", ["turn_search", "turn_count"]))

    hits = await db.search_keyword("turn_search")
    assert [h["name"] for h in hits] == ["turn_search"]
    assert hits[0]["category"] == "plugin"
    assert hits[0]["source_id"] == "memdb"
    assert hits[0]["status"] == "unloaded"
    assert "turn_search" not in await svc.snapshot_loaded()

    ok, _ = await svc.load_tool("turn_search")
    assert ok is True
    assert "turn_search" in await svc.snapshot_loaded()


@pytest.mark.asyncio
async def test_plugin_tool_disabled_in_its_section_reports_disabled(db, svc):
    """``enabled`` mirrors the section a plugin tool is configured in, so a
    disabled one refuses to load instead of silently injecting."""
    rows = _plugin_rows("memdb", ["turn_search"])
    rows[0]["enabled"] = False
    await db.reconcile(rows)

    assert await svc.effective_status("turn_search") == "disabled"
    ok, reason = await svc.load_tool("turn_search")
    assert ok is False and "is disabled" in reason


# ── Plugin down / up ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plugin_down_marks_its_rows_and_ready_resets_them(db, svc):
    """A dead plugin's tools must not read as merely unloaded: rows go
    ``error`` (searchable, not loadable), and the reconnect clears the mark —
    per row, since each tool carries its own default."""
    rows = _plugin_rows("memdb", ["turn_search", "turn_count"])
    await db.reconcile(rows)
    await svc.load_tool("turn_count")

    marked = await svc.mark_source_error("memdb")
    assert marked == 2
    assert await svc.effective_status("turn_search") == "error"
    ok, reason = await svc.load_tool("turn_search")
    assert ok is False and "is not up right now" in reason

    reset = await svc.mark_plugin_connected("memdb")
    # The autoloaded/loaded one is NOT remembered — a plugin restart resets to
    # the default, exactly as an MCP server reconnect does.
    assert reset == 2
    assert await svc.effective_status("turn_search") == "unloaded"
    assert await svc.effective_status("turn_count") == "unloaded"


@pytest.mark.asyncio
async def test_mark_plugin_connected_honours_per_tool_autoload(db, svc):
    """Per-row defaults are why a plugin uses its own reset: an ``autoload``
    tool comes back loaded, its neighbour does not."""
    svc.autoload = frozenset({"turn_count"})
    await db.reconcile(_plugin_rows("memdb", ["turn_search", "turn_count"]))
    await svc.mark_source_error("memdb")

    await svc.mark_plugin_connected("memdb")

    assert await svc.effective_status("turn_count") == "loaded"
    assert await svc.effective_status("turn_search") == "unloaded"


@pytest.mark.asyncio
async def test_plugin_rows_are_spared_by_the_unconfigured_source_purge(db, svc):
    """``purge_missing_sources`` keeps only the CONFIGURED mcp/rest servers —
    plugin rows carry a source_id too, so without the category scoping every
    plugin tool would be deleted on every boot and every gateway reconcile."""
    await db.reconcile(_plugin_rows("memdb", ["turn_search"]))
    await db.reconcile([
        {"name": "gh__search", "description": "", "category": "mcp",
         "source_id": "gh", "schema": "", "enabled": None,
         "status": "unloaded"},
        {"name": "gone__tool", "description": "", "category": "mcp",
         "source_id": "gone", "schema": "", "enabled": None,
         "status": "unloaded"},
    ])

    assert await db.list_source_ids() == {"gh", "gone"}          # servers only
    assert await db.list_source_ids(categories=None) == {"gh", "gone", "memdb"}

    purged = await svc.purge_unconfigured_sources({"gh"})
    assert purged == {"gone"}
    assert await db.get_tool("turn_search") is not None
    assert await db.get_tool("gone__tool") is None


@pytest.mark.asyncio
async def test_gateway_death_does_not_touch_plugin_rows(db, svc):
    """``plugin`` is a source owner, not an EXTERNAL one: the gateway dying
    must mark the external rows, never memdb's."""
    await db.reconcile(_plugin_rows("memdb", ["turn_search"]))
    await db.reconcile([
        {"name": "gh__search", "description": "", "category": "mcp",
         "source_id": "gh", "schema": "", "enabled": None,
         "status": "unloaded"},
    ])

    marked = await svc.mark_all_external_error()

    assert marked == 1
    assert await svc.effective_status("gh__search") == "error"
    assert await svc.effective_status("turn_search") == "unloaded"


@pytest.mark.asyncio
async def test_purge_source_drops_a_skipped_plugins_rows(db, svc):
    """A plugin that never starts owns no rows — otherwise tool_search keeps
    offering tools that cannot run."""
    await db.reconcile(_plugin_rows("wechat", ["wechat_login"]))

    assert await svc.purge_source("wechat") == 1
    assert await db.get_tool("wechat_login") is None
    assert SERVER_CATEGORIES == frozenset({"mcp", "rest-api"})
