"""Tests for Slife.mcp.tool_adapter — MCPProxyTool and create_proxy_tools."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.mcp.tool_adapter import MCPProxyTool, create_proxy_tools
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

    def test_description_prefixed_with_server(self):
        info = make_tool_info(server="memory", name="save", description="Save data")
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, info)

        assert tool.description == "[memory] Save data"

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
        on_disc = AsyncMock()

        tool = MCPProxyTool(
            client, info,
            on_server_added=on_add,
            on_server_removed=on_remove,
            on_server_disclosure_changed=on_disc,
        )

        assert tool._on_server_added is on_add
        assert tool._on_server_removed is on_remove
        assert tool._on_server_disclosure_changed is on_disc


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

        tool = MCPProxyTool(client, info)
        result = await tool.execute(server="filesystem")

        client.call_tool.assert_called_once_with("mcp_list_tools", {"server": "filesystem"})
        assert result == '{"tools":[]}'

    @pytest.mark.asyncio
    async def test_external_tool_routes_via_mcp_call_tool(self):
        """Non-mcp servers route through mcp_call_tool."""
        info = make_tool_info(server="filesystem", name="read_file")
        client = make_mock_mcp_client()
        client.call_tool.return_value = "file contents"

        tool = MCPProxyTool(client, info)
        result = await tool.execute(path="/tmp/test.txt")

        args = client.call_tool.call_args[0]
        assert args[0] == "mcp_call_tool"
        assert args[1]["server"] == "filesystem"
        assert args[1]["tool_name"] == "read_file"

    @pytest.mark.asyncio
    async def test_mcp_add_server_persists_on_success(self):
        """mcp_add_server triggers persistence callback on success."""
        info = make_tool_info(server="mcp", name="mcp_add_server")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "connected"})
        on_add = AsyncMock()

        tool = MCPProxyTool(client, info, on_server_added=on_add)
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
    async def test_mcp_add_server_skips_persist_on_failure(self):
        """Failed connection does not trigger persist."""
        info = make_tool_info(server="mcp", name="mcp_add_server")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "error", "error": "boom"})
        on_add = AsyncMock()

        tool = MCPProxyTool(client, info, on_server_added=on_add)
        await tool.execute(name="bad", command="badcmd")

        on_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_add_server_handles_parse_error(self):
        """Handles JSON parse errors gracefully."""
        info = make_tool_info(server="mcp", name="mcp_add_server")
        client = make_mock_mcp_client()
        client.call_tool.return_value = "not json"
        on_add = AsyncMock()

        tool = MCPProxyTool(client, info, on_server_added=on_add)
        result = await tool.execute(name="test")

        assert result == "not json"
        on_add.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_remove_server_triggers_callback(self):
        """mcp_remove_server triggers removal callback."""
        info = make_tool_info(server="mcp", name="mcp_remove_server")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "removed"})
        on_remove = AsyncMock()

        tool = MCPProxyTool(client, info, on_server_removed=on_remove)
        await tool.execute(name="oldserver")

        on_remove.assert_called_once_with(name="oldserver")

    @pytest.mark.asyncio
    async def test_mcp_remove_server_skips_on_failure(self):
        """Non-removed status skips callback."""
        info = make_tool_info(server="mcp", name="mcp_remove_server")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "not_found"})
        on_remove = AsyncMock()

        tool = MCPProxyTool(client, info, on_server_removed=on_remove)
        await tool.execute(name="missing")

        on_remove.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_set_disclosure_triggers_callback(self):
        """mcp_set_disclosure triggers disclosure callback."""
        info = make_tool_info(server="mcp", name="mcp_set_disclosure")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"disclosure": "lazy"})
        on_disc = AsyncMock()

        tool = MCPProxyTool(client, info, on_server_disclosure_changed=on_disc)
        await tool.execute(name="myserver", disclosure="lazy")

        on_disc.assert_called_once_with(name="myserver", disclosure="lazy")

    @pytest.mark.asyncio
    async def test_mcp_set_disclosure_skips_non_eager_lazy(self):
        """Invalid disclosure value skips callback."""
        info = make_tool_info(server="mcp", name="mcp_set_disclosure")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"disclosure": "invalid"})
        on_disc = AsyncMock()

        tool = MCPProxyTool(client, info, on_server_disclosure_changed=on_disc)
        await tool.execute(name="test")

        on_disc.assert_not_called()

    @pytest.mark.asyncio
    async def test_mcp_set_disclosure_handles_parse_error(self):
        """Gracefully handles parse error on disclosure."""
        info = make_tool_info(server="mcp", name="mcp_set_disclosure")
        client = make_mock_mcp_client()
        client.call_tool.return_value = "bad json"
        on_disc = AsyncMock()

        tool = MCPProxyTool(client, info, on_server_disclosure_changed=on_disc)
        await tool.execute(name="test")

        on_disc.assert_not_called()

    @pytest.mark.asyncio
    async def test_execute_source_stripped_for_wrapper(self):
        """Source dict is stripped from kwargs for wrapper tools."""
        info = make_tool_info(server="mcp", name="mcp_add_server")
        client = make_mock_mcp_client()
        client.call_tool.return_value = json.dumps({"status": "connected"})

        tool = MCPProxyTool(client, info)
        await tool.execute(name="test", command="cmd", source={"url": "x"})

        # source key should not be passed to the MCP client
        call_kwargs = client.call_tool.call_args[0][1]
        assert "source" not in call_kwargs


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
        on_disc = AsyncMock()
        tools_list = [{"server": "srv", "name": "t", "description": "", "inputSchema": {}}]

        result = create_proxy_tools(
            client, tools_list,
            on_server_added=on_add,
            on_server_removed=on_remove,
            on_server_disclosure_changed=on_disc,
        )

        assert result[0]._on_server_added is on_add
        assert result[0]._on_server_removed is on_remove
        assert result[0]._on_server_disclosure_changed is on_disc

    def test_empty_list_returns_empty(self):
        client = make_mock_mcp_client()
        result = create_proxy_tools(client, [])
        assert result == []
