"""Tests for the protocol-era glue (`slife/mcp/era.py`).

Two behaviours matter and both are era-dependent: how a link adopts its
peer's protocol (modern `server/discover` vs the legacy handshake), and how
a change notification reaches us at each era (a `subscriptions/listen`
stream on modern, the session channel on legacy).
"""

import pytest; pytestmark = pytest.mark.unit

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.mcp import era


class _FakeSubscription:
    """An async iterator that yields *events*, then ends."""

    def __init__(self, events, exc=None):
        self._events = list(events)
        self._exc = exc

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._events:
            return self._events.pop(0)
        if self._exc is not None:
            exc, self._exc = self._exc, None
            raise exc
        raise StopAsyncIteration


class _FakeListen:
    """Stands in for `mcp.client.subscriptions.listen` (an async CM).

    Records each round's entry, yields *events*, then replays the queued
    per-round exception (if any).
    """

    def __init__(self, rounds):
        self.rounds = rounds          # list of (events, exc_or_None)
        self.entered = 0

    def __call__(self, session, **kwargs):
        assert kwargs.get("tools_list_changed") is True
        index = self.entered
        self.entered += 1

        class _CM:
            async def __aenter__(_self):
                if index >= len(self.rounds):
                    # Rounds exhausted → the supervisor must stop retrying
                    # (a real peer that can't serve listen says exactly this).
                    from mcp.client.subscriptions import ListenNotSupportedError
                    raise ListenNotSupportedError("test: no more rounds")
                events, exc = self.rounds[index]
                return _FakeSubscription(events, exc)

            async def __aexit__(_self, *tb):
                return False

        return _CM()


class TestNegotiateEra:
    @pytest.mark.asyncio
    async def test_uses_the_sdk_auto_policy(self):
        """The SDK's `mode='auto'` policy drives the negotiation — we never
        hand-roll the probe/fallback decision."""
        session = MagicMock(protocol_version="2026-07-28")
        negotiate_auto = AsyncMock()
        with patch("mcp.client._probe.negotiate_auto", negotiate_auto):
            version = await era.negotiate_era(session)

        negotiate_auto.assert_awaited_once_with(session)
        assert version == "2026-07-28"

    @pytest.mark.asyncio
    async def test_transport_errors_propagate(self):
        """An unreachable peer is a connect failure, never an era verdict."""
        session = MagicMock()
        with patch(
            "mcp.client._probe.negotiate_auto",
            AsyncMock(side_effect=ConnectionError("refused")),
        ):
            with pytest.raises(ConnectionError):
                await era.negotiate_era(session)

    def test_peer_era_reads_the_adopted_result(self):
        modern = MagicMock(discover_result=object(), initialize_result=None)
        legacy = MagicMock(discover_result=None, initialize_result=object())
        assert era.peer_era(modern) == "modern"
        assert era.peer_era(legacy) == "legacy"
        assert era.peer_era(MagicMock(discover_result=None, initialize_result=None)) == "unknown"


class TestWatchToolsChanged:
    @pytest.mark.asyncio
    async def test_calls_handler_per_event_and_relistens_after_a_drop(self):
        from mcp.client.subscriptions import SubscriptionLost

        handler = AsyncMock()
        fake = _FakeListen([
            ([object(), object()], SubscriptionLost("dropped")),
            ([object()], None),
        ])
        with (
            patch("mcp.client.subscriptions.listen", fake),
            # no backoff between rounds in the test
            patch("slife.timeouts.timeouts", MagicMock(ready=MagicMock(relisten=0))),
        ):
            # The supervisor ends on its own once the peer refuses a stream.
            await asyncio.wait_for(era.watch_tools_changed(MagicMock(), handler), 1)

        # 2 events + 1 event, across a drop and a re-listen
        assert handler.await_count == 3
        assert fake.entered == 3   # two rounds + the refusing third

    @pytest.mark.asyncio
    async def test_cancellation_propagates(self):
        """A disconnect cancels the supervisor — it must not swallow it."""
        handler = AsyncMock()
        fake = _FakeListen([([], None)])
        with (
            patch("mcp.client.subscriptions.listen", fake),
            patch("slife.timeouts.timeouts", MagicMock(ready=MagicMock(relisten=5))),
        ):
            task = asyncio.create_task(era.watch_tools_changed(MagicMock(), handler))
            await asyncio.sleep(0.02)     # parked in the backoff sleep
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    @pytest.mark.asyncio
    async def test_legacy_peer_returns_immediately(self):
        """`ListenNotSupportedError` (a pre-2026 peer) ends the supervisor —
        its notifications ride the session channel instead."""
        from mcp.client.subscriptions import ListenNotSupportedError

        handler = AsyncMock()

        def _listen(session, **kwargs):
            raise ListenNotSupportedError("2025-11-25")

        with patch("mcp.client.subscriptions.listen", _listen):
            await era.watch_tools_changed(MagicMock(), handler)   # returns, no raise

        handler.assert_not_awaited()
