"""End-to-end A2A mesh over a real MQTT v5 broker (mosquitto) — optional.

Two ``A2AMesh`` instances (agents ``e2e-a`` / ``e2e-b``) speak the standard
A2A-over-MQTT profile against a live broker: presence discovery via retained
Agent Cards, a SendMessage task round-trip with an out-of-band completion, a
CancelTask round-trip, and fire-and-forget broadcast events.

Each scenario runs inside a dedicated event loop: on Windows an explicitly
constructed :class:`asyncio.SelectorEventLoop` (aiomqtt's socket callbacks
need ``add_reader``/``add_writer``, which the default Proactor loop lacks) —
in its own thread, so the process-wide loop policy of the pytest suite is
never touched.  Skipped when no broker is reachable on localhost:1883.

Run with mosquitto up:
    uv run pytest tests/e2e/test_a2a_mesh_e2e.py -m e2e
"""

import asyncio
import logging
import sys

import aiomqtt
import pytest

from slife.a2a.broker import probe_broker
from slife.a2a.config import A2AConfig
from slife.a2a.mesh import A2AMesh
from slife.a2a.task_store import clear_store

pytestmark = pytest.mark.e2e

logger = logging.getLogger(__name__)

BROKER_HOST = "localhost"
BROKER_PORT = 1883


async def _clear_retained(agent: str) -> None:
    """Delete our agent's retained discovery card (publish empty retained
    payload) so an e2e run never leaves a stale offline card polluting other
    sessions' presence on a shared broker."""
    try:
        async with aiomqtt.Client(
            hostname=BROKER_HOST, port=BROKER_PORT,
            protocol=aiomqtt.ProtocolVersion.V5,
            identifier=f"e2e-cleanup-{agent}",
        ) as client:
            await client.publish(
                f"$a2a/v1/discovery/default/default/{agent}",
                b"", qos=1, retain=True,
            )
    except Exception:
        # NOT swallowed: a silent failure here is how a stale retained card
        # outlives the run and greets every later slife session as a live peer.
        logger.warning("e2e_retained_clear_failed agent=%s", agent, exc_info=True)


async def _finish_meshes(a: A2AMesh, b: A2AMesh) -> None:
    try:
        await a.disconnect()
        await b.disconnect()
    finally:
        await _clear_retained("e2e-a")
        await _clear_retained("e2e-b")


def _with_selector_loop(coro_factory):
    """Run *coro_factory()* to completion on a loop that supports socket
    selectors — always true on POSIX; on Windows, an explicitly-constructed
    SelectorEventLoop in this thread (the default Proactor loop raises
    NotImplementedError on ``add_reader``, which aiomqtt relies on).

    Nested between two sync pytest tests so the process-wide event loop
    policy of the rest of the suite is untouched.
    """
    if sys.platform != "win32":
        return asyncio.run(coro_factory())
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro_factory())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _mesh(name: str) -> A2AMesh:
    return A2AMesh(A2AConfig(
        enabled=True, agent_name=name,
        broker_host=BROKER_HOST, broker_port=BROKER_PORT,
    ))


async def _wait(pred, what: str, timeout: float = 6.0) -> None:
    """Poll up to *timeout* s for *pred* to hold."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture(autouse=True)
def _fresh_store():
    clear_store()
    yield
    clear_store()


def _clear_retained_sync(agent: str) -> None:
    """Blocking form of :func:`_clear_retained` (the fixture below is sync)."""
    try:
        _with_selector_loop(lambda: _clear_retained(agent))
    except Exception:
        logger.warning("e2e_retained_clear_failed agent=%s", agent, exc_info=True)


@pytest.fixture(autouse=True)
def _no_stale_presence_cards():
    """Clear the two fixed e2e identities' retained cards around EVERY scenario.

    The broker is shared and retained cards outlive the process, so a run that
    was interrupted (Ctrl-C, a hard kill) leaves `e2e-a` / `e2e-b` advertising
    themselves as online.  A stale retained *online* card is indistinguishable
    from a live peer — the mesh announces it — so those leftovers surface as
    phantom "⚡ e2e-a online" lines in every later slife session, not just in
    this suite.  Clearing BEFORE the run too makes the suite self-healing: it
    always removes whatever the previous crashed run left behind.
    """
    for agent in ("e2e-a", "e2e-b"):
        _clear_retained_sync(agent)
    yield
    for agent in ("e2e-a", "e2e-b"):
        _clear_retained_sync(agent)


async def _scenario_task_round_trip():
    """b sends a task → a receives it (sender stamped) → a completes it
    out-of-band → b gets the result pushed.  Presence: both see each other;
    broadcast events reach the receiver."""
    if not await probe_broker(BROKER_HOST, BROKER_PORT):
        pytest.skip("mosquitto not reachable at localhost:1883")

    a, b = _mesh("e2e-a"), _mesh("e2e-b")
    a_inbound, a_agents, a_events, b_completions = [], [], [], []
    a.on_inbound_task = lambda s, c, t, kind="task": a_inbound.append((s, c, t, kind))
    a.on_agent_change = lambda card, ev: a_agents.append((str(card.agent_name), ev))
    a.on_broadcast_event = lambda s, t: a_events.append((s, t))
    b.on_task_completion = (
        lambda c, r, x, p, kind="task": b_completions.append((c, r, x, p, kind))
    )

    try:
        await a.connect()
        await b.connect()
        try:
            await _wait(
                lambda: any(
                    str(c.agent_name) == "e2e-b" and c.status == "online"
                    for c in a.list_agents()
                ),
                # Mosquitto retains cards across runs: a stale offline card
                # (a previous session's LWT) can arrive before e2e-b's fresh
                # online one — wait for the ONLINE transition, not mere presence.
                "e2e-a to see e2e-b online",
            )
            assert ("e2e-b", "online") in a_agents
            # Own echo is filtered — only the peer appears.
            assert ("e2e-a", "online") not in a_agents

            task_id = await b.send_message("e2e-a", "hello a")
            await _wait(lambda: bool(a_inbound), "e2e-a to receive the task")
            assert a_inbound[0][:2] == ("e2e-b", "hello a")
            assert a_inbound[0][2] == task_id

            assert a.complete_task(task_id, "answer for b") == "ok"
            await _wait(lambda: bool(b_completions), "e2e-b to receive the result")
            assert b_completions[0][1] == "answer for b"
            assert b_completions[0][2] is False

            await b.broadcast("all hands on deck")
            await _wait(lambda: bool(a_events), "e2e-a to receive the broadcast")
            assert a_events == [("e2e-b", "all hands on deck")]
        finally:
            await _finish_meshes(a, b)
    finally:
        clear_store()


async def _scenario_cancel_round_trip():
    """b cancels an in-flight task on a: a's on_peer_cancel fires (harness
    preempt) and b receives a cancelled completion."""
    if not await probe_broker(BROKER_HOST, BROKER_PORT):
        pytest.skip("mosquitto not reachable at localhost:1883")

    a, b = _mesh("e2e-a"), _mesh("e2e-b")
    a_inbound, a_peer_cancels, b_completions = [], [], []
    a.on_inbound_task = lambda s, c, t, kind="task": a_inbound.append((s, c, t, kind))
    a.on_peer_cancel = lambda t: a_peer_cancels.append(t)
    b.on_task_completion = (
        lambda c, r, x, p, kind="task": b_completions.append((c, r, x, p, kind))
    )

    try:
        await a.connect()
        await b.connect()
        try:
            task_id = await b.send_message("e2e-a", "work that takes a while")
            await _wait(lambda: bool(a_inbound), "e2e-a to receive the task")

            status = await b.cancel_task("e2e-a", task_id)
            assert status == "cancelled"

            # a's responder preempts the blocked on_request...
            await _wait(lambda: bool(a_peer_cancels), "e2e-a's peer-cancel hook")
            assert a_peer_cancels == [task_id]
            # ...and b receives the cancelled completion.
            await _wait(lambda: bool(b_completions), "e2e-b to receive the cancel")
            assert b_completions[0][0] == task_id
            assert b_completions[0][2] is True
        finally:
            await _finish_meshes(a, b)
    finally:
        clear_store()


def test_task_round_trip_and_presence():
    _with_selector_loop(_scenario_task_round_trip)


def test_cancel_round_trip():
    _with_selector_loop(_scenario_cancel_round_trip)