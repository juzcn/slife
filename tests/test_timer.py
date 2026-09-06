"""Tests for the wait_minutes timer tool and the [Timer] wake."""

import asyncio

import pytest

pytestmark = pytest.mark.unit

from slife.agent.schedules import is_autonomous_trigger
from slife.agent.timer import TIMER_MARK, is_timer_trigger, timer_text


class TestTimerText:
    def test_text_carries_filter_mark(self):
        assert timer_text(5, "check deploy").startswith(TIMER_MARK)

    def test_text_relays_note(self):
        assert "check deploy" in timer_text(5, "check deploy")

    def test_text_without_note(self):
        assert "Resume what you were doing" in timer_text(5, "")


class TestTimerTrigger:
    def test_is_timer_trigger(self):
        assert is_timer_trigger("[Timer] Your 5-minute timer elapsed.")
        assert not is_timer_trigger("hello")

    def test_is_autonomous_trigger_covers_timer(self):
        assert is_autonomous_trigger("[Timer] wake")
        assert is_autonomous_trigger("[Heartbeat] click")
        assert is_autonomous_trigger("[Schedule x] due")


class TestWaitMinutesTool:
    def _tool(self, wake=None):
        from slife.tools.timer import WaitMinutesTool

        tool = WaitMinutesTool()
        if wake is not None:
            ctx = type("Ctx", (), {})()
            ctx.schedule_wakeup = wake
            tool._ctx = ctx
        return tool

    @pytest.mark.asyncio
    async def test_rejects_non_positive(self):
        assert "at least 1" in await self._tool().execute(minutes=0)

    @pytest.mark.asyncio
    async def test_rejects_non_integer(self):
        assert "whole number" in await self._tool().execute(minutes=2.5)

    @pytest.mark.asyncio
    async def test_rejects_bool(self):
        assert "whole number" in await self._tool().execute(minutes=True)

    @pytest.mark.asyncio
    async def test_unavailable_without_wake_hook(self):
        assert "unavailable" in await self._tool().execute(minutes=5)

    @pytest.mark.asyncio
    async def test_schedules_wake_with_minutes_and_note(self):
        calls = []

        async def wake(delay, note):
            calls.append((delay, note))

        out = await self._tool(wake=wake).execute(minutes=5, note="check deploy")
        assert calls == [(300, "check deploy")]
        assert "Timer set" in out


class TestScheduleWakeup:
    def _service(self):
        from slife.agent.service import AgentService

        srv = AgentService.__new__(AgentService)
        srv._timer_tasks = set()

        posted = []

        class _Inbox:
            async def post(self, msg):
                posted.append(msg)

        srv.inbox = _Inbox()
        surfaced = []

        async def _surface(text):
            surfaced.append(text)

        srv._on_timer = _surface
        return srv, posted, surfaced

    @pytest.mark.asyncio
    async def test_posts_timer_message(self):
        srv, posted, surfaced = self._service()
        await srv.schedule_wakeup(0.001, "check deploy")
        assert srv._timer_tasks
        await asyncio.sleep(0.05)
        assert len(posted) == 1
        msg = posted[0]
        assert msg.content.startswith(TIMER_MARK)
        assert "check deploy" in msg.content
        assert msg.source == "system"
        assert msg.channel.kind == "system"


class TestSurfaceTimerReply:
    def _service(self):
        from slife.agent.service import AgentService

        srv = AgentService.__new__(AgentService)
        surfaced = []

        async def _surface(text):
            surfaced.append(text)

        srv._on_timer = _surface
        return srv, surfaced

    @pytest.mark.asyncio
    async def test_dot_is_quiet(self):
        srv, surfaced = self._service()
        await srv._surface_timer_reply(".")
        assert surfaced == []

    @pytest.mark.asyncio
    async def test_content_surfaces(self):
        srv, surfaced = self._service()
        await srv._surface_timer_reply("deploy is green")
        assert surfaced == ["deploy is green"]
