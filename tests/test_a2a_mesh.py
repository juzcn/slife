"""Tests for slife.a2a.mesh — the A2A mesh driver over the official SDK.

The wire is the ``a2a-over-mqtt`` SDK; these tests cover thie mesh's own
glue with aiomqtt/paho fully mocked (no broker needed): outbound sends,
reply routing/classification, presence discovery, the inbound completion
bridge, the requester retry profile, and broadcast events.
"""

import asyncio
import json
import pytest; pytestmark = pytest.mark.unit


from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from a2a_over_mqtt import A2ARequest, make_artifact_event, make_status_event
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.properties import Properties

from slife.a2a.config import A2AConfig
from slife.a2a.mesh import A2AMesh, _OutboundSend, _backoff_delay
from slife.a2a.card import AgentCard
from slife.a2a.identity import AgentName
from slife.a2a.task_store import clear_store, get_store


def _config(agent: str = "self-1") -> A2AConfig:
    return A2AConfig(
        enabled=True, agent_name=agent, broker_host="localhost",
        broker_port=1883, org="default", unit="default",
    )


@pytest.fixture(autouse=True)
def _fresh_store():
    clear_store()
    yield
    clear_store()


def _msg(topic: str, payload: bytes | str, *, corr: str | None = None,
         status: str | None = None) -> SimpleNamespace:
    props = None
    if corr is not None or status is not None:
        props = Properties(PacketTypes.PUBLISH)
        if corr is not None:
            props.CorrelationData = corr.encode()
        if status is not None:
            props.UserProperty = [("a2a-status", status)]
    return SimpleNamespace(
        topic=SimpleNamespace(value=topic),
        payload=payload.encode() if isinstance(payload, str) else payload,
        properties=props,
    )


def _make_mesh():
    return A2AMesh(_config())


def _spawn_inbound(mesh, task_id, sender="peer-1", text="do it", variables=None):
    """Run one inbound request through the responder, returning its task.

    The task stays pending until something resolves it — that is the whole
    point of the completion bridge, and what a restart takes away.
    """
    return asyncio.create_task(
        mesh._responder.on_request(
            A2ARequest(text=text, request_id=task_id, task_id=task_id,
                       sender=sender, variables=variables or {}),
            stream=AsyncMock(),
        ),
    )


class TestSendMessage:
    @pytest.mark.asyncio
    async def test_publishes_standard_sendmessage(self):
        mesh = _make_mesh()
        with patch.object(mesh, "_publish_props", new=AsyncMock()) as pub:
            with patch.object(mesh, "_deliver", new=AsyncMock()):
                task_id = await mesh.send_message("peer-1", "hello")
        topic, payload, props = pub.await_args.args
        assert topic == "$a2a/v1/request/default/default/peer-1"
        env = json.loads(payload)
        assert env["method"] == "SendMessage"
        assert env["id"] == task_id
        msg = env["params"]["message"]
        assert msg["taskId"] == task_id
        assert msg["parts"][0]["text"] == "hello"
        assert env["params"]["metadata"]["sender"] == "self-1"
        # message_type rides metadata.variables so only task_request is a task.
        assert env["params"]["metadata"]["variables"]["message_type"] == "task_request"
        # The reply topic session + correlation travel in MQTT v5 properties.
        assert props.ResponseTopic.startswith("$a2a/v1/reply/default/default/self-1/")
        assert props.CorrelationData == task_id.encode()
        # Recorded pending in the task store.
        rec = get_store().get(task_id)
        assert rec is not None
        assert rec.status == "pending"
        assert rec.agent_name == "peer-1"

    @pytest.mark.asyncio
    async def test_message_type_creates_no_task(self):
        """message_type='message' is NOT a task: no store record, and the
        request is stamped so the peer classifies it as a conversation."""
        mesh = _make_mesh()
        with patch.object(mesh, "_publish_props", new=AsyncMock()) as pub:
            with patch.object(mesh, "_deliver", new=AsyncMock()):
                mid = await mesh.send_message("peer-1", "hi", message_type="message")
        env = json.loads(pub.await_args.args[1])
        assert env["params"]["metadata"]["variables"]["message_type"] == "message"
        assert get_store().get(mid) is None

    @pytest.mark.asyncio
    async def test_returns_task_id_immediately(self):
        mesh = _make_mesh()
        with patch.object(mesh, "_publish_props", new=AsyncMock()):
            with patch.object(mesh, "_deliver", new=AsyncMock()):
                task_id = await mesh.send_message("peer-1", "hi")
        assert isinstance(task_id, str) and len(task_id) == 32


class TestReplyRouting:
    @pytest.mark.asyncio
    async def test_artifact_then_terminal_completes(self):
        mesh = _make_mesh()
        completions = []
        mesh.on_task_completion = lambda c, r, x, p, k="task": completions.append((c, r, x, p, k))
        mesh._corr_to_task["corr1"] = "task-1"
        get_store().record_send("task-1", "peer-1", "hi", "mqtt")
        topic = "$a2a/v1/reply/default/default/self-1/s1"

        mesh._handle_reply(_msg(topic, make_artifact_event("r", "task-1", "the answer"), corr="corr1"))
        assert get_store().get("task-1").status == "pending"  # artifact not terminal

        mesh._handle_reply(_msg(topic, make_status_event("r", "task-1", "completed", "ctx"), corr="corr1"))
        rec = get_store().get("task-1")
        assert rec.status == "completed"
        assert rec.result == "the answer"  # artifact text survived the terminal (which carries none)
        assert completions == [("task-1", "the answer", False, "peer-1", "task")]

    @pytest.mark.asyncio
    async def test_terminal_cancelled(self):
        mesh = _make_mesh()
        completions = []
        mesh.on_task_completion = lambda c, r, x, p, k="task": completions.append((c, r, x, p, k))
        mesh._corr_to_task["corr2"] = "task-2"
        get_store().record_send("task-2", "peer-2", "x", "mqtt")
        mesh._handle_reply(
            _msg(
                "$a2a/v1/reply/default/default/self-1/s2",
                make_status_event("r", "task-2", "canceled", "ctx"),
                corr="corr2",
            ),
        )
        assert get_store().get("task-2").status == "cancelled"
        assert completions[-1][2] is True  # cancelled=True

    @pytest.mark.asyncio
    async def test_message_reply_pushed_as_message_not_task(self):
        """A MESSAGE conversation (record=False) terminal reply pushes as a
        message completion — no task record is ever written."""
        mesh = _make_mesh()
        completions = []
        mesh.on_task_completion = lambda c, r, x, p, k="task": completions.append(
            (c, r, x, p, k),
        )
        mesh._corr_to_task["corr-m"] = "task-m"
        send = _OutboundSend("task-m", "peer-1", "sm", "x", record=False)
        mesh._sends["task-m"] = send
        topic = "$a2a/v1/reply/default/default/self-1/sm"
        mesh._handle_reply(
            _msg(topic, make_artifact_event("r", "task-m", "ok then"), corr="corr-m"),
        )
        mesh._handle_reply(
            _msg(topic, make_status_event("r", "task-m", "completed", ""), corr="corr-m"),
        )
        assert completions == [("task-m", "ok then", False, "peer-1", "message")]
        assert get_store().get("task-m") is None

    @pytest.mark.asyncio
    async def test_unknown_correlation_ignored(self):
        mesh = _make_mesh()
        completions = []
        mesh.on_task_completion = lambda c, r, x, p, k="task": completions.append(c)
        mesh._handle_reply(
            _msg("$a2a/v1/reply/default/default/self-1/s3",
                 make_status_event("r", "nope", "completed", ""), corr="ghost"),
        )
        assert completions == []

    @pytest.mark.asyncio
    async def test_non_terminal_replies_ignored(self):
        mesh = _make_mesh()
        completions = []
        mesh.on_task_completion = lambda c, r, x, p, k="task": completions.append(c)
        mesh._corr_to_task["corr4"] = "task-4"
        get_store().record_send("task-4", "p4", "x", "mqtt")
        mesh._handle_reply(_msg("$a2a/v1/reply/default/default/self-1/s4",
                               make_status_event("r", "task-4", "submitted", ""), corr="corr4"))
        mesh._handle_reply(_msg("$a2a/v1/reply/default/default/self-1/s4",
                               make_status_event("r", "task-4", "working", "thinking"), corr="corr4"))
        assert completions == []
        assert get_store().get("task-4").status == "pending"


class TestReplyRetryRouting:
    """Retried requests reuse Task.id with a fresh correlation — a reply under
    either correlation routes to the same store record."""

    @pytest.mark.asyncio
    async def test_retry_correlation_routes_to_same_task(self):
        mesh = _make_mesh()
        completions = []
        mesh.on_task_completion = lambda c, r, x, p, k="task": completions.append(c)
        mesh._corr_to_task["orig"] = "task-r"
        mesh._corr_to_task["retry-corr"] = "task-r"
        get_store().record_send("task-r", "p", "x", "mqtt")
        mesh._handle_reply(_msg("$a2a/v1/reply/default/default/self-1/sr",
                               make_artifact_event("r", "task-r", "from retry"), corr="retry-corr"))
        mesh._handle_reply(_msg("$a2a/v1/reply/default/default/self-1/sr",
                               make_status_event("r", "task-r", "completed", ""), corr="retry-corr"))
        assert get_store().get("task-r").result == "from retry"
        assert completions == ["task-r"]


class TestPresence:
    def test_online_event_and_no_op_on_repeat(self):
        mesh = _make_mesh()
        events = []
        mesh.on_agent_change = lambda card, e: events.append((card, e))
        topic = "$a2a/v1/discovery/default/default/peer-1"
        mesh._handle_discovery(_msg(topic, b'{"name":"peer-1"}', status="online"))
        assert len(events) == 1
        card, event = events[0]
        assert card.agent_name == AgentName("peer-1")
        assert card.status == "online"
        assert event == "online"
        mesh._handle_discovery(_msg(topic, b'{"name":"peer-1"}', status="online"))
        assert len(events) == 1  # no transition, no event

    def test_offline_and_lwt(self):
        mesh = _make_mesh()
        events = []
        mesh.on_agent_change = lambda card, e: events.append((card, e))
        topic = "$a2a/v1/discovery/default/default/peer-1"
        mesh._handle_discovery(_msg(topic, b"x", status="online"))
        events.clear()
        mesh._handle_discovery(_msg(topic, b"x", status="lwt"))
        assert events[-1][1] == "offline"
        assert events[-1][0].status == "offline"

    def test_own_echo_filtered(self):
        mesh = _make_mesh()
        events = []
        mesh.on_agent_change = lambda card, e: events.append(e)
        mesh._handle_discovery(
            _msg("$a2a/v1/discovery/default/default/self-1", b"x", status="online"),
        )
        assert events == []

    def test_cold_retained_offline_cached_not_announced(self):
        """A stale retained offline card (peer gone before we subscribed) is
        cached but NOT announced — dead cards from past sessions must not fire
        fake ✗ offline lines.  Its first online card IS a transition."""
        mesh = _make_mesh()
        events = []
        mesh.on_agent_change = lambda card, e: events.append((card, e))
        topic = "$a2a/v1/discovery/default/default/ghost"
        mesh._handle_discovery(_msg(topic, b"x", status="offline"))
        assert events == []
        assert "ghost" in {str(c.agent_name) for c in mesh.list_agents()}
        mesh._handle_discovery(_msg(topic, b"x", status="online"))
        assert len(events) == 1
        assert events[0][1] == "online"

    def test_deleted_card_retires_the_peer(self):
        """A deleted retained card arrives as an EMPTY publish with no
        properties, and the absent-property default is "online" — so the
        deletion used to resurrect the peer it retired, leaving a test agent
        online in every running session forever.  Deleting the card must drop
        the peer, not re-announce it."""
        mesh = _make_mesh()
        events = []
        mesh.on_agent_change = lambda card, e: events.append((card, e))
        topic = "$a2a/v1/discovery/default/default/peer-1"
        mesh._handle_discovery(_msg(topic, b'{"name":"peer-1"}', status="online"))
        events.clear()

        mesh._handle_discovery(_msg(topic, b"", status=None))

        assert "peer-1" not in {str(c.agent_name) for c in mesh.list_agents()}
        assert [e[1] for e in events] == ["offline"]   # announced, not silent

    def test_deleting_an_unknown_card_is_silent(self):
        """The cleanup fixture clears cards BEFORE a run too; a delete for a
        peer we never saw must not invent an offline line."""
        mesh = _make_mesh()
        events = []
        mesh.on_agent_change = lambda card, e: events.append((card, e))
        mesh._handle_discovery(
            _msg("$a2a/v1/discovery/default/default/nobody", b"", status=None),
        )
        assert events == []

    def test_list_agents_own_first_then_peers_sorted(self):
        mesh = _make_mesh()
        mesh._handle_discovery(
            _msg("$a2a/v1/discovery/default/default/zed", b"x", status="online"),
        )
        mesh._handle_discovery(
            _msg("$a2a/v1/discovery/default/default/alpha", b"x", status="offline"),
        )
        cards = mesh.list_agents()
        assert [str(c.agent_name) for c in cards] == ["self-1", "alpha", "zed"]
        # Own card reflects OUR connection state (no connection in this test).
        assert cards[0].status == "offline"


class TestBroadcast:
    async def _publish(self, mesh):
        with patch.object(mesh, "_publish_plain", new=AsyncMock()) as pub:
            await mesh.broadcast("all hands on deck")
        return pub

    @pytest.mark.asyncio
    async def test_publishes_event_qos0_with_sender(self):
        mesh = _make_mesh()
        pub = await self._publish(mesh)
        topic, payload, qos = pub.await_args.args[0], pub.await_args.args[1], pub.await_args.kwargs.get("qos", pub.await_args.args[2] if len(pub.await_args.args) > 2 else 1)
        assert topic == "$a2a/v1/event/default/default/broadcast"
        assert json.loads(payload) == {"sender": "self-1", "text": "all hands on deck"}
        assert qos == 0

    @pytest.mark.asyncio
    async def test_received_event_routed_and_own_echo_filtered(self):
        mesh = _make_mesh()
        events = []
        mesh.on_broadcast_event = lambda s, t: events.append((s, t))
        mesh._handle_event(
            _msg("$a2a/v1/event/default/default/broadcast",
                 b'{"sender":"bill","text":"hi all"}'),
        )
        mesh._handle_event(
            _msg("$a2a/v1/event/default/default/broadcast",
                 b'{"sender":"self-1","text":"echo"}'),
        )
        mesh._handle_event(_msg("$a2a/v1/event/default/default/broadcast", b"not json"))
        assert events == [("bill", "hi all")]


class TestResponseBackoff:
    def test_backoff_jitter_bounds(self):
        for attempt, base in enumerate([1.0, 2.0, 4.0]):
            for _ in range(20):
                d = _backoff_delay(attempt)
                assert base * 0.8 <= d <= base * 1.2
        # Attempts beyond the base table cap at the last entry (4 s ± jitter).
        for _ in range(20):
            assert 4.0 * 0.8 <= _backoff_delay(99) <= 4.0 * 1.2

    @pytest.mark.asyncio
    async def test_deliver_retries_with_new_correlation_until_attempts(self):
        mesh = _make_mesh()
        send = _OutboundSend("t1", "peer-1", "s1", '{"payload":true}')
        with patch.object(mesh, "_publish_attempt", new=AsyncMock()) as pub:
            with patch("slife.timeouts.timeouts.deliver.reply_first", 0.01):
                with patch("slife.a2a.mesh._backoff_delay", return_value=0.01):
                    await mesh._deliver(send)
        # Initial attempt (in send_message) + 2 retries = 3 total; retries here.
        assert pub.await_count == 2
        corrs = [c[1] for c in pub.await_args_list]
        assert all(c is not None for c in corrs)

    @pytest.mark.asyncio
    async def test_deliver_stops_once_delivered(self):
        mesh = _make_mesh()
        send = _OutboundSend("t1", "peer-1", "s1", "x")
        send.delivered = True
        with patch.object(mesh, "_publish_attempt", new=AsyncMock()) as pub:
            with patch("slife.timeouts.timeouts.deliver.reply_first", 0.01):
                await mesh._deliver(send)
        pub.assert_not_awaited()


class TestCompleteTaskBridge:
    @pytest.mark.asyncio
    async def test_resolves_with_result(self):
        mesh = _make_mesh()
        inbound = []
        mesh.on_inbound_task = lambda s, c, t, kind="task": inbound.append(
            (s, c, t, kind),
        )
        task = _spawn_inbound(mesh, "t1")
        await asyncio.sleep(0)
        assert inbound == [("peer-1", "do it", "t1", "task")]
        assert mesh.complete_task("t1", "the answer") == "ok"
        assert await asyncio.wait_for(task, 1) == "the answer"

    @pytest.mark.asyncio
    async def test_message_type_enqueues_conversation_not_task(self):
        """A MESSAGE-typed message is a conversation: enqueued task-less
        (kind="message"), no bridge, and on_request finishes immediately."""
        mesh = _make_mesh()
        inbound = []
        mesh.on_inbound_task = lambda s, c, t, kind="task": inbound.append(
            (s, c, t, kind),
        )
        task = _spawn_inbound(mesh, "m1", variables={"message_type": "message"})
        assert await asyncio.wait_for(task, 1) is None
        assert inbound == [("peer-1", "do it", "", "message")]

    @pytest.mark.asyncio
    async def test_task_response_type_ignored_not_a_task(self):
        """A received TASK_RESPONSE is not enqueued as a task at all."""
        mesh = _make_mesh()
        inbound = []
        mesh.on_inbound_task = lambda s, c, t, kind="task": inbound.append(t)
        task = _spawn_inbound(
            mesh, "tr1", variables={"message_type": "task_response"},
        )
        assert await asyncio.wait_for(task, 1) is None
        assert inbound == []

    @pytest.mark.asyncio
    async def test_cancel_raises_cancelled_error(self):
        mesh = _make_mesh()
        task = _spawn_inbound(mesh, "t2")
        await asyncio.sleep(0)
        assert mesh.complete_task("t2", "", cancelled=True) == "ok"
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 1)

    @pytest.mark.asyncio
    async def test_unknown_and_duplicate_refused(self):
        mesh = _make_mesh()
        assert mesh.complete_task("nope", "x").startswith("Error")
        task = _spawn_inbound(mesh, "t3")
        await asyncio.sleep(0)
        assert mesh.complete_task("t3", "once") == "ok"
        assert mesh.complete_task("t3", "twice").startswith("Error")
        await asyncio.wait_for(task, 1)

    @pytest.mark.asyncio
    async def test_an_answered_task_is_not_orphaned(self):
        """Completing a task clears it from the store, so the next process
        does not report it as work it died holding."""
        mesh = _make_mesh()
        task = _spawn_inbound(mesh, "t-done")
        await asyncio.sleep(0)
        assert mesh.complete_task("t-done", "the answer") == "ok"
        await asyncio.wait_for(task, 1)
        assert _make_mesh().stale_inbound() == []


class TestOrphanedTaskReporting:
    """A restart orphans every inbound task in flight — the bridge and the
    peer's reply topic both die with the process.  The replacement process
    must say so, not answer with the generic 'unknown id'."""

    def _orphan(self, task_id="ec604319", peer="jack"):
        """Leave a task in flight, then start a fresh process over the same
        state file (``_make_mesh`` reads the file conftest points at)."""
        mesh = _make_mesh()
        mesh._note_inbound(task_id, peer)
        return _make_mesh()

    @pytest.mark.asyncio
    async def test_orphan_is_reported_after_a_restart(self):
        restarted = self._orphan()
        assert [t["task_id"] for t in restarted.stale_inbound()] == ["ec604319"]
        assert restarted.stale_inbound()[0]["peer"] == "jack"

    @pytest.mark.asyncio
    async def test_completing_an_orphan_names_the_peer_and_the_way_out(self):
        restarted = self._orphan()
        msg = restarted.complete_task("ec604319", "too late")
        assert msg.startswith("Error")
        assert "jack" in msg
        assert "message_type='message'" in msg

    @pytest.mark.asyncio
    async def test_a_never_seen_id_gets_the_generic_error(self):
        """A typo must not be excused as a restart casualty."""
        mesh = _make_mesh()
        msg = mesh.complete_task("never-seen", "x")
        assert msg.startswith("Error")
        assert "message_type='message'" not in msg
        assert "marker" in msg

    @pytest.mark.asyncio
    async def test_answering_the_peer_with_a_message_clears_it(self):
        restarted = self._orphan()
        assert len(restarted.stale_inbound()) == 1
        with patch.object(restarted, "_publish_props", new=AsyncMock()), \
                patch.object(restarted, "_deliver", new=AsyncMock()):
            await restarted.send_message("jack", "here it is",
                                         message_type="message")
        assert restarted.stale_inbound() == []

    @pytest.mark.asyncio
    async def test_a_new_task_to_the_peer_does_not_clear_it(self):
        """Sending a *new* task is not answering the orphaned one."""
        restarted = self._orphan()
        with patch.object(restarted, "_publish_props", new=AsyncMock()), \
                patch.object(restarted, "_deliver", new=AsyncMock()):
            await restarted.send_message("jack", "unrelated work")
        assert len(restarted.stale_inbound()) == 1

    @pytest.mark.asyncio
    async def test_a_retried_task_is_no_longer_orphaned(self):
        """The peer re-sending the same Task.id genuinely re-registers the
        bridge, so it becomes completable again."""
        restarted = self._orphan()
        mesh = restarted
        task = _spawn_inbound(mesh, "ec604319")
        await asyncio.sleep(0)
        assert mesh.stale_inbound() == []
        assert mesh.complete_task("ec604319", "late but fine") == "ok"
        await asyncio.wait_for(task, 1)

    @pytest.mark.asyncio
    async def test_a_message_conversation_is_never_tracked(self):
        """A MESSAGE (and a stray TASK_RESPONSE) is not a task — nothing will
        ever complete it, so tracking it would report it stale forever."""
        mesh = _make_mesh()
        task = _spawn_inbound(mesh, "m1", variables={"message_type": "message"})
        await asyncio.wait_for(task, 1)
        assert _make_mesh().stale_inbound() == []


class TestWorkingKeepalive:
    @pytest.mark.asyncio
    async def test_keepalive_streams_working_until_resolved(self, monkeypatch):
        """A slow inbound task keeps the standard stream alive with periodic
        ``working`` updates (a peer's 30 s stream_idle_timeout would otherwise
        fire while the harness is still thinking)."""
        monkeypatch.setattr("slife.timeouts.timeouts.deliver.keepalive", 0.01)
        mesh = _make_mesh()
        stream = AsyncMock()
        task = asyncio.create_task(mesh._responder.on_request(
            A2ARequest(text="hi", request_id="t-k", task_id="t-k",
                       sender="peer-1"),
            stream=stream,
        ))
        await asyncio.sleep(0.05)
        assert stream.await_count >= 1  # pings flowed while unresolved
        assert mesh.complete_task("t-k", "the answer") == "ok"
        assert await asyncio.wait_for(task, 1) == "the answer"
        count_at_resolve = stream.await_count
        await asyncio.sleep(0.05)
        # The pinger stopped after resolution — no further stream calls.
        assert stream.await_count == count_at_resolve


class TestCancellationPath:
    @pytest.mark.asyncio
    async def test_peer_cancel_is_delivered_unless_closing(self):
        mesh = _make_mesh()
        # A live link is what a peer's CancelTask can arrive on.
        mesh._connected = True
        delivered = []
        # The withdrawal is a delivery to the harness — task id AND peer, so
        # the harness can name the sender it is telling the model about.
        mesh.on_peer_cancel = lambda t, peer: delivered.append((t, peer))
        task = self_spawn_local(mesh, "t9")

        async def _cancel_task(t):
            await asyncio.sleep(0.01)
            t.cancel()

        canceller = asyncio.create_task(_cancel_task(task))
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        finally:
            await canceller
        assert delivered == [("t9", "peer-1")]
        # A late task_response for the withdrawn id gets the truthful answer —
        # not the typo-flavoured "unknown task_id … check the marker", which
        # would send the model back to re-read a marker that is correct.
        assert "withdrawn by peer-1" in mesh.complete_task("t9", "late result")

    @pytest.mark.asyncio
    async def test_closing_suppresses_delivery(self):
        mesh = _make_mesh()
        mesh._connected = True
        mesh._closing = True
        delivered = []
        mesh.on_peer_cancel = lambda t, peer: delivered.append((t, peer))
        task = self_spawn_local(mesh, "t10")

        async def _cancel_task(t):
            await asyncio.sleep(0.01)
            t.cancel()

        canceller = asyncio.create_task(_cancel_task(task))
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        finally:
            await canceller
        assert delivered == []

    @pytest.mark.asyncio
    async def test_link_loss_is_not_a_withdrawal(self):
        """A dropped link must not be reported as a peer withdrawal.

        The SDK cancels EVERY inflight handler when the responder's own
        session ends, which is indistinguishable from a CancelTask by
        exception alone — so the delivery is gated on the link being live.
        Silence is right: the peer's requester re-sends the task.
        """
        mesh = _make_mesh()          # _connected stays False: the link is down
        delivered = []
        mesh.on_peer_cancel = lambda t, peer: delivered.append((t, peer))
        task = self_spawn_local(mesh, "t11")

        async def _cancel_task(t):
            await asyncio.sleep(0.01)
            t.cancel()

        canceller = asyncio.create_task(_cancel_task(task))
        try:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
        finally:
            await canceller
        assert delivered == []

    @pytest.mark.asyncio
    async def test_disconnect_cancels_tracking(self):
        mesh = _make_mesh()
        send = _OutboundSend("t1", "peer-1", "s1", "x")
        send.loops.add(asyncio.create_task(mesh._deliver(send)))
        mesh._sends["t1"] = send
        mesh._corr_to_task["t1"] = "t1"
        await mesh.disconnect()
        assert mesh._sends == {}
        assert mesh._corr_to_task == {}
        assert mesh._connected is False


def self_spawn_local(mesh, task_id):
    loop = asyncio.get_running_loop()
    return loop.create_task(
        mesh._responder.on_request(
            A2ARequest(text="hi", request_id=task_id, task_id=task_id,
                       sender="peer-1"),
            stream=AsyncMock(),
        ),
    )