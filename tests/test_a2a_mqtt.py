"""Tests for slife.a2a.mqtt — MQTTAdapter, MQTTMessage."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from slife.a2a.mqtt import MQTTAdapter, MQTTMessage


# ── MQTTMessage ─────────────────────────────────────────────────────────────


class TestMQTTMessage:
    """Tests for MQTTMessage dataclass."""

    def test_default_values(self):
        msg = MQTTMessage(topic="test/topic", payload="hello", qos=0, retain=False)
        assert msg.topic == "test/topic"
        assert msg.payload == "hello"
        assert msg.qos == 0
        assert msg.retain is False

    def test_custom_values(self):
        msg = MQTTMessage(
            topic="slife/agent/presence",
            payload='{"status":"online"}',
            qos=1,
            retain=True,
        )
        assert msg.qos == 1
        assert msg.retain is True


# ── MQTTAdapter ─────────────────────────────────────────────────────────────


class TestMQTTAdapterInit:
    """Tests for MQTTAdapter initialization."""

    def test_initial_state(self):
        adapter = MQTTAdapter("test-client")
        assert adapter._client_id == "test-client"
        assert adapter.is_connected is False
        assert adapter._client is None


class TestMQTTAdapterProperties:
    """Tests for MQTTAdapter properties."""

    def test_is_connected(self):
        adapter = MQTTAdapter("test")
        assert not adapter.is_connected
        adapter._connected = True
        assert adapter.is_connected


# ── MQTTAdapter connect ─────────────────────────────────────────────────────


class TestMQTTAdapterConnect:
    """Tests for connect."""

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt.Client")
    async def test_connect_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        adapter = MQTTAdapter("agent-01")

        # Start connect task in background, then immediately trigger the event
        async def connect_and_signal():
            task = asyncio.create_task(adapter.connect("localhost", 1883))
            await asyncio.sleep(0)
            # Simulate the _on_connect callback being called by paho
            adapter._connect_event.set()
            await task

        await connect_and_signal()

        assert adapter.is_connected
        mock_client_cls.assert_called_once()
        mock_client.connect_async.assert_called_once_with("localhost", 1883, keepalive=30)
        mock_client.loop_start.assert_called_once()

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt.Client")
    async def test_connect_already_connected(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        adapter = MQTTAdapter("agent-01")
        adapter._connected = True

        await adapter.connect()
        mock_client_cls.assert_not_called()

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt.Client")
    async def test_connect_sets_will(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        adapter = MQTTAdapter("agent-01")

        task = asyncio.create_task(adapter.connect("localhost", 1883))
        await asyncio.sleep(0)
        adapter._connect_event.set()
        await task

        mock_client.will_set.assert_called_once_with(
            "slife/agent-01/presence",
            json.dumps({"status": "offline"}),
            qos=1,
            retain=False,
        )


# ── MQTTAdapter disconnect ──────────────────────────────────────────────────


class TestMQTTAdapterDisconnect:
    """Tests for disconnect."""

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt.Client")
    async def test_disconnect_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        adapter = MQTTAdapter("agent-01")
        adapter._connected = True
        adapter._client = mock_client

        await adapter.disconnect()

        assert not adapter.is_connected
        mock_client.publish.assert_called()  # publishes offline
        mock_client.loop_stop.assert_called_once()
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_not_connected_noop(self):
        adapter = MQTTAdapter("agent-01")
        # Not connected — should not raise
        await adapter.disconnect()


# ── MQTTAdapter publish ─────────────────────────────────────────────────────


class TestMQTTAdapterPublish:
    """Tests for publish."""

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt.Client")
    async def test_publish_not_connected_raises(self, mock_client_cls):
        adapter = MQTTAdapter("agent-01")
        with pytest.raises(RuntimeError, match="not connected"):
            await adapter.publish("topic", "payload")

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt.Client")
    async def test_publish_success(self, mock_client_cls):
        mock_client = MagicMock()
        mock_info = MagicMock()
        mock_info.rc = 0  # MQTT_RC_SUCCESS
        mock_client.publish.return_value = mock_info

        adapter = MQTTAdapter("agent-01")
        adapter._client = mock_client

        await adapter.publish("test/topic", "hello", qos=1, retain=False)
        mock_client.publish.assert_called_once_with("test/topic", "hello", qos=1, retain=False)


# ── MQTTAdapter subscribe ───────────────────────────────────────────────────


class TestMQTTAdapterSubscribe:
    """Tests for subscribe."""

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt.Client")
    async def test_subscribe_success(self, mock_client_cls):
        mock_client = MagicMock()
        adapter = MQTTAdapter("agent-01")
        adapter._client = mock_client

        await adapter.subscribe("slife/+/presence", qos=1)

        mock_client.subscribe.assert_called_once_with("slife/+/presence", qos=1)
        assert "slife/+/presence" in adapter._queues

    @pytest.mark.asyncio
    async def test_subscribe_not_connected_raises(self):
        adapter = MQTTAdapter("agent-01")
        with pytest.raises(RuntimeError, match="not connected"):
            await adapter.subscribe("topic")


# ── MQTTAdapter message routing ──────────────────────────────────────────────


class TestMQTTAdapterMessageRouting:
    """Tests for _on_message callback and topic routing."""

    @patch("slife.a2a.mqtt.mqtt.Client")
    def test_on_message_routes_to_matching_queue(self, mock_client_cls):
        adapter = MQTTAdapter("agent-01")
        adapter._connected = True
        adapter._queues["slife/+/presence"] = asyncio.Queue(maxsize=10)

        mock_msg = MagicMock()
        mock_msg.topic = "slife/agent-02/presence"
        mock_msg.payload = b'{"status":"online"}'
        mock_msg.qos = 1
        mock_msg.retain = False

        adapter._on_message(None, None, mock_msg)

        # Queue should receive the message
        queue = adapter._queues["slife/+/presence"]
        assert not queue.empty()

    @patch("slife.a2a.mqtt.mqtt.Client")
    def test_on_message_no_match(self, mock_client_cls):
        adapter = MQTTAdapter("agent-01")
        adapter._connected = True

        mock_msg = MagicMock()
        mock_msg.topic = "other/topic"
        mock_msg.payload = b"data"
        mock_msg.qos = 0
        mock_msg.retain = False

        # Should not raise — just log
        adapter._on_message(None, None, mock_msg)

    @patch("slife.a2a.mqtt.mqtt.Client")
    def test_on_message_queue_full(self, mock_client_cls):
        adapter = MQTTAdapter("agent-01")
        adapter._connected = True

        # Queue with size 1 already full
        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait(MagicMock())
        adapter._queues["slife/+/presence"] = full_queue

        mock_msg = MagicMock()
        mock_msg.topic = "slife/x/presence"
        mock_msg.payload = b"overflow"
        mock_msg.qos = 1
        mock_msg.retain = False

        # Should not raise — just log warning
        adapter._on_message(None, None, mock_msg)


# ── MQTTAdapter callback stubs ──────────────────────────────────────────────


class TestMQTTAdapterCallbacks:
    """Tests for paho callback handlers."""

    @patch("slife.a2a.mqtt.mqtt.Client")
    def test_on_connect(self, mock_client_cls):
        adapter = MQTTAdapter("test")
        adapter._connect_event = asyncio.Event()

        adapter._on_connect(None, None, None, None, None)
        assert adapter._connect_event.is_set()

    @patch("slife.a2a.mqtt.mqtt.Client")
    def test_on_disconnect(self, mock_client_cls):
        adapter = MQTTAdapter("test")
        adapter._connected = True

        adapter._on_disconnect(None, None, None, None, None)
        assert not adapter.is_connected
