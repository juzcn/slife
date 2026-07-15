"""Tests for slife.agent.service — AgentService lifecycle and message processing."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from slife.agent.service import AgentService
from slife.agent.llm_client import TokenUsage


# ── Helpers ─────────────────────────────────────────────────────────────────


def make_mock_config():
    """Build a mock Config suitable for AgentService tests."""
    from slife.config import Config, ModelConfig

    model = ModelConfig(
        ref="deepseek/deepseek-v4-flash",
        provider="deepseek",
        api_model="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        api_key="sk-test",
        base_url="https://api.deepseek.com",
        api="openai-completions",
        supports_vision=False,
        max_tokens=4096,
        context_window=131072,
        temperature=0.7,
        top_p=1.0,
        thinking_enabled=False,
        reasoning_effort=None,
    )

    config = Config(
        models=[model],
        active_model_ref="deepseek/deepseek-v4-flash",
        tools=[{"name": "execute_shell", "timeout": 30}],
        max_iterations=10,
    )
    return config


# ── AgentService initialisation ─────────────────────────────────────────────


class TestAgentServiceInit:
    """Tests for AgentService.__init__."""

    def test_basic_initialization(self):
        config = make_mock_config()
        service = AgentService(config)

        assert service.config is config
        assert service.llm_client is not None
        assert service.agent_loop is not None
        assert service.tool_registry is not None
        assert service.conversation is not None
        assert isinstance(service.session_usage, TokenUsage)

    def test_initial_mcp_state(self):
        config = make_mock_config()
        service = AgentService(config)

        assert service._mcp_client is None
        assert service._mcp_process is None
        assert service.mcp_enabled is False

    def test_initial_a2a_state(self):
        config = make_mock_config()
        service = AgentService(config)

        assert service._a2a_client is None
        assert service._a2a_broker is None
        assert service.a2a_enabled is False


class TestAgentServiceProperties:
    """Tests for AgentService properties."""

    def test_model_display_name(self):
        service = AgentService(make_mock_config())
        assert "DeepSeek" in service.model_display_name

    def test_thinking_enabled(self):
        config = make_mock_config()
        service = AgentService(config)
        assert service.thinking_enabled is False

    def test_subagent_manager_none_initially(self):
        service = AgentService(make_mock_config())
        assert service.subagent_manager is None


class TestAgentServiceClear:
    """Tests for AgentService.clear()."""

    def test_clear_resets_usage(self):
        service = AgentService(make_mock_config())
        service.session_usage = TokenUsage(
            prompt_tokens=500, completion_tokens=300, total_tokens=800,
        )

        service.clear()

        assert service.session_usage.total_tokens == 0

    def test_clear_preserves_system_prompt(self):
        service = AgentService(make_mock_config())
        initial_count = len(service.conversation.messages)
        # System prompt should be present
        assert initial_count >= 1

        service.clear()

        # clear() preserves the system prompt
        assert len(service.conversation.messages) == 1
        assert service.conversation.messages[0]["role"] == "system"


# ── AgentService MCP lifecycle ──────────────────────────────────────────────


class TestAgentServiceMCPLifecycle:
    """Tests for start_mcp and stop_mcp."""

    @pytest.mark.asyncio
    async def test_start_mcp_disabled_noop(self):
        service = AgentService(make_mock_config())
        # Config's mcp_config default is disabled
        await service.start_mcp()
        assert service._mcp_client is None

    @pytest.mark.asyncio
    @patch("slife.agent.service.MCPClient.is_wrapper_running", return_value=True)
    async def test_start_mcp_http(self, mock_probe):
        config = make_mock_config()
        # Enable MCP in config
        config.mcp_config = MagicMock()
        config.mcp_config.enabled = True
        config.mcp_config.wrapper_url = "http://test:9876/mcp"

        service = AgentService(config)

        with patch.object(service, "_connect_mcp_wrapper", AsyncMock()) as mock_connect, \
             patch.object(service, "_register_mcp_wrapper_tools", AsyncMock()) as mock_register, \
             patch.object(service, "_auto_connect_mcp_servers", AsyncMock()) as mock_auto:
            await service.start_mcp()

            mock_connect.assert_called_once()
            mock_register.assert_called_once()
            mock_auto.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_mcp_with_client(self):
        service = AgentService(make_mock_config())
        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()
        service._mcp_client = mock_client

        await service.stop_mcp()

        mock_client.disconnect.assert_called_once()
        assert service._mcp_client is None

    @pytest.mark.asyncio
    async def test_stop_mcp_with_process(self):
        service = AgentService(make_mock_config())
        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        service._mcp_process = mock_process

        await service.stop_mcp()

        mock_process.stop.assert_called_once()
        assert service._mcp_process is None

    @pytest.mark.asyncio
    async def test_stop_mcp_handles_errors(self):
        service = AgentService(make_mock_config())
        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock(side_effect=Exception("boom"))
        service._mcp_client = mock_client

        # Should not raise
        await service.stop_mcp()
        assert service._mcp_client is None


# ── AgentService memory ─────────────────────────────────────────────────────


class TestAgentServiceMemory:
    """Tests for memory-related methods."""

    def test_memory_not_enabled_initially(self):
        service = AgentService(make_mock_config())
        assert service.memory_enabled is False

    @pytest.mark.asyncio
    async def test_start_memory_disabled_noop(self):
        config = make_mock_config()
        config.memory_config.enabled = False
        service = AgentService(config)
        result = await service.start_memory()
        assert result is None

    @pytest.mark.asyncio
    async def test_save_to_memory_disabled_noop(self):
        service = AgentService(make_mock_config())
        # Should not raise
        await service.save_to_memory(turn_count=1, token_count=100)

    @pytest.mark.asyncio
    async def test_check_interrupted_disabled_returns_none(self):
        service = AgentService(make_mock_config())
        result = await service.check_interrupted()
        assert result is None

    @pytest.mark.asyncio
    async def test_stop_memory_noop_when_disabled(self):
        service = AgentService(make_mock_config())
        await service.stop_memory()  # Should not raise


# ── AgentService A2A ────────────────────────────────────────────────────────


class TestAgentServiceA2A:
    """Tests for A2A lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_a2a_disabled_noop(self):
        service = AgentService(make_mock_config())
        await service.start_a2a()
        assert service._a2a_client is None

    @pytest.mark.asyncio
    async def test_stop_a2a_noop_when_disabled(self):
        service = AgentService(make_mock_config())
        await service.stop_a2a()  # Should not raise


# ── AgentService subagent ───────────────────────────────────────────────────


class TestAgentServiceSubagent:
    """Tests for subagent lifecycle."""

    @pytest.mark.asyncio
    async def test_start_subagent_no_config_noop(self):
        service = AgentService(make_mock_config())
        await service.start_subagent()
        assert service._subagent_manager is None

    @pytest.mark.asyncio
    async def test_stop_subagent_noop_when_disabled(self):
        service = AgentService(make_mock_config())
        await service.stop_subagent()  # Should not raise


# ── AgentService callbacks ─────────────────────────────────────────────────


class TestAgentServiceCallbacks:
    """Tests for A2A activity callbacks."""

    @pytest.mark.asyncio
    async def test_on_a2a_activity_register_and_fire(self):
        service = AgentService(make_mock_config())
        cb = AsyncMock()
        service.on_a2a_activity(cb)

        await service._notify_a2a_activity("test_event", data="hello")

        cb.assert_called_once_with("test_event", data="hello")

    @pytest.mark.asyncio
    async def test_callback_error_is_swallowed(self):
        service = AgentService(make_mock_config())
        bad_cb = AsyncMock(side_effect=Exception("broken"))
        good_cb = AsyncMock()
        service.on_a2a_activity(bad_cb)
        service.on_a2a_activity(good_cb)

        await service._notify_a2a_activity("event")

        good_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_inbox_handler_factory_when_no_inbox(self):
        service = AgentService(make_mock_config())
        # Should not raise — inbox is None
        service.set_inbox_handler_factory(lambda: None)


# ── AgentService process_message ────────────────────────────────────────────


class TestAgentServiceProcessMessage:
    """Tests for process_message."""

    @pytest.mark.asyncio
    async def test_process_message_legacy_path(self):
        """When inbox is None, uses legacy direct path."""
        service = AgentService(make_mock_config())

        mock_result = MagicMock()
        mock_result.text = "response text"
        mock_result.usage = TokenUsage()

        service.agent_loop.run = AsyncMock(return_value=mock_result)
        service.conversation.add_user_message = MagicMock()

        handler = MagicMock()
        result = await service.process_message("hello", None, handler)

        service.agent_loop.run.assert_called_once()
        assert result.text == "response text"

    @pytest.mark.asyncio
    async def test_process_message_inbox_path(self):
        """When inbox is set, routes through inbox."""
        service = AgentService(make_mock_config())

        # Set up an inbox mock
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        mock_convs = MagicMock()
        mock_convs.register_handler = MagicMock()
        mock_inbox._conversations = mock_convs
        service.inbox = mock_inbox

        handler = MagicMock()
        result = await service.process_message("hello", None, handler)

        mock_inbox.post.assert_called_once()
        mock_convs.register_handler.assert_called_once()
        # Returns placeholder when using inbox
        assert result.text == ""


# ── AgentService stop_memory ────────────────────────────────────────────────


class TestAgentServiceStopMemory:
    """Tests for stop_memory."""

    @pytest.mark.asyncio
    async def test_stop_memory_with_active_client(self):
        service = AgentService(make_mock_config())
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock()
        mock_client.disconnect = AsyncMock()
        service._memory_client = mock_client
        service._diary_rowid = 42

        await service.stop_memory()

        mock_client.call_tool.assert_called_once_with(
            "memory_close_diary",
            {"rowid": 42, "author": service.config.user},
        )
        mock_client.disconnect.assert_called_once()
        assert service._memory_client is None
        assert service._diary_rowid is None

    @pytest.mark.asyncio
    async def test_stop_memory_with_process(self):
        service = AgentService(make_mock_config())
        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        service._memory_process = mock_process

        await service.stop_memory()

        mock_process.stop.assert_called_once()
        assert service._memory_process is None

    @pytest.mark.asyncio
    async def test_stop_memory_handles_errors(self):
        service = AgentService(make_mock_config())
        mock_client = MagicMock()
        mock_client.is_connected = True
        # call_tool raises — disconnect should still be attempted
        mock_client.call_tool = AsyncMock(side_effect=Exception("boom"))
        mock_client.disconnect = AsyncMock()
        service._memory_client = mock_client
        service._diary_rowid = 1

        await service.stop_memory()

        mock_client.disconnect.assert_called_once()
