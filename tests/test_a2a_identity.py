"""Tests for slife.a2a.identity — AgentName, AgentMessage, HUMAN, WECHAT."""

import pytest; pytestmark = pytest.mark.unit


import pytest

from slife.a2a.identity import AgentName, AgentMessage, HUMAN, WECHAT


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
