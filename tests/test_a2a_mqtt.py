"""Tests for slife.a2a.mqtt — MQTTAdapter, MQTTMessage."""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import json
from unittest.mock import AsyncMock, Mock, Mock, patch, PropertyMock

import pytest

from slife.a2a.mqtt import MQTTAdapter, MQTTMessage


# ── Helpers ──────────────────────────────────────────────────────────────

def _make_mock_mqtt():
    """Return a Mock standing in for the paho.mqtt.client module."""
    return Mock()


# ── MQTTMessage ──────────────────────────────────────────────────────────


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
            topic="Slife/agent/presence",
            payload='{"status":"online"}',
            qos=1,
            retain=True,
        )
        assert msg.qos == 1
        assert msg.retain is True


# ── MQTTAdapter ──────────────────────────────────────────────────────────


class TestMQTTAdapterInit:
    """Tests for MQTTAdapter initialization."""

    def test_initial_state(self):
        import os
        adapter = MQTTAdapter("test-client")
        assert adapter._agent_name == "test-client"
        assert adapter._client_id == f"test-client-{os.getpid()}"
        assert adapter.is_connected is False
        assert adapter._client is None


class TestMQTTAdapterProperties:
    """Tests for MQTTAdapter properties."""

    def test_is_connected(self):
        adapter = MQTTAdapter("test")
        assert not adapter.is_connected
        adapter._connected = True
        assert adapter.is_connected


# ── MQTTAdapter connect ──────────────────────────────────────────────────


class TestMQTTAdapterConnect:
    """Tests for connect."""

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt")
    async def test_connect_success(self, mock_mqtt):
        mock_client = Mock()
        mock_mqtt.Client.return_value = mock_client

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
        mock_mqtt.Client.assert_called_once()
        mock_client.connect_async.assert_called_once_with("localhost", 1883, keepalive=30)
        mock_client.loop_start.assert_called_once()

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt")
    async def test_connect_already_connected(self, mock_mqtt):
        mock_client = Mock()
        mock_mqtt.Client.return_value = mock_client

        adapter = MQTTAdapter("agent-01")
        adapter._connected = True

        await adapter.connect()
        mock_mqtt.Client.assert_not_called()

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt")
    async def test_connect_sets_will(self, mock_mqtt):
        mock_client = Mock()
        mock_mqtt.Client.return_value = mock_client

        adapter = MQTTAdapter("agent-01")

        task = asyncio.create_task(adapter.connect("localhost", 1883))
        await asyncio.sleep(0)
        adapter._connect_event.set()
        await task

        mock_client.will_set.assert_called_once_with(
            "Slife/agent-01/presence",
            # agent_name lets peers' watchdog identify who went offline.
            json.dumps({"status": "offline", "agent_name": "agent-01"}),
            qos=1,
            retain=False,
        )


# ── MQTTAdapter disconnect ───────────────────────────────────────────────


class TestMQTTAdapterDisconnect:
    """Tests for disconnect."""

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt")
    async def test_disconnect_success(self, mock_mqtt):
        mock_client = Mock()
        mock_mqtt.Client.return_value = mock_client

        adapter = MQTTAdapter("agent-01")
        adapter._connected = True
        adapter._client = mock_client

        await adapter.disconnect()

        assert not adapter.is_connected
        # Publishes an offline presence carrying agent_name so the peer
        # watchdog can identify who left.
        offline_payload = json.loads(mock_client.publish.call_args.args[1])
        assert offline_payload == {"status": "offline", "agent_name": "agent-01"}
        mock_client.loop_stop.assert_called_once()
        mock_client.disconnect.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_not_connected_noop(self):
        adapter = MQTTAdapter("agent-01")
        # Not connected — should not raise
        await adapter.disconnect()


# ── MQTTAdapter publish ──────────────────────────────────────────────────


class TestMQTTAdapterPublish:
    """Tests for publish."""

    @pytest.mark.asyncio
    async def test_publish_not_connected_raises(self):
        adapter = MQTTAdapter("agent-01")
        with pytest.raises(RuntimeError, match="not connected"):
            await adapter.publish("topic", "payload")

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt")
    async def test_publish_success(self, mock_mqtt):
        mock_client = Mock()
        mock_info = Mock()
        mock_info.rc = 0  # MQTT_RC_SUCCESS
        mock_client.publish.return_value = mock_info

        adapter = MQTTAdapter("agent-01")
        adapter._client = mock_client

        await adapter.publish("test/topic", "hello", qos=1, retain=False)
        mock_client.publish.assert_called_once_with("test/topic", "hello", qos=1, retain=False)


# ── MQTTAdapter subscribe ────────────────────────────────────────────────


class TestMQTTAdapterSubscribe:
    """Tests for subscribe."""

    @pytest.mark.asyncio
    @patch("slife.a2a.mqtt.mqtt")
    async def test_subscribe_success(self, mock_mqtt):
        mock_client = Mock()
        adapter = MQTTAdapter("agent-01")
        adapter._client = mock_client

        await adapter.subscribe("Slife/+/presence", qos=1)

        mock_client.subscribe.assert_called_once_with("Slife/+/presence", qos=1)
        assert "Slife/+/presence" in adapter._queues

    @pytest.mark.asyncio
    async def test_subscribe_not_connected_raises(self):
        adapter = MQTTAdapter("agent-01")
        with pytest.raises(RuntimeError, match="not connected"):
            await adapter.subscribe("topic")


# ── MQTTAdapter message routing ──────────────────────────────────────────


class TestMQTTAdapterMessageRouting:
    """Tests for _on_message callback and topic routing."""

    @patch("slife.a2a.mqtt.mqtt")
    def test_on_message_routes_to_matching_queue(self, mock_mqtt):
        # topic_matches_sub is called on the mock module
        mock_mqtt.topic_matches_sub.return_value = True

        adapter = MQTTAdapter("agent-01")
        adapter._connected = True
        adapter._queues["Slife/+/presence"] = asyncio.Queue(maxsize=10)

        mock_msg = Mock()
        mock_msg.topic = "Slife/agent-02/presence"
        mock_msg.payload = b'{"status":"online"}'
        mock_msg.qos = 1
        mock_msg.retain = False

        adapter._on_message(None, None, mock_msg)

        # Queue should receive the message
        queue = adapter._queues["Slife/+/presence"]
        assert not queue.empty()

    @patch("slife.a2a.mqtt.mqtt")
    def test_on_message_no_match(self, mock_mqtt):
        mock_mqtt.topic_matches_sub.return_value = False

        adapter = MQTTAdapter("agent-01")
        adapter._connected = True

        mock_msg = Mock()
        mock_msg.topic = "other/topic"
        mock_msg.payload = b"data"
        mock_msg.qos = 0
        mock_msg.retain = False

        # Should not raise — just log
        adapter._on_message(None, None, mock_msg)

    @patch("slife.a2a.mqtt.mqtt")
    def test_on_message_queue_full(self, mock_mqtt):
        mock_mqtt.topic_matches_sub.return_value = True

        adapter = MQTTAdapter("agent-01")
        adapter._connected = True

        # Queue with size 1 already full
        full_queue = asyncio.Queue(maxsize=1)
        full_queue.put_nowait(Mock())
        adapter._queues["Slife/+/presence"] = full_queue

        mock_msg = Mock()
        mock_msg.topic = "Slife/x/presence"
        mock_msg.payload = b"overflow"
        mock_msg.qos = 1
        mock_msg.retain = False

        # Should not raise — just log warning
        adapter._on_message(None, None, mock_msg)


# ── MQTTAdapter callback stubs ───────────────────────────────────────────


class TestMQTTAdapterCallbacks:
    """Tests for paho callback handlers."""

    def test_on_connect(self):
        adapter = MQTTAdapter("test")
        adapter._connect_event = asyncio.Event()

        adapter._on_connect(None, None, None, None, None)
        assert adapter._connect_event.is_set()

    def test_on_disconnect(self):
        adapter = MQTTAdapter("test")
        adapter._connected = True

        adapter._on_disconnect(None, None, None, None, None)
        assert not adapter.is_connected


# ── MQTTAdapter reconnect (REVIEW H6) ────────────────────────────────────


class TestMQTTAdapterReconnect:
    """A broker disconnect must not kill the A2A mesh.

    On paho's auto-reconnect, _on_connect must restore _connected,
    re-issue every subscription (clean_start drops them), and let the app
    re-announce presence via the on_reconnect callback.
    """

    def test_on_connect_restores_connected_and_resubscribes(self):
        adapter = MQTTAdapter("test")
        adapter._connect_event = asyncio.Event()
        adapter._ever_connected = True  # this is a reconnect
        adapter._subscriptions = {
            "Slife/+/presence": 1,
            "Slife/test/tasks/inbox": 1,
        }
        mock_client = Mock()

        adapter._on_connect(mock_client, None, None, None, None)

        assert adapter.is_connected
        assert mock_client.subscribe.call_count == 2
        mock_client.subscribe.assert_any_call("Slife/+/presence", qos=1)
        mock_client.subscribe.assert_any_call("Slife/test/tasks/inbox", qos=1)

    def test_on_connect_first_connect_does_not_resubscribe(self):
        adapter = MQTTAdapter("test")
        adapter._connect_event = asyncio.Event()
        adapter._subscriptions = {"Slife/+/presence": 1}
        mock_client = Mock()

        adapter._on_connect(mock_client, None, None, None, None)

        assert adapter.is_connected
        assert adapter._ever_connected
        mock_client.subscribe.assert_not_called()  # first connect

    @pytest.mark.asyncio
    async def test_messages_survives_transient_disconnect(self):
        """messages() keeps delivering across a disconnect/reconnect cycle."""
        adapter = MQTTAdapter("test")
        adapter._queues["t"] = asyncio.Queue()

        it = adapter.messages("t").__aiter__()
        adapter._queues["t"].put_nowait(MQTTMessage(topic="t", payload="a"))
        assert (await asyncio.wait_for(it.__anext__(), timeout=1)).payload == "a"

        # Disconnect + reconnect (as paho would do automatically).
        adapter._on_disconnect(None, None, None, None, None)
        assert not adapter.is_connected
        adapter._on_connect(Mock(), None, None, None, None)
        assert adapter.is_connected

        # The generator never exited — it still delivers.
        adapter._queues["t"].put_nowait(MQTTMessage(topic="t", payload="b"))
        assert (await asyncio.wait_for(it.__anext__(), timeout=1)).payload == "b"

        # Deliberate shutdown ends the generator.
        adapter._closed = True
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(it.__anext__(), timeout=1)

    @pytest.mark.asyncio
    async def test_on_reconnect_fires_callback(self):
        """The app's presence re-announce runs on a reconnect."""
        adapter = MQTTAdapter("test")
        adapter._connect_event = asyncio.Event()
        adapter._ever_connected = True
        adapter._loop = asyncio.get_running_loop()

        fired = asyncio.Event()
        async def cb():
            fired.set()
        adapter.on_reconnect = cb

        adapter._on_connect(Mock(), None, None, None, None)

        await asyncio.wait_for(fired.wait(), timeout=1)
        assert fired.is_set()


class TestMQTTAdapterConcurrency:
    """Regression: subscribe() mutating _queues while paho's callback thread
    iterates it used to raise RuntimeError (dict changed size during
    iteration), which paho re-raises and kills its network thread — silent,
    permanent message loss with no reconnect."""

    @pytest.mark.asyncio
    async def test_on_message_survives_concurrent_subscribe(self):
        import threading as _threading
        import time as _time

        adapter = MQTTAdapter("race-test")
        adapter._client = Mock()
        adapter._connected = True

        # Pre-populate so each _on_message call iterates several topics; a
        # slow matcher widens the iteration window so a concurrent subscribe()
        # mutation (adding NEW queues) lands mid-iteration and would crash the
        # racy version with "dictionary changed size during iteration".
        for i in range(8):
            adapter._queues[f"Slife/race-test/base/{i}"] = asyncio.Queue()

        def _slow_match(sub: str, topic: str) -> bool:
            _time.sleep(0.0005)
            return topic.startswith("Slife/race-test/")

        errors: list[Exception] = []
        with patch("slife.a2a.mqtt.mqtt") as mock_mqtt:
            mock_mqtt.topic_matches_sub.side_effect = _slow_match

            def _deliver() -> None:
                msg = Mock()
                msg.topic = "Slife/race-test/presence"
                msg.payload = b'{"status":"online"}'
                for _ in range(30):
                    try:
                        adapter._on_message(None, None, msg)
                    except Exception as e:  # pragma: no cover
                        errors.append(e)
                        break

            thread = _threading.Thread(target=_deliver)
            thread.start()
            # Pace the subscribes so the dict grows DURING the deliver
            # thread's slow iterations (not all up-front, which would leave
            # nothing to mutate mid-iteration).
            for i in range(30):
                await adapter.subscribe(f"Slife/race-test/tasks/t{i}")
                await asyncio.sleep(0.0005)
            thread.join()

        assert not errors
