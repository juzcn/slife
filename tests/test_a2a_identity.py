"""Tests for slife.a2a.identity — AgentName, AgentMessage, HUMAN, WECHAT."""

import pytest; pytestmark = pytest.mark.unit


import pytest

from slife.a2a.identity import (
    AgentName,
    AgentMessage,
    Channel,
    HUMAN,
    WECHAT,
)


# ── AgentName ──────────────────────────────────────────────────────────────


class TestAgentName:
    """Tests for the AgentName NewType."""

    def test_agent_name_is_string(self):
        """AgentName is a str subtype."""
        aid = AgentName("test-agent")
        assert aid == "test-agent"
        assert isinstance(aid, str)

    def test_agent_name_equality(self):
        """Same string == same agent."""
        a = AgentName("agent-1")
        b = AgentName("agent-1")
        assert a == b

    def test_agent_name_inequality(self):
        """Different strings != same agent."""
        a = AgentName("agent-1")
        b = AgentName("agent-2")
        assert a != b

    def test_agent_name_hashable(self):
        """AgentName can be used as dict key and in sets."""
        d = {AgentName("a"): 1, AgentName("b"): 2}
        assert d[AgentName("a")] == 1


# ── HUMAN / WECHAT constants ─────────────────────────────────────────────


class TestConstants:
    """Tests for HUMAN and WECHAT sentinel values."""

    def test_human_is_agent_name(self):
        """HUMAN is an AgentName."""
        assert isinstance(HUMAN, str)
        assert HUMAN == "human"

    def test_wechat_is_agent_name(self):
        """WECHAT is an AgentName."""
        assert isinstance(WECHAT, str)
        assert WECHAT == "wechat"

    def test_constants_are_different(self):
        """HUMAN and WECHAT are different values."""
        assert HUMAN != WECHAT


# ── AgentMessage ─────────────────────────────────────────────────────────


class TestAgentMessage:
    """Tests for the AgentMessage dataclass."""

    def test_minimal_creation(self):
        """AgentMessage requires only source and content."""
        msg = AgentMessage(source=HUMAN, content="hello")
        assert msg.source == HUMAN
        assert msg.content == "hello"

    def test_defaults(self):
        """Check all default field values."""
        msg = AgentMessage(source=AgentName("bot"), content="test")
        assert msg.images == []
        assert msg.reply_to is None
        assert msg.correlation_id is None
        assert msg.metadata == {}
        assert msg.on_reply is None
        assert msg.handler is None
        assert msg.channel == Channel.human()

    def test_full_creation(self):
        """All fields can be set explicitly."""
        async def my_reply(text: str) -> None:
            pass

        msg = AgentMessage(
            source=AgentName("sub-1"),
            content="task result",
            images=["img1.png"],
            reply_to="task-123",
            correlation_id="corr-456",
            metadata={"channel": "mqtt"},
            on_reply=my_reply,
            handler=None,
        )
        assert msg.source == "sub-1"
        assert msg.content == "task result"
        assert msg.images == ["img1.png"]
        assert msg.reply_to == "task-123"
        assert msg.correlation_id == "corr-456"
        assert msg.metadata == {"channel": "mqtt"}
        assert msg.on_reply is my_reply
        assert msg.handler is None

    def test_equality(self):
        """Two messages with same fields are equal (dataclass default)."""
        m1 = AgentMessage(source=HUMAN, content="hi")
        m2 = AgentMessage(source=HUMAN, content="hi")
        assert m1 == m2

    def test_inequality_different_source(self):
        """Different source → not equal."""
        m1 = AgentMessage(source=HUMAN, content="hi")
        m2 = AgentMessage(source=WECHAT, content="hi")
        assert m1 != m2

    def test_inequality_different_content(self):
        """Different content → not equal."""
        m1 = AgentMessage(source=HUMAN, content="hi")
        m2 = AgentMessage(source=HUMAN, content="bye")
        assert m1 != m2

    def test_metadata_is_per_instance(self):
        """metadata dict is not shared between instances."""
        m1 = AgentMessage(source=HUMAN, content="a")
        m2 = AgentMessage(source=HUMAN, content="b")
        m1.metadata["key"] = "val"
        assert "key" not in m2.metadata

    def test_images_is_per_instance(self):
        """images list is not shared between instances."""
        m1 = AgentMessage(source=HUMAN, content="a")
        m2 = AgentMessage(source=HUMAN, content="b")
        m1.images.append("img.png")
        assert m2.images == []


# ── Channel ──────────────────────────────────────────────────────────────


class TestChannel:
    """Tests for the Channel type — kind + per-channel payload."""

    def test_factories(self):
        """Each factory produces the right kind."""
        assert Channel.human().kind == "human"
        assert Channel.wechat().kind == "wechat"
        assert Channel.subagent("w1").kind == "subagent"
        assert Channel.heartbeat().kind == "heartbeat"
        assert Channel.a2a("Jack").kind == "a2a"
        assert Channel.system().kind == "system"

    def test_a2a_payload_holds_peer_name(self):
        """A2A channel carries the peer agent name as payload."""
        assert Channel.a2a("Jack").data == {"agent_name": "Jack"}

    def test_subagent_payload(self):
        """Subagent channel carries name/task_id/scheduled."""
        ch = Channel.subagent("w1", task_id="t9", scheduled=True)
        assert ch.data == {"name": "w1", "task_id": "t9", "scheduled": True}

    def test_display_prefixes(self):
        """Prefixes match the settled display labels."""
        assert Channel.human().display_prefix() == "You> "
        assert Channel.wechat().display_prefix() == "Wechat> "
        assert Channel.heartbeat().display_prefix() == "Heartbeat> "
        assert Channel.a2a("Jack").display_prefix() == "A2A(Jack)"
        assert Channel.subagent("w1").display_prefix() == "Subagent(w1)> "
        # No name → fallback label, still renders.
        assert Channel.subagent("").display_prefix() == "Subagent(subagent)> "
        # System is filtered from the chat view.
        assert Channel.system().display_prefix() is None

    def test_to_db_a2a_keeps_peer_as_identity(self):
        """A2A identity stays the peer name (FTS-searchable), name in data."""
        assert Channel.a2a("Jack").to_db() == ("Jack", {"agent_name": "Jack"})

    def test_to_db_builtins(self):
        """Built-in kinds persist as kind + payload."""
        assert Channel.human().to_db() == ("human", {})
        ch = Channel.subagent("w1", task_id="t9", scheduled=False)
        assert ch.to_db() == ("subagent", {"name": "w1", "task_id": "t9", "scheduled": False})

    def test_from_db_round_trip(self):
        """to_db → from_db round-trips every kind unchanged."""
        for ch in (Channel.human(), Channel.wechat(),
                   Channel.heartbeat(), Channel.subagent("w1"),
                   Channel.a2a("Jack"), Channel.system()):
            identity, data = ch.to_db()
            restored = Channel.from_db(identity, data)
            assert restored == ch
            assert restored.data == ch.data

    def test_from_db_peer_name_identity_is_a2a(self):
        """Peer-name identity (the a2a to_db format) → a2a peer."""
        ch = Channel.from_db("Jack", "{}")
        assert ch.kind == "a2a"
        assert ch.data == {"agent_name": "Jack"}
        assert ch.display_prefix() == "A2A(Jack)"

    def test_from_db_unknown_identity_is_a2a_peer(self):
        """Any unknown identity string classifies as an A2A peer."""
        ch = Channel.from_db("schedule", "{}")
        assert ch.kind == "a2a"
        assert ch.data == {"agent_name": "schedule"}

    def test_from_db_empty_identity_is_human(self):
        """Empty channel string → human."""
        assert Channel.from_db("", "{}") == Channel.human()

    def test_from_db_bad_payload_json(self):
        """Bad payload JSON degrades to {} — never raises."""
        ch = Channel.from_db("subagent", "not-json")
        assert ch.kind == "subagent"
        assert ch.data == {}

    def test_from_db_merges_payload_identity(self):
        """An explicit payload merges identity + data."""
        ch = Channel.from_db("Jack", '{"agent_name": "Jack", "extra": 1}')
        assert ch.data == {"agent_name": "Jack", "extra": 1}
