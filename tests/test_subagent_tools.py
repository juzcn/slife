"""Tests for Slife.tools.subagent — local worker tool definitions and execute logic."""

import pytest; pytestmark = pytest.mark.unit


import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Ensure the slife.subagent package is loaded so patch() can resolve the
# MANAGER_PATH target regardless of test collection order.
import slife.subagent.process  # noqa: F401

from slife.tools.subagent import (
    ListSubagentsTool,
    SpawnSubagentTool,
    StopSubagentTool,
    SubagentCancelTaskTool,
    SubagentGetTaskResultTool,
    SubagentListTasksTool,
    SubagentSendTaskAsyncTool,
    SubagentSendTaskTool,
)

# Patch paths: tools use lazy imports from Slife.subagent.process
MANAGER_PATH = "slife.subagent.process.get_manager"

# ═══════════════════════════════════════════════════════════════════════════
# Metadata tests — every tool
# ═══════════════════════════════════════════════════════════════════════════


TOOLS = [
    ListSubagentsTool,
    SpawnSubagentTool,
    StopSubagentTool,
    SubagentSendTaskTool,
    SubagentSendTaskAsyncTool,
    SubagentGetTaskResultTool,
    SubagentListTasksTool,
    SubagentCancelTaskTool,
]


class TestAllToolsMetadata:
    """Every subagent tool must have name, description, parameters, and execute."""

    @pytest.mark.parametrize("tool_cls", TOOLS)
    def test_has_name(self, tool_cls):
        assert tool_cls.name, f"{tool_cls.__name__} missing name"
        assert isinstance(tool_cls.name, str)

    @pytest.mark.parametrize("tool_cls", TOOLS)
    def test_has_description(self, tool_cls):
        assert tool_cls.description, f"{tool_cls.__name__} missing description"

    @pytest.mark.parametrize("tool_cls", TOOLS)
    def test_has_parameters_dict(self, tool_cls):
        assert isinstance(tool_cls.parameters, dict)
        assert "type" in tool_cls.parameters
        assert tool_cls.parameters["type"] == "object"

    @pytest.mark.parametrize("tool_cls", TOOLS)
    def test_has_execute(self, tool_cls):
        assert hasattr(tool_cls, "execute")
        assert callable(getattr(tool_cls, "execute"))


# ═══════════════════════════════════════════════════════════════════════════
# ListSubagentsTool
# ═══════════════════════════════════════════════════════════════════════════


class TestListSubagentsTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = ListSubagentsTool()
            result = await tool.execute()
            assert "not running yet" in result

    @pytest.mark.asyncio
    async def test_no_subagents(self):
        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=[])
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = ListSubagentsTool()
            result = await tool.execute()
            assert "No local subagents" in result

    @pytest.mark.asyncio
    async def test_with_subagents(self):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.is_ready = True
        mock_proc.is_running = True
        mock_proc.context_source = "cloned"
        mock_proc.is_busy = True
        mock_proc.queued = 2
        mock_proc.pending_async_count = 1

        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["sub-1", "sub-2"])
        mock_mgr.get = MagicMock(return_value=mock_proc)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = ListSubagentsTool()
            result = await tool.execute()
            assert "sub-1" in result
            assert "sub-2" in result
            assert "pid=12345" in result
            assert "context: cloned" in result
            assert "busy: 2 in flight" in result
            assert "async: 1" in result


# ═══════════════════════════════════════════════════════════════════════════
# SpawnSubagentTool
# ═══════════════════════════════════════════════════════════════════════════


class TestSpawnSubagentTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SpawnSubagentTool()
            result = await tool.execute()
            assert "not running yet" in result

    @pytest.mark.asyncio
    async def test_spawn_success(self):
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-1")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SpawnSubagentTool()
            result = await tool.execute(subagent_name="worker")
            assert "sub-1" in result
            assert "spawned" in result.lower()

    @pytest.mark.asyncio
    async def test_spawn_requires_name(self):
        """No auto-generated id — subagent_name is required."""
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-2")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SpawnSubagentTool()
            result = await tool.execute()

        assert "subagent_name is required" in result
        mock_mgr.spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_spawn_failure(self):
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(side_effect=RuntimeError("no memory"))

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SpawnSubagentTool()
            result = await tool.execute()
            assert "Error" in result

    @pytest.mark.asyncio
    async def test_spawn_clone_context_default_false(self):
        """Spawn defaults to a clean context (no cloned messages)."""
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-3")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SpawnSubagentTool()
            await tool.execute(subagent_name="worker")

        kwargs = mock_mgr.spawn.call_args.kwargs
        assert kwargs["context_source"] == "clean"
        assert kwargs["context_messages"] is None

    @pytest.mark.asyncio
    async def test_spawn_clone_context_true_clones(self):
        """clone_context=True passes the parent conversation messages to spawn."""
        from slife.agent.conversation import Conversation
        from slife.config import Config, ModelConfig
        from slife.tools.context import ToolContext

        conv = Conversation(system_prompt="SYS")
        conv.add_user_message("t1")
        conv.add_assistant_message("r1")
        mc = ModelConfig(
            ref="t/m", provider="t", api_model="m", display_name="M",
            api_key="k", context_window=1000,
        )
        cfg = Config(models=[mc], active_model_ref="t/m", tools=[], agent_name="testbot")

        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-4")
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SpawnSubagentTool()
            object.__setattr__(tool, "_ctx", ToolContext(conversation=conv, config=cfg))
            await tool.execute(subagent_name="worker", clone_context=True)

        kwargs = mock_mgr.spawn.call_args.kwargs
        assert kwargs["context_source"] == "cloned"
        assert kwargs["context_messages"] is not None
        roles = [m["role"] for m in kwargs["context_messages"]]
        assert roles == ["user", "assistant"]

    def test_serialize_cloned_context_drops_system(self):
        """The parent's system message (incl. footer) is not serialized."""
        from slife.agent.conversation import Conversation
        from slife.config import Config, ModelConfig
        from slife.tools.context import ToolContext
        from slife.tools.subagent import _serialize_cloned_context

        conv = Conversation(system_prompt="PARENT_SYS")
        conv.add_user_message("t1")
        conv.add_assistant_message("r1")
        mc = ModelConfig(
            ref="t/m", provider="t", api_model="m", display_name="M",
            api_key="k", context_window=1000,
        )
        cfg = Config(models=[mc], active_model_ref="t/m", tools=[], agent_name="testbot")

        data = _serialize_cloned_context(ToolContext(conversation=conv, config=cfg))
        assert data is not None
        assert all(m.get("role") != "system" for m in data)


# ═══════════════════════════════════════════════════════════════════════════
# StopSubagentTool
# ═══════════════════════════════════════════════════════════════════════════


class TestStopSubagentTool:
    @pytest.mark.asyncio
    async def test_missing_agent_name(self):
        tool = StopSubagentTool()
        result = await tool.execute(subagent_name="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = StopSubagentTool()
            result = await tool.execute(subagent_name="sub-1")
            assert "not running yet" in result

    @pytest.mark.asyncio
    async def test_stop_success(self):
        mock_mgr = MagicMock()
        mock_mgr.stop = AsyncMock(return_value=True)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = StopSubagentTool()
            result = await tool.execute(subagent_name="sub-1")
            assert "stopped" in result.lower()

    @pytest.mark.asyncio
    async def test_stop_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.stop = AsyncMock(return_value=False)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = StopSubagentTool()
            result = await tool.execute(subagent_name="sub-1")
            assert "not found" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Task delegation — sync / async / poll
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentSendTaskTool:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = SubagentSendTaskTool()
        result = await tool.execute(subagent_name="", task="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentSendTaskTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
            assert "not running yet" in result

    @pytest.mark.asyncio
    async def test_send_success(self):
        mock_mgr = MagicMock()
        mock_mgr.is_busy = MagicMock(return_value=False)
        mock_mgr.send_task = AsyncMock(return_value="done result")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
        assert result == "done result"
        mock_mgr.send_task.assert_awaited_once_with("sub-1", "do X")

    @pytest.mark.asyncio
    async def test_send_timeout_reports_task_still_running(self):
        """A sync timeout does not cancel the task — the tool says so."""
        mock_mgr = MagicMock()
        mock_mgr.is_busy = MagicMock(return_value=False)
        mock_mgr.send_task = AsyncMock(side_effect=TimeoutError("timeout"))

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
        assert "still running" in result
        assert "delivered automatically" in result

    @pytest.mark.asyncio
    async def test_send_busy_converts_to_async(self):
        """A sync send to a busy worker queues the task as async — no resend."""
        mock_mgr = MagicMock()
        mock_mgr.is_busy = MagicMock(return_value=True)
        mock_mgr.queued_count = MagicMock(return_value=2)
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-9")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
        assert "queued" in result
        assert "converted to async" in result
        assert "rpc-9" in result
        mock_mgr.send_task_async.assert_awaited_once_with("sub-1", "do X")
        mock_mgr.send_task.assert_not_called()


class TestSubagentSendTaskAsyncTool:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = SubagentSendTaskAsyncTool()
        result = await tool.execute(subagent_name="", task="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentSendTaskAsyncTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
            assert "not running yet" in result

    @pytest.mark.asyncio
    async def test_send_async_success(self):
        mock_mgr = MagicMock()
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-1")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskAsyncTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
        assert "rpc-1" in result
        assert "delivered automatically" in result
        mock_mgr.send_task_async.assert_awaited_once_with("sub-1", "do X")


class TestSubagentGetTaskResultTool:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = SubagentGetTaskResultTool()
        result = await tool.execute(subagent_name="", task_id="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentGetTaskResultTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
            assert "not running yet" in result

    @pytest.mark.asyncio
    async def test_result_pending(self):
        mock_mgr = MagicMock()
        mock_mgr.get_task_result = MagicMock(return_value=None)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentGetTaskResultTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
        assert result == "pending"

    @pytest.mark.asyncio
    async def test_result_ready(self):
        mock_mgr = MagicMock()
        mock_mgr.get_task_result = MagicMock(return_value="the result")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentGetTaskResultTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
        assert result == "the result"


class TestSubagentListTasksTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentListTasksTool()
            result = await tool.execute()
            assert "not running yet" in result

    @pytest.mark.asyncio
    async def test_no_records(self):
        mock_mgr = MagicMock()
        mock_mgr.list_tasks = MagicMock(return_value=[])
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentListTasksTool()
            result = await tool.execute()
        assert "No subagent task records" in result

    @pytest.mark.asyncio
    async def test_lists_records(self):
        mock_mgr = MagicMock()
        mock_mgr.list_tasks = MagicMock(return_value=[
            {
                "task_id": "rpc-1", "agent_name": "sub-1", "status": "pending",
                "preview": "do X", "result": None,
            },
            {
                "task_id": "rpc-2", "agent_name": "sub-2", "status": "completed",
                "preview": "do Y", "result": "done",
            },
        ])
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentListTasksTool()
            result = await tool.execute()
        assert "rpc-1" in result
        assert "rpc-2" in result
        assert "pending" in result
        assert "completed" in result

    @pytest.mark.asyncio
    async def test_filters_passed_through(self):
        mock_mgr = MagicMock()
        mock_mgr.list_tasks = MagicMock(return_value=[])
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentListTasksTool()
            await tool.execute(subagent_name="sub-1", status="pending")
        mock_mgr.list_tasks.assert_called_once_with(
            agent_name="sub-1", status="pending",
        )


class TestSubagentCancelTaskTool:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = SubagentCancelTaskTool()
        result = await tool.execute(subagent_name="", task_id="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentCancelTaskTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
            assert "not running yet" in result

    @pytest.mark.asyncio
    async def test_cancel_success(self):
        mock_mgr = MagicMock()
        mock_mgr.cancel_task = AsyncMock(return_value=True)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentCancelTaskTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
        assert "cancelled" in result.lower()
        mock_mgr.cancel_task.assert_awaited_once_with("sub-1", "rpc-1")

    @pytest.mark.asyncio
    async def test_cancel_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.cancel_task = AsyncMock(return_value=False)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentCancelTaskTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
        assert "not found" in result.lower()
