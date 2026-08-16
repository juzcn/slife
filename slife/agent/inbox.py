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

from openai import BadRequestError, ContentFilterFinishReasonError

from slife.a2a.identity import AgentName, AgentMessage
from slife.agent.conversation import Conversation

if TYPE_CHECKING:
    from collections.abc import Callable

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
        await inbox.post(AgentMessage(source=AgentName("human"), content="hi"))
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
        #: correlation_id of the message currently being processed (remote
        #: A2A/subagent tasks), used by :meth:`cancel_correlation`.
        self._current_corr: str | None = None
        # Frozen — memory is broken.  No new turns are processed; queued
        # messages are dropped (running a turn that can't be persisted is
        # pointless).  Set by AgentService on a fatal memory-save failure.
        self._frozen: bool = False
        self._frozen_reason: str = ""

    # ── Cancel ────────────────────────────────────────────────────────

    def cancel(self) -> None:
        """Cancel the currently running agent loop (if any).

        Safe to call when nothing is running — does nothing.
        """
        self._agent_loop.cancel()

    def freeze(self, reason: str) -> None:
        """Freeze the inbox — stop processing new turns.

        Used when memory (a core feature) is broken: any turn saved after
        this would be lost, so new turns are dropped instead of run.  The
        process stays alive so the TUI can show *reason*; the user fixes
        the DB and restarts.
        """
        self._frozen = True
        self._frozen_reason = reason
        logger.error("inbox_frozen reason=%s", reason)

    def cancel_correlation(self, corr_id: str) -> None:
        """Cancel the task carrying *corr_id* — Esc-equivalent for a remote
        A2A / subagent task (REVIEW C5).

        Drops the message if it is still queued (never runs); otherwise, if
        it is the message currently being processed, stops the running agent
        loop at the next safe point — the same mechanism as the TUI Esc
        binding.  Unknown corr_ids are a no-op.
        """
        if not corr_id:
            return
        # Remove a queued-but-not-yet-started message with this corr_id.
        rest: list[AgentMessage] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item.correlation_id == corr_id:
                logger.info("inbox_queued_task_cancelled corr_id=%s", corr_id)
                continue
            rest.append(item)
        for item in rest:
            self._queue.put_nowait(item)
        # Stop the loop if this corr_id is the message being processed now.
        if self._current_corr == corr_id:
            logger.info("inbox_active_task_cancelled corr_id=%s", corr_id)
            self.cancel()

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
            if self._frozen:
                # Memory is broken — drop rather than run (the turn couldn't
                # be saved anyway).
                logger.warning(
                    "inbox_frozen_dropped source=%s reason=%.120s",
                    msg.source, self._frozen_reason,
                )
                continue
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
        from slife.a2a.identity import HEARTBEAT, HUMAN, WECHAT
        from slife.subagent.identity import SUBAGENT

        # Heartbeat is internal (not a peer terminal) — no A2A busy status
        # or task_received/completed TUI noise for autonomous turns.
        is_remote = msg.source not in (HUMAN, WECHAT, SUBAGENT, HEARTBEAT)
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
        self._current_corr = msg.correlation_id or None

        # Notify the TUI so the status bar shows "⏳ processing" for ANY turn
        # (including autonomous heartbeat turns, whose silent handler never
        # fires on_token_usage to refresh the status bar).
        if self._on_activity:
            try:
                await self._on_activity("busy")
            except Exception:
                pass

        conversation = None
        handler = None
        result = None
        rolled_back = False
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

            # Run the agent loop — cancelled / max-iterations are now
            # returned as AgentResult(cancelled=True) with accumulated
            # usage, not raised as exceptions.
            result = await self._agent_loop.run(
                user_input=msg.content,
                conversation=conversation,
                images=msg.images if msg.images else None,
                handler=handler,
            )

            if result.cancelled:
                logger.info("inbox_cancelled_or_max_iter source=%s", msg.source)
                # Finalize the handler so the last assistant message is marked complete
                if handler is not None:
                    try:
                        handler.finalize_current()
                    except Exception:
                        pass

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

            # Route reply to originating channel (WeChat, etc.).  Pass the
            # cancelled flag so the channel can signal cancellation to the
            # sender (REVIEW C5); callbacks with the older text-only
            # signature fall back gracefully.
            if msg.on_reply is not None:
                reply_text = result.text if hasattr(result, "text") else str(result)
                cancelled = bool(getattr(result, "cancelled", False))
                try:
                    await msg.on_reply(reply_text, cancelled=cancelled)
                except TypeError:
                    await msg.on_reply(reply_text)
                except Exception as e:
                    logger.debug("on_reply_error channel=%s err=%s",
                                 msg.metadata.get("channel", "?"), e)

        except Exception as e:
            logger.warning("inbox_process_error source=%s err=%s", msg.source, e)
            # Finalize the handler so the TUI spinner stops — without
            # this the chat view stays in a permanent loading state.
            if handler is not None:
                try:
                    handler.finalize_current()
                except Exception:
                    pass
            # Only rollback on content-policy / bad-request errors
            # where the conversation itself is the problem.  Transient
            # errors (connection, timeout, rate-limit, server errors)
            # keep the conversation intact so typing "go" continues
            # with full context.
            if conversation is not None and isinstance(
                e, (BadRequestError, ContentFilterFinishReasonError),
            ):
                try:
                    conversation.pop_last_turn()
                    # The rejected turn was rolled back — the finally must NOT
                    # re-save it.  The backward text match in save_to_memory
                    # would otherwise match an earlier turn with identical text
                    # (heartbeat content is constant) and duplicate it as a
                    # fresh diary row.
                    rolled_back = True
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
            # ★ Persist turn unconditionally — even on cancel, error,
            # or max-iterations.  Preserves everything that was produced
            # so far so the conversation and images are never lost.  The
            # one exception: a content-policy / bad-request rollback, whose
            # rejected turn must not be saved (re-saving would also match an
            # earlier identical turn and duplicate it).
            if self._on_turn_complete and conversation is not None and not rolled_back:
                try:
                    token_count = 0
                    if result is not None and hasattr(result, "usage"):
                        token_count = result.usage.total_tokens
                    await self._on_turn_complete(
                        user_message=msg.content,
                        token_count=token_count,
                        conversation=conversation,
                        channel=str(msg.source),
                        images=getattr(msg, "images", None),
                        # The user-input timestamp captured by the TUI
                        # handler — becomes the diary created_at so restore
                        # shows the same time as the live display.  Absent
                        # for non-TUI handlers (None → store uses now).
                        created_at=getattr(msg.handler, "_timestamp", None),
                        # The handler receives the completion time so the
                        # live assistant message matches completed_at.
                        handler=msg.handler,
                    )
                except Exception:
                    logger.warning("on_turn_complete_error", exc_info=True)

            # Return to idle
            self._processing = False
            self._current_corr = None
            if self._a2a_client:
                await self._a2a_client.update_status("idle")

    async def _publish_reply(
        self, reply_to: str, corr_id: str | None, result,
    ) -> None:
        """Publish a task result back to the requester."""
        import json as _json

        # Callers guarantee _a2a_client is set before invoking
        assert self._a2a_client is not None

        text = result.text if hasattr(result, "text") else str(result)
        payload = _json.dumps({
            "correlation_id": corr_id or "",
            "result": text,
        })
        await self._a2a_client.publish_message(reply_to, payload, qos=1)


class ConversationStore:
    """Manages per-source-agent conversations.

    The human's conversation persists across messages (so the operator
    has a continuous back-and-forth).  Remote agent conversations are
    fresh each time (one-shot task model).
    """

    def __init__(self, system_prompt: str):
        self._system_prompt = system_prompt
        self._convs: dict[AgentName, Conversation] = {}
        self._handler_factories: dict[AgentName, "AgentEventHandler | None"] = {}
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
        self, source: AgentName, handler: "AgentEventHandler | None",
    ) -> None:
        """Register a handler (or None) for a specific source agent.

        The human agent gets a TUIHandler (streams to chat); remote
        agents get ``None`` (no UI streaming, just the final result).
        """
        self._handler_factories[source] = handler

    def handler_for(self, source: AgentName) -> "AgentEventHandler | None":
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

    def get_or_create(self, source: AgentName) -> Conversation:
        """Get or create a conversation for *source*.

        Human (TUI) and WeChat conversations are persistent so the
        operator has a continuous back-and-forth.  Remote agent
        conversations are fresh each message (one-shot).
        """
        from slife.a2a.identity import HUMAN, WECHAT
        from slife.subagent.identity import SUBAGENT

        if source in (HUMAN, WECHAT, SUBAGENT):
            # Persistent conversation for human / WeChat / subagent sources.
            # SUBAGENT shares the HUMAN conversation so the user sees
            # subagent results inline — but the diary records channel
            # as "subagent" for audit/memory_search distinguishability.
            conv_source = HUMAN if source == SUBAGENT else source
            if conv_source not in self._convs:
                self._convs[conv_source] = Conversation(
                    system_prompt=self._system_prompt,
                )
            return self._convs[conv_source]

        # One-shot conversation for remote agents
        return Conversation(system_prompt=self._system_prompt)

    def update_system_prompt(self, new_prompt: str) -> None:
        """Rebuild the system prompt for existing persistent conversations and
        for ones created later.

        Called after a model switch: without it, the persistent WeChat
        conversation (and any conversation created after the switch) keeps
        running on the old model's system prompt (stale model name, vision
        flag, context window, A2A config).
        """
        self._system_prompt = new_prompt
        for conv in self._convs.values():
            if conv.messages and conv.messages[0]["role"] == "system":
                conv.messages[0]["content"] = new_prompt

    def clear(self, source: AgentName) -> None:
        """Clear conversation history for *source*."""
        if source in self._convs:
            self._convs[source].clear()
            del self._convs[source]
