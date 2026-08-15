"""Tests for Slife.agent.service — AgentService lifecycle and message processing."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import json as _json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.agent.service import AgentService, compact_tool_results
from slife.agent.plugins import PluginStartStatus
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

        assert service._plugins["a2a"].process is None
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

    @pytest.mark.asyncio
    async def test_on_server_updated_disabled_persists(self, sample_config):
        """_on_server_updated(enabled=False) unregisters tools and persists."""
        service = AgentService(sample_config)
        with patch.object(service.config, "set_server_enabled") as mock_set, \
             patch.object(service.tool_registry, "unregister_by_prefix", return_value=3) as mock_unreg:
            await service._on_server_updated("filesystem", False)

            mock_unreg.assert_called_once_with("filesystem__")
            mock_set.assert_called_once_with("filesystem", False)

    @pytest.mark.asyncio
    async def test_on_server_updated_enabled_persists(self, sample_config):
        """_on_server_updated(enabled=True) re-discovers and persists."""
        service = AgentService(sample_config)
        with patch.object(service.config, "set_server_enabled") as mock_set, \
             patch.object(service.tool_registry, "unregister_by_prefix", return_value=0) as mock_unreg, \
             patch.object(service, "_discover_and_register_external_tools", AsyncMock()) as mock_disc:
            await service._on_server_updated("filesystem", True)

            mock_unreg.assert_called_once_with("filesystem__")
            mock_disc.assert_awaited_once_with(server_name="filesystem")
            mock_set.assert_called_once_with("filesystem", True)


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
    async def test_save_to_memory_passes_created_at(self, sample_config):
        """The turn-start timestamp captured at display time flows to the
        __memory_save_turn tool as created_at (→ diary), keeping restore
        aligned with the live TUI."""
        from datetime import datetime

        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        conv = service.conversation
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        ts = datetime(2026, 8, 12, 14, 32, 9).astimezone()
        await service.save_to_memory(
            user_message="hi", token_count=10,
            conversation=conv, channel="human", created_at=ts,
        )

        mock_client.call_tool.assert_awaited_once()
        tool_name, args = mock_client.call_tool.await_args.args
        assert tool_name == "__memory_save_turn"
        assert args["created_at"].startswith("2026-08-12T14:32:09")
        # completed_at is captured after _ensure_turn_consistent — an ISO
        # timestamp from this run (not the threaded created_at).
        assert args["completed_at"].startswith(
            datetime.now().astimezone().strftime("%Y-%m-%d")
        )

    @pytest.mark.asyncio
    async def test_save_to_memory_no_created_at_omits_key(self, sample_config):
        """Without a threaded timestamp the tool is called without created_at."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        conv = service.conversation
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        await service.save_to_memory(
            user_message="hi", token_count=10, conversation=conv,
        )
        tool_name, args = mock_client.call_tool.await_args.args
        assert tool_name == "__memory_save_turn"
        assert "created_at" not in args

    @pytest.mark.asyncio
    async def test_save_to_memory_matches_sanitized_user_message(self, sample_config):
        """A user message containing an API key is sanitized on store, but the
        turn must still be saved — the backscan compares sanitized forms, so
        the assistant reply is persisted (not an empty turn)."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        secret = "sk-" + "a" * 24  # matches sanitize_secrets' sk- pattern
        conv = service.conversation
        conv.add_user_message(f"my key is {secret}")  # sanitized on store
        conv.add_assistant_message("got it")

        await service.save_to_memory(
            user_message=f"my key is {secret}", conversation=conv,
        )

        mock_client.call_tool.assert_awaited_once()
        tool_name, args = mock_client.call_tool.await_args.args
        assert tool_name == "__memory_save_turn"
        assert args["messages"]  # not an empty turn

    @pytest.mark.asyncio
    async def test_save_to_memory_skips_when_user_message_absent(self, sample_config):
        """When the user message is no longer in the conversation (rolled back
        on a content-policy error), nothing is saved — no empty diary row."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        # Empty conversation — no matching user message to anchor the turn.
        await service.save_to_memory(user_message="hi", token_count=10)

        mock_client.call_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_save_to_memory_fatal_error_freezes_inbox(self, sample_config):
        """A persistent memory-save failure (plugin returns {"error": ...})
        must NOT be silent — it sets memory-broken, freezes the inbox, and
        fires the on_memory_broken callback (TUI red banner)."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(
            return_value='{"error": "table diary has no column named images"}',
        )
        service._plugins["memdb"].client = mock_client

        conv = service.conversation
        conv.add_user_message("hi")
        conv.add_assistant_message("hello back")

        surfaced: list[str] = []
        service.on_memory_broken(surfaced.append)

        await service.save_to_memory(
            user_message="hi", token_count=10, conversation=conv,
        )

        assert service._memory_broken is True
        assert "no column named images" in service._memory_error
        assert surfaced == ["table diary has no column named images"]
        # Inbox frozen — new turns are dropped, not run without memory.
        assert service.inbox._frozen is True
        assert "记忆保存失败" in service.inbox._frozen_reason

    @pytest.mark.asyncio
    async def test_save_to_memory_compacts_oversized_tool_result(self, sample_config):
        """An oversized tool result is compacted to a head+tail digest in the
        DIARY copy, while the live conversation keeps the full output (the
        model reasoned over it this turn)."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        big = "y" * 50000
        conv = service.conversation
        conv.add_user_message("read the big file")
        conv.add_assistant_message(
            "", tool_calls=[{"id": "call_1", "type": "function",
                             "function": {"name": "read_file", "arguments": "{}"}}],
        )
        conv.add_tool_result("call_1", big)
        conv.add_assistant_message("the file is huge.")

        await service.save_to_memory(user_message="read the big file", conversation=conv)

        tool_name, args = mock_client.call_tool.await_args.args
        assert tool_name == "__memory_save_turn"
        # The persisted turn carries the digest, not the full blob.
        persisted_tool = next(m for m in args["messages"] if m.get("role") == "tool")
        assert len(persisted_tool["content"]) < 9000
        assert "[compacted at save: original 50000 chars" in persisted_tool["content"]
        assert "by re-running read_file" in persisted_tool["content"]
        # Live conversation is untouched — the model still has the full result.
        live_tool = next(m for m in conv.messages if m.get("role") == "tool")
        assert len(live_tool["content"]) == 50000

    @pytest.mark.asyncio
    async def test_save_to_memory_small_tool_result_untouched(self, sample_config):
        """Results within the memory budget are persisted as-is."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.is_connected = True
        mock_client.call_tool = AsyncMock(return_value="{}")
        service._plugins["memdb"].client = mock_client

        small = "z" * 100
        conv = service.conversation
        conv.add_user_message("hi")
        conv.add_assistant_message(
            "", tool_calls=[{"id": "call_1", "type": "function",
                             "function": {"name": "check", "arguments": "{}"}}],
        )
        conv.add_tool_result("call_1", small)
        conv.add_assistant_message("checked.")

        await service.save_to_memory(user_message="hi", conversation=conv)
        _, args = mock_client.call_tool.await_args.args
        persisted_tool = next(m for m in args["messages"] if m.get("role") == "tool")
        assert persisted_tool["content"] == small

    @pytest.mark.asyncio
    async def test_stop_memdb_noop_when_disabled(self, sample_config):
        service = AgentService(sample_config)
        await service.stop_memdb()  # Should not raise


class TestCompactToolResults:
    """Direct tests for the save-side compaction helper."""

    def test_compacts_oversized_result_to_head_tail(self):
        messages = [
            {"role": "assistant", "tool_calls": [
                {"id": "c1", "function": {"name": "run_python_script", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "A" * 1000 + "B" * 1000},
        ]
        n = compact_tool_results(messages, budget_chars=200)
        assert n == 1
        content = messages[1]["content"]
        assert len(content) < 400  # head 100 + marker + tail 100
        assert content.startswith("A" * 100)
        assert content.endswith("B" * 100)
        assert "[compacted at save: original 2000 chars" in content
        assert "by re-running run_python_script" in content

    def test_leaves_small_results_untouched(self):
        messages = [{"role": "tool", "tool_call_id": "c1", "content": "small"}]
        n = compact_tool_results(messages, budget_chars=8000)
        assert n == 0
        assert messages[0]["content"] == "small"

    def test_zero_budget_is_noop(self):
        messages = [{"role": "tool", "tool_call_id": "c1", "content": "x" * 50000}]
        n = compact_tool_results(messages, budget_chars=0)
        assert n == 0
        assert len(messages[0]["content"]) == 50000

    def test_does_not_mutate_input_dicts(self):
        big = "x" * 50000
        original = {"role": "tool", "tool_call_id": "c1", "content": big}
        messages = [original]
        n = compact_tool_results(messages, budget_chars=100)
        assert n == 1
        # the list slot is swapped for a copy — the caller's dict is untouched
        assert original["content"] == big
        assert messages[0] is not original

    def test_marker_omits_tool_name_when_unknown(self):
        messages = [{"role": "tool", "tool_call_id": "orphan", "content": "x" * 50000}]
        n = compact_tool_results(messages, budget_chars=100)
        assert n == 1
        assert "by re-running the tool" in messages[0]["content"]


# ── AgentService A2A ────────────────────────────────────────────────────────


class TestAgentServiceA2A:
    """Tests for A2A lifecycle methods."""

    @pytest.mark.asyncio
    async def test_start_a2a_disabled_noop(self, sample_config):
        service = AgentService(sample_config)
        result = await service.start_a2a()
        assert result is PluginStartStatus.SKIPPED
        assert service._plugins["a2a"].process is None

    @pytest.mark.asyncio
    async def test_start_a2a_broker_unreachable_skips(self, sample_config):
        """A2A enabled but no broker on the port → SKIPPED, not a failure.

        This is the "mosquitto 未启动" case: the plugin is expected to be
        skipped (A2A disabled at runtime), never reported as a crash.
        """
        from slife.a2a.config import A2AConfig
        service = AgentService(sample_config)
        service.config.a2a_config = A2AConfig(
            enabled=True, broker_host="localhost", broker_port=1883,
        )
        with patch(
            "slife.a2a.broker.probe_broker", AsyncMock(return_value=False),
        ):
            result = await service.start_a2a()
        assert result is PluginStartStatus.SKIPPED
        assert service._plugins["a2a"].process is None
        assert service.config.a2a_config.enabled is False  # downgraded

    @pytest.mark.asyncio
    async def test_stop_a2a_noop_when_disabled(self, sample_config):
        service = AgentService(sample_config)
        await service.stop_a2a()  # Should not raise

    @pytest.mark.asyncio
    async def test_a2a_poll_prepends_task_id(self, sample_config):
        """Inbound a2a tasks surface [Task <id> from <source>] to the LLM,
        so the receiver knows the task_id it is responding to."""
        import json as _json

        service = AgentService(sample_config)
        mock_a2a = MagicMock()
        mock_a2a.is_connected = True
        calls = [0]

        async def mock_call_tool(name, _):
            if name == "__a2a_drain_incoming":
                calls[0] += 1
                if calls[0] == 1:
                    return _json.dumps({
                        "tasks": [{
                            "source": "Jack", "content": "do X",
                            "reply_to": "Slife/slife/tasks/result",
                            "correlation_id": "cid-1",
                        }],
                        "presence": [], "cancellations": [],
                        "task_completions": [],
                    })
                service._plugins["a2a"].client = None  # end the loop
                return _json.dumps({
                    "tasks": [], "presence": [],
                    "cancellations": [], "task_completions": [],
                })
            return "{}"

        mock_a2a.call_tool = mock_call_tool
        service._plugins["a2a"].client = mock_a2a

        posted = []
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock(side_effect=lambda msg: posted.append(msg))
        mock_inbox.cancel_correlation = MagicMock()
        service.inbox = mock_inbox

        await service._a2a_poll_loop(interval=0.001)

        assert len(posted) == 1
        assert posted[0].content == "[Task cid-1 from Jack] do X"
        assert posted[0].correlation_id == "cid-1"


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
            assert result is PluginStartStatus.STARTED

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
            if tool_name == "__wechat_drain_incoming":
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
            if tool_name == "__wechat_drain_incoming":
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
            if tool_name == "__wechat_drain_incoming":
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
            if c[0][0] == "__wechat_dispatch_reply"
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
            if tool_name == "__wechat_drain_incoming":
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
            if tool_name == "__wechat_drain_incoming":
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


# ── Direct model switching (Ctrl+S, no LLM needed) ─────────────────────


def _two_model_config():
    """A Config with two models, first active."""
    from slife.config import Config, ModelConfig

    return Config(
        models=[
            ModelConfig(ref="deepseek/dsf", provider="deepseek", api_model="dsf", display_name="DSF", api_key="k"),
            ModelConfig(ref="openai/gpt", provider="openai", api_model="gpt", display_name="GPT", api_key="k"),
        ],
        active_model_ref="deepseek/dsf",
        tools=[],
    )


class TestSwitchModel:
    def test_switch_updates_runtime(self):
        service = AgentService(_two_model_config())
        msg = service.switch_model("openai/gpt")
        assert service.config.active_model_ref == "openai/gpt"
        assert service.config.active_model.display_name == "GPT"
        assert service.llm_client is not None
        assert "GPT" in msg

    def test_switch_persists_active_model(self, tmp_path):
        from slife.tools._config_io import read_config, write_config

        config = _two_model_config()
        path = tmp_path / "slife.json5"
        config._path = path
        write_config(path, {"models": {"providers": {}}, "active_model": "deepseek/dsf"})
        service = AgentService(config)

        service.switch_model("openai/gpt")

        raw = read_config(path)
        assert raw["active_model"] == "openai/gpt"

    def test_switch_unknown_ref_raises(self):
        service = AgentService(_two_model_config())
        with pytest.raises(ValueError, match="Unknown model ref"):
            service.switch_model("nope/x")

    def test_switch_no_config_path_still_switches(self):
        """In-memory switch works even without a writable config file."""
        service = AgentService(_two_model_config())
        service.config._path = None
        msg = service.switch_model("openai/gpt")
        assert service.config.active_model_ref == "openai/gpt"
        assert "GPT" in msg


class TestGetRecentTurns:
    """Restore fetch: page-by-page (batch), select newest within budget,
    return oldest-first so the conversation rebuilds chronologically."""

    def _make_db(self, tmp_path, n):
        import sqlite3

        db = tmp_path / "test.db"
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE diary (user_message TEXT, messages TEXT, summary TEXT, "
            "tags TEXT, images TEXT NOT NULL DEFAULT '', channel TEXT, "
            "created_at TEXT, completed_at TEXT, "
            "who_helped TEXT, what_model TEXT, token_count INT)"
        )
        for i in range(1, n + 1):
            con.execute(
                "INSERT INTO diary (user_message, messages, channel, created_at, token_count) "
                "VALUES (?, ?, 'human', ?, ?)",
                (
                    f"msg {i}",
                    _json.dumps([{"role": "assistant", "content": "x" * 90}]),
                    f"2026-08-12T{i:02d}:00:00+08:00",
                    100 + i,
                ),
            )
        con.commit()
        con.close()
        return db

    @pytest.mark.asyncio
    async def test_batches_newest_within_budget_oldest_first(
        self, sample_config, tmp_path, monkeypatch
    ):
        from slife.agent.service import AgentService

        db = self._make_db(tmp_path, 5)
        srv = AgentService(sample_config)
        # Small budget: each turn ~41 tokens (est), budget 200 fits 4.
        srv.config.active_model.context_window = 1000
        srv.config.context_floor = 0.2
        monkeypatch.setattr(srv, "_get_memory_db_path", lambda: db)

        turns, skipped = await srv.get_recent_turns(limit=2)  # batches of 2 → 3 pages

        ids = [t["rowid"] for t in turns]
        assert ids == sorted(ids), "must be oldest-first for the restore"
        assert ids == [2, 3, 4, 5], "newest 4 within the budget, oldest-first"
        assert skipped == 1, "5 fetched turns, budget fits 4 → 1 dropped"

    @pytest.mark.asyncio
    async def test_broken_db_raises_memory_error(
        self, sample_config, tmp_path, monkeypatch,
    ):
        """A present-but-broken memory DB raises MemoryDatabaseError —
        restore treats it as fatal (startup abort) instead of silently
        returning [] and starting a memory-less session."""
        import sqlite3
        from slife.agent.service import AgentService, MemoryDatabaseError

        # Old-schema DB — missing the `images` column the store SELECTs.
        db = tmp_path / "old.db"
        con = sqlite3.connect(str(db))
        con.execute(
            "CREATE TABLE diary (user_message TEXT, messages TEXT, summary TEXT, "
            "tags TEXT, channel TEXT, created_at TEXT, "
            "who_helped TEXT, what_model TEXT, token_count INT)"
        )
        con.execute(
            "INSERT INTO diary (user_message, messages, channel, created_at, token_count) "
            "VALUES ('hi', '[]', 'human', '2026-08-12T00:00:00+08:00', 100)"
        )
        con.commit()
        con.close()

        srv = AgentService(sample_config)
        monkeypatch.setattr(srv, "_get_memory_db_path", lambda: db)
        with pytest.raises(MemoryDatabaseError):
            await srv.get_recent_turns()
