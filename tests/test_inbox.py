"""Tests for Slife.agent.inbox — ConversationStore and Inbox."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.a2a.identity import AgentId, AgentMessage, HUMAN
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
