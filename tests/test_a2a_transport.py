"""Tests for slife.a2a.transport — TransportMessage and TransportAdapter ABC."""

import pytest; pytestmark = pytest.mark.unit


import pytest

from slife.a2a.transport import TransportMessage, TransportAdapter


# ── TransportMessage ──────────────────────────────────────────────────────


class TestTransportMessage:
    """Tests for the TransportMessage dataclass."""

    def test_creation(self):
        """TransportMessage has topic and payload."""
        msg = TransportMessage(topic="Slife/agent-1/presence", payload='{"status":"online"}')
        assert msg.topic == "Slife/agent-1/presence"
        assert msg.payload == '{"status":"online"}'

    def test_empty_payload(self):
        """Payload can be empty string."""
        msg = TransportMessage(topic="test/empty", payload="")
        assert msg.payload == ""

    def test_equality(self):
        """Same topic and payload → equal."""
        m1 = TransportMessage(topic="a", payload="b")
        m2 = TransportMessage(topic="a", payload="b")
        assert m1 == m2

    def test_inequality_different_topic(self):
        """Different topic → not equal."""
        m1 = TransportMessage(topic="a", payload="x")
        m2 = TransportMessage(topic="b", payload="x")
        assert m1 != m2

    def test_inequality_different_payload(self):
        """Different payload → not equal."""
        m1 = TransportMessage(topic="a", payload="x")
        m2 = TransportMessage(topic="a", payload="y")
        assert m1 != m2

    def test_topic_with_mqtt_wildcards(self):
        """Topics can contain MQTT wildcards."""
        msg = TransportMessage(topic="Slife/+/presence", payload="wild")
        assert "+" in msg.topic

    def test_multiline_payload(self):
        """Payload can be multi-line."""
        payload = "line1\nline2\nline3"
        msg = TransportMessage(topic="test", payload=payload)
        assert "\n" in msg.payload
        assert msg.payload.count("\n") == 2


# ── TransportAdapter ABC ──────────────────────────────────────────────────


class TestTransportAdapterABC:
    """Tests for the TransportAdapter abstract base class."""

    def test_cannot_instantiate_abstract(self):
        """TransportAdapter cannot be instantiated directly."""
        with pytest.raises(TypeError):
            TransportAdapter()  # type: ignore[abstract]

    def test_concrete_subclass_must_implement_abstracts(self):
        """A subclass missing abstract methods cannot be instantiated."""
        class _BadAdapter(TransportAdapter):
            pass  # no abstract methods implemented

        with pytest.raises(TypeError) as exc_info:
            _BadAdapter()  # type: ignore[abstract]
        assert "abstract" in str(exc_info.value).lower()

    def test_concrete_subclass_instantiable(self):
        """A subclass implementing all abstract methods is instantiable."""
        class _GoodAdapter(TransportAdapter):
            async def connect(self, _host: str, _port: int) -> None:  # noqa: ARG002
                pass
            async def disconnect(self) -> None:
                pass
            async def publish(self, _topic: str, _payload: str, _qos: int = 1, _retain: bool = False) -> None:  # noqa: ARG002
                pass
            async def subscribe(self, _topic: str, _qos: int = 1) -> None:  # noqa: ARG002
                pass
            def messages(self, _topic_filter: str):  # noqa: ARG002
                async def _gen():
                    return
                    yield  # type: ignore[unreachable]
                return _gen()

            @property
            def is_connected(self) -> bool:
                return True

        adapter = _GoodAdapter()
        assert adapter.is_connected is True

    def test_abstract_methods_defined(self):
        """All expected abstract methods are defined on the ABC."""
        abstract_methods = {"connect", "disconnect", "publish", "subscribe", "messages"}
        for name in abstract_methods:
            assert hasattr(TransportAdapter, name), f"Missing abstract method: {name}"

    def test_is_connected_property_defined(self):
        """is_connected is an abstract property."""
        assert hasattr(TransportAdapter, "is_connected")
