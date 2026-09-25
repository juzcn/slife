"""Tests for the autonomous heartbeat — the "." reply contract and TUI mark."""

import pytest; pytestmark = pytest.mark.unit

import asyncio
from types import SimpleNamespace

import pytest

from slife.agent.heartbeat import HEARTBEAT_MARK, HEARTBEAT_PROMPT, heartbeat_period


class TestHeartbeatPeriod:
    """``0`` is OFF — it never falls back to the registry default."""

    def test_configured_value_wins(self):
        assert heartbeat_period(
            SimpleNamespace(heartbeat_interval=90)  # noqa-timeout
        ) == 90.0

    def test_zero_is_off(self):
        assert heartbeat_period(
            SimpleNamespace(heartbeat_interval=0)  # noqa-timeout
        ) == 0.0

    def test_negative_is_off(self):
        assert heartbeat_period(SimpleNamespace(heartbeat_interval=-5)) <= 0

    def test_absent_resolves_to_registry(self):
        import slife.timeouts as timeouts

        assert heartbeat_period(SimpleNamespace(heartbeat_interval=None)) == float(
            timeouts.timeouts.pacing.heartbeat
        )

    def test_unparseable_resolves_to_registry(self):
        import slife.timeouts as timeouts

        assert heartbeat_period(SimpleNamespace(heartbeat_interval="soon")) == float(
            timeouts.timeouts.pacing.heartbeat
        )


class TestHeartbeatLoop:
    """The loop's own gate: off means no beat, and no sleeping either."""

    def _service(self, interval):
        posted: list = []

        class _Inbox:
            busy = False
            pending = False

            async def post(self, msg):
                posted.append(msg)

        return (
            SimpleNamespace(
                config=SimpleNamespace(heartbeat_interval=interval),
                inbox=_Inbox(),
                surface_autonomous_reply=None,
            ),
            posted,
        )

    @pytest.mark.asyncio
    async def test_zero_posts_nothing(self):
        from slife.agent.heartbeat import heartbeat_loop

        svc, posted = self._service(0)
        await asyncio.wait_for(heartbeat_loop(svc), timeout=1)  # noqa-timeout
        assert posted == []

    @pytest.mark.asyncio
    async def test_enabled_interval_posts_a_beat(self, monkeypatch):
        """Positive interval → sleeps that long, then posts one heartbeat."""
        from slife.agent import heartbeat as hb

        slept: list[float] = []

        async def fake_sleep(seconds):
            slept.append(seconds)
            if len(slept) > 1:
                raise asyncio.CancelledError

        monkeypatch.setattr(
            hb,
            "asyncio",
            SimpleNamespace(sleep=fake_sleep, CancelledError=asyncio.CancelledError),
        )
        svc, posted = self._service(7)
        with pytest.raises(asyncio.CancelledError):
            await hb.heartbeat_loop(svc)
        assert slept == [7.0, 7.0]
        assert len(posted) == 1


class TestHeartbeatPrompt:
    def test_prompt_carries_filter_mark(self):
        """The TUI filters heartbeat turns by the mark on the trigger message."""
        assert HEARTBEAT_PROMPT.startswith(HEARTBEAT_MARK)

    def test_mark_not_empty(self):
        assert HEARTBEAT_MARK and HEARTBEAT_MARK.startswith("[")


class TestSilentHandler:
    @pytest.mark.asyncio
    async def test_renders_nothing(self):
        """The heartbeat turn renders nothing to the chat — the TUI surfaces
        only non-"." content via on_autonomous, and filters the rest."""
        from slife.agent.heartbeat import _SilentHandler

        h = _SilentHandler()
        await h.on_thinking_chunk("reasoning")
        await h.on_text_chunk("content")
        await h.on_tool_call(None)
        assert await h.on_tool_approval(None) is True
        await h.on_tool_result("1", "result", False)
        await h.on_token_usage(None)
        h.finalize_current()


class TestSurfaceAutonomousReply:
    """The reply contract: exactly "." → quiet; any other content → act."""

    def _service(self):
        from slife.agent.service import AgentService

        srv = AgentService.__new__(AgentService)
        surfaced: list[str] = []
        beats: list[str] = []

        async def _surface(text):
            surfaced.append(text)

        async def _beat(outcome):
            beats.append(outcome)

        srv._on_autonomous = _surface
        srv._on_heartbeat = _beat
        return srv, surfaced, beats

    @pytest.mark.asyncio
    async def test_dot_is_quiet(self):
        srv, surfaced, beats = self._service()
        await srv.surface_autonomous_reply(".")
        assert surfaced == []
        assert beats == ["quiet"]

    @pytest.mark.asyncio
    async def test_empty_is_quiet(self):
        srv, surfaced, beats = self._service()
        await srv.surface_autonomous_reply("  ")
        assert surfaced == []
        assert beats == ["quiet"]

    @pytest.mark.asyncio
    async def test_content_is_act(self):
        srv, surfaced, beats = self._service()
        await srv.surface_autonomous_reply("I noticed X from earlier")
        assert surfaced == ["I noticed X from earlier"]
        assert beats == ["act"]
