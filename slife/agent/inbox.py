"""Inbox — unified message entry point for all agents.

Human keyboard, MQTT tasks, CLI — every message from every agent
flows through the same queue and the same processing pipeline.
The channel (TUI / MQTT / …) only affects *display* and *reply routing*,
not *processing* logic.
"""

from __future__ import annotations

import asyncio
import json as _json
import logging
from typing import TYPE_CHECKING

from slife.a2a.identity import AgentName, AgentMessage
from slife.agent.message_history import MessageHistory

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from slife.agent.loop import AgentLoop, AgentEventHandler

logger = logging.getLogger(__name__)


def _is_bad_request(exc: BaseException) -> bool:
    """True if *exc* is a provider bad-request rejection: the provider read
    the payload and refused it.

    Both SDK generations (openai, anthropic — the latter also covering
    Bailian/Qwen via the Anthropic-compatible endpoint) raise an API-status
    error with ``status_code == 400`` for malformed-request rejects and for
    content-filter rejects; matching the status code instead of the SDK's
    class names keeps this provider-agnostic (a hard ``openai`` import here
    both pulled in the package for a match and missed the anthropic backends
    entirely).  4xx auth errors (401/403) are deliberately NOT included — the
    turn is valid, the credentials are the problem.

    This is the class that costs an **attachment**, never the turn: something
    in the payload was refused, and the attachment is the one part of it we
    can drop without losing what the user said.  Whether the turn goes too is
    :func:`_is_content_filter`'s question, and only that one.
    """
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and status == 400:
        return True
    # openai's client-side content-filter signal (no HTTP round-trip).
    return type(exc).__name__ == "ContentFilterFinishReasonError"


def _error_reason(exc: BaseException) -> str:
    """``error (400 invalid_request_error)`` — the stop token for a dead turn.

    Structured fields only: the HTTP status and the provider's error code when
    the SDK exposes them (the same two fields the classifiers above read),
    else a class name — one hop down the cause chain first, so the retry
    ladder's wrapper reports the transport failure it wrapped
    (``RemoteProtocolError``) rather than its own generic ``RuntimeError``.

    A **content-filter** reject is not among the failures this can label: that
    turn is rolled back and never saved (:func:`_is_content_filter`), so no
    closing line is written for it at all.

    The provider's *message* never rides along: this token lands in the LLM's
    context and in the diary, and error text can echo the request (keys
    included).  The code itself is scrubbed and bounded for the same reason —
    it comes off the wire.
    """
    from slife.logfmt import sanitize_secrets

    parts: list[str] = []
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        parts.append(str(status))
    code = getattr(exc, "code", None)
    if not isinstance(code, str) or not code:
        body = getattr(exc, "body", None)
        if isinstance(body, dict):
            err = body.get("error")
            err = err if isinstance(err, dict) else body
            raw = err.get("code") or err.get("type")
            code = raw if isinstance(raw, str) else ""
    if code:
        parts.append(code)
    if not parts:
        cause = exc.__cause__ or exc.__context__
        parts.append(type(cause).__name__ if cause is not None else type(exc).__name__)
    labelled = sanitize_secrets(" ".join(parts))[:60]
    return f"error ({' '.join(labelled.split())})"


#: Substrings that mark a rejection as a **content filter** rather than a
#: malformed request.  Providers spell it differently and share no code:
#: OpenAI/Azure report ``content_filter``, DashScope/Qwen (Bailian)
#: ``data_inspection_failed``, and Anthropic returns a plain
#: ``invalid_request_error`` whose *message* is the only place the filtering
#: policy is named.  Hence codes first, message last: an unmatched filter
#: reject is the expensive direction (the message is what the provider
#: refuses, so keeping it re-earns the rejection on every later turn), while
#: a false match costs one dropped turn.
_FILTER_MARKERS = (
    "content_filter",
    "contentfilter",
    "content_policy",
    "content policy",
    "content filtering",
    "inappropriate content",
    "data_inspection",
    "datainspection",
    "prohibited content",
    "safety_violation",
)


def _is_content_filter(exc: BaseException) -> bool:
    """True if *exc* is a **content filter** reject — the one failure that
    drops the turn.

    A filtered message is what the provider will not accept, so keeping it in
    the context only re-earns the same rejection on every later turn.  That is
    the sole exception to "a turn is saved unconditionally": every other
    rejection — a malformed part, an image the provider could not fetch or
    will not read — leaves the turn in place, because the user's message was
    not what was refused.
    """
    if type(exc).__name__ == "ContentFilterFinishReasonError":
        return True
    if not _is_bad_request(exc):
        return False
    # The code the SDK exposes, the raw response body it carries, and the
    # rendered message — providers put the signal in any of the three.
    parts = [str(getattr(exc, "code", "") or "")]
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        err = err if isinstance(err, dict) else body
        for key in ("code", "type"):
            parts.append(str(err.get(key) or ""))
    parts.append(str(exc))
    haystack = " ".join(parts).lower()
    return any(marker in haystack for marker in _FILTER_MARKERS)


class MemorySaveError(RuntimeError):
    """Raised when a completed turn could not be persisted to memory.

    Memory writes are mandatory: a turn that cannot be saved is surfaced to
    the user exactly like an LLM API error — reported, and the turn is not
    treated as a clean completion — instead of being silently dropped or
    warn-and-continued.  ``Inbox._process_one`` catches it and reports it.
    """


def _channel_persist(msg: AgentMessage) -> tuple[str, str]:
    """(``diary.channel`` identity, payload JSON) for the turn save.

    Every message carries a typed channel; the channel yields its identity
    and JSON payload.
    """
    identity, data = msg.channel.to_db()
    if not data:
        return identity, "{}"
    return identity, _json.dumps(data, ensure_ascii=False)


class Inbox:
    """Unified message inbox — every agent's input arrives here.

    Serialises concurrent messages from multiple agents: even if human
    and a remote agent send at the same time, only one AgentLoop runs
    at a time.  While the loop is running the agent card shows "busy".

    Usage::

        inbox = Inbox(agent_loop, histories)
        await inbox.post(AgentMessage(source=AgentName("human"), content="hi"))
    """

    def __init__(
        self,
        agent_loop: "AgentLoop",
        histories: "MessageHistoryStore",
        on_activity: "Callable | None" = None,
        on_turn_complete: "Callable | None" = None,
        ready: "Callable[[], Awaitable[None]] | None" = None,
    ):
        self._agent_loop = agent_loop
        self._histories = histories
        self._on_activity = on_activity  # async cb(kind, **kwargs)
        self._on_turn_complete = on_turn_complete  # async cb(user_message, token_count, context_tokens, history)
        #: Startup gate — awaited once before the first message is consumed.
        #: The service opens for input only after every plugin spawn
        #: converged, so user input can never race ahead of core services.
        #: ``None`` (no gate) preserves the old behaviour for standalone use.
        self._ready = ready
        #: Bounded inbox: a message burst (WeChat/A2A flood while a long turn
        #: runs, or while frozen) must not grow memory without limit.  Beyond
        #: the cap new messages are dropped and logged; the presence deque
        #: already has an analogous 1000-entry bound.
        self._queue: asyncio.Queue[AgentMessage] = asyncio.Queue(
            maxsize=1000,
        )
        self._runner_task: asyncio.Task | None = None
        self._processing: bool = False
        #: correlation_id of the message currently being processed (a
        #: subagent worker's task), used by :meth:`cancel_correlation`.
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
        """Cancel the worker task carrying *corr_id* — the parent's control
        signal (``worker/cancel``), Esc-equivalent.

        Drops the message if it is still queued (never runs); otherwise, if
        it is the message currently being processed, stops the running agent
        loop at the next safe point — the same mechanism as the TUI Esc
        binding.  Unknown corr_ids are a no-op.

        A *peer's* A2A cancel does NOT come through here.  Corr-matching can
        tell which turn a task's message *started*, but not whether the model
        is still working on that task — a task may take many turns, and
        cut-in can have moved the running turn on to other input — so the
        abort would be aimed by an identity that has already drifted.  The
        withdrawal is therefore delivered to the model as an inbound
        ``cancel_task`` message and the model, which does know, decides.  A
        worker is the opposite case: one task per turn, so the parent's
        cancel has an exact target.  See ``slife.a2a.mesh.on_peer_cancel``.
        """
        if not corr_id:
            return
        # Remove a queued-but-not-yet-started message with this corr_id.
        if self.drop_queued(corr_id):
            logger.info("inbox_queued_task_cancelled corr_id=%s", corr_id)
        # Stop the loop if this corr_id is the message being processed now.
        if self._current_corr == corr_id:
            logger.info("inbox_active_task_cancelled corr_id=%s", corr_id)
            self.cancel()

    def drop_queued(self, corr_id: str) -> bool:
        """Drop a not-yet-started message with *corr_id*; True when one went.

        Drain-rebuild (the survivors keep their FIFO order).  This is the
        narrow, unambiguous half of cancellation — the message never ran, so
        no judgment about "what is the agent doing" is involved.  A message
        already being processed is deliberately NOT touched; the caller
        decides what a *running* turn means for it (the A2A peer cancel hands
        that to the model instead — see :meth:`cancel_correlation`).
        """
        if not corr_id or self._queue.empty():
            return False
        dropped = False
        rest: list[AgentMessage] = []
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if item.correlation_id == corr_id:
                dropped = True
                continue
            rest.append(item)
        for item in rest:
            self._queue.put_nowait(item)
        return dropped

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
        """Drop a message into the inbox.  Non-blocking, never raises.

        Never waits on a full queue: a flood while the inbox is otherwise
        saturated drops the oldest-aggressor messages with a warning rather
        than wedging the caller (a WeChat poll loop must never block on the
        inbox).
        """
        try:
            self._queue.put_nowait(msg)
        except asyncio.QueueFull:
            logger.warning(
                "inbox_overflow_dropped source=%s pending=%d content=%.80s",
                msg.source, self._queue.qsize(), msg.content,
            )
        logger.debug(
            "inbox_post source=%s content=%.80s", msg.source, msg.content,
        )

    def has_queued(self, predicate: "Callable[[AgentMessage], bool]") -> bool:
        """True if any *queued* (not yet processed) message satisfies
        *predicate*.  Non-destructive — the scheduler uses it to decide
        whether a pending-fire guard can be dropped without re-firing.

        Peek the underlying deque directly (the queue's public API offers
        no non-destructive scan); the attribute is stable in every
        supported asyncio version.
        """
        deque = getattr(self._queue, "_queue", (), )
        return any(predicate(m) for m in deque)

    # ── Mid-turn input injection (cut-in mode) ────────────────────────

    def has_injectable(self) -> bool:
        """True if any queued message may cut into the running turn.

        Cut-in mode makes *any* queued message injectable ("push all") — the
        loop's iteration-boundary check asks, extracts one, and the model
        decides what to do with it.  The gate lives on the loop
        (``cutin_enabled``), not here: in queue mode this is never consulted.
        Non-destructive.
        """
        return not self._queue.empty()

    def extract_injectable(self) -> "AgentMessage | None":
        """Pull the FIRST queued message out for mid-turn injection.

        Drain-rebuild (same shape as :meth:`cancel_correlation`), preserving
        the survivors' FIFO order — pinned by the cancel-correlation ordering
        tests.  The picked message is consumed once and never runs as its own
        turn; if the model ignores it, sender-side timeout+degrade is the
        backstop.  Returns ``None`` when the queue is empty.
        """
        if self._queue.empty():
            return None
        picked = self._queue.get_nowait()
        rest: list[AgentMessage] = []
        while not self._queue.empty():
            rest.append(self._queue.get_nowait())
        for item in rest:
            self._queue.put_nowait(item)
        return picked

    # ── Run ───────────────────────────────────────────────────────────

    async def run(self) -> None:
        """Process messages forever.  Call as a background task.

        Await the startup gate (when one is wired) before consuming — the
        service must not process any turn until every plugin spawn has
        converged.  Posts made before the gate queue as usual.
        """
        if self._ready is not None:
            await self._ready()
        logger.info("inbox_start")
        while True:
            # Notify TUI that processing completed so the status bar
            # can clear the "⏳ processing" indicator.  Emitted before the
            # frozen check so a dropped message can't leave the status bar
            # stuck "processing" (a busy event for a task that never ran).
            if self._on_activity:
                try:
                    await self._on_activity("idle")
                except Exception:
                    pass
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

    async def _process_one(self, msg: AgentMessage) -> None:
        """Process a single message through the agent loop."""
        from slife.a2a.identity import HEARTBEAT, HUMAN, SYSTEM, WECHAT
        from slife.subagent.identity import SUBAGENT

        # Announce the correlation id BEFORE the first await so a cancel
        # that races the notification window is not lost: cancel_correlation
        # re-checks _current_corr, which would otherwise still be the previous
        # message's (or None) while this one is being announced.
        self._current_corr = msg.correlation_id or None
        self._processing = True

        # Heartbeat / system (schedule triggers) are internal (not peer
        # terminals) — no A2A busy status or task_received/completed TUI noise
        # for autonomous turns.
        is_remote = msg.source not in (HUMAN, WECHAT, SUBAGENT, HEARTBEAT, SYSTEM)
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

        # Notify TUI of subagent completions so they get the same
        # `⚙️ subagent> ` user bubble that session restore shows (the
        # channel="subagent" turns restore to that prefix).  Live and
        # restore must agree on how a local worker completion reads.
        if msg.source == SUBAGENT and self._on_activity:
            try:
                # Thread the worker name so the live bubble renders
                # ``Subagent(<name>)> `` and agrees with restore.
                await self._on_activity(
                    "subagent_message",
                    content=msg.content,
                    name=(msg.channel.data.get("name", "")
                          if msg.channel and msg.channel.kind == "subagent"
                          else ""),
                )
            except Exception:
                pass

        # Notify the TUI so the status bar shows "⏳ processing" for ANY turn
        # (including autonomous heartbeat turns, whose silent handler never
        # fires on_token_usage to refresh the status bar).
        if self._on_activity:
            try:
                await self._on_activity("busy")
            except Exception:
                pass

        history = None
        handler = None
        result = None
        rolled_back = False
        #: Why this turn ended early, for the save point's closing line
        #: (``esc`` / ``max_iterations`` from the loop, ``error (<Type>)``
        #: here).  Empty on a turn that ended by itself.
        stop_reason = ""
        try:
            # Reset cancel state for the new message
            self._agent_loop.reset_cancel()

            # Get or create history for this source
            history = self._histories.get_or_create(msg.source)

            # Build a handler appropriate for the source
            # Prefer the handler attached to the message (TUI path).
            # Fall back to the per-source registry / default factory
            # (remote A2A messages that don't carry their own handler).
            handler = msg.handler
            if handler is None:
                handler = self._histories.handler_for(msg.source)

            # Run the agent loop — cancelled / max-iterations are now
            # returned as AgentResult(cancelled=True) with accumulated
            # usage, not raised as exceptions.
            result = await self._agent_loop.run(
                user_input=msg.content,
                history=history,
                images=msg.images if msg.images else None,
                handler=handler,
            )

            if result.cancelled:
                # The loop is the only party that knows why it stopped.
                stop_reason = result.stop_reason
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

            # Route reply to originating channel (WeChat, etc.).  Pass the
            # cancelled flag so the channel can signal cancellation to the
            # sender ; callbacks with the older text-only
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
            # The reason the turn died: status + provider code when the SDK
            # has them, else a class name.  Never the message — the closing
            # line lands in the LLM's context and the diary, and error text
            # can carry secrets.
            stop_reason = _error_reason(e)
            # Finalize the handler so the TUI spinner stops — without
            # this the chat view stays in a permanent loading state.
            if handler is not None:
                try:
                    handler.finalize_current()
                except Exception:
                    pass
            # A rejected request costs whatever the provider refused of it.
            # Attachments first: a block rides in the session only, so it is
            # re-sent with every later request and rejected the same way —
            # one failed attach, then a session that errored on every turn
            # until a restart.
            #
            # The turn goes too in exactly one case: a *content filter*
            # reject, where the message itself is what the provider will not
            # accept and keeping it would re-earn the rejection forever.  Any
            # other rejection is a malformed *request*, not a bad history —
            # the user's message stands, so the turn is kept and saved.
            # Transient failures (connection, timeout, rate-limit, server
            # errors) cost nothing at all: they say nothing about the
            # payload, so the context is left as it is and typing "go"
            # continues with it.
            images_dropped = 0
            if history is not None and _is_bad_request(e):
                try:
                    images_dropped = self._agent_loop.forget_images(history)
                except Exception:
                    pass
                if _is_content_filter(e):
                    try:
                        history.pop_last_turn()
                        # The rejected turn was rolled back — the finally must
                        # NOT re-save it.  The backward text match in
                        # save_to_memory would otherwise match an earlier turn
                        # with identical text (heartbeat content is constant)
                        # and duplicate it as a fresh diary row.
                        rolled_back = True
                    except Exception:
                        pass
            # Notify TUI so the user sees the error in chat.  ``dropped``
            # carries the rollback verdict explicitly (never re-derived from
            # the error text): a rolled-back turn is GONE from the context, so
            # no later turn will see the message.  That is a different fact
            # from "the call failed" — the obvious reading of a bare error is
            # "send it again", which is exactly what does not apply here.
            if self._on_activity:
                try:
                    await self._on_activity(
                        "loop_error",
                        source=msg.source,
                        error=str(e),
                        dropped=rolled_back,
                        images_dropped=images_dropped,
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
                        # The turn FAILED — the TUI's turn-end line must not
                        # read as a success.  Explicit, never re-derived from
                        # ``result``: a normal reply may legitimately start
                        # with "Error:".
                        error=True,
                    )
                except Exception:
                    pass
            # Notify channel on error
            if msg.on_reply is not None:
                try:
                    await msg.on_reply(f"Error: {e}")
                except Exception:
                    pass
        finally:
            # ★ Persist turn unconditionally — even on cancel, error,
            # or max-iterations.  Preserves everything that was produced so
            # far.  The one exception: a **content filter** reject, whose
            # message the provider will not accept, so the turn must not be
            # saved (re-saving would also match an earlier identical turn and
            # duplicate it).  A malformed *request* is not that: the turn is
            # saved and only the attachment it was rejected for is dropped.
            if self._on_turn_complete and history is not None and not rolled_back:
                try:
                    token_count = 0
                    if result is not None and hasattr(result, "usage"):
                        token_count = result.usage.total_tokens
                    # The last API call's prompt + completion tokens = the exact
                    # token count of the persisted history as the next request
                    # would re-send it (per-history, from the loop's usage
                    # cache).  Persisted so restore can prime the _turn_prompt
                    # with the real exit-time occupancy instead of an
                    # estimate.  Absent on a cancel-without-API → 0 (restore
                    # falls back to the token estimate).
                    usage = self._agent_loop._usage_by_history.get(id(history))
                    context_tokens = (
                        usage.prompt_tokens + usage.completion_tokens
                        if usage else 0
                    )
                    channel_identity, channel_data = _channel_persist(msg)
                    await self._on_turn_complete(
                        user_message=msg.content,
                        token_count=token_count,
                        context_tokens=context_tokens,
                        history=history,
                        channel=channel_identity,
                        channel_data=channel_data,
                        # Why the turn ended early (empty on a clean turn) —
                        # the save point's closing line names it.
                        stop_reason=stop_reason,
                        # The user-input timestamp captured by the TUI
                        # handler — becomes the diary created_at so restore
                        # shows the same time as the live display.  Absent
                        # for non-TUI handlers (None → store uses now).
                        created_at=getattr(msg.handler, "_timestamp", None),
                        # The handler receives the completion time so the
                        # live assistant message matches completed_at.
                        handler=msg.handler,
                    )
                except MemorySaveError as e:
                    # Memory writes are mandatory: a save failure is surfaced
                    # exactly like an LLM API error — a red error line — and
                    # the turn is NOT treated as a clean completion.  The
                    # inbox is not frozen here (transient — memdb may be mid
                    # watchdog-restart; a later turn saves once the channel
                    # returns).  The history stays intact: the turn content is
                    # valid, only its persistence failed.
                    #
                    # The channel is NOT re-replied: the run itself succeeded
                    # and its result was already routed to the channel above —
                    # a second on_reply(error) would confuse a remote peer
                    # whose task already resolved.
                    logger.warning(
                        "turn_save_failed source=%s err=%s", msg.source, e,
                    )
                    if handler is not None:
                        try:
                            handler.finalize_current()
                        except Exception:
                            pass
                    if self._on_activity:
                        try:
                            await self._on_activity(
                                "loop_error", source=msg.source, error=str(e),
                            )
                        except Exception:
                            pass
                except Exception:
                    logger.warning("on_turn_complete_error", exc_info=True)

            # Return to idle
            self._processing = False
            self._current_corr = None


class MessageHistoryStore:
    """Manages the main agent's single shared history.

    The main agent has ONE context: every inbox message, whatever its
    source, is a user message into that one history.  The source is
    metadata on the user message (the diary's ``channel`` column and
    the TUI prefix), never a selector for a different context.
    """

    def __init__(self, system_prompt: str):
        self._system_prompt = system_prompt
        self._by_source: dict[AgentName, MessageHistory] = {}
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

    def get_or_create(self, source: AgentName) -> MessageHistory:
        """Get the main agent's shared history.

        The main agent has ONE context: every message that enters the
        inbox — from the human TUI, WeChat, a heartbeat/autonomous
        trigger, a scheduled run, or a subagent completion — is a user
        message into the same history.  *source* only labels who sent
        it (the diary's ``channel`` column and the TUI prefix); it never
        selects a different context.
        """
        from slife.a2a.identity import HUMAN

        base = self._by_source.get(HUMAN)
        if base is None:
            base = MessageHistory(
                system_prompt=self._system_prompt,
            )
            self._by_source[HUMAN] = base
        return base

    def update_system_prompt(self, new_prompt: str) -> None:
        """Rebuild the system prompt of the single shared history.

        Called after a model switch: without it, the main context keeps
        running on the old model's system prompt (stale model name, vision
        flag, context window, A2A config).
        """
        from slife.a2a.identity import HUMAN

        self._system_prompt = new_prompt
        base = self._by_source.get(HUMAN)
        if base is not None and base.messages and base.messages[0]["role"] == "system":
            base.messages[0]["content"] = new_prompt

    def clear(self) -> None:
        """Clear the main agent's in-memory context — the whole of it.

        The main agent has a single shared history, so there is no
        per-source clearing.  This empties the in-memory conversation
        only (persisted turns in memory are untouched) and resets the
        bound ``self.message_history`` object in place.
        """
        from slife.a2a.identity import HUMAN

        base = self._by_source.get(HUMAN)
        if base is not None:
            base.clear()


class WorkerHistoryStore(MessageHistoryStore):
    """A worker's histories: one fresh history per task, nothing carried over.

    A subagent's turns are ephemeral by design (``DESIGN.md`` §6): each task
    runs on its own context, seeded — when the parent sent a clone — from the
    parent's history as it stood at spawn.  The seed is read through
    *context_provider* at creation time, so a clone that arrives before the
    first task is the seed, and one that arrives later cannot retroactively
    rewrite a history already in flight.

    It lives beside the main agent's store, and ``AgentService`` picks between
    the two from its role (``slife/agent/roles.py``).  It used to live in the
    worker's boot, which reached into ``inbox._histories`` to replace what the
    service had just built — one of the places where a worker's differences
    were applied *outside* the service that owns them.
    """

    def __init__(
        self,
        system_prompt: str,
        context_provider: "Callable[[], list[dict] | None]",
    ):
        super().__init__(system_prompt)
        self._context_provider = context_provider

    def get_or_create(self, source: AgentName) -> MessageHistory:
        """A fresh history for this task — the cloned context, or an empty one."""
        messages = self._context_provider()
        if messages:
            return MessageHistory.from_history(self._system_prompt, messages)
        return MessageHistory(system_prompt=self._system_prompt)
