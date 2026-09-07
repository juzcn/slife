"""Tests for Slife.mcp.tool_adapter — MCPProxyTool and create_proxy_tools."""

import pytest; pytestmark = pytest.mark.unit


import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute, create_proxy_tools
from slife.tools.base import Tool


# ── Helpers ─────────────────────────────────────────────────────────────────


def make_mock_mcp_client():
    """Create a mock MCPClient."""
    client = MagicMock()
    client.call_tool = AsyncMock()
    return client


def make_tool_info(server="test_server", name="test_tool", description="A test tool", input_schema=None):
    """Create a tool info dict for proxy creation."""
    if input_schema is None:
        input_schema = {
            "type": "object",
            "properties": {"arg1": {"type": "string"}},
            "required": ["arg1"],
        }
    return {
        "server": server,
        "name": name,
        "description": description,
        "inputSchema": input_schema,
    }


# ── MCPProxyTool tests ──────────────────────────────────────────────────────


class TestMCPProxyToolConstruction:
    """Tests for MCPProxyTool.__init__ and construction."""

    def test_namespaced_name(self):
        info = make_tool_info(server="filesystem", name="read_file")
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)

        assert tool.name == "filesystem__read_file"

    def test_external_server_tool_cannot_shadow_native(self):
        """An external server whose tool happens to start with the server name
        still gets the ``{server}__{tool}`` namespace — never registered as-is,
        which would shadow a native tool and break ``{name}__`` unregistration
        (REVIEW NEW-H4)."""
        info = make_tool_info(server="check", name="check_mcp")
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)  # default route = EXTERNAL

        assert tool.name == "check__check_mcp"

    def test_direct_server_keeps_as_is_name(self):
        """Built-in plugin tools that already carry their server prefix keep it
        as-is — no redundant ``wechat__wechat_login`` (REVIEW F-prefix)."""
        info = make_tool_info(server="wechat", name="wechat_login")
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info, route=ProxyRoute.DIRECT)

        assert tool.name == "wechat_login"

    def test_direct_builtin_registers_bare_name(self):
        """Built-in plugin tools (DIRECT) register under their bare semantic
        name — no ``{server}__`` prefix (e.g. ``memdb__turn_search`` →
        ``turn_search``)."""
        info = make_tool_info(server="memdb", name="turn_search")
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info, route=ProxyRoute.DIRECT)

        assert tool.name == "turn_search"

    def test_external_server_keeps_full_namespace(self):
        """External MCP server tools (EXTERNAL) ALWAYS keep the full
        ``{server}__{tool}`` namespace — never a bare name, so a
        server-supplied tool can't shadow a native one (e.g. github's
        ``create_issue`` → ``github__create_issue``)."""
        info = make_tool_info(server="github", name="create_issue")
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info, route=ProxyRoute.EXTERNAL)

        assert tool.name == "github__create_issue"

    def test_description_prefixed_with_server(self):
        info = make_tool_info(server="memdb", name="save", description="Save data")
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)

        assert tool.description == "[memdb] Save data"

    def test_empty_description(self):
        info = make_tool_info(server="x", name="y", description="")
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)

        assert tool.description == "[x] "

    def test_input_schema_not_object_type(self):
        """Non-object schema gets wrapped."""
        info = make_tool_info(input_schema={"type": "string", "properties": {}})
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)

        assert tool.parameters["type"] == "object"
        assert "properties" in tool.parameters

    def test_input_schema_missing_type(self):
        """Missing type gets corrected."""
        info = make_tool_info(input_schema={"properties": {"a": {"type": "int"}}})
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)

        assert tool.parameters["type"] == "object"

    def test_skip_auto_register_is_true(self):
        info = make_tool_info()
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)

        assert tool._skip_auto_register is True

    def test_stores_callbacks(self):
        info = make_tool_info()
        client = make_mock_mcp_client()
        on_add = AsyncMock()
        on_remove = AsyncMock()
        on_upd = AsyncMock()

        tool = MCPProxyTool(
            client, info,
            on_server_added=on_add,
            on_server_removed=on_remove,
            on_server_updated=on_upd,
        )

        assert tool._on_server_added is on_add
        assert tool._on_server_removed is on_remove
        assert tool._on_server_updated is on_upd


class TestMCPProxyToolToOpenaiFunction:
    """Tests for to_openai_function."""

    def test_returns_function_dict(self):
        info = make_tool_info(
            server="mem", name="remember",
            description="Remember something",
            input_schema={"type": "object", "properties": {"text": {"type": "string"}}},
        )
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)

        result = tool.to_openai_function()

        assert result["type"] == "function"
        fn = result["function"]
        assert fn["name"] == "mem__remember"
        assert fn["description"] == "[mem] Remember something"
        assert "properties" in fn["parameters"]


# ── Execute tests ──────────────────────────────────────────────────────────


class TestMCPProxyToolExecute:
    """Tests for MCPProxyTool.execute."""

    @pytest.mark.asyncio
    async def test_wrapper_tool_calls_directly(self):
        """Server='mcp' tools call the MCP client directly."""
        info = make_tool_info(server="mcp", name="mcp_list_tools")
        client = make_mock_mcp_client()
        client.call_tool.return_value = '{"tools":[]}'

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER)
        result = await tool.execute(server="filesystem")

        client.call_tool.assert_called_once_with("mcp_list_tools", {"server": "filesystem"})
        assert result == '{"tools":[]}'

    @pytest.mark.asyncio
    async def test_external_tool_routes_via_harness_call_tool(self):
        """Non-mcp servers route through the harness __mcp_call_tool tool."""
        info = make_tool_info(server="filesystem", name="read_file")
        client = make_mock_mcp_client()
        client.call_tool.return_value = "file contents"

        tool = MCPProxyTool(client, info)
        result = await tool.execute(path="/tmp/test.txt")

        args = client.call_tool.call_args[0]
        assert args[0] == "__mcp_call_tool"
        assert args[1]["server"] == "filesystem"
        assert args[1]["tool_name"] == "read_file"

    @pytest.mark.asyncio
    async def test_mcp_set_persists_on_success(self):
        """mcp_set triggers persistence callback on success."""
        info = make_tool_info(server="mcp", name="mcp_set")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "connected"})
        on_add = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_added=on_add)
        result = await tool.execute(
            name="myserver", command="python",
            args=["-m", "myserver"], env={"KEY": "VAL"},
            description="My server", source={"url": "http://example.com"},
        )

        on_add.assert_called_once()
        call_args = on_add.call_args
        assert call_args.kwargs["name"] == "myserver"
        assert call_args.kwargs["command"] == "python"
        assert call_args.kwargs["args"] == ["-m", "myserver"]
        assert call_args.kwargs["env"] == {"KEY": "VAL"}
        assert call_args.kwargs["source"] == {"url": "http://example.com"}

    @pytest.mark.asyncio
    async def test_mcp_set_skips_persist_on_failure(self):
        """Failed connection does not trigger persist."""
        info = make_tool_info(server="mcp", name="mcp_set")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "error", "error": "boom"})
        on_add = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_added=on_add)
        await tool.execute(name="bad", command="badcmd")

        on_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_set_handles_parse_error(self):
        """Handles JSON parse errors gracefully."""
        info = make_tool_info(server="mcp", name="mcp_set")
        client = make_mock_mcp_client()
        client.call_tool.return_value = "not json"
        on_add = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_added=on_add)
        result = await tool.execute(name="test")

        assert result == "not json"
        on_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_set_disabled_triggers_update(self):
        """mcp_set disabled status triggers on_server_updated(False)."""
        info = make_tool_info(server="mcp", name="mcp_set")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "disabled"})
        on_upd = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_updated=on_upd)
        await tool.execute(name="myserver", enabled=False)

        on_upd.assert_called_once_with(name="myserver", enabled=False)

    @pytest.mark.asyncio
    async def test_mcp_set_enabled_connected_triggers_update(self):
        """mcp_set_enabled connected triggers on_server_updated(True)."""
        info = make_tool_info(server="mcp", name="mcp_set_enabled")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "connected"})
        on_upd = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_updated=on_upd)
        await tool.execute(name="myserver", enabled=True)

        on_upd.assert_called_once_with(name="myserver", enabled=True)

    @pytest.mark.asyncio
    async def test_mcp_set_enabled_disabled_triggers_update(self):
        """mcp_set_enabled disabled triggers on_server_updated(False)."""
        info = make_tool_info(server="mcp", name="mcp_set_enabled")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "disabled"})
        on_upd = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_updated=on_upd)
        await tool.execute(name="myserver", enabled=False)

        on_upd.assert_called_once_with(name="myserver", enabled=False)

    @pytest.mark.asyncio
    async def test_mcp_remove_triggers_callback(self):
        """mcp_remove triggers removal callback."""
        info = make_tool_info(server="mcp", name="mcp_remove")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "removed"})
        on_remove = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_removed=on_remove)
        await tool.execute(name="oldserver")

        on_remove.assert_called_once_with(name="oldserver")

    @pytest.mark.asyncio
    async def test_mcp_remove_skips_on_failure(self):
        """Non-removed status skips callback."""
        info = make_tool_info(server="mcp", name="mcp_remove")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "not_found"})
        on_remove = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_removed=on_remove)
        await tool.execute(name="missing")

        on_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_source_stripped_for_wrapper(self):
        """Source dict is stripped from kwargs for wrapper tools."""
        info = make_tool_info(server="mcp", name="mcp_set")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "connected"})

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER)
        await tool.execute(name="test", command="cmd", source={"url": "x"})

        # source key should not be passed to the MCP client
        call_kwargs = client.call_tool.call_args[0][1]
        assert "source" not in call_kwargs

    @pytest.mark.asyncio
    async def test_source_not_a_dict_stripped_from_mcp_call(self):
        """source that isn't a dict is still stripped from kwargs — callback gets None."""
        info = make_tool_info(server="mcp", name="mcp_set")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "connected"})

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER)
        await tool.execute(name="test", source="string-source")

        # source is always stripped from MCP client call
        call_kwargs = client.call_tool.call_args[0][1]
        assert "source" not in call_kwargs

    @pytest.mark.asyncio
    async def test_memory_server_calls_directly(self):
        """Memory server tools call the MCP client directly (no routing layer)."""
        info = make_tool_info(server="memdb", name="memory_search", description="Search")
        client = make_mock_mcp_client()
        client.call_tool.return_value = "search results"

        tool = MCPProxyTool(client, info, route=ProxyRoute.DIRECT)
        result = await tool.execute(query="test query")

        client.call_tool.assert_called_once_with("memory_search", {"query": "test query"})
        assert result == "search results"

    @pytest.mark.asyncio
    async def test_handle_add_server_callback_exception_swallowed(self):
        """Exceptions in on_server_added callback are swallowed."""
        info = make_tool_info(server="mcp", name="mcp_set")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "connected"})
        on_add = AsyncMock(side_effect=RuntimeError("callback exploded"))

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_added=on_add)
        # Should not raise
        result = await tool.execute(name="test", command="cmd")
        assert json.loads(result)["status"] == "connected"

    @pytest.mark.asyncio
    async def test_handle_remove_server_callback_exception_swallowed(self):
        """Exceptions in on_server_removed callback are swallowed."""
        info = make_tool_info(server="mcp", name="mcp_remove")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "removed"})
        on_remove = AsyncMock(side_effect=RuntimeError("callback error"))

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_removed=on_remove)
        # Should not raise
        result = await tool.execute(name="old")
        assert json.loads(result)["status"] == "removed"

    @pytest.mark.asyncio
    async def test_handle_remove_server_parse_error(self):
        """Parse error in remove_server result is handled gracefully."""
        info = make_tool_info(server="mcp", name="mcp_remove")
        client = make_mock_mcp_client()
        client.call_tool.return_value = "not valid json"
        on_remove = AsyncMock()

        tool = MCPProxyTool(client, info, route=ProxyRoute.WRAPPER, on_server_removed=on_remove)
        await tool.execute(name="test")
        on_remove.assert_not_called()


# ── create_proxy_tools ──────────────────────────────────────────────────────


class TestCreateProxyTools:
    """Tests for create_proxy_tools factory function."""

    def test_creates_proxy_tools_from_list(self):
        client = make_mock_mcp_client()
        tools_list = [
            {"server": "srv1", "name": "tool_a", "description": "A", "inputSchema": {}},
            {"server": "srv1", "name": "tool_b", "description": "B", "inputSchema": {}},
            {"server": "srv2", "name": "tool_c", "description": "C", "inputSchema": {}},
        ]

        result = create_proxy_tools(client, tools_list)

        assert len(result) == 3
        assert all(isinstance(t, MCPProxyTool) for t in result)
        assert result[0].name == "srv1__tool_a"
        assert result[1].name == "srv1__tool_b"
        assert result[2].name == "srv2__tool_c"

    def test_passes_callbacks_through(self):
        client = make_mock_mcp_client()
        on_add = AsyncMock()
        on_remove = AsyncMock()
        on_upd = AsyncMock()
        tools_list = [{"server": "srv", "name": "t", "description": "", "inputSchema": {}}]

        result = create_proxy_tools(
            client, tools_list,
            on_server_added=on_add,
            on_server_removed=on_remove,
            on_server_updated=on_upd,
        )

        assert result[0]._on_server_added is on_add
        assert result[0]._on_server_removed is on_remove
        assert result[0]._on_server_updated is on_upd

    def test_empty_list_returns_empty(self):
        client = make_mock_mcp_client()
        result = create_proxy_tools(client, [])
        assert result == []
