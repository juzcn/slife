"""Autonomous heartbeat — a periodic window for the agent to think or act.

Without user input the agent is completely still.  A heartbeat gives it a
regular opportunity for self-initiated behavior (a precondition for
emergent consciousness): every idle interval a heartbeat message is posted
to the inbox and runs as a normal agent-loop turn, saved like any other turn.

The turn's output contract is ``.`` (nothing worth saying) or real content
(an autonomous act).  The ``.`` is the minimal non-empty assistant reply —
it satisfies the user→assistant role alternation (two consecutive user
messages would be rejected by the Anthropic wire) while signalling
"checked in, nothing to do".  The turn renders nowhere in the chat (the
silent handler); only a non-``.`` reply is surfaced to the TUI as an
autonomous message (⚡ 自主).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from slife.agent.loop import TokenUsage, ToolCallInfo
    from slife.agent.service import AgentService

logger = logging.getLogger(__name__)

#: Default idle heartbeat interval (seconds) — overridable via
#: ``agent.heartbeat_interval`` in slife.json5.
HEARTBEAT_INTERVAL = 1800

# The "[Heartbeat]" prefix is the TUI filter mark — restore / live both
# recognise heartbeat turns by it.  The reply contract lives in the
# system prompt (section 9), so the trigger can stay short.
HEARTBEAT_PROMPT = (
    "[Heartbeat] click.  Reply per your heartbeat contract in the system "
    "prompt: real content if you have something worth saying, otherwise "
    "exactly \".\"."
)

#: TUI filter mark — a turn whose user message starts with this is a heartbeat.
HEARTBEAT_MARK = "[Heartbeat]"


class _SilentHandler:
    """No-op handler for heartbeat turns.

    The turn runs normally through the agent loop (saved like any other
    turn) but renders nothing to the chat — the final reply
    is delivered to the caller via ``on_reply``, which surfaces non-``.``
    content as an autonomous message (⚡ 自主).
    """

    async def on_thinking_chunk(self, chunk: str) -> None:
        pass

    async def on_text_chunk(self, chunk: str) -> None:
        pass

    async def on_tool_call(
        self, tool_call: "ToolCallInfo", iteration: int = 0, max_iterations: int = 30
    ) -> None:
        pass

    async def on_tool_approval(self, tool_call: "ToolCallInfo") -> bool:
        return True

    async def on_tool_result(
        self, tool_call_id: str, result: str, is_error: bool
    ) -> None:
        pass

    async def on_token_usage(self, usage: "TokenUsage") -> None:
        pass

    async def on_stream_retry(self) -> None:
        pass

    async def on_max_iterations(self, iterations: int) -> None:
        pass

    async def on_memory_save_warning(self, message: str) -> None:
        pass

    def finalize_current(self) -> None:
        pass


async def heartbeat_loop(service: "AgentService") -> None:
    """Periodically post a heartbeat message while the agent is idle.

    Skips the beat when a turn is in progress or messages are queued, so
    the heartbeat never competes with real user/remote work.  The message
    flows through the normal inbox pipeline (own turn, loop, and save);
    ``on_reply`` surfaces non-``.`` output to the TUI (⚡ 自主).
    """
    from slife.a2a.identity import HEARTBEAT, AgentMessage, Channel

    try:
        interval = float(getattr(service.config, "heartbeat_interval", HEARTBEAT_INTERVAL))
    except (TypeError, ValueError):
        interval = HEARTBEAT_INTERVAL
    if interval <= 0:
        # A non-positive interval (e.g. a bad config value) would either
        # raise in asyncio.sleep or spin the loop — fall back to default.
        interval = HEARTBEAT_INTERVAL
    while True:
        await asyncio.sleep(interval)
        try:
            inbox = service.inbox
            if inbox is not None and (inbox.busy or inbox.pending):
                continue  # not idle — skip this beat
            await inbox.post(
                AgentMessage(
                    source=HEARTBEAT,
                    content=HEARTBEAT_PROMPT,
                    handler=_SilentHandler(),
                    on_reply=service.surface_autonomous_reply,
                    channel=Channel.heartbeat(),
                )
            )
            logger.info("heartbeat_posted")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug("heartbeat_error err=%s", e)
