"""Tests for slife.tools.meta — ListToolsTool, CheckAsyncTool, CancelAsyncTool, ClearContextTool."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
from unittest.mock import MagicMock, patch

import pytest

from slife.tools.meta import (
    ListToolsTool,
    CheckAsyncTool,
    CancelAsyncTool,
    ClearContextTool,
    _classify,
    _tasks,
    schedule,
    _get_task,
    _pop_task,
)


# ── _classify ─────────────────────────────────────────────────────────────


class TestClassify:
    """Tests for the _classify helper."""

    def test_a2a_tools(self):
        assert _classify("a2a_list_agents") == "Agent Communication (A2A)"
        assert _classify("a2a_send_task") == "Agent Communication (A2A)"
        assert _classify("a2a_broadcast") == "Agent Communication (A2A)"

    def test_subagent_tools(self):
        assert _classify("subagent_send_task") == "Subagent (local workers)"
        assert _classify("subagent_send_task_async") == "Subagent (local workers)"
        assert _classify("subagent_get_task_result") == "Subagent (local workers)"

    def test_cli_tools(self):
        assert _classify("cli_set") == "CLI"
        assert _classify("cli_list") == "CLI"

    def test_rest_api_tools(self):
        assert _classify("rest_api_set") == "REST API"
        assert _classify("rest_api_list") == "REST API"

    def test_config_tools(self):
        assert _classify("config_env_set") == "Config"
        assert _classify("native_tool_set") == "Config"

    def test_skill_tools(self):
        assert _classify("skill_set_enabled") == "Skills"
        assert _classify("skill_list") == "Skills"
        assert _classify("skill_use") == "Skills"
        assert _classify("skill_set") == "Skills"
        assert _classify("skill_remove") == "Skills"

    def test_system_tools(self):
        assert _classify("check_memdb") == "System"
        assert _classify("system_health") == "System"

    def test_execution_tools(self):
        assert _classify("execute_shell") == "Execution"
        assert _classify("run_python_script") == "Execution"
        assert _classify("install_python_package") == "Execution"

    def test_credential_tools(self):
        assert _classify("credential_check") == "Credentials"
        assert _classify("credential_inject") == "Credentials"
        assert _classify("credential_uninject") == "Credentials"

    def test_meta_tools(self):
        # list_tools, cancel_async, clear_context → Meta
        # check_async starts with "check_" → System (checked first in _classify order)
        assert _classify("list_tools") == "Meta"
        assert _classify("cancel_async") == "Meta"
        assert _classify("clear_context") == "Meta"
        # check_async matches startswith("check_") before the Meta tuple check
        assert _classify("check_async") == "System"

    def test_unknown_tool(self):
        assert _classify("some_random_tool") == "Other"


# ── ListToolsTool ─────────────────────────────────────────────────────────


class TestListToolsTool:
    """Tests for ListToolsTool."""

    def test_metadata(self):
        tool = ListToolsTool()
        assert tool.name == "list_tools"
        assert tool.category == "Meta"
        assert "category" in tool.parameters["properties"]

    @pytest.mark.asyncio
    async def test_registry_unavailable(self):
        """When registry is None, returns clear message."""
        tool = ListToolsTool()
        try:
            result = await tool.execute()
            assert "not available" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_registry_empty(self):
        """When registry has no tools, returns appropriate message."""
        from slife.tools.registry import ToolRegistry
        tool = ListToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=ToolRegistry())
            result = await tool.execute()
            assert "no tools" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_native_tools_listed(self):
        """Native tools are listed under their categories."""
        from slife.tools.registry import ToolRegistry
        from slife.tools.base import Tool

        class _TestNative(Tool):
            name = "check_something"
            description = "Checks something."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        registry = ToolRegistry()
        registry.register(_TestNative())

        tool = ListToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute(category="native")
            assert "check_something" in result
            assert "System" in result  # auto-classified as System
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_mcp_tools_listed(self):
        """MCP proxy tools are listed under their servers."""
        from slife.tools.registry import ToolRegistry
        from slife.mcp.tool_adapter import MCPProxyTool
        from unittest.mock import AsyncMock

        # Create a minimal MCPProxyTool-like mock with the class being
        # MCPProxyTool so isinstance checks pass.
        mock_tool = MagicMock(spec=MCPProxyTool)
        mock_tool.name = "memory__search"
        mock_tool._server = "memdb"
        mock_tool.description = "Search memory."
        mock_tool.category = "Memory"
        mock_tool.parameters = {"type": "object", "properties": {}}

        registry = ToolRegistry()
        registry.register(mock_tool)

        tool = ListToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute(category="mcp")
            assert "memdb" in result.lower() or "memory__search" in result
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_category_all_shows_both(self):
        """category='all' shows both native and MCP sections."""
        from slife.tools.registry import ToolRegistry
        from slife.tools.base import Tool

        class _Native(Tool):
            name = "echo"
            description = "Echo back."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        registry = ToolRegistry()
        registry.register(_Native())

        tool = ListToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute(category="all")
            assert "Native Tools" in result
            assert "echo" in result
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_category_mcp_no_servers(self):
        """When category is mcp and no MCP tools, shows appropriate message."""
        from slife.tools.registry import ToolRegistry
        from slife.tools.base import Tool

        # Need at least one native tool so registry isn't empty,
        # but no MCPProxyTool instances.
        class _Native(Tool):
            name = "echo"
            description = "Echo back."
            parameters = {"type": "object", "properties": {}}
            async def execute(self, **kwargs): return "ok"

        registry = ToolRegistry()
        registry.register(_Native())

        tool = ListToolsTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(registry=registry)
            result = await tool.execute(category="mcp")
            assert "no mcp servers connected" in result.lower()
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
    async def test_conversation_not_initialised(self):
        """When conversation is None, returns appropriate message."""
        tool = ClearContextTool()
        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(conversation=None)
            result = await tool.execute()
            assert "not yet initialised" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_already_clean(self):
        """When context is already clean, returns appropriate message."""
        from slife.agent.conversation import Conversation
        tool = ClearContextTool()
        conv = Conversation(system_prompt="You are helpful.")

        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(conversation=conv)
            result = await tool.execute()
            assert "already clean" in result.lower()
        finally:
            tool._ctx = None

    @pytest.mark.asyncio
    async def test_clears_history(self):
        """Clears old turns, keeps system prompt and current turn."""
        from slife.agent.conversation import Conversation
        tool = ClearContextTool()
        conv = Conversation(system_prompt="You are helpful.")
        # Add TWO turns — clear_history preserves the last user message
        # and everything after it (the "current turn").  Only turns before
        # the last user message are cleared.
        conv.add_user_message("old question")       # turn 1 (will be cleared)
        conv.add_assistant_message(content="old answer")
        conv.add_user_message("current question")    # turn 2 (current, preserved)
        conv.add_assistant_message(content="current answer")

        from slife.tools.context import ToolContext
        try:
            tool._ctx = ToolContext(conversation=conv)
            result = await tool.execute()
            assert "Cleared" in result
            assert "remaining" in result.lower()
            # System prompt should still be there
            assert len(conv.messages) >= 1
        finally:
            tool._ctx = None
