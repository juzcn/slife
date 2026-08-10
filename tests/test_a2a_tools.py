"""Tests for Slife.tools.a2a — A2A tool definitions and execute logic."""

import pytest; pytestmark = pytest.mark.unit


import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.tools.a2a import (
    A2AListSubagentsTool,
    SubagentSpawnTool,
    SubagentStopTool,
    A2ANotifyUserTool,
)

# Patch paths: tools use lazy imports from Slife.a2a.client / Slife.subagent.process
CLIENT_PATH = "slife.a2a.client.get_client"
MANAGER_PATH = "slife.subagent.process.get_manager"

# ═══════════════════════════════════════════════════════════════════════════
# Metadata tests — every tool
# ═══════════════════════════════════════════════════════════════════════════


TOOLS = [
    A2AListSubagentsTool,
    SubagentSpawnTool,
    SubagentStopTool,
    A2ANotifyUserTool,
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


class TestA2AListSubagentsTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = A2AListSubagentsTool()
            result = await tool.execute()
            assert "not running yet" in result

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


class TestSubagentSpawnTool:
    @pytest.mark.asyncio
    async def test_no_manager(self):
        with patch(MANAGER_PATH, return_value=None):
            tool = SubagentSpawnTool()
            result = await tool.execute()
            assert "not running yet" in result

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

    @pytest.mark.asyncio
    async def test_spawn_context_default_pure(self):
        """Spawn defaults to a pure context (no cloned messages)."""
        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-3")

        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSpawnTool()
            await tool.execute()

        kwargs = mock_mgr.spawn.call_args.kwargs
        assert kwargs["context_source"] == "pure"
        assert kwargs["context_messages"] is None

    @pytest.mark.asyncio
    async def test_spawn_context_cloned_serializes(self):
        """context='cloned' passes the parent conversation messages to spawn."""
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
        cfg = Config(models=[mc], active_model_ref="t/m", tools=[], agent_id="testbot")

        mock_mgr = MagicMock()
        mock_mgr.spawn = AsyncMock(return_value="sub-4")
        with patch(MANAGER_PATH, return_value=mock_mgr):
            tool = SubagentSpawnTool()
            object.__setattr__(tool, "_ctx", ToolContext(conversation=conv, config=cfg))
            await tool.execute(context="cloned")

        kwargs = mock_mgr.spawn.call_args.kwargs
        assert kwargs["context_source"] == "cloned"
        assert kwargs["context_messages"] is not None
        roles = [m["role"] for m in kwargs["context_messages"]]
        assert roles == ["user", "assistant"]

    def test_serialize_cloned_context_drops_system(self):
        """The parent's system message (incl. footer) is not serialized."""
        from slife.agent.conversation import Conversation
        from slife.config import Config, ModelConfig
        from slife.tools.a2a import _serialize_cloned_context
        from slife.tools.context import ToolContext

        conv = Conversation(system_prompt="PARENT_SYS")
        conv.add_user_message("t1")
        conv.add_assistant_message("r1")
        mc = ModelConfig(
            ref="t/m", provider="t", api_model="m", display_name="M",
            api_key="k", context_window=1000,
        )
        cfg = Config(models=[mc], active_model_ref="t/m", tools=[], agent_id="testbot")

        data = _serialize_cloned_context(ToolContext(conversation=conv, config=cfg))
        assert data is not None
        assert all(m.get("role") != "system" for m in data)


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
            assert "not running yet" in result

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


class TestA2ANotifyUserTool:
    @pytest.mark.asyncio
    async def test_missing_message(self):
        tool = A2ANotifyUserTool()
        result = await tool.execute(message="")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_notification_sent(self):
        tool = A2ANotifyUserTool()
        with patch("slife.platform.desktop_notify"):
            result = await tool.execute(title="Test", message="Hello world")
        assert "Notification sent" in result
        assert "Test" in result
        assert "Hello world" in result


# ═══════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
# A2ABroadcastTool
# ═══════════════════════════════════════════════════════════════════════════

