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
            {**make_tool_info(server="mcp"), "name": "mcp_list"},
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
        # A denied call must never surface a tool widget — the approval
        # prompt itself carries the "denied" state.
        handler.on_tool_call.assert_not_called()

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
        # Approved → the tool widget is mounted (runs) after the prompt.
        handler.on_tool_call.assert_called_once()

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


# ── ApprovalPrompt — inline row, Y/N/Esc decide (priority, REVIEW C7) ──


class TestApprovalPrompt:
    """The inline approval row's priority bindings let Y/N/Esc really decide,
    instead of being stolen by the App's ``escape -> cancel`` binding or the
    ChatView printable-key redirect (REVIEW C7)."""

    def _make(self):
        import asyncio

        from slife.ui.approval_prompt import ApprovalPrompt

        tool_call = ToolCallInfo(id="call_1", name="test_tool", arguments={"a": 1})
        future = asyncio.Future()
        return ApprovalPrompt(tool_call, future), future

    @pytest.mark.asyncio
    async def test_action_approve_sets_true(self):
        prompt, future = self._make()
        prompt.action_approve()
        assert future.result() is True
        assert prompt._decided == "approved"

    @pytest.mark.asyncio
    async def test_action_deny_sets_false(self):
        prompt, future = self._make()
        prompt.action_deny()
        assert future.result() is False
        assert prompt._decided == "denied"

    @pytest.mark.asyncio
    async def test_second_keypress_ignored(self):
        prompt, future = self._make()
        prompt.action_approve()
        prompt.action_deny()  # repeat press must not override the decision
        assert future.result() is True
        assert prompt._decided == "approved"

    @pytest.mark.asyncio
    async def test_bindings_map_y_n_escape_priority(self):
        prompt, _ = self._make()
        keys = {b.key: b for b in prompt.BINDINGS}
        assert keys["y"].action == "approve" and keys["y"].priority
        assert keys["n"].action == "deny" and keys["n"].priority
        assert keys["escape"].action == "deny" and keys["escape"].priority

    # ── Regression (C7): the App's escape binding must not steal Esc from
    # the inline prompt's priority deny.  Textual's priority pass iterates
    # the binding chain REVERSED (App first), so a priority escape→cancel on
    # the App would fire before the prompt's deny and cancel the loop instead.

    def test_app_escape_binding_is_not_priority(self):
        from slife.ui.app import SlifeApp

        esc = [b for b in SlifeApp.BINDINGS if b.key == "escape"]
        assert len(esc) == 1
        assert not esc[0].priority, (
            "App escape→cancel must not be priority: Textual's priority pass "
            "checks the App first, stealing Esc from the approval prompt's deny."
        )

    def test_priority_pass_resolves_keys_to_prompt(self):
        """Reproduce Textual's priority-pass resolution over the real binding
        declarations and assert the prompt wins Y/N/Esc.

        Textual 8.x `App._check_bindings(key, priority=True)` iterates
        ``reversed(screen._binding_chain)`` — the App is checked first, and
        only bindings whose ``priority`` flag equals the pass run.  So the
        App's escape→cancel (non-priority) never fires in the priority pass,
        and the prompt's priority y/n/escape all resolve to it.
        """
        from slife.ui.app import SlifeApp
        from slife.ui.approval_prompt import ApprovalPrompt

        def _resolve_priority_pass(
            bindings_by_node: list[tuple[str, list]]
        ) -> list[tuple[str, str, str]]:
            resolved: list[tuple[str, str, str]] = []
            for ns, bindings in reversed(bindings_by_node):  # App → ... → prompt
                for b in bindings:
                    if b.priority:
                        resolved.append((ns, b.key, b.action))
            return resolved

        full_chain = [("app", SlifeApp.BINDINGS), ("prompt", ApprovalPrompt.BINDINGS)]
        resolved = _resolve_priority_pass(full_chain)

        # The prompt's Y/N/Esc win the priority pass…
        assert ("prompt", "y", "approve") in resolved
        assert ("prompt", "n", "deny") in resolved
        assert ("prompt", "escape", "deny") in resolved
        # …and the App's non-priority escape→cancel never fires there.
        assert not any(ns == "app" and key == "escape" for ns, key, _ in resolved)

    @pytest.mark.asyncio
    async def test_pilot_keys_resolve_through_real_dispatch(self):
        """End-to-end through Textual's real key dispatch (priority pass):
        Y resolves the future True, Esc resolves it False, and markup-hazardous
        args render literally (no MarkupError)."""
        import asyncio

        from textual.app import App

        from slife.ui.approval_prompt import ApprovalPrompt

        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            tool_call = ToolCallInfo(
                id="call_1",
                name="test_tool",
                arguments={"q": "a & [b] 'c'", "n": 1},
            )

            future = asyncio.Future()
            prompt = ApprovalPrompt(tool_call, future)
            await app.mount(prompt)
            prompt.focus()
            await pilot.pause()
            assert "a & [b] 'c'" in str(prompt.render())
            await pilot.press("y")
            assert future.result() is True

            future2 = asyncio.Future()
            prompt2 = ApprovalPrompt(tool_call, future2)
            await app.mount(prompt2)
            prompt2.focus()
            await pilot.pause()
            await pilot.press("escape")
            assert future2.result() is False
