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
    RemoveSubagentTool,
    SubagentCancelTaskTool,
    SubagentGetTaskResultTool,
    SubagentListTasksTool,
    SubagentRunTaskBackgroundTool,
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
    RemoveSubagentTool,
    SubagentSendTaskTool,
    SubagentSendTaskAsyncTool,
    SubagentRunTaskBackgroundTool,
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
            assert result == "Error: subagent manager is not running."

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
        mock_proc.pending_async_count = 1

        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["sub-1", "sub-2"])
        mock_mgr.get = MagicMock(return_value=mock_proc)
        mock_mgr.is_idle = MagicMock(return_value=False)
        mock_mgr.is_busy = MagicMock(return_value=True)
        mock_mgr.queued_count = MagicMock(return_value=2)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = ListSubagentsTool()
            result = await tool.execute()
            assert "sub-1" in result
            assert "sub-2" in result
            assert "pid=12345" in result
            assert "busy: 2 in flight" in result
            assert "async: 1" in result

    @pytest.mark.asyncio
    async def test_an_idle_worker_is_marked_idle(self):
        """The label is the manager's own predicate — the same one the pool
        chooser reads, so "idle" here means a background task would take it."""
        mock_proc = MagicMock()
        mock_proc.pid = 999
        mock_proc.is_ready = True
        mock_proc.pending_async_count = 0

        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["worker-1"])
        mock_mgr.get = MagicMock(return_value=mock_proc)
        mock_mgr.is_idle = MagicMock(return_value=True)
        mock_mgr.is_busy = MagicMock(return_value=False)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            result = await ListSubagentsTool().execute()

        assert "worker-1" in result
        assert "[idle]" in result
        assert "busy" not in result
        assert "async" not in result


# ═══════════════════════════════════════════════════════════════════════════
# SpawnSubagentTool
# ═══════════════════════════════════════════════════════════════════════════


class TestSpawnSubagentTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SpawnSubagentTool()
            result = await tool.execute()
            assert result == "Error: subagent manager is not running."

    @pytest.mark.asyncio
    async def test_spawn_success(self):
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-1")
        mock_mgr.spawned_running = MagicMock(return_value=False)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SpawnSubagentTool()
            result = await tool.execute(subagent_name="worker")
            assert "sub-1" in result
            assert "spawned" in result.lower()

    @pytest.mark.asyncio
    async def test_spawn_reuses_running_worker(self):
        """A running worker is reported as reused, not spawned."""
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="worker")
        mock_mgr.spawned_running = MagicMock(return_value=True)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SpawnSubagentTool()
            result = await tool.execute(subagent_name="worker")
            assert "worker" in result
            assert "reused" in result.lower()

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
    async def test_spawn_takes_no_context(self):
        """A spawn starts a process — the context is the task's, not the spawn's."""
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-3")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SpawnSubagentTool()
            await tool.execute(subagent_name="worker")

        assert mock_mgr.spawn.call_args.kwargs == {"name": "worker"}

    def test_serialize_cloned_context_drops_system(self):
        """The parent's system message is not serialized."""
        from slife.config import Config, ModelConfig
        from slife.tools.context import ToolContext
        from slife.tools.subagent import _serialize_cloned_context

        mc = ModelConfig(
            ref="t/m", provider="t", api_model="m", display_name="M",
            api_key="k", context_window=1000,
        )
        cfg = Config(models=[mc], active_model_ref="t/m", tools=[], agent_name="testbot")

        data = _serialize_cloned_context(
            ToolContext(message_history=_parent_context(), config=cfg)
        )
        assert data is not None
        assert all(m.get("role") != "system" for m in data)
        assert [m["role"] for m in data] == ["user", "assistant"]

    def test_the_clone_excludes_the_turn_in_flight(self):
        """The parent's unfinished turn is its own business, not context.

        The snapshot is taken inside the delegating tool call, so that turn is
        the parent's request, its reasoning and the ``assistant(tool_calls=…)``
        doing the delegating — repaired on arrival to "(Tool execution
        interrupted)" and read by the worker as *its own* interrupted action.
        A live test had the worker conclude it was the parent, then go and
        continue the parent's work instead of the task.
        """
        from slife.config import Config, ModelConfig
        from slife.tools.context import ToolContext
        from slife.tools.subagent import _serialize_cloned_context

        mc = ModelConfig(
            ref="t/m", provider="t", api_model="m", display_name="M",
            api_key="k", context_window=1000,
        )
        cfg = Config(models=[mc], active_model_ref="t/m", tools=[], agent_name="testbot")

        data = _serialize_cloned_context(ToolContext(
            message_history=_parent_context(in_flight=True), config=cfg,
        ))
        assert data == [
            {"role": "user", "content": "t1", "_turn_id": 7},
            {"role": "assistant", "content": "r1"},
        ], "the settled turn is carried; the turn being run is not"

    def test_a_parent_with_no_settled_turn_clones_nothing(self):
        """A task sent from a session's first turn has nothing to carry.

        Every turn so far is in flight, so the clone is empty and the worker
        runs on the task alone — which is what the task text is for.
        """
        from slife.agent.message_history import MessageHistory
        from slife.config import Config, ModelConfig
        from slife.tools.context import ToolContext
        from slife.tools.subagent import _serialize_cloned_context

        conv = MessageHistory(system_prompt="PARENT_SYS")
        conv.add_user_message("the only turn, still running")
        mc = ModelConfig(
            ref="t/m", provider="t", api_model="m", display_name="M",
            api_key="k", context_window=1000,
        )
        cfg = Config(models=[mc], active_model_ref="t/m", tools=[], agent_name="testbot")

        assert _serialize_cloned_context(
            ToolContext(message_history=conv, config=cfg)
        ) == []


# ═══════════════════════════════════════════════════════════════════════════
# RemoveSubagentTool
# ═══════════════════════════════════════════════════════════════════════════


class TestRemoveSubagentTool:
    @pytest.mark.asyncio
    async def test_missing_agent_name(self):
        tool = RemoveSubagentTool()
        result = await tool.execute(subagent_name="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = RemoveSubagentTool()
            result = await tool.execute(subagent_name="sub-1")
            assert result == "Error: subagent manager is not running."

    @pytest.mark.asyncio
    async def test_remove_success(self):
        """The reply says what the caller now faces, not just "done".

        The worker is gone from the fleet and its task records with it, and
        spawning the name again is a fresh worker — three facts a caller acts
        on, none of them implied by the word "stopped".
        """
        mock_mgr = MagicMock()
        mock_mgr.stop = AsyncMock(return_value=True)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = RemoveSubagentTool()
            result = await tool.execute(subagent_name="sub-1")
            assert "removed" in result.lower()
            assert "no longer running" in result
            assert "starts a fresh worker" in result

    @pytest.mark.asyncio
    async def test_remove_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.stop = AsyncMock(return_value=False)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = RemoveSubagentTool()
            result = await tool.execute(subagent_name="sub-1")
            assert "not found" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════
# Task delegation — sync / async / poll
# ═══════════════════════════════════════════════════════════════════════════


def _parent_context(*, in_flight: bool = False):
    """A parent history as the loop hands it to a tool: one settled turn, and
    optionally the turn being run right now (which carries no turn rowid)."""
    from slife.agent.message_history import MessageHistory

    conv = MessageHistory(system_prompt="PARENT_SYS")
    conv.add_user_message("t1")
    conv.add_assistant_message("r1")
    conv.messages[1]["_turn_id"] = 7          # the settled turn, as saved
    if in_flight:
        conv.add_user_message("what we are doing now")
        conv.add_assistant_message("", tool_calls=[{
            "id": "call_00_x", "type": "function",
            "function": {"name": "subagent_send_task_async", "arguments": "{}"},
        }])
    return conv


def _tool_with_parent_context(tool, *, in_flight: bool = False):
    """Give *tool* a live parent context, as the loop does per tool batch."""
    from slife.config import Config, ModelConfig
    from slife.tools.context import ToolContext

    mc = ModelConfig(
        ref="t/m", provider="t", api_model="m", display_name="M",
        api_key="k", context_window=1000,
    )
    cfg = Config(models=[mc], active_model_ref="t/m", tools=[], agent_name="testbot")
    object.__setattr__(
        tool, "_ctx",
        ToolContext(message_history=_parent_context(in_flight=in_flight), config=cfg),
    )
    return tool


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
            assert result == "Error: subagent manager is not running."

    @pytest.mark.asyncio
    async def test_send_success(self):
        mock_mgr = MagicMock()
        mock_mgr.is_busy = MagicMock(return_value=False)
        mock_mgr.send_task = AsyncMock(return_value="done result")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
        assert result == "done result"
        mock_mgr.send_task.assert_awaited_once_with(
            "sub-1", "do X", timeout=None, seed=None,
        )

    @pytest.mark.asyncio
    async def test_send_timeout_reports_preempted(self):
        """A sync timeout preempts the stuck task and reports it honestly."""
        mock_mgr = MagicMock()
        mock_mgr.is_busy = MagicMock(return_value=False)
        mock_mgr.send_task = AsyncMock(side_effect=TimeoutError("timeout"))

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
        assert "Timed out" in result
        assert "NOT delivered automatically" in result

    @pytest.mark.asyncio
    async def test_a_worker_timeout_hands_back_the_task_id(self):
        """The preempted task's id is the caller's only way to its late result.

        Without it the timeout is a dead end: the result is stored, and the
        caller has nothing to poll with.
        """
        from slife.subagent.process import TaskTimeout

        mock_mgr = MagicMock()
        mock_mgr.is_busy = MagicMock(return_value=False)
        mock_mgr.send_task = AsyncMock(
            side_effect=TaskTimeout("timed out", "rpc-42"),
        )

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")

        assert "rpc-42" in result
        assert "subagent_get_task_result" in result

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
        mock_mgr.send_task_async.assert_awaited_once_with(
            "sub-1", "do X", seed=None,
        )
        mock_mgr.send_task.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_task_carries_the_context_as_it_stands_at_send_time(self):
        """The parent's live messages ride the task, its system prompt dropped.

        This is the whole point of the refactor: the context is decided when
        the task is sent, because that is the only moment it is in hand.
        """
        mock_mgr = MagicMock()
        mock_mgr.is_busy = MagicMock(return_value=False)
        mock_mgr.send_task = AsyncMock(return_value="ok")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = _tool_with_parent_context(SubagentSendTaskTool())
            await tool.execute(subagent_name="sub-1", task="do X")

        seed = mock_mgr.send_task.call_args.kwargs["seed"]
        assert [m["role"] for m in seed] == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_a_queued_task_keeps_the_context_of_its_own_send(self):
        """A task queued behind a busy worker still carries its own snapshot."""
        mock_mgr = MagicMock()
        mock_mgr.is_busy = MagicMock(return_value=True)
        mock_mgr.queued_count = MagicMock(return_value=1)
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-9")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = _tool_with_parent_context(SubagentSendTaskTool())
            await tool.execute(subagent_name="sub-1", task="do X")

        seed = mock_mgr.send_task_async.call_args.kwargs["seed"]
        assert [m["role"] for m in seed] == ["user", "assistant"]


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
            assert result == "Error: subagent manager is not running."

    @pytest.mark.asyncio
    async def test_send_async_success(self):
        mock_mgr = MagicMock()
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-1")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskAsyncTool()
            result = await tool.execute(subagent_name="sub-1", task="do X")
        assert "rpc-1" in result
        assert "delivered automatically" in result
        assert "subagent_get_task_result" in result  # auto is also pollable
        # mode defaults to "auto" (push).
        mock_mgr.send_task_async.assert_awaited_once_with(
            "sub-1", "do X", mode="auto", seed=None,
        )

    @pytest.mark.asyncio
    async def test_the_task_carries_the_context_as_it_stands_at_send_time(self):
        mock_mgr = MagicMock()
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-1")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = _tool_with_parent_context(SubagentSendTaskAsyncTool())
            await tool.execute(subagent_name="sub-1", task="do X")

        seed = mock_mgr.send_task_async.call_args.kwargs["seed"]
        assert [m["role"] for m in seed] == ["user", "assistant"]

    @pytest.mark.asyncio
    async def test_send_async_poll_mode_disables_push(self):
        mock_mgr = MagicMock()
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-2")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskAsyncTool()
            result = await tool.execute(
                subagent_name="sub-1", task="do X", mode="poll",
            )
        assert "rpc-2" in result
        assert "Auto-push disabled" in result
        assert "subagent_get_task_result" in result
        mock_mgr.send_task_async.assert_awaited_once_with(
            "sub-1", "do X", mode="poll", seed=None,
        )

    @pytest.mark.asyncio
    async def test_send_async_rejects_invalid_mode(self):
        mock_mgr = MagicMock()
        mock_mgr.send_task_async = AsyncMock()
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSendTaskAsyncTool()
            result = await tool.execute(
                subagent_name="sub-1", task="do X", mode="push-forever",
            )
        assert result.startswith("Error")
        mock_mgr.send_task_async.assert_not_awaited()


class TestSubagentRunTaskBackgroundTool:
    """The worker is the pool's choice; the caller only brings the task."""

    @pytest.mark.asyncio
    async def test_missing_task(self):
        tool = SubagentRunTaskBackgroundTool()
        result = await tool.execute(task="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentRunTaskBackgroundTool()
            result = await tool.execute(task="do X")
        assert result == "Error: subagent manager is not running."

    @pytest.mark.asyncio
    async def test_reuses_an_idle_worker(self):
        """The pool's answer is the worker — and the caller is told which one,
        because polling, cancelling and removing are all addressed by it."""
        mock_mgr = MagicMock()
        mock_mgr.idle_worker = MagicMock(return_value="worker-2")
        mock_mgr.spawn = AsyncMock()
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-7")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            result = await SubagentRunTaskBackgroundTool().execute(task="do X")

        assert "worker-2" in result
        assert "rpc-7" in result
        assert "reused an idle worker" in result
        assert "subagent_get_task_result" in result
        assert "subagent_cancel_task" in result
        mock_mgr.spawn.assert_not_awaited()
        mock_mgr.send_task_async.assert_awaited_once_with(
            "worker-2", "do X", mode="auto", seed=None,
        )

    @pytest.mark.asyncio
    async def test_spawns_when_no_worker_is_idle(self):
        mock_mgr = MagicMock()
        mock_mgr.idle_worker = MagicMock(return_value=None)
        mock_mgr.count = 1
        mock_mgr.max_subagents = 5
        mock_mgr.spawn = AsyncMock(return_value="worker-1")
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-1")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            result = await SubagentRunTaskBackgroundTool().execute(task="do X")

        mock_mgr.spawn.assert_awaited_once_with()
        assert "worker-1" in result
        assert "a new one was spawned" in result
        mock_mgr.send_task_async.assert_awaited_once_with(
            "worker-1", "do X", mode="auto", seed=None,
        )

    @pytest.mark.asyncio
    async def test_a_full_pool_is_reported_not_queued(self):
        """Every worker busy and no room to add one: say so, send nothing.

        Queueing onto a worker the caller never chose would hide the wait, and
        the cap is the pool's to state.
        """
        mock_mgr = MagicMock()
        mock_mgr.idle_worker = MagicMock(return_value=None)
        mock_mgr.count = 2
        mock_mgr.max_subagents = 2
        mock_mgr.list = MagicMock(return_value=["worker-2", "worker-1"])
        mock_mgr.queued_count = MagicMock(
            side_effect=lambda n: {"worker-1": 3, "worker-2": 1}[n],
        )
        mock_mgr.spawn = AsyncMock()
        mock_mgr.send_task_async = AsyncMock()

        with patch(MANAGER_PATH, return_value=mock_mgr):
            result = await SubagentRunTaskBackgroundTool().execute(task="do X")

        assert result.startswith("Error")
        assert "pool is at its limit (2)" in result
        assert "worker-1 (3 in flight)" in result
        assert "worker-2 (1 in flight)" in result
        mock_mgr.spawn.assert_not_awaited()
        mock_mgr.send_task_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_spawn_failure_that_is_not_the_cap(self):
        """Room in the pool, but the child would not start — not the cap."""
        mock_mgr = MagicMock()
        mock_mgr.idle_worker = MagicMock(return_value=None)
        mock_mgr.count = 0
        mock_mgr.max_subagents = 5
        mock_mgr.spawn = AsyncMock(side_effect=OSError("no interpreter"))
        mock_mgr.send_task_async = AsyncMock()

        with patch(MANAGER_PATH, return_value=mock_mgr):
            result = await SubagentRunTaskBackgroundTool().execute(task="do X")

        assert result.startswith("Error spawning subagent")
        mock_mgr.send_task_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_poll_mode_disables_push(self):
        mock_mgr = MagicMock()
        mock_mgr.idle_worker = MagicMock(return_value="worker-1")
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-2")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            result = await SubagentRunTaskBackgroundTool().execute(
                task="do X", mode="poll",
            )
        assert "Auto-push disabled" in result
        mock_mgr.send_task_async.assert_awaited_once_with(
            "worker-1", "do X", mode="poll", seed=None,
        )

    @pytest.mark.asyncio
    async def test_rejects_invalid_mode(self):
        mock_mgr = MagicMock()
        mock_mgr.send_task_async = AsyncMock()
        with patch(MANAGER_PATH, return_value=mock_mgr):
            result = await SubagentRunTaskBackgroundTool().execute(
                task="do X", mode="push-forever",
            )
        assert result.startswith("Error")
        mock_mgr.send_task_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_task_carries_the_context_as_it_stands_at_send_time(self):
        mock_mgr = MagicMock()
        mock_mgr.idle_worker = MagicMock(return_value="worker-1")
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-1")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = _tool_with_parent_context(SubagentRunTaskBackgroundTool())
            await tool.execute(task="do X")

        seed = mock_mgr.send_task_async.call_args.kwargs["seed"]
        assert [m["role"] for m in seed] == ["user", "assistant"]


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
            assert result == "Error: subagent manager is not running."

    @pytest.mark.asyncio
    async def test_result_pending(self):
        mock_mgr = MagicMock()
        mock_mgr.get_task_result = MagicMock(return_value=("pending", None))

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentGetTaskResultTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
        assert result == "pending"

    @pytest.mark.asyncio
    async def test_result_ready(self):
        mock_mgr = MagicMock()
        mock_mgr.get_task_result = MagicMock(return_value=("completed", "the result"))

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentGetTaskResultTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
        assert result == "the result"

    @pytest.mark.asyncio
    async def test_unknown_task_id_says_so(self):
        """An unknown id is not "pending" — waiting is the wrong response."""
        mock_mgr = MagicMock()
        mock_mgr.get_task_result = MagicMock(return_value=("unknown", None))

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentGetTaskResultTool()
            result = await tool.execute(subagent_name="sub-1", task_id="typo")
        assert "Unknown" in result or "unknown" in result
        assert result != "pending"

    @pytest.mark.asyncio
    async def test_cancelled_task_reports_its_state(self):
        mock_mgr = MagicMock()
        mock_mgr.get_task_result = MagicMock(
            return_value=("cancelled", "Cancelled by parent"),
        )

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentGetTaskResultTool()
            result = await tool.execute(subagent_name="sub-1", task_id="rpc-1")
        assert result == "Cancelled by parent"


class TestSubagentListTasksTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentListTasksTool()
            result = await tool.execute()
            assert result == "Error: subagent manager is not running."

    @pytest.mark.asyncio
    async def test_no_records(self):
        mock_mgr = MagicMock()
        mock_mgr.list_tasks = MagicMock(return_value=([], 0))
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentListTasksTool()
            result = await tool.execute()
        assert "No subagent task records" in result

    @pytest.mark.asyncio
    async def test_lists_records(self):
        mock_mgr = MagicMock()
        mock_mgr.list_tasks = MagicMock(return_value=([
            {
                "task_id": "rpc-1", "agent_name": "sub-1", "status": "pending",
                "preview": "do X", "result": None,
            },
            {
                "task_id": "rpc-2", "agent_name": "sub-2", "status": "completed",
                "preview": "do Y", "result": "done",
            },
        ], 2))
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentListTasksTool()
            result = await tool.execute()
        assert "rpc-1" in result
        assert "rpc-2" in result
        assert "pending" in result
        assert "completed" in result
        assert "2" in result

    @pytest.mark.asyncio
    async def test_a_truncated_list_says_how_many_are_hidden(self):
        """A capped list must not present itself as the whole of it."""
        mock_mgr = MagicMock()
        mock_mgr.list_tasks = MagicMock(return_value=([
            {
                "task_id": "rpc-1", "agent_name": "sub-1", "status": "pending",
                "preview": "do X", "result": None,
            },
        ], 137))
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentListTasksTool()
            result = await tool.execute()
        assert "1 of 137" in result

    @pytest.mark.asyncio
    async def test_filters_passed_through(self):
        mock_mgr = MagicMock()
        mock_mgr.list_tasks = MagicMock(return_value=([], 0))
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
            assert result == "Error: subagent manager is not running."

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
