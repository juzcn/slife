"""Tests for slife.plugins.mcp.server — wrapper-server tool registration.

Regression test for a decorator-detachment bug: the
``@mcp.tool(name="mcp_set")`` decorator must bind to the
``mcp_set`` function.  When a helper (``_server_config_equal``)
was accidentally placed between the decorator and the function, the tool
was registered with the helper's ``(a, b)`` signature, so every startup
auto-connect call failed pydantic validation ("Missing required argument
'a'") and no external MCP server could load.
"""

import pytest; pytestmark = pytest.mark.unit


import importlib
import logging
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def restore_root_logger():
    """Importing the server reconfigures logging — restore it afterwards."""
    root = logging.getLogger()
    original_handlers = list(root.handlers)
    original_level = root.level
    yield
    root.handlers.clear()
    root.handlers.extend(original_handlers)
    root.setLevel(original_level)


def _import_mcp_server():
    """Import the wrapper server fresh, stubbing the logging side-effect."""
    sys.modules.pop("slife.plugins.mcp.server", None)
    with patch(
        "slife.server_utils.setup_server_logging",
        return_value=Path("unused.log"),
    ):
        return importlib.import_module("slife.plugins.mcp.server")


class TestAddServerToolRegistration:
    """mcp_set must be registered with its real signature."""

    @pytest.mark.asyncio
    async def test_add_server_has_real_parameters(self, restore_root_logger):
        srv = _import_mcp_server()
        tools = await srv.mcp.list_tools()
        by_name = {t.name: t for t in tools}

        assert "mcp_set" in by_name
        props = by_name["mcp_set"].parameters.get("properties", {})
        # The real function's parameters.  The helper had only (a, b) —
        # if the decorator is mis-bound these are all absent.
        for expected in (
            "name", "command", "args", "env", "url",
            "headers", "description", "enabled",
        ):
            assert expected in props, f"missing param: {expected}"
        assert by_name["mcp_set"].parameters.get("required") == ["name"]

    @pytest.mark.asyncio
    async def test_helper_not_exposed_as_tool(self, restore_root_logger):
        srv = _import_mcp_server()
        tools = await srv.mcp.list_tools()
        names = {t.name for t in tools}
        assert "_server_config_equal" not in names

    @pytest.mark.asyncio
    async def test_expected_tool_set(self, restore_root_logger):
        srv = _import_mcp_server()
        tools = await srv.mcp.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "mcp_set",
            "mcp_set_enabled",
            "mcp_remove",
            "mcp_list",
            "mcp_list_tools",
            "__mcp_call_tool",
            "__mcp_connection_status",
        }

    @pytest.mark.asyncio
    async def test_mcp_set_rejects_reserved_builtin_names(self, restore_root_logger):
        """REVIEW C8 — an external server cannot take a built-in plugin name,
        or its tools would collide/misroute in the harness namespace."""
        import json as _json

        srv = _import_mcp_server()
        for reserved in ("mcp", "memdb", "wechat", "memfiles", "a2a", "media"):
            result = await getattr(srv, "mcp_set")(name=reserved, command="echo")
            parsed = _json.loads(result)
            assert parsed.get("status") == "error", reserved
            assert "reserved" in parsed.get("error", ""), reserved

    @pytest.mark.asyncio
    async def test_lifespan_shuts_down_pool(self, restore_root_logger):
        """REVIEW M3 — the plugin lifespan releases the connection pool on
        server shutdown, so HTTP/SSE/stdio connections don't leak."""
        srv = _import_mcp_server()
        with patch.object(srv, "_pool") as mock_pool:
            mock_pool.shutdown = AsyncMock()
            async with srv._mcp_lifespan(None):
                pass
            mock_pool.shutdown.assert_awaited_once()
