"""Tests for Slife.agent.inbox — ConversationStore and Inbox."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.a2a.identity import AgentId, AgentMessage, HUMAN, WECHAT
from slife.agent.inbox import ConversationStore


# ── ConversationStore ───────────────────────────────────────────────────


class TestConversationStore:
    """Tests for ConversationStore — per-agent conversation management."""

    @pytest.fixture
    def store(self):
        return ConversationStore(system_prompt="You are helpful.")

    def test_get_or_create_human_persistent(self, store):
        conv1 = store.get_or_create(HUMAN)
        conv2 = store.get_or_create(HUMAN)
        assert conv1 is conv2  # Same conversation object

    def test_get_or_create_human_has_system_prompt(self, store):
        conv = store.get_or_create(HUMAN)
        msgs = conv.to_openai_messages()
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "You are helpful."

    def test_get_or_create_remote_agent_one_shot(self, store):
        remote = AgentId("agent-7")
        conv1 = store.get_or_create(remote)
        conv2 = store.get_or_create(remote)
        assert conv1 is not conv2  # Fresh conversation each time

    def test_get_or_create_remote_has_system_prompt(self, store):
        remote = AgentId("agent-7")
        conv = store.get_or_create(remote)
        msgs = conv.to_openai_messages()
        assert msgs[0]["content"] == "You are helpful."

    def test_register_and_get_handler(self, store):
        handler = MagicMock()
        store.register_handler(HUMAN, handler)
        assert store.handler_for(HUMAN) is handler

    def test_handler_for_unregistered_returns_none(self, store):
        assert store.handler_for(AgentId("unknown")) is None

    def test_register_none_handler(self, store):
        store.register_handler(AgentId("bot"), None)
        assert store.handler_for(AgentId("bot")) is None

    def test_clear_removes_conversation(self, store):
        conv = store.get_or_create(HUMAN)
        store.clear(HUMAN)
        # After clear, a new get_or_create should give a fresh conversation
        new_conv = store.get_or_create(HUMAN)
        assert new_conv is not conv

    def test_clear_unknown_agent_noop(self, store):
        """Clearing an unknown agent should not raise."""
        store.clear(AgentId("unknown"))  # Should not raise


# ── AgentMessage ────────────────────────────────────────────────────────


class TestAgentMessage:
    """Tests for AgentMessage dataclass."""

    def test_minimal_message(self):
        msg = AgentMessage(source=HUMAN, content="hi")
        assert msg.source == HUMAN
        assert msg.content == "hi"
        assert msg.images == []
        assert msg.reply_to is None
        assert msg.correlation_id is None

    def test_full_message(self):
        msg = AgentMessage(
            source=AgentId("agent-1"),
            content="task result",
            images=["img1.png"],
            reply_to="Slife/human/tasks",
            correlation_id="corr-123",
        )
        assert msg.images == ["img1.png"]
        assert msg.reply_to == "Slife/human/tasks"
        assert msg.correlation_id == "corr-123"

    def test_agent_id_new_type(self):
        """AgentId is a NewType — behaves like str."""
        aid = AgentId("my-agent")
        assert aid == "my-agent"
        assert isinstance(aid, str)

    def test_human_is_agent_id(self):
        assert HUMAN == "human"
        assert isinstance(HUMAN, str)


# ── Inbox — construction and post ───────────────────────────────────────


class TestInboxConstruction:
    """Tests for Inbox.__init__ and post."""

    @pytest.fixture
    def mock_loop(self):
        return AsyncMock()

    @pytest.fixture
    def mock_store(self):
        return MagicMock(spec=ConversationStore)

    def test_construction(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)
        assert inbox._agent_loop is mock_loop
        assert inbox._conversations is mock_store
        assert inbox._a2a_client is None
        assert inbox._on_activity is None

    def test_construction_with_a2a_client(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        mock_client = MagicMock()
        inbox = Inbox(mock_loop, mock_store, a2a_client=mock_client)
        assert inbox._a2a_client is mock_client

    @pytest.mark.asyncio
    async def test_post_enqueues_message(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)
        msg = AgentMessage(source=HUMAN, content="hello")
        await inbox.post(msg)
        assert not inbox._queue.empty()

    @pytest.mark.asyncio
    async def test_post_from_remote_agent(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)
        msg = AgentMessage(source=AgentId("agent-3"), content="task done")
        await inbox.post(msg)
        assert not inbox._queue.empty()

    def test_busy_initial_false(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)
        assert inbox.busy is False

    def test_pending_initial_zero(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)
        assert inbox.pending == 0

    @pytest.mark.asyncio
    async def test_pending_increases_after_post(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)
        await inbox.post(AgentMessage(source=HUMAN, content="1"))
        await inbox.post(AgentMessage(source=HUMAN, content="2"))
        assert inbox.pending == 2


# ── Inbox — _process_one ──────────────────────────────────────────────


class TestInboxProcessOne:
    """Tests for Inbox._process_one and related processing."""

    @pytest.fixture
    def mock_loop(self):
        loop = MagicMock()
        loop.run = AsyncMock()
        return loop

    @pytest.fixture
    def mock_store(self):
        from slife.agent.inbox import ConversationStore
        store = MagicMock(spec=ConversationStore)
        store.get_or_create = MagicMock()
        store.handler_for = MagicMock(return_value=None)
        return store

    @pytest.fixture
    def mock_a2a_client(self):
        client = MagicMock()
        client.update_status = AsyncMock()
        client._adapter = MagicMock()
        client._adapter.publish = AsyncMock()
        return client

    def _make_msg(self, source=None, content="hi", images=None,
                  handler=None, reply_to=None, corr_id=None, on_reply=None):
        return AgentMessage(
            source=source or HUMAN,
            content=content,
            images=images or [],
            handler=handler,
            reply_to=reply_to,
            correlation_id=corr_id,
            on_reply=on_reply,
        )

    @pytest.mark.asyncio
    async def test_process_human_message(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)

        mock_result = MagicMock()
        mock_result.text = "response"
        mock_result.usage.total_tokens = 50
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg()
        await inbox._process_one(msg)
        mock_loop.run.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_process_remote_task_updates_status(self, mock_loop, mock_store,
                                                       mock_a2a_client):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store, a2a_client=mock_a2a_client)

        mock_result = MagicMock()
        mock_result.text = "done"
        mock_result.usage.total_tokens = 30
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(source=AgentId("remote-1"), content="task",
                             reply_to="Slife/human/tasks", corr_id="corr-1")
        # Patch _publish_reply to avoid real MQTT publish
        inbox._publish_reply = AsyncMock()
        await inbox._process_one(msg)

        # Should have set busy and then idle again
        mock_a2a_client.update_status.assert_any_call("busy")
        mock_a2a_client.update_status.assert_any_call("idle")
        assert inbox.busy is False  # Should be idle after processing

    @pytest.mark.asyncio
    async def test_process_wechat_peer_message(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox

        on_activity = AsyncMock()
        inbox = Inbox(mock_loop, mock_store, on_activity=on_activity)

        mock_result = MagicMock()
        mock_result.text = "reply via wechat"
        mock_result.usage.total_tokens = 20
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(source=WECHAT, content="wechat message")
        await inbox._process_one(msg)

        # on_activity should have been called for peer_message
        calls = [c[0][0] for c in on_activity.call_args_list]
        assert "peer_message" in calls

    @pytest.mark.asyncio
    async def test_process_error_handling(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)

        mock_loop.run = AsyncMock(side_effect=RuntimeError("loop crashed"))
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(content="bad input", reply_to="topic/x")
        # Should not raise — error is caught
        await inbox._process_one(msg)
        assert inbox.busy is False

    @pytest.mark.asyncio
    async def test_process_on_turn_complete_called(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        on_turn_complete = AsyncMock()
        inbox = Inbox(mock_loop, mock_store, on_turn_complete=on_turn_complete)

        mock_result = MagicMock()
        mock_result.text = "ok"
        mock_result.usage.total_tokens = 77
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(content="go")
        await inbox._process_one(msg)

        on_turn_complete.assert_awaited_once()
        call_kwargs = on_turn_complete.call_args[1]
        assert call_kwargs["token_count"] == 77

    @pytest.mark.asyncio
    async def test_process_on_turn_complete_error_swallowed(self, mock_loop,
                                                             mock_store):
        from slife.agent.inbox import Inbox
        on_turn_complete = AsyncMock(side_effect=RuntimeError("persist failed"))
        inbox = Inbox(mock_loop, mock_store, on_turn_complete=on_turn_complete)

        mock_result = MagicMock()
        mock_result.text = "ok"
        mock_result.usage.total_tokens = 1
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(content="go")
        # Should not raise
        await inbox._process_one(msg)

    @pytest.mark.asyncio
    async def test_process_with_on_reply_callback(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        on_reply = AsyncMock()
        inbox = Inbox(mock_loop, mock_store)

        mock_result = MagicMock()
        mock_result.text = "Here is the answer."
        mock_result.usage.total_tokens = 10
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(content="question", on_reply=on_reply)
        await inbox._process_one(msg)

        on_reply.assert_awaited_once_with("Here is the answer.")

    @pytest.mark.asyncio
    async def test_process_on_reply_error_swallowed(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        on_reply = AsyncMock(side_effect=RuntimeError("send failed"))
        inbox = Inbox(mock_loop, mock_store)

        mock_result = MagicMock()
        mock_result.text = "answer"
        mock_result.usage.total_tokens = 5
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(content="q", on_reply=on_reply)
        # Should not raise
        await inbox._process_one(msg)

    @pytest.mark.asyncio
    async def test_process_error_publishes_reply(self, mock_loop, mock_store,
                                                  mock_a2a_client):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store, a2a_client=mock_a2a_client)

        mock_loop.run = AsyncMock(side_effect=ValueError("broken"))
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(source=AgentId("remote"), content="do it",
                             reply_to="Slife/human/tasks", corr_id="err-1")
        await inbox._process_one(msg)

        mock_a2a_client._adapter.publish.assert_awaited_once()
        call_args = mock_a2a_client._adapter.publish.call_args
        assert "Error" in call_args[0][1]  # payload contains error

    @pytest.mark.asyncio
    async def test_process_on_activity_error_swallowed(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        on_activity = AsyncMock(side_effect=RuntimeError("activity failed"))
        inbox = Inbox(mock_loop, mock_store, on_activity=on_activity)

        mock_result = MagicMock()
        mock_result.text = "ok"
        mock_result.usage.total_tokens = 1
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(source=AgentId("remote-2"), content="task")
        # Should not raise — error is caught in try/except pass
        await inbox._process_one(msg)

    @pytest.mark.asyncio
    async def test_process_uses_message_handler(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        handler = MagicMock()
        inbox = Inbox(mock_loop, mock_store)

        mock_result = MagicMock()
        mock_result.text = "handled"
        mock_result.usage.total_tokens = 1
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(content="q", handler=handler)
        await inbox._process_one(msg)

        mock_loop.run.assert_awaited_once()
        # handler from the message should be used, not from the store
        call_kwargs = mock_loop.run.call_args[1]
        assert call_kwargs["handler"] is handler

    @pytest.mark.asyncio
    async def test_process_with_images(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)

        mock_result = MagicMock()
        mock_result.text = "I see the image"
        mock_result.usage.total_tokens = 100
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(content="describe this", images=["img.png"])
        await inbox._process_one(msg)

        mock_loop.run.assert_awaited_once()
        call_kwargs = mock_loop.run.call_args[1]
        assert call_kwargs["images"] == ["img.png"]

    @pytest.mark.asyncio
    async def test_process_no_images(self, mock_loop, mock_store):
        from slife.agent.inbox import Inbox
        inbox = Inbox(mock_loop, mock_store)

        mock_result = MagicMock()
        mock_result.text = "ok"
        mock_result.usage.total_tokens = 1
        mock_loop.run = AsyncMock(return_value=mock_result)
        mock_store.get_or_create.return_value = MagicMock()

        msg = self._make_msg(content="hi", images=[])
        await inbox._process_one(msg)

        call_kwargs = mock_loop.run.call_args[1]
        assert call_kwargs["images"] is None


# ── Inbox — run loop ──────────────────────────────────────────────────


class TestInboxRun:
    """Tests for Inbox.run() background processing loop."""

    @pytest.mark.asyncio
    async def test_run_processes_single_message(self):
        from slife.agent.inbox import Inbox
        mock_loop = MagicMock()
        mock_loop.run = AsyncMock()
        mock_store = MagicMock()
        mock_store.get_or_create = MagicMock()
        mock_store.handler_for = MagicMock(return_value=None)

        inbox = Inbox(mock_loop, mock_store)
        # Post a message, then cancel the loop after first message
        await inbox.post(AgentMessage(source=HUMAN, content="one"))

        # Run the loop in a task and cancel after brief processing
        import asyncio
        task = asyncio.create_task(inbox.run())
        # Give it time to process one message
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # The queue should be empty or nearly empty
        assert inbox.pending == 0

    @pytest.mark.asyncio
    async def test_run_with_wechat_message(self):
        from slife.agent.inbox import Inbox
        mock_loop = MagicMock()
        mock_loop.run = AsyncMock()
        mock_store = MagicMock()
        mock_store.get_or_create = MagicMock()
        mock_store.handler_for = MagicMock(return_value=None)

        inbox = Inbox(mock_loop, mock_store)
        await inbox.post(AgentMessage(source=WECHAT, content="wechat msg"))

        import asyncio
        task = asyncio.create_task(inbox.run())
        await asyncio.sleep(0.01)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── Inbox — _publish_reply ─────────────────────────────────────────────


class TestInboxPublishReply:
    """Tests for Inbox._publish_reply()."""

    @pytest.mark.asyncio
    async def test_publish_with_result_object(self):
        from slife.agent.inbox import Inbox
        mock_loop = MagicMock()
        mock_store = MagicMock()
        mock_a2a = MagicMock()
        mock_a2a._adapter.publish = AsyncMock()

        inbox = Inbox(mock_loop, mock_store, a2a_client=mock_a2a)

        mock_result = MagicMock()
        mock_result.text = "the answer"

        await inbox._publish_reply("topic/reply", "corr-42", mock_result)
        mock_a2a._adapter.publish.assert_awaited_once_with(
            "topic/reply", mock_a2a._adapter.publish.call_args[0][1], qos=1,
        )
        payload = mock_a2a._adapter.publish.call_args[0][1]
        assert "corr-42" in payload
        assert "the answer" in payload

    @pytest.mark.asyncio
    async def test_publish_with_string_result(self):
        from slife.agent.inbox import Inbox
        mock_loop = MagicMock()
        mock_store = MagicMock()
        mock_a2a = MagicMock()
        mock_a2a._adapter.publish = AsyncMock()

        inbox = Inbox(mock_loop, mock_store, a2a_client=mock_a2a)

        await inbox._publish_reply("t/x", None, "just a string")
        mock_a2a._adapter.publish.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_publish_with_empty_correlation_id(self):
        from slife.agent.inbox import Inbox
        mock_loop = MagicMock()
        mock_store = MagicMock()
        mock_a2a = MagicMock()
        mock_a2a._adapter.publish = AsyncMock()

        inbox = Inbox(mock_loop, mock_store, a2a_client=mock_a2a)
        mock_result = MagicMock()
        mock_result.text = "result"

        await inbox._publish_reply("t/y", None, mock_result)
        payload = mock_a2a._adapter.publish.call_args[0][1]
        assert '"correlation_id": ""' in payload


# ── ConversationStore — default handler factory ───────────────────────


class TestConversationStoreDefaultHandler:
    """Tests for ConversationStore.set_default_handler_factory /
    handler_for fallback chain."""

    def test_set_and_use_default_factory(self):
        store = ConversationStore(system_prompt="test")
        default_handler = MagicMock()
        factory = MagicMock(return_value=default_handler)
        store.set_default_handler_factory(factory)

        # Unknown source with no registered handler
        result = store.handler_for(AgentId("unknown-bot"))
        assert result is default_handler
        factory.assert_called_once()

    def test_registered_handler_takes_precedence_over_default(self):
        store = ConversationStore(system_prompt="test")
        registered = MagicMock()
        default = MagicMock()
        store.register_handler(AgentId("bot"), registered)
        store.set_default_handler_factory(lambda: default)

        result = store.handler_for(AgentId("bot"))
        assert result is registered

    def test_human_handler_fallback(self):
        store = ConversationStore(system_prompt="test")
        human_handler = MagicMock()
        store.register_handler(HUMAN, human_handler)

        # Unknown source should fall back to human handler
        result = store.handler_for(AgentId("unknown"))
        assert result is human_handler


# ── ConversationStore — WeChat persistence ────────────────────────────


class TestConversationStoreWeChat:
    """Tests for ConversationStore.get_or_create with WeChat source."""

    def test_wechat_conversation_is_persistent(self):
        store = ConversationStore(system_prompt="test")
        conv1 = store.get_or_create(WECHAT)
        conv2 = store.get_or_create(WECHAT)
        assert conv1 is conv2

    def test_wechat_conversation_has_system_prompt(self):
        store = ConversationStore(system_prompt="be helpful")
        conv = store.get_or_create(WECHAT)
        msgs = conv.to_openai_messages()
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "be helpful"
