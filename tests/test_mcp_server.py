"""Tests for slife.plugins.mcp.server — wrapper-server tool registration.

Regression test for a decorator-detachment bug: the
``@mcp.tool(name="mcp_add_server")`` decorator must bind to the
``mcp_add_server`` function.  When a helper (``_server_config_equal``)
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
from unittest.mock import patch

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
    """mcp_add_server must be registered with its real signature."""

    @pytest.mark.asyncio
    async def test_add_server_has_real_parameters(self, restore_root_logger):
        srv = _import_mcp_server()
        tools = await srv.mcp.list_tools()
        by_name = {t.name: t for t in tools}

        assert "mcp_add_server" in by_name
        props = by_name["mcp_add_server"].parameters.get("properties", {})
        # The real function's parameters.  The helper had only (a, b) —
        # if the decorator is mis-bound these are all absent.
        for expected in (
            "name", "command", "args", "env", "url",
            "headers", "description", "activate", "enabled",
        ):
            assert expected in props, f"missing param: {expected}"
        assert by_name["mcp_add_server"].parameters.get("required") == ["name"]

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
            "mcp_add_server",
            "mcp_remove_server",
            "mcp_list_servers",
            "mcp_list_tools",
            "mcp_call_tool",
            "mcp_set_server",
        }
