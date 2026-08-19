"""Tests for the autonomous heartbeat — the "." reply contract and TUI mark."""

import pytest; pytestmark = pytest.mark.unit

import pytest

from slife.agent.heartbeat import HEARTBEAT_MARK, HEARTBEAT_PROMPT


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
