"""Tests for Slife.tools.a2a — A2A tool definitions and execute logic."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.tools.a2a import (
    A2AListAgentsTool,
    A2AListSubagentsTool,
    A2ASendTaskTool,
    A2ASendTaskAsyncTool,
    A2AGetTaskResultTool,
    A2ACancelTaskTool,
    A2AListTasksTool,
    A2ASubscribeTaskTool,
    SubagentSpawnTool,
    SubagentStopTool,
    A2AGetAgentCardTool,
    A2ANotifyUserTool,
    A2ABroadcastTool,
)

# Patch paths: tools use lazy imports from Slife.a2a.client / Slife.subagent.process
CLIENT_PATH = "Slife.a2a.client.get_client"
MANAGER_PATH = "Slife.subagent.process.get_manager"

# ═══════════════════════════════════════════════════════════════════════════
# Metadata tests — every tool
# ═══════════════════════════════════════════════════════════════════════════


TOOLS = [
    A2AListAgentsTool,
    A2AListSubagentsTool,
    A2ASendTaskTool,
    A2ASendTaskAsyncTool,
    A2AGetTaskResultTool,
    A2ACancelTaskTool,
    A2AListTasksTool,
    A2ASubscribeTaskTool,
    SubagentSpawnTool,
    SubagentStopTool,
    A2AGetAgentCardTool,
    A2ANotifyUserTool,
    A2ABroadcastTool,
]


class TestAllToolsMetadata:
    """Every A2A tool must have name, description, parameters, and execute."""

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
# A2AListAgentsTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2AListAgentsTool:
    @pytest.mark.asyncio
    async def test_no_client_available(self):
        with patch(CLIENT_PATH, return_value=None):
            tool = A2AListAgentsTool()
            result = await tool.execute()
            assert "not active" in result

    @pytest.mark.asyncio
    async def test_no_peers(self):
        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[])
        with patch(CLIENT_PATH, return_value=mock_client):
            tool = A2AListAgentsTool()
            result = await tool.execute()
            # Now always includes self — never returns "No remote agents"
            assert "(you)" in result

    @pytest.mark.asyncio
    async def test_with_peers(self):
        from slife.a2a.card import AgentCard
        from slife.a2a.identity import AgentId

        mock_client = MagicMock()
        mock_client.list_agents = AsyncMock(return_value=[
            AgentCard(agent_id=AgentId("peer-1"), display_name="Peer 1", status="idle"),
            AgentCard(agent_id=AgentId("peer-2"), display_name="", status="busy"),
        ])
        with patch(CLIENT_PATH, return_value=mock_client):
            tool = A2AListAgentsTool()
            result = await tool.execute()
            assert "peer-1" in result
            assert "Peer 1" in result
            assert "peer-2" in result
            assert "idle" in result
            assert "(you)" in result  # self is always listed
            assert "busy" in result


# ═══════════════════════════════════════════════════════════════════════════
# A2AListSubagentsTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2AListSubagentsTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = A2AListSubagentsTool()
            result = await tool.execute()
            assert "not enabled" in result

    @pytest.mark.asyncio
    async def test_no_subagents(self):
        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=[])
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = A2AListSubagentsTool()
            result = await tool.execute()
            assert "No local subagents" in result

    @pytest.mark.asyncio
    async def test_with_subagents(self):
        mock_proc = MagicMock()
        mock_proc.pid = 12345
        mock_proc.is_ready = True
        mock_proc.is_running = True

        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["sub-1", "sub-2"])
        mock_mgr.get = MagicMock(return_value=mock_proc)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = A2AListSubagentsTool()
            result = await tool.execute()
            assert "sub-1" in result
            assert "sub-2" in result
            assert "pid=12345" in result


# ═══════════════════════════════════════════════════════════════════════════
# A2ASendTaskTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2ASendTaskTool:
    @pytest.mark.asyncio
    async def test_missing_required_params(self):
        tool = A2ASendTaskTool()
        result = await tool.execute(agent_id="", task="")
        assert "Error" in result
        assert "required" in result

    @pytest.mark.asyncio
    async def test_missing_task(self):
        tool = A2ASendTaskTool()
        result = await tool.execute(agent_id="agent-1", task="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        with patch(MANAGER_PATH, return_value=None), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ASendTaskTool()
            result = await tool.execute(agent_id="unknown", task="do stuff")
            assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_routes_to_subagent(self):
        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["sub-1"])
        mock_mgr.send_task = AsyncMock(return_value="Task completed: result here")

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ASendTaskTool()
            result = await tool.execute(agent_id="sub-1", task="do stuff")
            assert "Task completed" in result
            mock_mgr.send_task.assert_awaited_once_with("sub-1", "do stuff")

    @pytest.mark.asyncio
    async def test_subagent_timeout(self):
        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["sub-1"])
        mock_mgr.send_task = AsyncMock(side_effect=TimeoutError())

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ASendTaskTool()
            result = await tool.execute(agent_id="sub-1", task="do stuff")
            assert "timed out" in result.lower()

    @pytest.mark.asyncio
    async def test_routes_to_mqtt_peer(self):
        mock_client = MagicMock()
        mock_client.send_task = AsyncMock(return_value="MQTT result")

        with patch(MANAGER_PATH, return_value=None), \
             patch(CLIENT_PATH, return_value=mock_client):
            tool = A2ASendTaskTool()
            result = await tool.execute(agent_id="peer-1", task="do remote")
            assert "MQTT result" in result


# ═══════════════════════════════════════════════════════════════════════════
# A2ASendTaskAsyncTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2ASendTaskAsyncTool:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = A2ASendTaskAsyncTool()
        result = await tool.execute(agent_id="", task="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_subagent_async_success(self):
        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["sub-1"])
        mock_mgr.send_task_async = AsyncMock(return_value="rpc-123")

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ASendTaskAsyncTool()
            result = await tool.execute(agent_id="sub-1", task="async work")
            assert "rpc-123" in result
            assert "asynchronously" in result

    @pytest.mark.asyncio
    async def test_mqtt_async_success(self):
        mock_client = MagicMock()
        mock_client.send_task_async = AsyncMock(return_value="corr-456")

        with patch(MANAGER_PATH, return_value=None), \
             patch(CLIENT_PATH, return_value=mock_client):
            tool = A2ASendTaskAsyncTool()
            result = await tool.execute(agent_id="peer-1", task="async remote")
            assert "corr-456" in result
            assert "MQTT" in result

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        with patch(MANAGER_PATH, return_value=None), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ASendTaskAsyncTool()
            result = await tool.execute(agent_id="unknown", task="work")
            assert "not found" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════
# A2AGetTaskResultTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2AGetTaskResultTool:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = A2AGetTaskResultTool()
        result = await tool.execute(agent_id="", task_id="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_task_not_found_agent_exists(self):
        from slife.a2a.task_store import clear_store

        clear_store()
        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["sub-1"])
        mock_mgr.get_task_result = MagicMock(return_value=None)

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2AGetTaskResultTool()
            result = await tool.execute(agent_id="sub-1", task_id="t-unknown")
            assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        from slife.a2a.task_store import clear_store

        clear_store()
        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=[])
        mock_mgr.get_task_result = MagicMock(return_value=None)

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2AGetTaskResultTool()
            result = await tool.execute(agent_id="unknown", task_id="t-x")
            assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_task_found_in_store(self):
        from slife.a2a.task_store import get_store, clear_store

        clear_store()
        store = get_store()
        store.record_send("t1", "agent-1", "test task", "subagent")
        store.record_result("t1", "All done!")

        with patch(MANAGER_PATH, return_value=None), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2AGetTaskResultTool()
            result = await tool.execute(agent_id="agent-1", task_id="t1")
            assert "COMPLETED" in result
            assert "All done!" in result


# ═══════════════════════════════════════════════════════════════════════════
# A2ACancelTaskTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2ACancelTaskTool:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = A2ACancelTaskTool()
        result = await tool.execute(agent_id="", task_id="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_cancel_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.get_task_result = MagicMock(return_value=None)

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ACancelTaskTool()
            result = await tool.execute(agent_id="sub-1", task_id="t-x")
            assert "not found" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════
# A2AListTasksTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2AListTasksTool:
    @pytest.mark.asyncio
    async def test_empty_store(self):
        from slife.a2a.task_store import clear_store

        clear_store()
        tool = A2AListTasksTool()
        result = await tool.execute()
        assert "No tasks found" in result

    @pytest.mark.asyncio
    async def test_with_tasks(self):
        from slife.a2a.task_store import get_store, clear_store

        clear_store()
        store = get_store()
        store.record_send("t1", "agent-1", "task one", "mqtt")
        store.record_send("t2", "agent-2", "task two", "subagent")
        store.record_result("t1", "done")

        tool = A2AListTasksTool()
        result = await tool.execute()
        assert "t1" in result
        assert "t2" in result

    @pytest.mark.asyncio
    async def test_with_filter(self):
        from slife.a2a.task_store import get_store, clear_store

        clear_store()
        store = get_store()
        store.record_send("t1", "agent-1", "task one", "mqtt")
        store.record_send("t2", "agent-2", "task two", "subagent")

        tool = A2AListTasksTool()
        result = await tool.execute(status="pending")
        assert "t1" in result


# ═══════════════════════════════════════════════════════════════════════════
# A2ASubscribeTaskTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2ASubscribeTaskTool:
    @pytest.mark.asyncio
    async def test_missing_params(self):
        tool = A2ASubscribeTaskTool()
        result = await tool.execute(agent_id="", task_id="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_already_completed(self):
        from slife.a2a.task_store import get_store, clear_store

        clear_store()
        store = get_store()
        store.record_send("t1", "agent-1", "task", "mqtt")
        store.record_result("t1", "Already done")

        tool = A2ASubscribeTaskTool()
        result = await tool.execute(agent_id="agent-1", task_id="t1")
        assert "completed" in result.lower()
        assert "Already done" in result

    @pytest.mark.asyncio
    async def test_poll_until_complete(self):
        from slife.a2a.task_store import get_store, clear_store
        import asyncio

        clear_store()
        store = get_store()
        store.record_send("t1", "agent-1", "task", "mqtt")

        async def delayed_result():
            await asyncio.sleep(0.05)
            store.record_result("t1", "Delayed done")

        with patch(MANAGER_PATH, return_value=None), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ASubscribeTaskTool()
            task = asyncio.create_task(delayed_result())
            result = await tool.execute(agent_id="agent-1", task_id="t1", timeout=2.0)
            await task
            assert "completed" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════
# SubagentSpawnTool
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentSpawnTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentSpawnTool()
            result = await tool.execute()
            assert "not enabled" in result

    @pytest.mark.asyncio
    async def test_spawn_success(self):
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-1")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSpawnTool()
            result = await tool.execute(name="worker")
            assert "sub-1" in result
            assert "spawned" in result.lower()

    @pytest.mark.asyncio
    async def test_spawn_with_auto_name(self):
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-2")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSpawnTool()
            result = await tool.execute()
            assert "sub-2" in result

    @pytest.mark.asyncio
    async def test_spawn_failure(self):
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(side_effect=RuntimeError("no memory"))

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSpawnTool()
            result = await tool.execute()
            assert "Error" in result


# ═══════════════════════════════════════════════════════════════════════════
# SubagentStopTool
# ═══════════════════════════════════════════════════════════════════════════


class TestSubagentStopTool:
    @pytest.mark.asyncio
    async def test_missing_agent_id(self):
        tool = SubagentStopTool()
        result = await tool.execute(agent_id="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentStopTool()
            result = await tool.execute(agent_id="sub-1")
            assert "not enabled" in result

    @pytest.mark.asyncio
    async def test_stop_success(self):
        mock_mgr = MagicMock()
        mock_mgr.stop = AsyncMock(return_value=True)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentStopTool()
            result = await tool.execute(agent_id="sub-1")
            assert "stopped" in result.lower()

    @pytest.mark.asyncio
    async def test_stop_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.stop = AsyncMock(return_value=False)

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentStopTool()
            result = await tool.execute(agent_id="sub-1")
            assert "not found" in result.lower()


# ═══════════════════════════════════════════════════════════════════════════
# A2AGetAgentCardTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2AGetAgentCardTool:
    @pytest.mark.asyncio
    async def test_missing_agent_id(self):
        tool = A2AGetAgentCardTool()
        result = await tool.execute(agent_id="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=[])

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2AGetAgentCardTool()
            result = await tool.execute(agent_id="unknown")
            assert "not found" in result.lower()

    @pytest.mark.asyncio
    async def test_local_subagent_card(self):
        mock_proc = MagicMock()
        mock_proc.pid = 9999
        mock_proc.is_ready = True
        mock_proc.is_running = True

        mock_mgr = MagicMock()
        mock_mgr.list = MagicMock(return_value=["sub-1"])
        mock_mgr.get = MagicMock(return_value=mock_proc)

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2AGetAgentCardTool()
            result = await tool.execute(agent_id="sub-1")
            assert "local subagent" in result
            assert "pid=9999" in result

    @pytest.mark.asyncio
    async def test_mqtt_peer_card(self):
        from slife.a2a.card import AgentCard
        from slife.a2a.identity import AgentId

        mock_client = MagicMock()
        mock_client.get_agent_card = MagicMock(return_value=AgentCard(
            agent_id=AgentId("peer-1"), display_name="Remote", status="idle",
        ))

        with patch(MANAGER_PATH, return_value=None), \
             patch(CLIENT_PATH, return_value=mock_client):
            tool = A2AGetAgentCardTool()
            result = await tool.execute(agent_id="peer-1")
            assert "MQTT" in result
            assert "Remote" in result


# ═══════════════════════════════════════════════════════════════════════════
# A2ANotifyUserTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2ANotifyUserTool:
    @pytest.mark.asyncio
    async def test_missing_message(self):
        tool = A2ANotifyUserTool()
        result = await tool.execute(message="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_notification_sent(self):
        tool = A2ANotifyUserTool()
        with patch("Slife.tools.a2a._desktop_notify"):
            result = await tool.execute(title="Test", message="Hello world")
        assert "Notification sent" in result
        assert "Test" in result
        assert "Hello world" in result


# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# A2ABroadcastTool
# ═══════════════════════════════════════════════════════════════════════════


class TestA2ABroadcastTool:
    @pytest.mark.asyncio
    async def test_missing_task(self):
        tool = A2ABroadcastTool()
        result = await tool.execute(task="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_no_agents_available(self):
        with patch(MANAGER_PATH, return_value=None), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ABroadcastTool()
            result = await tool.execute(task="hello everyone")
            assert "No agents available" in result

    @pytest.mark.asyncio
    async def test_broadcast_to_subagents(self):
        mock_mgr = MagicMock()
        mock_mgr.broadcast = AsyncMock(return_value=["corr-1", "corr-2"])

        with patch(MANAGER_PATH, return_value=mock_mgr), \
             patch(CLIENT_PATH, return_value=None):
            tool = A2ABroadcastTool()
            result = await tool.execute(task="hello")
            assert "2 agent" in result
            assert "corr-1" in result
            assert "corr-2" in result
