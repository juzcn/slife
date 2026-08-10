"""Tests for A2AClient presence watchdog / offline detection.

Covers the fix where a peer that vanishes silently (its offline presence
message is lost or lacks ``agent_id``) is still pruned: the watchdog must
run the stale-peer prune on every presence sighting — including our own
heartbeat echo — not only on messages from identified peers.
"""

import asyncio
import json
import time as _time

import pytest; pytestmark = pytest.mark.unit

from slife.a2a.client import A2AClient
from slife.a2a.config import A2AConfig
from slife.a2a.identity import AgentId
from slife.a2a.card import AgentCard
from slife.a2a.transport import TransportMessage


class _EchoThenBlockAdapter:
    """Adapter that yields our own presence echo once, then blocks."""

    def __init__(self, agent_id: str):
        self._agent_id = agent_id
        self.is_connected = True

    def messages(self, topic_filter):
        async def gen():
            yield TransportMessage(
                topic=f"Slife/{self._agent_id}/presence",
                payload=json.dumps(
                    {"agent_id": self._agent_id, "status": "online"},
                ),
            )
            await asyncio.Event().wait()  # block after the echo
        return gen()


class TestPresenceWatchdog:
    def _client(self):
        cfg = A2AConfig(
            enabled=True, agent_id="jack",
            heartbeat_interval=1, heartbeat_timeout=1,
        )
        return A2AClient(cfg)

    @pytest.mark.asyncio
    async def test_prunes_stale_peer_on_own_presence_echo(self):
        """Prune runs even when only our own presence echo arrives, so a
        peer that vanished silently is still marked offline."""
        client = self._client()
        client._peers[AgentId("slife")] = (
            AgentCard(agent_id=AgentId("slife"), display_name="", status="idle"),
            _time.monotonic() - 10,  # last heard long ago (> heartbeat_timeout)
        )

        events: list[tuple[str, str]] = []
        client.on_agent_change(lambda card, ev: events.append((str(card.agent_id), ev)))

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
    async def test_offline_message_with_agent_id_removes_peer(self):
        """A presence message with status=offline + agent_id removes the peer
        and fires the offline notification."""
        client = self._client()
        client._peers[AgentId("slife")] = (
            AgentCard(agent_id=AgentId("slife"), display_name="", status="idle"),
            _time.monotonic(),
        )

        events: list[tuple[str, str]] = []
        client.on_agent_change(lambda card, ev: events.append((str(card.agent_id), ev)))

        class _OfflineAdapter:
            def __init__(self):
                self.is_connected = True
            def messages(self, topic_filter):
                async def gen():
                    yield TransportMessage(
                        topic="Slife/slife/presence",
                        payload=json.dumps(
                            {"status": "offline", "agent_id": "slife"},
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
