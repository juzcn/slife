"""Tests for Slife.agent.service — AgentService lifecycle and message processing."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import json as _json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.agent.service import AgentService
from slife.agent.llm_client import TokenUsage
from slife.a2a.identity import HUMAN, WECHAT


# ── AgentService initialisation ─────────────────────────────────────────────


class TestAgentServiceInit:
    """Tests for AgentService.__init__."""

    def test_basic_initialization(self, sample_config):
        config = sample_config
        service = AgentService(config)

        assert service.config is config
        assert service.llm_client is not None
        assert service.agent_loop is not None
        assert service.tool_registry is not None
        assert service.conversation is not None
        assert isinstance(service.session_usage, TokenUsage)

    def test_initial_mcp_state(self, sample_config):
        config = sample_config
        service = AgentService(config)

        assert service._plugins["mcp"].client is None
        assert service._plugins["mcp"].process is None
        assert service.mcp_enabled is False

    def test_initial_a2a_state(self, sample_config):
        config = sample_config
        service = AgentService(config)

        assert service._plugins["mqtt"].process is None
        assert service.a2a_enabled is False


class TestAgentServiceProperties:
    """Tests for AgentService properties."""

    def test_model_display_name(self, sample_config):
        service = AgentService(sample_config)
        assert "DeepSeek" in service.model_display_name

    def test_thinking_enabled(self, sample_config):
        config = sample_config
        service = AgentService(config)
        assert service.thinking_enabled is False

    def test_subagent_manager_none_initially(self, sample_config):
        service = AgentService(sample_config)
        assert service.subagent_manager is None


class TestAgentServiceClear:
    """Tests for AgentService.clear()."""

    def test_clear_resets_usage(self, sample_config):
        service = AgentService(sample_config)
        service.session_usage = TokenUsage(
            prompt_tokens=500, completion_tokens=300, total_tokens=800,
        )

        service.clear()

        assert service.session_usage.total_tokens == 0

    def test_clear_preserves_system_prompt(self, sample_config):
        service = AgentService(sample_config)
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
    async def test_start_mcp_always_enabled(self, sample_config):
        config = sample_config
        config.mcp_config = MagicMock()

        service = AgentService(config)

        with patch.object(service, "_connect_mcp_wrapper", AsyncMock()) as mock_connect, \
             patch.object(service, "_register_plugin_tools", AsyncMock()) as mock_register, \
             patch.object(service, "_auto_connect_mcp_servers", AsyncMock()) as mock_auto:
            await service.start_mcp()

            mock_connect.assert_called_once()
            mock_register.assert_called_once()
            mock_auto.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_mcp_with_client(self, sample_config):
        service = AgentService(sample_config)
        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock()
        service._plugins["mcp"].client = mock_client

        await service.stop_mcp()

        mock_client.disconnect.assert_called_once()
        assert service._plugins["mcp"].client is None

    @pytest.mark.asyncio
    async def test_stop_mcp_with_process(self, sample_config):
        service = AgentService(sample_config)
        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        service._plugins["mcp"].process = mock_process

        await service.stop_mcp()

        mock_process.stop.assert_called_once()
        assert service._plugins["mcp"].process is None

    @pytest.mark.asyncio
    async def test_stop_mcp_handles_errors(self, sample_config):
        service = AgentService(sample_config)
        mock_client = MagicMock()
        mock_client.disconnect = AsyncMock(side_effect=Exception("boom"))
        service._plugins["mcp"].client = mock_client

        # Should not raise
        await service.stop_mcp()
        assert service._plugins["mcp"].client is None


# ── AgentService memory ─────────────────────────────────────────────────────


class TestAgentServiceMemory:
    """Tests for memory-related methods."""

    def test_memory_not_enabled_initially(self, sample_config):
        service = AgentService(sample_config)
        assert service.memdb_enabled is False

    @pytest.mark.asyncio
    async def test_start_memdb_always_runs(self, sample_config):
        config = sample_config
        service = AgentService(config)
        with patch.object(service, "_spawn_and_register_plugin", AsyncMock()) as mock_spawn:
            result = await service.start_memdb()
            mock_spawn.assert_called_once()
            assert result is True

    @pytest.mark.asyncio
    async def test_save_to_memory_disabled_noop(self, sample_config):
        service = AgentService(sample_config)
        # Should not raise — memdb_enabled is False, so it returns early
        await service.save_to_memory(user_message="test", token_count=100)

    @pytest.mark.asyncio
    async def test_save_to_memory_no_user_message(self, sample_config):
        service = AgentService(sample_config)
        # Should not raise with no user_message
        await service.save_to_memory()

    @pytest.mark.asyncio
    async def test_stop_memdb_noop_when_disabled(self, sample_config):
        service = AgentService(sample_config)
        await service.stop_memdb()  # Should not raise


# ── AgentService A2A ────────────────────────────────────────────────────────


class TestAgentServiceA2A:
    """Tests for A2A lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_mqtt_disabled_noop(self, sample_config):
        service = AgentService(sample_config)
        result = await service.start_mqtt()
        assert result is False
        assert service._plugins["mqtt"].process is None

    @pytest.mark.asyncio
    async def test_stop_mqtt_noop_when_disabled(self, sample_config):
        service = AgentService(sample_config)
        await service.stop_mqtt()  # Should not raise


# ── AgentService subagent ───────────────────────────────────────────────────


class TestAgentServiceSubagent:
    """Tests for subagent lifecycle."""

    @pytest.mark.asyncio
    async def test_start_subagent_always_creates_manager(self, sample_config):
        """Subagent is always enabled — start_subagent always creates a manager."""
        service = AgentService(sample_config)
        await service.start_subagent()
        assert service._subagent_manager is not None
        assert service._subagent_manager.count == 0

    @pytest.mark.asyncio
    async def test_stop_subagent_noop_when_disabled(self, sample_config):
        service = AgentService(sample_config)
        await service.stop_subagent()  # Should not raise


# ── AgentService callbacks ─────────────────────────────────────────────────


class TestAgentServiceCallbacks:
    """Tests for A2A activity callbacks."""

    @pytest.mark.asyncio
    async def test_on_a2a_activity_register_and_fire(self, sample_config):
        service = AgentService(sample_config)
        cb = AsyncMock()
        service.on_a2a_activity(cb)

        await service._notify_a2a_activity("test_event", data="hello")

        cb.assert_called_once_with("test_event", data="hello")

    @pytest.mark.asyncio
    async def test_callback_error_is_swallowed(self, sample_config):
        service = AgentService(sample_config)
        bad_cb = AsyncMock(side_effect=Exception("broken"))
        good_cb = AsyncMock()
        service.on_a2a_activity(bad_cb)
        service.on_a2a_activity(good_cb)

        await service._notify_a2a_activity("event")

        good_cb.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_inbox_handler_factory_when_no_inbox(self, sample_config):
        service = AgentService(sample_config)
        # Should not raise — inbox is None
        service.set_inbox_handler_factory(lambda: None)


# ── AgentService process_message ────────────────────────────────────────────


class TestAgentServiceProcessMessage:
    """Tests for process_message."""

    @pytest.mark.asyncio
    async def test_process_message_unified_queue(self, sample_config):
        """Always routes through inbox — handler is attached to the message."""
        from slife.a2a.identity import HUMAN

        service = AgentService(sample_config)

        # inbox is always created in __init__
        assert service.inbox is not None

        # Set up inbox mock
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        handler = MagicMock()
        result = await service.process_message("hello", None, handler)

        # Should post to inbox
        mock_inbox.post.assert_called_once()

        # The message should carry the handler
        call_args = mock_inbox.post.call_args[0]
        msg = call_args[0]
        assert msg.handler is handler
        assert msg.content == "hello"
        assert msg.source == HUMAN

        # Returns placeholder
        assert result.text == ""


# ── AgentService stop_memdb ────────────────────────────────────────────────


class TestAgentServiceStopMemory:
    """Tests for stop_memdb."""

    @pytest.mark.asyncio
    async def test_stop_memdb_with_active_client(self, sample_config):
        service = AgentService(sample_config)
        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()
        service._plugins["memdb"].client = mock_client

        await service.stop_memdb()

        mock_client.disconnect.assert_called_once()
        assert service._plugins["memdb"].client is None

    @pytest.mark.asyncio
    async def test_stop_memdb_with_process(self, sample_config):
        service = AgentService(sample_config)
        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        service._plugins["memdb"].process = mock_process  # pyright: ignore[reportAttributeAccessIssue]

        await service.stop_memdb()

        mock_process.stop.assert_called_once()
        assert service._plugins["memdb"].process is None

    @pytest.mark.asyncio
    async def test_stop_memdb_handles_errors(self, sample_config):
        service = AgentService(sample_config)
        mock_client = MagicMock()
        mock_client.is_connected = True
        # call_tool raises — disconnect should still be attempted
        mock_client.call_tool = AsyncMock(side_effect=Exception("boom"))
        mock_client.disconnect = AsyncMock()
        service._plugins["memdb"].client = mock_client

        await service.stop_memdb()

        mock_client.disconnect.assert_called_once()


# ── Inbox: always-active unified message queue ───────────────────────────────


class TestAgentServiceInbox:
    """Tests for the always-active unified inbox."""

    def test_inbox_always_created(self, sample_config):
        """Inbox is created in __init__ — not conditional on A2A."""
        service = AgentService(sample_config)
        assert service.inbox is not None

    def test_inbox_has_correct_wiring(self, sample_config):
        """Inbox is wired with agent_loop, conversations, and on_turn_complete."""
        service = AgentService(sample_config)
        inbox = service.inbox

        assert inbox._agent_loop is service.agent_loop
        # _on_activity is a bound method — use equality not identity
        assert inbox._on_activity.__func__ is service._notify_a2a_activity.__func__  # type: ignore[union-attr]
        assert inbox._on_turn_complete.__func__ is service.save_to_memory.__func__  # type: ignore[union-attr]
        # HUMAN conversation is pre-seeded from service.conversation
        assert inbox._conversations._convs.get(HUMAN) is service.conversation

    @pytest.mark.asyncio
    async def test_start_inbox_creates_background_task(self, sample_config):
        """start_inbox launches inbox.run() as a background task."""
        service = AgentService(sample_config)

        # Replace inbox.run with a mock so we don't actually start the loop
        mock_run = AsyncMock()
        service.inbox._runner_task = None  # ensure clean state
        with patch.object(service.inbox, "run", mock_run):
            await service.start_inbox()

        assert service._inbox_task is not None

    @pytest.mark.asyncio
    async def test_stop_inbox_cancels_task(self, sample_config):
        """stop_inbox cancels the background task and waits for it."""
        service = AgentService(sample_config)

        # Create a real cancellable task
        async def _fake_run():
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise

        service._inbox_task = asyncio.create_task(_fake_run())
        await asyncio.sleep(0)  # let it start

        await service.stop_inbox()

        assert service._inbox_task is None

    @pytest.mark.asyncio
    async def test_stop_inbox_noop_when_not_started(self, sample_config):
        """stop_inbox is safe when inbox was never started."""
        service = AgentService(sample_config)
        service._inbox_task = None
        await service.stop_inbox()  # Should not raise

    @pytest.mark.asyncio
    async def test_process_message_routes_through_inbox(self, sample_config):
        """process_message enqueues via inbox with handler on the message."""
        service = AgentService(sample_config)

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        handler = MagicMock()
        result = await service.process_message("test msg", None, handler)

        mock_inbox.post.assert_called_once()
        msg = mock_inbox.post.call_args[0][0]
        assert msg.source == HUMAN
        assert msg.content == "test msg"
        assert msg.handler is handler
        assert result.text == ""  # placeholder


# ── WeChat lifecycle ─────────────────────────────────────────────────────────


class TestAgentServiceWeChat:
    """Tests for WeChat plugin lifecycle and message processing."""

    def test_wechat_not_enabled_initially(self, sample_config):
        """WeChat client is None until start_wechat is called."""
        service = AgentService(sample_config)
        assert service.wechat_enabled is False
        assert service._plugins["wechat"].client is None

    @pytest.mark.asyncio
    async def test_stop_wechat_noop_when_disabled(self, sample_config):
        """stop_wechat is safe when WeChat was never started."""
        service = AgentService(sample_config)
        await service.stop_wechat()  # Should not raise

    @pytest.mark.asyncio
    async def test_start_wechat_with_mocked_internals(self, sample_config):
        """start_wechat spawns the server, registers tools, and starts polling."""
        service = AgentService(sample_config)

        # WeChat must be enabled in config for start_wechat to proceed
        mock_wechat_cfg = MagicMock()
        mock_wechat_cfg.enabled = True
        service.config.wechat_config = mock_wechat_cfg

        # _spawn_and_register_plugin is mocked so _wechat_client is never
        # set — wire up a mock client for the check_status call and poll loop.
        mock_wechat_client = MagicMock()
        mock_wechat_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["wechat"].client = mock_wechat_client  # pyright: ignore[reportAttributeAccessIssue]

        with patch.object(service, "_spawn_and_register_plugin", AsyncMock()) as mock_spawn:
            result = await service.start_wechat()

            mock_spawn.assert_called_once()
            assert result is True

    @pytest.mark.asyncio
    async def test_stop_wechat_cancels_poll_and_disconnects(self, sample_config):
        """stop_wechat stops the poll loop and disconnects the client."""
        service = AgentService(sample_config)

        # Set up a fake poll task
        async def _fake_poll():
            try:
                while True:
                    await asyncio.sleep(60)
            except asyncio.CancelledError:
                raise

        service._plugins["wechat"].poll_task = asyncio.create_task(_fake_poll())

        mock_client = MagicMock()
        mock_client.is_connected = True
        mock_client.disconnect = AsyncMock()
        service._plugins["wechat"].client = mock_client

        mock_process = MagicMock()
        mock_process.stop = AsyncMock()
        service._plugins["wechat"].process = mock_process  # pyright: ignore[reportAttributeAccessIssue]

        await service.stop_wechat()

        # Poll task cancelled and cleaned up
        assert service._plugins["wechat"].poll_task is None
        # Client disconnected
        mock_client.disconnect.assert_called_once()
        assert service._plugins["wechat"].client is None
        # Process stopped
        mock_process.stop.assert_called_once()
        assert service._plugins["wechat"].process is None

    @pytest.mark.asyncio
    async def test_wechat_poll_posts_to_inbox(self, sample_config):
        """The poll loop fetches messages and posts AgentMessages to inbox."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "_wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    return _json.dumps({"messages": [{
                        "to_user_id": "wx_user_123",
                        "context_token": "ctx_abc",
                        "text": "你好",
                    }]})
                # Disconnect after first poll to exit the loop cleanly
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        # Message posted to inbox
        mock_inbox.post.assert_called_once()
        msg = mock_inbox.post.call_args[0][0]
        assert msg.source == WECHAT
        assert msg.content == "你好"
        assert msg.metadata["channel"] == "wechat"
        assert msg.on_reply is not None

    @pytest.mark.asyncio
    async def test_wechat_poll_skips_empty_text(self, sample_config):
        """Messages with empty text are not posted to inbox."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "_wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    return _json.dumps({"messages": [
                        {"to_user_id": "wx_1", "context_token": "c1", "text": "   "},
                        {"to_user_id": "wx_2", "context_token": "c2", "text": "real"},
                    ]})
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        # Only the non-empty message is posted
        assert mock_inbox.post.call_count == 1
        msg = mock_inbox.post.call_args[0][0]
        assert msg.content == "real"

    @pytest.mark.asyncio
    async def test_wechat_reply_callback_sends_message(self, sample_config):
        """The on_reply callback delivers the response text via send_message."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "_wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    return _json.dumps({"messages": [{
                        "to_user_id": "wx_123",
                        "context_token": "ctx_xyz",
                        "text": "帮我查一下天气",
                    }]})
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            elif tool_name == "send_typing":
                return "{}"
            elif tool_name == "send_message":
                return "{}"
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        # Extract the reply callback from the posted message
        msg = mock_inbox.post.call_args[0][0]
        assert msg.on_reply is not None

        # Reset call_tool to track post-poll calls
        mock_wc.call_tool = AsyncMock(return_value="{}")

        await msg.on_reply("今天北京晴，25°C")

        # Verify wechat_dispatch_reply was called with correct params
        send_calls = [
            c for c in mock_wc.call_tool.call_args_list
            if c[0][0] == "_wechat_dispatch_reply"
        ]
        assert len(send_calls) == 1
        _, send_args = send_calls[0][0]
        assert send_args["to_user_id"] == "wx_123"
        assert send_args["context_token"] == "ctx_xyz"
        assert send_args["text"] == "今天北京晴，25°C"

    @pytest.mark.asyncio
    async def test_wechat_typing_sent_on_arrival(self, sample_config):
        """send_typing(status=1) is called when a message arrives."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "_wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    return _json.dumps({"messages": [{
                        "to_user_id": "wx_1",
                        "context_token": "ctx_1",
                        "text": "hello",
                    }]})
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = AsyncMock(side_effect=mock_call_tool)
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        await service._wechat_poll_loop(interval=0.001)

        # After refactor: typing is managed server-side by the plugin.
        # The harness only calls wechat_drain_incoming; the plugin internally
        # starts the typing keep-alive. Verify message arrived at inbox instead.
        mock_inbox.post.assert_called_once()
        msg = mock_inbox.post.call_args[0][0]
        assert msg.content == "hello"
        assert msg.on_reply is not None

    @pytest.mark.asyncio
    async def test_wechat_poll_error_handling(self, sample_config):
        """Poll errors are caught and do not crash the loop."""
        service = AgentService(sample_config)

        mock_wc = MagicMock()
        mock_wc.is_connected = True

        call_count = [0]

        async def mock_call_tool(tool_name, _):
            if tool_name == "_wechat_drain_incoming":
                call_count[0] += 1
                if call_count[0] == 1:
                    raise Exception("network error")
                # Second call succeeds but disconnects
                mock_wc.is_connected = False
                return _json.dumps({"messages": []})
            return "{}"

        mock_wc.call_tool = mock_call_tool
        service._plugins["wechat"].client = mock_wc

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        # Should not raise
        await service._wechat_poll_loop(interval=0.001)

        # Error on first poll, second poll should still run
        assert call_count[0] == 2
