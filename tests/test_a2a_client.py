"""Tests for A2AClient presence watchdog / offline detection.

Covers the fix where a peer that vanishes silently (its offline presence
message is lost or lacks ``agent_name``) is still pruned: the watchdog must
run the stale-peer prune on every presence sighting — including our own
heartbeat echo — not only on messages from identified peers.
"""

import asyncio
import json
import time as _time

import pytest; pytestmark = pytest.mark.unit

from slife.a2a.client import A2AClient
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentName
from slife.a2a.card import AgentCard
from slife.a2a.transport import TransportMessage


class _RecordingAdapter:
    """Adapter stub that records published (topic, payload) pairs."""

    def __init__(self):
        self.is_connected = True
        self.published: list[tuple[str, str]] = []

    async def publish(self, topic, payload, qos=1, retain=False):
        self.published.append((topic, payload))

    async def connect(self, *a, **k):  # pragma: no cover
        pass

    async def disconnect(self):  # pragma: no cover
        pass

    async def subscribe(self, *a, **k):  # pragma: no cover
        pass

    def messages(self, topic_filter):  # pragma: no cover
        async def gen():
            yield TransportMessage(topic=topic_filter, payload="{}")
            await asyncio.Event().wait()
        return gen()


class TestSendTaskWire:
    def _client(self):
        cfg = A2AConfig(enabled=True, agent_name="jack")
        return A2AClient(cfg, transport=_RecordingAdapter())

    @pytest.mark.asyncio
    async def test_send_task_publishes_sendmessage_envelope(self):
        """The outbound task is the official SendMessage JSON-RPC request."""
        client = self._client()
        adapter = client._adapter

        async def _publish(topic, payload, qos=1, retain=False):
            adapter.published.append((topic, payload))
            # Resolve the pending future for the corr_id carried in the payload.
            cid = json.loads(payload).get("id")
            fut = client._pending_tasks.get(cid)
            if fut and not fut.done():
                fut.set_result("the result")

        adapter.publish = _publish

        result = await client.send_task(AgentName("peer-1"), "do X", timeout=5)
        assert result == "the result"

        topic, payload = adapter.published[0]
        assert topic == "Slife/peer-1/tasks/inbox"
        env = json.loads(payload)
        assert env["method"] == "SendMessage"
        assert env["_slife"]["source"] == "jack"
        assert env["_slife"]["reply_to"] == "Slife/jack/tasks/result"
        assert env["params"]["message"]["content"][0]["text"] == "do X"
        assert env["params"]["message"]["role"] == "user"

    @pytest.mark.asyncio
    async def test_send_task_async_publishes_sendmessage_envelope(self):
        client = self._client()
        adapter = client._adapter

        corr_id = await client.send_task_async(AgentName("peer-1"), "do X")
        assert corr_id
        topic, payload = adapter.published[0]
        assert topic == "Slife/peer-1/tasks/inbox"
        env = json.loads(payload)
        assert env["id"] == corr_id
        assert env["params"]["message"]["content"][0]["text"] == "do X"


class TestCancelTask:
    """REVIEW C5 — cancel_task returns a status string and never mislabels
    a completed result as cancelled or discards it."""

    def _client(self):
        cfg = A2AConfig(enabled=True, agent_name="jack")
        return A2AClient(cfg, transport=_RecordingAdapter())

    def setup_method(self):
        from slife.a2a.task_store import clear_store
        clear_store()

    @pytest.mark.asyncio
    async def test_cancel_completed_async_result_not_consumed(self):
        from slife.a2a.task_store import get_store

        client = self._client()
        get_store().record_send("cid-1", "peer-1", "do X", "mqtt")
        get_store().record_result("cid-1", "the answer")
        client._completed_tasks["cid-1"] = "the answer"

        status = await client.cancel_task(AgentName("peer-1"), "cid-1")

        assert status == "completed"
        # Result stays retrievable — not consumed, not marked cancelled.
        assert client.get_task_result("cid-1") == "the answer"
        assert get_store().get("cid-1").status == "completed"

    @pytest.mark.asyncio
    async def test_cancel_pending_sync_waiter(self):
        from slife.a2a.task_store import get_store

        client = self._client()
        get_store().record_send("cid-1", "peer-1", "do X", "mqtt")
        fut = asyncio.get_event_loop().create_future()
        client._pending_tasks["cid-1"] = fut

        status = await client.cancel_task(AgentName("peer-1"), "cid-1")

        assert status == "cancelled"
        assert fut.cancelled()
        assert get_store().get("cid-1").status == "cancelled"
        # The official CancelTask request went to the target's inbox.
        topic, payload = client._adapter.published[0]
        assert topic == "Slife/peer-1/tasks/inbox"
        assert json.loads(payload)["method"] == "CancelTask"

    @pytest.mark.asyncio
    async def test_cancel_failed_task_is_terminal(self):
        from slife.a2a.task_store import get_store

        client = self._client()
        get_store().record_send("cid-1", "peer-1", "do X", "mqtt")
        get_store().record_error("cid-1", "timeout")

        status = await client.cancel_task(AgentName("peer-1"), "cid-1")

        assert status == "failed"
        assert get_store().get("cid-1").status == "failed"

    @pytest.mark.asyncio
    async def test_cancel_unknown_task_still_dispatches_notice(self):
        client = self._client()

        status = await client.cancel_task(AgentName("peer-1"), "cid-unknown")

        assert status == "not_found"
        assert len(client._adapter.published) == 1  # best-effort notice sent


class TestIncomingCancel:
    """REVIEW C5 — inbound CancelTask routing and cancelled-result recording."""

    def _client(self):
        cfg = A2AConfig(enabled=True, agent_name="jack")
        return A2AClient(cfg, transport=_RecordingAdapter())

    def setup_method(self):
        from slife.a2a.task_store import clear_store
        clear_store()

    @pytest.mark.asyncio
    async def test_canceltask_invokes_callback(self):
        from slife.a2a import wire

        client = self._client()
        got: list[str] = []

        async def _cb(corr_id: str):
            got.append(corr_id)

        client.on_incoming_cancel(_cb)

        payload = json.dumps(wire.cancel_task_envelope("cid-1", "peer-1"))
        await client._handle_incoming_task(
            TransportMessage(topic="Slife/jack/tasks/inbox", payload=payload),
        )

        assert got == ["cid-1"]

    @pytest.mark.asyncio
    async def test_cancelled_result_recorded_as_cancelled(self):
        from slife.a2a import wire
        from slife.a2a.task_store import get_store

        client = self._client()
        get_store().record_send("cid-1", "peer-1", "do X", "mqtt")

        task = wire.Task.cancelled("cid-1", "stopped by peer")
        payload = json.dumps(wire.task_result_envelope("cid-1", task))
        await client._handle_result(
            TransportMessage(topic="Slife/jack/tasks/result", payload=payload),
        )

        assert get_store().get("cid-1").status == "cancelled"
        assert client.get_task_result("cid-1") == "stopped by peer"

    @pytest.mark.asyncio
    async def test_async_result_fires_on_task_result(self):
        """An outbound async result fires on_task_result (auto-push)."""
        from slife.a2a import wire

        client = self._client()
        got: list[tuple[str, str, bool]] = []

        async def _cb(corr_id, result, cancelled):
            got.append((corr_id, result, cancelled))

        client.on_task_result(_cb)

        task = wire.Task.completed("cid-9", "the answer")
        payload = json.dumps(wire.task_result_envelope("cid-9", task))
        await client._handle_result(
            TransportMessage(topic="Slife/jack/tasks/result", payload=payload),
        )

        assert got == [("cid-9", "the answer", False)]
        assert client.get_task_result("cid-9") == "the answer"


class _EchoThenBlockAdapter:
    """Adapter that yields our own presence echo once, then blocks."""

    def __init__(self, agent_name: str):
        self._agent_name = agent_name
        self.is_connected = True

    def messages(self, topic_filter):
        async def gen():
            yield TransportMessage(
                topic=f"Slife/{self._agent_name}/presence",
                payload=json.dumps(
                    {"agent_name": self._agent_name, "status": "online"},
                ),
            )
            await asyncio.Event().wait()  # block after the echo
        return gen()


class TestPresenceWatchdog:
    def _client(self):
        cfg = A2AConfig(
            enabled=True, agent_name="jack",
            heartbeat_interval=1, heartbeat_timeout=1,
        )
        return A2AClient(cfg)

    @pytest.mark.asyncio
    async def test_prunes_stale_peer_on_own_presence_echo(self):
        """Prune runs even when only our own presence echo arrives, so a
        peer that vanished silently is still marked offline."""
        client = self._client()
        client._peers[AgentName("slife")] = (
            AgentCard(agent_name=AgentName("slife"), status="idle"),
            _time.monotonic() - 10,  # last heard long ago (> heartbeat_timeout)
        )

        events: list[tuple[str, str]] = []
        client.on_agent_change(lambda card, ev: events.append((str(card.agent_name), ev)))

        client._adapter = _EchoThenBlockAdapter("jack")

        task = asyncio.create_task(client._peer_watchdog_loop())
        try:
            for _ in range(100):
                if "slife" not in client._peers:
                    break
                await asyncio.sleep(0.02)
            assert "slife" not in client._peers
            assert ("slife", "timeout") in events
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    @pytest.mark.asyncio
    async def test_offline_message_with_agent_name_removes_peer(self):
        """A presence message with status=offline + agent_name removes the peer
        and fires the offline notification."""
        client = self._client()
        client._peers[AgentName("slife")] = (
            AgentCard(agent_name=AgentName("slife"), status="idle"),
            _time.monotonic(),
        )

        events: list[tuple[str, str]] = []
        client.on_agent_change(lambda card, ev: events.append((str(card.agent_name), ev)))

        class _OfflineAdapter:
            def __init__(self):
                self.is_connected = True
            def messages(self, topic_filter):
                async def gen():
                    yield TransportMessage(
                        topic="Slife/slife/presence",
                        payload=json.dumps(
                            {"status": "offline", "agent_name": "slife"},
                        ),
                    )
                    await asyncio.Event().wait()
                return gen()

        client._adapter = _OfflineAdapter()

        task = asyncio.create_task(client._peer_watchdog_loop())
        try:
            for _ in range(100):
                if "slife" not in client._peers:
                    break
                await asyncio.sleep(0.02)
            assert "slife" not in client._peers
            assert ("slife", "offline") in events
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
