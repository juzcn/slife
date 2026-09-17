"""Wrapper execution gate tests — __mcp_call_tool's per-mcp disabled guard,
plus the shared hybrid-score annotator (moved with the catalog).

The wrapper's search/load surface (mcp_tool_search / __mcp_get_tool / the
in-memory ToolStore) was retired with the unified host catalog — search is
tested host-side (``test_tools_meta`` / ``test_tools_catalog``).
"""

import importlib
import json
import logging
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import pytest_asyncio

from slife.plugins.mcp_gateway.connection import ServerConfig


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
    sys.modules.pop("slife.plugins.mcp_gateway.server", None)
    with patch(
        "slife.server_utils.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        return importlib.import_module("slife.plugins.mcp_gateway.server")


class _FakeConn:
    tools_ok = True

    def __init__(self, tools, enabled=True):
        self.config = ServerConfig(name="x", command="x", enabled=enabled)
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
        return f"[fake] {server}__{tool_name} ok"


@pytest.mark.asyncio
async def test_call_tool_refuses_disabled_server(restore_root_logger):
    s = _import_mcp_server()
    pool = _FakePool()
    pool._connections = {
        "svcA": _FakeConn([{"name": "search"}], enabled=False),
    }
    with patch.object(s, "_pool", pool):
        raw = await s.__mcp_call_tool("svcA", "search", "{}")
    data = json.loads(raw)
    assert data["status"] == "error"
    assert "disabled" in data["error"]


@pytest.mark.asyncio
async def test_call_tool_allows_enabled(restore_root_logger):
    s = _import_mcp_server()
    pool = _FakePool()
    pool._connections = {"svcA": _FakeConn([{"name": "list"}], enabled=True)}
    with patch.object(s, "_pool", pool):
        raw = await s.__mcp_call_tool("svcA", "list", "{}")
    assert raw == "[fake] svcA__list ok"


@pytest.mark.asyncio
async def test_call_tool_passes_when_server_not_registered(restore_root_logger):
    """An unregistered server name (reconcile hasn't seen it) is default —
    no catalog gate anymore, the pool config decides; an unknown name is
    allowed through to the pool which will raise if truly absent."""
    s = _import_mcp_server()
    pool = _FakePool()
    with patch.object(s, "_pool", pool):
        raw = await s.__mcp_call_tool("ghost", "list", "{}")
    assert raw == "[fake] ghost__list ok"


class TestAnnotateScores:
    """annotate_scores 0–1 normalizes the cosine distance — the shared score
    contract (now hosted with the catalog)."""

    def test_cosine_maps_to_cosine_similarity(self):
        from slife.tools.catalog_search import annotate_scores
        assert annotate_scores([{"distance": 0.0}])[0]["similarity"] == 1.0
        assert annotate_scores([{"distance": 0.3}])[0]["similarity"] == 0.7
        # Opposite vectors (cosine distance > 1) clip to 0.
        assert annotate_scores([{"distance": 1.3}])[0]["similarity"] == 0.0

    def test_keyword_only_results_untouched(self):
        from slife.tools.catalog_search import annotate_scores
        results = annotate_scores([{"full_name": "a", "distance": None}])
        assert "similarity" not in results[0]