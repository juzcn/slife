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
from slife.agent.loop import AgentCancelled, MaxIterationsExceeded

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
        on_turn_complete: "Callable | None" = None,
    ):
        self._agent_loop = agent_loop
        self._conversations = conversations
        self._a2a_client = a2a_client
        self._on_activity = on_activity  # async cb(kind, **kwargs)
        self._on_turn_complete = on_turn_complete  # async cb(user_message, token_count, conversation)
        self._queue: asyncio.Queue[AgentMessage] = asyncio.Queue()
        self._runner_task: asyncio.Task | None = None
        self._processing: bool = False

    # ── Cancel ────────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Cancel the currently running agent loop (if any).

        Safe to call when nothing is running — does nothing.
        """
        self._agent_loop.cancel()

    # ── Post ──────────────────────────────────────────────────────────

    @property
    def busy(self) -> bool:
        """True when the inbox is currently processing a message."""
        return self._processing

    @property
    def pending(self) -> int:
        """Number of messages waiting in the queue (approx)."""
        return self._queue.qsize()

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
            # Notify TUI that processing completed so the status bar
            # can clear the "⏳ processing" indicator.
            if self._on_activity:
                try:
                    await self._on_activity("idle")
                except Exception:
                    pass

    async def _process_one(self, msg: AgentMessage) -> None:
        """Process a single message through the agent loop."""
        from slife.a2a.identity import HUMAN, WECHAT

        is_remote = msg.source not in (HUMAN, WECHAT)
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

        # Notify TUI of peer terminal messages (WeChat etc.)
        # so they appear in the chat view with a source prefix.
        if msg.source == WECHAT and self._on_activity:
            try:
                await self._on_activity(
                    "peer_message", source="wechat", content=msg.content,
                )
            except Exception:
                pass

        # Mark busy while processing
        if self._a2a_client:
            await self._a2a_client.update_status("busy")
        self._processing = True

        try:
            # Reset cancel state for the new message
            self._agent_loop.reset_cancel()

            # Get or create conversation for this source
            conversation = self._conversations.get_or_create(msg.source)

            # Build a handler appropriate for the source
            # Prefer the handler attached to the message (TUI path).
            # Fall back to the per-source registry / default factory
            # (remote A2A messages that don't carry their own handler).
            handler = msg.handler
            if handler is None:
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

            # Persist turn to memory (unified path for all sources).
            if self._on_turn_complete:
                try:
                    await self._on_turn_complete(
                        user_message=msg.content,
                        token_count=result.usage.total_tokens
                        if hasattr(result, "usage") else 0,
                        conversation=conversation,
                        channel=str(msg.source),
                    )
                except Exception:
                    logger.warning("on_turn_complete_error", exc_info=True)

            # Route reply to originating channel (WeChat, etc.)
            if msg.on_reply is not None:
                reply_text = result.text if hasattr(result, "text") else str(result)
                try:
                    await msg.on_reply(reply_text)
                except Exception as e:
                    logger.debug("on_reply_error channel=%s err=%s",
                                 msg.metadata.get("channel", "?"), e)

        except AgentCancelled:
            logger.info("inbox_cancelled source=%s", msg.source)
        except MaxIterationsExceeded as e:
            # Info-level: the TUI shows a red system message via _on_activity
            # so there's no need to alarm the user with a stderr warning.
            logger.info("inbox_process_error source=%s err=%s", msg.source, e)
            # Finalize the handler so the last assistant message is marked complete
            if handler is not None:
                try:
                    handler.finalize_current()
                except Exception:
                    pass
            # Notify TUI so the user sees the iteration-limit message
            if self._on_activity:
                try:
                    await self._on_activity(
                        "loop_error",
                        source=msg.source,
                        error=str(e),
                    )
                except Exception:
                    pass
        except Exception as e:
            logger.warning("inbox_process_error source=%s err=%s", msg.source, e)
            # Finalize the handler so the TUI spinner stops — without
            # this the chat view stays in a permanent loading state.
            if handler is not None:
                try:
                    handler.finalize_current()
                except Exception:
                    pass
            # Rollback the failed turn so the conversation isn't
            # poisoned for the next message (e.g. content-policy
            # rejections would keep failing on retry otherwise).
            try:
                conversation.pop_last_turn()
            except Exception:
                pass
            # Notify TUI so the user sees the error in chat
            if self._on_activity:
                try:
                    await self._on_activity(
                        "loop_error",
                        source=msg.source,
                        error=str(e),
                    )
                except Exception:
                    pass
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
            # Also notify channel on error
            if msg.on_reply is not None:
                try:
                    await msg.on_reply(f"Error: {e}")
                except Exception:
                    pass
        finally:
            # Return to idle
            self._processing = False
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
        self._default_handler_factory: "Callable[[], AgentEventHandler] | None" = (
            None
        )

    def set_default_handler_factory(
        self, factory: "Callable[[], AgentEventHandler]",
    ) -> None:
        """Set a factory that creates handlers for sources without one.

        Called at startup so remote A2A tasks always have a handler
        available, even before the first human message is typed.
        """
        self._default_handler_factory = factory

    def register_handler(
        self, source: AgentId, handler: "AgentEventHandler | None",
    ) -> None:
        """Register a handler (or None) for a specific source agent.

        The human agent gets a TUIHandler (streams to chat); remote
        agents get ``None`` (no UI streaming, just the final result).
        """
        self._handler_factories[source] = handler

    def handler_for(self, source: AgentId) -> "AgentEventHandler | None":
        """Return the handler for *source*.

        Falls back to the human handler, then to the default factory,
        so remote tasks always stream to the TUI chat view.
        """
        from slife.a2a.identity import HUMAN

        handler = self._handler_factories.get(
            source
        ) or self._handler_factories.get(HUMAN)
        if handler is not None:
            return handler
        if self._default_handler_factory is not None:
            return self._default_handler_factory()
        return None

    def get_or_create(self, source: AgentId) -> Conversation:
        """Get or create a conversation for *source*.

        Human (TUI) and WeChat conversations are persistent so the
        operator has a continuous back-and-forth.  Remote agent
        conversations are fresh each message (one-shot).
        """
        from slife.a2a.identity import HUMAN, WECHAT

        if source in (HUMAN, WECHAT):
            # Persistent conversation for human / WeChat operators
            if source not in self._convs:
                self._convs[source] = Conversation(
                    system_prompt=self._system_prompt,
                )
            return self._convs[source]

        # One-shot conversation for remote agents
        return Conversation(system_prompt=self._system_prompt)

    def clear(self, source: AgentId) -> None:
        """Clear conversation history for *source*."""
        if source in self._convs:
            self._convs[source].clear()
            del self._convs[source]
