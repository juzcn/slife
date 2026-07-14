"""Inbox — unified message entry point for all agents.

Human keyboard, MQTT tasks, CLI — every message from every agent
flows through the same queue and the same processing pipeline.
The channel (TUI / MQTT / …) only affects *display* and *reply routing*,
not *processing* logic.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from slife.a2a.identity import AgentId, AgentMessage
from slife.agent.conversation import Conversation

if TYPE_CHECKING:
    from slife.a2a.client import A2AClient
    from slife.agent.loop import AgentLoop, AgentEventHandler

logger = logging.getLogger(__name__)


class Inbox:
    """Unified message inbox — every agent's input arrives here.

    Serialises concurrent messages from multiple agents: even if human
    and a remote agent send at the same time, only one AgentLoop runs
    at a time.  While the loop is running the agent card shows "busy".

    Usage::

        inbox = Inbox(agent_loop, conversations)
        await inbox.post(AgentMessage(source=AgentId("human"), content="hi"))
    """

    def __init__(
        self,
        agent_loop: "AgentLoop",
        conversations: "ConversationStore",
        a2a_client: "A2AClient | None" = None,
        on_activity: "Callable | None" = None,
    ):
        self._agent_loop = agent_loop
        self._conversations = conversations
        self._a2a_client = a2a_client
        self._on_activity = on_activity  # async cb(kind, **kwargs)
        self._queue: asyncio.Queue[AgentMessage] = asyncio.Queue()
        self._runner_task: asyncio.Task | None = None

    # ── Post ──────────────────────────────────────────────────────────

    async def post(self, msg: AgentMessage) -> None:
        """Drop a message into the inbox.  Non-blocking, never raises."""
        await self._queue.put(msg)
        logger.debug(
            "inbox_post source=%s content=%.80s", msg.source, msg.content,
        )

    # ── Run ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Process messages forever.  Call as a background task."""
        logger.info("inbox_start")
        while True:
            msg = await self._queue.get()
            await self._process_one(msg)

    async def _process_one(self, msg: AgentMessage) -> None:
        """Process a single message through the agent loop."""
        from slife.a2a.identity import HUMAN

        is_remote = msg.source != HUMAN
        logger.info(
            "inbox_process source=%s corr_id=%s content=%.80s remote=%s",
            msg.source, msg.correlation_id, msg.content, is_remote,
        )

        # Notify TUI that a remote task was received
        if is_remote and self._on_activity:
            try:
                await self._on_activity(
                    "task_received", source=msg.source, content=msg.content,
                )
            except Exception:
                pass

        # Mark busy while processing
        if self._a2a_client:
            await self._a2a_client.update_status("busy")

        try:
            # Get or create conversation for this source
            conversation = self._conversations.get_or_create(msg.source)

            # Build a handler appropriate for the source
            handler = self._conversations.handler_for(msg.source)

            # Run the agent loop
            from slife.agent.loop import AgentResult
            result = await self._agent_loop.run(
                user_input=msg.content,
                conversation=conversation,
                images=msg.images if msg.images else None,
                handler=handler,
            )

            # Notify TUI that processing completed
            if is_remote and self._on_activity:
                try:
                    await self._on_activity(
                        "task_completed",
                        source=msg.source,
                        content=msg.content,
                        result=result.text if hasattr(result, "text") else str(result),
                    )
                except Exception:
                    pass

            # Reply via MQTT if this was a remote task
            if msg.reply_to and self._a2a_client:
                await self._publish_reply(msg.reply_to, msg.correlation_id, result)

        except Exception as e:
            logger.warning("inbox_process_error source=%s err=%s", msg.source, e)
            if is_remote and self._on_activity:
                try:
                    await self._on_activity(
                        "task_completed",
                        source=msg.source,
                        content=msg.content,
                        result=f"Error: {e}",
                    )
                except Exception:
                    pass
            if msg.reply_to and self._a2a_client:
                await self._publish_reply(
                    msg.reply_to, msg.correlation_id, f"Error: {e}",
                )
        finally:
            # Return to idle
            if self._a2a_client:
                await self._a2a_client.update_status("idle")

    async def _publish_reply(
        self, reply_to: str, corr_id: str | None, result,
    ) -> None:
        """Publish a task result back to the requester."""
        import json as _json

        text = result.text if hasattr(result, "text") else str(result)
        payload = _json.dumps({
            "correlation_id": corr_id or "",
            "result": text,
        })
        await self._a2a_client._adapter.publish(reply_to, payload, qos=1)


class ConversationStore:
    """Manages per-source-agent conversations.

    The human's conversation persists across messages (so the operator
    has a continuous back-and-forth).  Remote agent conversations are
    fresh each time (one-shot task model).
    """

    def __init__(self, system_prompt: str):
        self._system_prompt = system_prompt
        self._convs: dict[AgentId, Conversation] = {}
        self._handler_factories: dict[AgentId, "AgentEventHandler | None"] = {}

    def register_handler(
        self, source: AgentId, handler: "AgentEventHandler | None",
    ) -> None:
        """Register a handler (or None) for a specific source agent.

        The human agent gets a TUIHandler (streams to chat); remote
        agents get ``None`` (no UI streaming, just the final result).
        """
        self._handler_factories[source] = handler

    def handler_for(self, source: AgentId) -> "AgentEventHandler | None":
        """Return the handler for *source*, or None."""
        return self._handler_factories.get(source)

    def get_or_create(self, source: AgentId) -> Conversation:
        """Get or create a conversation for *source*.

        The human's conversation is persistent.  Remote agents get a
        fresh conversation each message (one-shot).
        """
        from slife.a2a.identity import HUMAN

        if source == HUMAN:
            # Persistent conversation for the human operator
            if HUMAN not in self._convs:
                self._convs[HUMAN] = Conversation(
                    system_prompt=self._system_prompt,
                )
            return self._convs[HUMAN]

        # One-shot conversation for remote agents
        return Conversation(system_prompt=self._system_prompt)

    def clear(self, source: AgentId) -> None:
        """Clear conversation history for *source*."""
        if source in self._convs:
            self._convs[source].clear()
            del self._convs[source]
