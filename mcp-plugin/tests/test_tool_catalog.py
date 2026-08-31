"""Tool catalog tools — mcp_tool_search / __mcp_get_tool and the
__mcp_call_tool disabled guard."""

import importlib
import json
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from mcp_plugin.connection import ServerStatus
from mcp_plugin.store import ToolStore


@pytest.fixture
def restore_root_logger():
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)


def _import_mcp_server():
    sys.modules.pop("mcp_plugin.server", None)
    with patch(
        "mcp_plugin.server_runtime.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        return importlib.import_module("mcp_plugin.server")


class _FakeConn:
    status = ServerStatus.CONNECTED

    def __init__(self, tools):
        self._tools = tools

    def list_tools(self):
        return list(self._tools)


class _FakePool:
    def __init__(self):
        self._connections = {}
        self.calls = []

    def get_server(self, name):
        return self._connections.get(name)

    async def call_tool(self, server, tool_name, arguments):
        self.calls.append((server, tool_name))
        return f'[fake] {server}__{tool_name} ok'


@pytest_asyncio.fixture
async def srv(tmp_path, restore_root_logger):
    """Server module with a real temp store + fake pool of two servers."""
    s = _import_mcp_server()
    store = ToolStore(tmp_path / "tools.db")
    await store.open()
    await store.sync_server("svcA", [
        {"name": "search", "description": "github repository search"},
        {"name": "list", "description": "list issues"},
    ])
    await store.sync_server("svcB", [
        {"name": "search", "description": "search the web"},
    ])
    s._store = store
    pool = _FakePool()
    pool._connections = {
        "svcA": _FakeConn([
            {"name": "search", "description": "github repository search",
             "inputSchema": {"type": "object", "properties": {}}},
            {"name": "list", "description": "list issues",
             "inputSchema": {"type": "object", "properties": {}}},
        ]),
        "svcB": _FakeConn([
            {"name": "search", "description": "search the web",
             "inputSchema": {"type": "object", "properties": {}}},
        ]),
    }
    with patch.object(s, "_pool", pool):
        yield s, store
    await store.close()


@pytest.mark.asyncio
async def test_tool_search_hybrid_returns_distinct_full_names(srv):
    s, _store = srv
    raw = await s.mcp_tool_search("search", mode="hybrid")
    data = json.loads(raw)
    assert data["status"] == "ok"
    names = {r["full_name"] for r in data["results"]}
    # Same bare name across two servers → two distinct catalog entries.
    assert "svcA__search" in names and "svcB__search" in names


@pytest.mark.asyncio
async def test_tool_search_server_filter(srv):
    s, _store = srv
    raw = await s.mcp_tool_search("search", server="svcB")
    data = json.loads(raw)
    assert [r["full_name"] for r in data["results"]] == ["svcB__search"]


@pytest.mark.asyncio
async def test_tool_search_include_disabled(srv):
    s, store = srv
    await store.set_tool_enabled("svcA__search", False)
    raw = await s.mcp_tool_search("search", include_disabled=False)
    data = json.loads(raw)
    assert "svcA__search" not in {r["full_name"] for r in data["results"]}
    raw = await s.mcp_tool_search("search", include_disabled=True)
    data = json.loads(raw)
    assert "svcA__search" in {r["full_name"] for r in data["results"]}


@pytest.mark.asyncio
async def test_call_tool_refuses_disabled(srv):
    s, store = srv
    await store.set_tool_enabled("svcA__search", False)
    raw = await s.__mcp_call_tool("svcA", "search", "{}")
    data = json.loads(raw)
    assert data["status"] == "error"
    assert "disabled" in data["error"]


@pytest.mark.asyncio
async def test_call_tool_allows_enabled(srv):
    s, _store = srv
    raw = await s.__mcp_call_tool("svcA", "list", "{}")
    assert raw == "[fake] svcA__list ok"


@pytest.mark.asyncio
async def test_call_tool_passes_when_no_store(restore_root_logger):
    s = _import_mcp_server()
    pool = _FakePool()
    pool._connections = {"svcA": _FakeConn([{"name": "list"}])}
    with (
        patch.object(s, "_pool", pool),
        patch.object(s, "_ensure_store", AsyncMock(return_value=None)),  # DB failed to open
    ):
        # No store → default-enabled, call proceeds.
        raw = await s.__mcp_call_tool("svcA", "list", "{}")
    assert raw == "[fake] svcA__list ok"


@pytest.mark.asyncio
async def test_get_tool_returns_schema_and_enabled(srv):
    s, _store = srv
    raw = await s.__mcp_get_tool("svcA__search")
    data = json.loads(raw)
    assert data["status"] == "ok"
    assert data["server"] == "svcA"
    assert data["name"] == "search"
    assert data["enabled"] is True
    assert isinstance(data["inputSchema"], dict)

    await _store.set_tool_enabled("svcA__search", False)
    raw = await s.__mcp_get_tool("svcA__search")
    data = json.loads(raw)
    assert data["enabled"] is False


@pytest.mark.asyncio
async def test_get_tool_unknown(srv):
    s, _store = srv
    raw = await s.__mcp_get_tool("svcA__nonexistent")
    assert json.loads(raw)["status"] == "error"
    raw = await s.__mcp_get_tool("svcZ__search")  # server not connected
    assert json.loads(raw)["status"] == "error"


class TestAnnotateScores:
    """annotate_scores 0–1 normalizes the semantic distance (the MCP
    plugin's copy of the slife-side contract: cosine distance → true
    cosine similarity)."""

    def test_cosine_maps_to_cosine_similarity(self):
        from mcp_plugin.search import annotate_scores
        assert annotate_scores([{"distance": 0.0}])[0]["similarity"] == 1.0
        assert annotate_scores([{"distance": 0.3}])[0]["similarity"] == 0.7
        # Opposite vectors (cosine distance > 1) clip to 0.
        assert annotate_scores([{"distance": 1.3}])[0]["similarity"] == 0.0

    def test_keyword_only_results_untouched(self):
        from mcp_plugin.search import annotate_scores
        results = annotate_scores([{"full_name": "a", "distance": None}])
        assert "similarity" not in results[0]
