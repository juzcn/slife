"""Tests for tool approval (require_approval) — Tool ABC, MCPProxyTool, AgentLoop."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.tools.base import Tool, NO_PARAMS
from slife.mcp.tool_adapter import MCPProxyTool, create_proxy_tools
from slife.agent.loop import AgentLoop, AgentEventHandler, ToolCallInfo
from slife.agent.llm_client import TokenUsage


# ── Tool ABC — requires_approval ──────────────────────────────────────


class TestToolRequiresApproval:
    def test_default_is_false(self):
        """All tools default to requires_approval=False."""

        class PlainTool(Tool):
            name = "plain_tool"
            description = "A plain tool"
            parameters = NO_PARAMS

            async def execute(self, **kwargs) -> str:
                return "ok"

        tool = PlainTool()
        assert tool.requires_approval is False

    def test_can_set_true(self):
        """A tool can opt into requiring approval."""

        class GuardedTool(Tool):
            name = "guarded_tool"
            description = "A guarded tool"
            parameters = NO_PARAMS
            requires_approval = True

            async def execute(self, **kwargs) -> str:
                return "ok"

        tool = GuardedTool()
        assert tool.requires_approval is True


# ── MCPProxyTool — require_approval param ─────────────────────────────


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


class TestMCPProxyToolRequiresApproval:
    def test_default_is_false(self):
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, make_tool_info())
        assert tool.requires_approval is False

    def test_true_from_constructor(self):
        client = make_mock_mcp_client()
        tool = MCPProxyTool(client, make_tool_info(), require_approval=True)
        assert tool.requires_approval is True

    def test_create_proxy_tools_forwards_default(self):
        client = make_mock_mcp_client()
        tools = create_proxy_tools(client, [make_tool_info()])
        assert len(tools) == 1
        assert tools[0].requires_approval is False

    def test_create_proxy_tools_forwards_true(self):
        client = make_mock_mcp_client()
        tools = create_proxy_tools(client, [make_tool_info()], require_approval=True)
        assert len(tools) == 1
        assert tools[0].requires_approval is True

    @pytest.mark.asyncio
    async def test_execute_still_works_with_approval_true(self):
        """Setting require_approval doesn't affect execute() — it just stores metadata."""
        client = make_mock_mcp_client()
        client.call_tool.return_value = "result"
        tool = MCPProxyTool(
            client,
            {**make_tool_info(server="mcp"), "name": "mcp_list_servers"},
            require_approval=True,
        )
        result = await tool.execute()
        assert result == "result"
        # execute() still works — approval is checked at AgentLoop level


# ── AgentLoop — approval gate in _execute_tools ──────────────────────


class _ApprovalTool(Tool):
    """Test tool that tracks whether execute was called."""

    name = "approval_test_tool"
    description = "A tool that requires approval"
    parameters = NO_PARAMS
    requires_approval = True

    def __init__(self):
        self.executed = False

    async def execute(self, **kwargs) -> str:
        self.executed = True
        return "executed"


class TestAgentLoopApproval:
    @pytest.mark.asyncio
    async def test_approval_denied_skips_execution(self):
        """When approval is denied, tool execution is skipped."""
        tool = _ApprovalTool()
        registry = MagicMock()
        registry.get.return_value = tool
        registry.execute = AsyncMock()

        loop = AgentLoop(
            llm_client=MagicMock(),
            tool_registry=registry,
            max_iterations=30,
        )

        handler = MagicMock(spec=AgentEventHandler)
        handler.on_tool_approval = AsyncMock(return_value=False)
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()

        conversation = MagicMock()

        tc = ToolCallInfo(id="call_1", name="approval_test_tool", arguments={})

        await loop._execute_tools([tc], conversation, handler, iteration=1)

        # Tool should NOT have been executed
        assert tool.executed is False
        registry.execute.assert_not_called()
        # Result should be "denied"
        handler.on_tool_result.assert_called_once()
        call_args = handler.on_tool_result.call_args
        assert "用户拒绝" in call_args[0][1] or "拒绝" in str(call_args)

    @pytest.mark.asyncio
    async def test_approval_granted_proceeds_normally(self):
        """When approval is granted, tool executes normally."""
        tool = _ApprovalTool()
        registry = MagicMock()
        registry.get.return_value = tool
        # Simulate registry.execute calling tool.execute
        async def _exec(name, **kwargs):
            return await tool.execute(**kwargs)
        registry.execute = _exec

        loop = AgentLoop(
            llm_client=MagicMock(),
            tool_registry=registry,
            max_iterations=30,
        )

        handler = MagicMock(spec=AgentEventHandler)
        handler.on_tool_approval = AsyncMock(return_value=True)
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()

        conversation = MagicMock()

        tc = ToolCallInfo(id="call_2", name="approval_test_tool", arguments={})

        await loop._execute_tools([tc], conversation, handler, iteration=1)

        # Tool should have been executed
        assert tool.executed is True
        handler.on_tool_result.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_approval_needed_for_normal_tool(self):
        """Tools without requires_approval skip the approval gate entirely."""

        class NormalTool(Tool):
            name = "normal_tool"
            description = "A normal tool"
            parameters = NO_PARAMS

            async def execute(self, **kwargs) -> str:
                return "normal result"

        tool = NormalTool()
        registry = MagicMock()
        registry.get.return_value = tool
        async def _exec(name, **kwargs):
            return await tool.execute(**kwargs)
        registry.execute = _exec

        loop = AgentLoop(
            llm_client=MagicMock(),
            tool_registry=registry,
            max_iterations=30,
        )

        handler = MagicMock(spec=AgentEventHandler)
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()

        conversation = MagicMock()
        tc = ToolCallInfo(id="call_3", name="normal_tool", arguments={})

        await loop._execute_tools([tc], conversation, handler, iteration=1)

        # Approval was NOT asked (hasattr check skips the async mock)
        # Tool executed normally
        handler.on_tool_result.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_handler_auto_approves(self):
        """When handler is None (headless/subagent), tools auto-approve."""
        tool = _ApprovalTool()
        registry = MagicMock()
        registry.get.return_value = tool
        async def _exec(name, **kwargs):
            return await tool.execute(**kwargs)
        registry.execute = _exec

        loop = AgentLoop(
            llm_client=MagicMock(),
            tool_registry=registry,
            max_iterations=30,
        )

        conversation = MagicMock()
        tc = ToolCallInfo(id="call_4", name="approval_test_tool", arguments={})

        await loop._execute_tools([tc], conversation, None, iteration=1)

        # Tool executed (auto-approved since no handler)
        assert tool.executed is True

    @pytest.mark.asyncio
    async def test_multiple_tools_mixed_approval(self):
        """Batch execution: denied tool skipped, approved tool runs."""

        class ToolA(Tool):
            name = "tool_a"; description = "A"; parameters = NO_PARAMS
            requires_approval = True
            def __init__(self): self.executed = False
            async def execute(self, **kwargs) -> str:
                self.executed = True; return "a"

        class ToolB(Tool):
            name = "tool_b"; description = "B"; parameters = NO_PARAMS
            requires_approval = True
            def __init__(self): self.executed = False
            async def execute(self, **kwargs) -> str:
                self.executed = True; return "b"

        tool_a, tool_b = ToolA(), ToolB()

        # Approve A, deny B
        approval_results = {"tool_a": True, "tool_b": False}

        registry = MagicMock()
        def _get(name):
            return {"tool_a": tool_a, "tool_b": tool_b}[name]
        registry.get = _get
        async def _exec(name, **kwargs):
            tool = {"tool_a": tool_a, "tool_b": tool_b}[name]
            return await tool.execute(**kwargs)
        registry.execute = _exec

        loop = AgentLoop(
            llm_client=MagicMock(),
            tool_registry=registry,
            max_iterations=30,
        )

        handler = MagicMock(spec=AgentEventHandler)

        async def _approve(tc):
            return approval_results[tc.name]
        handler.on_tool_approval = _approve
        handler.on_tool_call = AsyncMock()
        handler.on_tool_result = AsyncMock()

        conversation = MagicMock()
        tcs = [
            ToolCallInfo(id="c1", name="tool_a", arguments={}),
            ToolCallInfo(id="c2", name="tool_b", arguments={}),
        ]

        await loop._execute_tools(tcs, conversation, handler, iteration=1)

        assert tool_a.executed is True  # approved
        assert tool_b.executed is False  # denied
