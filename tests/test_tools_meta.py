"""Tests for slife.tools.meta — ListNativeToolsTool, CheckAsyncTool, CancelAsyncTool, ClearContextTool."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
from unittest.mock import MagicMock, patch

import pytest

from slife.tools.meta import (
    ListNativeToolsTool,
    CheckAsyncTool,
    CancelAsyncTool,
    ClearContextTool,
    SetMaxIterationsTool,
    _native_category,
    _tasks,
    schedule,
    _get_task,
    _pop_task,
)


# ── _native_category ──────────────────────────────────────────────────────


class TestNativeCategory:
    """Tests for the source-based category helper (T-12)."""

    def test_native_tool_uses_its_own_category(self):
        from slife.tools.base import Tool

        class _T(Tool):
            name = "thing"
            description = "Does a thing."
            parameters = {"type": "object", "properties": {}}
            category = "Custom"
            async def execute(self, **kwargs): return "ok"

        assert _native_category(_T()) == "Custom"

    def test_plugin_tool_groups_by_plugin_name(self):
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

        tool = MagicMock(spec=MCPProxyTool)
        tool._server = "wechat"
        tool._route = ProxyRoute.DIRECT
        assert _native_category(tool) == "wechat"

    def test_plugin_tool_without_server_falls_back_to_plugins(self):
        from slife.mcp.tool_adapter import MCPProxyTool

        tool = MagicMock(spec=MCPProxyTool)
        del tool._server
        assert _native_category(tool) == "Plugins"

    def test_unknown_native_tool_defaults_to_other(self):
        from slife.tools.base import Tool

        class _T(Tool):
            name = "thing"
            description = "Does a thing."
            parameters = {"type": "object", "properties": {}}
            category = ""
            async def execute(self, **kwargs): return "ok"

        assert _native_category(_T()) == "Other"


# ── ListNativeToolsTool ──────────────────────────────────────────────────


class TestListNativeToolsTool:
    """Tests for ListNativeToolsTool."""

    def test_metadata(self):
        tool = ListNativeToolsTool()
        assert tool.name == "list_native_tools"
        assert tool.category == "Meta"
        assert tool.parameters == {"type": "object", "properties": {}, "required": []}

    @pytest.mark.asyncio
    async def test_registry_unavailable(self):
        """When registry is None, returns clear message."""
        tool = ListNativeToolsTool()
        try:
            result = await tool.execute()
            assert "not available" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_registry_empty(self):
        """When registry has no tools, returns appropriate message."""
        from slife.tools.registry import ToolRegistry
        tool = ListNativeToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=ToolRegistry())
            result = await tool.execute()
            assert "no tools" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_native_tools_listed(self):
        """Native tools are listed under their own category."""
        from slife.tools.registry import ToolRegistry
        from slife.tools.base import Tool

        class _TestNative(Tool):
            name = "check_something"
            description = "Checks something."
            parameters = {"type": "object", "properties": {}}
            category = "System"
            async def execute(self, **kwargs): return "ok"

        registry = ToolRegistry()
        registry.register(_TestNative())

        tool = ListNativeToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "check_something" in result
            assert "System" in result  # the tool's own category
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_mcp_proxy_tools_excluded(self):
        """External MCP proxy tools are NOT listed — the model already gets
        their full schemas natively, so a second listing is redundant cost."""
        from slife.tools.registry import ToolRegistry
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute
        from slife.tools.base import Tool

        class _Native(Tool):
            name = "echo"
            description = "Echo back."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        # Minimal MCPProxyTool-like mock so isinstance checks pass.
        # _route is an instance attribute (set in __init__), so spec= alone
        # does not expose it — set it explicitly to model an EXTERNAL proxy.
        mock_tool = MagicMock(spec=MCPProxyTool)
        mock_tool.name = "filesystem__read_file"
        mock_tool._server = "filesystem"
        mock_tool._route = ProxyRoute.EXTERNAL
        mock_tool.description = "Read a file."
        mock_tool.category = "MCP"
        mock_tool.parameters = {"type": "object", "properties": {}}

        registry = ToolRegistry()
        registry.register(mock_tool)
        registry.register(_Native())

        tool = ListNativeToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "echo" in result
            assert "filesystem__read_file" not in result
            assert "filesystem" not in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_builtin_plugin_tools_grouped_by_plugin_name(self):
        """Built-in plugin tools (bare names) group under their plugin name —
        source-based, no name-prefix guessing (T-12)."""
        from slife.tools.registry import ToolRegistry
        from slife.mcp.tool_adapter import MCPProxyTool, ProxyRoute

        plugin_tool = MagicMock(spec=MCPProxyTool)
        plugin_tool.name = "wechat_login"
        plugin_tool._server = "wechat"
        plugin_tool._route = ProxyRoute.DIRECT
        plugin_tool.description = "Log in to WeChat."
        plugin_tool.parameters = {"type": "object", "properties": {}}

        registry = ToolRegistry()
        registry.register(plugin_tool)

        tool = ListNativeToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "`wechat_login`" in result
            assert "wechat" in result  # grouped under the plugin name
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_harness_marker_for_underscore_tools(self):
        """Native harness tools (_sys_note) are shown with a marker."""
        from slife.tools.registry import ToolRegistry
        from slife.tools.base import Tool

        class _Harness(Tool):
            name = "_sys_note"
            description = "Current context status."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        registry = ToolRegistry()
        registry.register(_Harness())

        tool = ListNativeToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute()
            assert "`_sys_note`" in result
            assert "harness, auto-invoked" in result
        finally:
            tool._ctx = None


# ── Async task helpers ────────────────────────────────────────────────────


class TestAsyncTaskHelpers:
    """Tests for schedule, _get_task, _pop_task."""

    def setup_method(self):
        """Clear the global task dict before each test."""
        _tasks.clear()

    def teardown_method(self):
        """Clear after each test."""
        _tasks.clear()

    @pytest.mark.asyncio
    async def test_schedule_returns_task_id(self):
        """schedule() returns an 8-char hex task_id."""
        async def dummy():
            return "done"
        tid = schedule(dummy())
        assert len(tid) == 8
        assert all(c in "0123456789abcdef" for c in tid)

    @pytest.mark.asyncio
    async def test_schedule_adds_to_tasks(self):
        """schedule() adds the task to the global _tasks dict."""
        async def dummy():
            return "done"
        tid = schedule(dummy())
        assert tid in _tasks
        assert isinstance(_tasks[tid], asyncio.Task)

    @pytest.mark.asyncio
    async def test_get_task_returns_task(self):
        """_get_task retrieves a scheduled task."""
        async def dummy():
            return "done"
        tid = schedule(dummy())
        task = _get_task(tid)
        assert task is not None
        assert isinstance(task, asyncio.Task)

    def test_get_task_returns_none_for_missing(self):
        """_get_task returns None for unknown task_id."""
        assert _get_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_pop_task_removes_and_returns(self):
        """_pop_task removes the task from _tasks and returns it."""
        async def dummy():
            return "done"
        tid = schedule(dummy())
        assert tid in _tasks
        task = _pop_task(tid)
        assert task is not None
        assert tid not in _tasks

    def test_pop_task_returns_none_for_missing(self):
        """_pop_task returns None for unknown task_id."""
        assert _pop_task("nonexistent") is None

    @pytest.mark.asyncio
    async def test_scheduled_task_completes(self):
        """A scheduled task eventually completes with its result."""
        async def return_value():
            return "the result"
        tid = schedule(return_value())
        task = _get_task(tid)
        assert task is not None
        result = await task
        assert result == "the result"

    @pytest.mark.asyncio
    async def test_scheduled_task_captures_exception(self):
        """A failing task captures the exception in its result string."""
        async def raise_error():
            raise ValueError("test error")
        tid = schedule(raise_error())
        task = _get_task(tid)
        assert task is not None
        result = await task
        assert "ValueError" in result
        assert "test error" in result

    @pytest.mark.asyncio
    async def test_multiple_scheduled_tasks(self):
        """Multiple tasks can be scheduled simultaneously."""
        async def return_x(x):
            return x
        tid1 = schedule(return_x("one"))
        tid2 = schedule(return_x("two"))
        tid3 = schedule(return_x("three"))
        assert len(_tasks) == 3
        assert tid1 != tid2 != tid3


# ── CheckAsyncTool ────────────────────────────────────────────────────────


class TestCheckAsyncTool:
    """Tests for CheckAsyncTool."""

    def setup_method(self):
        _tasks.clear()

    def teardown_method(self):
        _tasks.clear()

    def test_metadata(self):
        tool = CheckAsyncTool()
        assert tool.name == "check_async"
        assert tool.category == "Meta"
        assert "task_id" in tool.parameters["required"]

    @pytest.mark.asyncio
    async def test_task_not_found(self):
        """Non-existent task_id returns error message."""
        tool = CheckAsyncTool()
        result = await tool.execute(task_id="nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_task_still_running(self):
        """A running task returns status message."""
        tool = CheckAsyncTool()

        async def slow_task():
            await asyncio.sleep(10)

        tid = schedule(slow_task())
        assert tid in _tasks

        result = await tool.execute(task_id=tid)
        assert "still running" in result

        # Clean up
        _tasks[tid].cancel()
        try:
            await _tasks[tid]
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    async def test_task_completed(self):
        """A completed task returns its result and is removed from _tasks."""
        tool = CheckAsyncTool()

        async def quick_task():
            return "all done!"

        tid = schedule(quick_task())
        # Wait for completion
        await asyncio.sleep(0.1)

        result = await tool.execute(task_id=tid)
        assert "Task completed" in result
        assert "all done!" in result
        assert tid not in _tasks  # popped after retrieval

    @pytest.mark.asyncio
    async def test_task_completed_with_error(self):
        """A failed task returns the error in the result."""
        tool = CheckAsyncTool()

        async def failing():
            raise RuntimeError("boom")

        tid = schedule(failing())
        await asyncio.sleep(0.1)

        result = await tool.execute(task_id=tid)
        assert "Task completed" in result
        assert "RuntimeError" in result
        assert "boom" in result
        assert tid not in _tasks


# ── CancelAsyncTool ───────────────────────────────────────────────────────


class TestCancelAsyncTool:
    """Tests for CancelAsyncTool."""

    def setup_method(self):
        _tasks.clear()

    def teardown_method(self):
        _tasks.clear()

    def test_metadata(self):
        tool = CancelAsyncTool()
        assert tool.name == "cancel_async"
        assert tool.category == "Meta"

    @pytest.mark.asyncio
    async def test_task_not_found(self):
        """Cancelling a non-existent task returns error."""
        tool = CancelAsyncTool()
        result = await tool.execute(task_id="nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_task_already_done(self):
        """Cancelling a finished task returns appropriate message."""
        tool = CancelAsyncTool()

        async def quick():
            return "done"

        tid = schedule(quick())
        await asyncio.sleep(0.1)  # let it complete

        result = await tool.execute(task_id=tid)
        assert "already completed" in result
        assert tid not in _tasks

    @pytest.mark.asyncio
    async def test_cancel_running_task(self):
        """A running task can be cancelled."""
        tool = CancelAsyncTool()

        async def slow():
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise

        tid = schedule(slow())
        assert tid in _tasks

        result = await tool.execute(task_id=tid)
        assert "cancelled" in result
        assert tid not in _tasks


# ── ClearContextTool ──────────────────────────────────────────────────────


class TestClearContextTool:
    """Tests for ClearContextTool."""

    def test_metadata(self):
        tool = ClearContextTool()
        assert tool.name == "clear_context"
        assert tool.category == "Meta"
        assert tool.parameters == {"type": "object", "properties": {}, "required": []}

    @pytest.mark.asyncio
    async def test_message_history_not_initialised(self):
        """When history is None, returns appropriate message."""
        tool = ClearContextTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(message_history=None)
            result = await tool.execute()
            assert "not yet initialised" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_already_clean(self):
        """When context is already clean, returns appropriate message."""
        from slife.agent.message_history import MessageHistory
        tool = ClearContextTool()
        conv = MessageHistory(system_prompt="You are helpful.")

        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(message_history=conv)
            result = await tool.execute()
            assert "already clean" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_clears_history(self):
        """Clears old turns, keeps system prompt and current turn."""
        from slife.agent.message_history import MessageHistory
        tool = ClearContextTool()
        conv = MessageHistory(system_prompt="You are helpful.")
        # Add TWO turns — clear_history preserves the last user message
        # and everything after it (the "current turn").  Only turns before
        # the last user message are cleared.
        conv.add_user_message("old question")       # turn 1 (will be cleared)
        conv.add_assistant_message(content="old answer")
        conv.add_user_message("current question")    # turn 2 (current, preserved)
        conv.add_assistant_message(content="current answer")

        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(message_history=conv)
            result = await tool.execute()
            assert "Cleared" in result
            assert "remaining" in result.lower()
            # System prompt should still be there
            assert len(conv.messages) >= 1
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_resets_context_time(self):
        """Clearing context restarts the "Context covers" range — otherwise
        the next _sys_note would keep reporting the pre-clear start."""
        from slife.agent.message_history import MessageHistory
        from slife.tools.context import ToolContext
        tool = ClearContextTool()
        conv = MessageHistory(system_prompt="You are helpful.")
        conv.add_user_message("old question")
        conv.add_assistant_message(content="old answer")
        conv.add_user_message("current question")
        conv.add_assistant_message(content="current answer")

        reset_called = []
        try:
            tool._ctx = ToolContext(
                message_history=conv,
                reset_context_time=lambda: reset_called.append(True),
            )
            result = await tool.execute()
            assert "Cleared" in result
            assert reset_called == [True]
        finally:
            tool._ctx = None


# ── SetMaxIterationsTool ─────────────────────────────────────────────


class TestSetMaxIterationsTool:
    """set_max_iterations delegates to the ctx hook."""

    def test_parameters_schema(self):
        assert "max_iterations" in SetMaxIterationsTool.parameters["required"]
        prop = SetMaxIterationsTool.parameters["properties"]["max_iterations"]
        assert prop["type"] == "integer"

    @pytest.mark.asyncio
    async def test_sets_via_ctx_hook(self):
        from slife.tools.context import ToolContext

        tool = SetMaxIterationsTool()
        tool._ctx = ToolContext(
            set_max_iterations=lambda n: f"Max iterations set to {n}",
        )

        result = await tool.execute(max_iterations=0)

        assert result == "Max iterations set to 0"

    @pytest.mark.asyncio
    async def test_loop_unavailable(self):
        tool = SetMaxIterationsTool()  # no _ctx → no hook
        result = await tool.execute(max_iterations=0)
        assert result.startswith("Error")
