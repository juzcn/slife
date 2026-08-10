"""Tests for model-driven tool approval (`_approve` meta param).

The harness no longer hardcodes approval on any tool (`requires_approval`
is gone).  The LLM decides per-call by passing `_approve: true` in the
tool arguments; the loop then asks the user via `on_tool_approval`.
"""

import pytest; pytestmark = pytest.mark.unit


from unittest.mock import AsyncMock, MagicMock

import pytest

from slife.tools.base import Tool, NO_PARAMS
from slife.mcp.tool_adapter import MCPProxyTool, create_proxy_tools
from slife.agent.loop import AgentLoop, AgentEventHandler, ToolCallInfo


# ── Tool ABC no longer carries approval metadata ──────────────────────


class TestToolNoApprovalMetadata:
    def test_requires_approval_attribute_removed(self):
        """Approval is model-decided; tools carry no approval flag."""
        assert not hasattr(Tool, "requires_approval")


# ── MCPProxyTool — no require_approval param ─────────────────────────


def make_mock_mcp_client():
    client = MagicMock()
    client.call_tool = AsyncMock()
    return client


def make_tool_info(server="test_server", name="test_tool", description="A test tool"):
    return {
        "server": server,
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": {"arg1": {"type": "string"}},
        },
    }


class TestMCPProxyToolNoApprovalParam:
    def test_constructor_has_no_approval_flag(self):
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, make_tool_info())
        assert not hasattr(tool, "requires_approval")

    def test_create_proxy_tools_has_no_approval_flag(self):
        client = make_mock_mcp_client()
        tools = create_proxy_tools(client, [make_tool_info()])
        assert len(tools) == 1
        assert not hasattr(tools[0], "requires_approval")

    @pytest.mark.asyncio
    async def test_execute_still_works(self):
        client = make_mock_mcp_client()
        client.call_tool.return_value = "result"
        tool = MCPProxyTool(
            client,
            {**make_tool_info(server="mcp"), "name": "mcp_list_servers"},
        )
        result = await tool.execute()
        assert result == "result"


# ── AgentLoop — approval gate driven by _approve ─────────────────────


class _TrackingTool(Tool):
    """Plain test tool that tracks whether execute was called."""

    name = "approval_test_tool"
    description = "A test tool"
    parameters = NO_PARAMS

    def __init__(self):
        self.executed = False

    async def execute(self, **kwargs) -> str:
        self.executed = True
        return "executed"


def _make_loop(registry):
    return AgentLoop(
        llm_client=MagicMock(),
        tool_registry=registry,
        max_iterations=30,
    )


class TestAgentLoopApproval:
    @pytest.mark.asyncio
    async def test_approve_true_denied_skips_execution(self):
        """`_approve: true` + user denies → tool not executed."""
        tool = _TrackingTool()
        registry = MagicMock()
        registry.get.return_value = tool
        registry.execute = AsyncMock()

        loop = _make_loop(registry)
        handler = MagicMock(spec=AgentEventHandler)
        handler.on_tool_approval = AsyncMock(return_value=False)
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()
        conversation = MagicMock()

        tc = ToolCallInfo(id="call_1", name="approval_test_tool", arguments={"_approve": True})
        await loop._execute_tools([tc], conversation, handler, iteration=1)

        assert tool.executed is False
        registry.execute.assert_not_called()
        handler.on_tool_result.assert_called_once()
        assert "denied by user" in handler.on_tool_result.call_args[0][1]

    @pytest.mark.asyncio
    async def test_approve_true_granted_proceeds(self):
        """`_approve: true` + user approves → tool executes."""
        tool = _TrackingTool()
        registry = MagicMock()
        registry.get.return_value = tool

        async def _exec(name, **kwargs):
            return await tool.execute(**kwargs)
        registry.execute = _exec

        loop = _make_loop(registry)
        handler = MagicMock(spec=AgentEventHandler)
        handler.on_tool_approval = AsyncMock(return_value=True)
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()
        conversation = MagicMock()

        tc = ToolCallInfo(id="call_2", name="approval_test_tool", arguments={"_approve": True})
        await loop._execute_tools([tc], conversation, handler, iteration=1)

        assert tool.executed is True
        handler.on_tool_result.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_approve_skips_gate(self):
        """Without `_approve` the gate is never consulted — pure model judgment."""
        tool = _TrackingTool()
        registry = MagicMock()
        registry.get.return_value = tool

        async def _exec(name, **kwargs):
            return await tool.execute(**kwargs)
        registry.execute = _exec

        loop = _make_loop(registry)
        handler = MagicMock(spec=AgentEventHandler)
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()
        conversation = MagicMock()

        tc = ToolCallInfo(id="call_3", name="approval_test_tool", arguments={})
        await loop._execute_tools([tc], conversation, handler, iteration=1)

        handler.on_tool_approval.assert_not_called()
        handler.on_tool_result.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_handler_auto_approves(self):
        """headless/subagent (no handler) → `_approve` auto-approved."""
        tool = _TrackingTool()
        registry = MagicMock()
        registry.get.return_value = tool

        async def _exec(name, **kwargs):
            return await tool.execute(**kwargs)
        registry.execute = _exec

        loop = _make_loop(registry)
        conversation = MagicMock()
        tc = ToolCallInfo(id="call_4", name="approval_test_tool", arguments={"_approve": True})
        await loop._execute_tools([tc], conversation, None, iteration=1)

        assert tool.executed is True

    @pytest.mark.asyncio
    async def test_mixed_batch_approval(self):
        """Batch: `_approve` granted runs, `_approve` denied skips, no-flag runs."""
        tools = {name: _TrackingTool() for name in ("tool_a", "tool_b", "tool_c")}
        for name, t in tools.items():
            t.name = name

        registry = MagicMock()

        def _get(name):
            return tools[name]
        registry.get = _get

        async def _exec(name, **kwargs):
            return await tools[name].execute(**kwargs)
        registry.execute = _exec

        loop = _make_loop(registry)
        handler = MagicMock(spec=AgentEventHandler)

        async def _approve(tc):
            return tc.id != "deny_me"
        handler.on_tool_approval = _approve
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()
        conversation = MagicMock()

        tcs = [
            ToolCallInfo(id="grant", name="tool_a", arguments={"_approve": True}),
            ToolCallInfo(id="deny_me", name="tool_b", arguments={"_approve": True}),
            ToolCallInfo(id="plain", name="tool_c", arguments={}),
        ]
        await loop._execute_tools(tcs, conversation, handler, iteration=1)

        assert tools["tool_a"].executed is True   # granted
        assert tools["tool_b"].executed is False  # denied
        assert tools["tool_c"].executed is True   # no flag → no gate
