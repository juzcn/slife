"""Function-calling agent loop with real-time streaming and thinking support."""

import asyncio
import importlib
import itertools
import json
import logging
import os
import time as _time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, AbstractSet, Protocol

from cachetools import FIFOCache

from slife.agent.llm_client import LLMClient, TokenUsage
from slife.logfmt import sanitize_secrets
from slife.agent.message_history import MessageHistory
from slife.platform import detect_current_shell
from slife.tools.registry import ToolRegistry
from slife.logfmt import format_turn_ts, request_scope, elapsed
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

if TYPE_CHECKING:
    from slife.tools.catalog_service import ToolCatalogService

logger = logging.getLogger(__name__)


def _function_from_schema(name: str, schema_json: str | None) -> dict | None:
    """An OpenAI function definition from the catalog's ``schema`` column.

    The column stores the compact tool descriptor ``{name, description,
    inputSchema}``.  Returns None when absent/unparseable so the caller falls
    back to the materialized instance.

    The caller's *name* is the REGISTRY key: for external mcp/rest-api rows
    that is ``{server}__{tool}``, while the descriptor's ``name`` is the bare
    server-side tool name.  The registry key MUST win — the injected function
    name is what the model calls and what ``registry.execute`` resolves.  A
    bare descriptor name would make every injected external call fail with
    ``Unknown tool '<bare>'``.
    """
    if not schema_json:
        return None
    try:
        desc = json.loads(schema_json)
    except (ValueError, TypeError):
        return None
    if not isinstance(desc, dict):
        return None
    return {
        "type": "function",
        "function": {
            "name": name or desc.get("name"),
            "description": desc.get("description") or "",
            "parameters": desc.get("inputSchema")
                          or {"type": "object", "properties": {}},
        },
    }


class AgentCancelled(Exception):
    """Raised when the agent loop is cancelled by user request."""
    pass


# ── LLM stream retry ───────────────────────────────────────────────

#: Bounded retry for transient LLM transport failures (e.g. DeepSeek closing
#: the streaming connection mid-body → httpx RemoteProtocolError). The SDK's
#: built-in max_retries only covers request-establishment errors, not body-read
#: failures during stream iteration — so we retry here, at the contract layer,
#: for every turn source (main agent, subagents, heartbeat, WeChat, A2A).
#: Attempts / base delay are developer-owned (registry timeouts.stream.*).

#: Caps on the per-session caches.  Heartbeat / A2A one-shot histories add
#: a usage entry keyed by ``id(history)`` every turn and the context-date
#: list grows per turn until a trim consumes it — without these bounds a long
#: session (or a huge context window that never trims) grows them forever.
_MAX_USAGE_CACHE = 1000
_MAX_CONTEXT_DATES = 5000

#: Inactivity watchdog on LLM streaming: a stream that produces no chunk
#: for this many seconds is declared "stalled" — the provider accepted the
#: request (``200 OK``) then went silent (Bailian served the weather turn
#: exactly like this: zero bytes for ~7 min before dropping the
#: connection).  Unlike ``stream_timeout`` — a *total* wall-clock cap on
#: one stream call — this resets on every chunk, so a slow-but-live
#: generation is never cut; only a truly dead stream is.  A stall routes
#: through the same retry ladder as any other transient transport failure.
#: Value is developer-owned (registry timeouts.work.stall).


class StreamStallError(TimeoutError):
    """An LLM stream produced no chunk for the inactivity deadline.

    Raised by the contract layer's read loop — a provider that answers
    ``200 OK`` and then sends nothing is indistinguishable from a slow
    generation anywhere higher up, so the stall is enforced with a
    per-chunk timeout (reset on every received chunk), never a total cap.
    Carries an actionable message: the deep ``ReadError`` chains from some
    gateways stringify to ``""``, which would otherwise surface to the
    user as an empty ``Error: ``.
    """

    def __init__(self, timeout: float):
        self.stall_timeout = timeout
        super().__init__(f"LLM stream stalled — no data for {timeout:g}s")


def _is_retryable_stream_error(exc: BaseException) -> bool:
    """True if *exc* is a transient LLM transport failure worth retrying.

    The ``*TransportError`` base covers ``RemoteProtocolError`` (peer closed
    the connection before the chunked body completed), ``ReadError``,
    ``ConnectError`` and the timeout classes.  Two HTTP generations coexist
    in the runtime — the classic ``httpx``/``httpcore`` stack and the
    ``httpx2``/``httpcore2`` fork the current anthropic / openai / mcp SDKs
    are built on — and a provider that drops the streamed connection raises
    a transport error from *either* generation (both have ``ReadError``
    variants that stringify to ``""``), so both must be classified here.
    ``httpcore2`` names its transport base ``NetworkError``, not
    ``TransportError``.  The SDKs' ``*APIConnectionError`` /
    ``*APITimeoutError`` wrap the same transport failures at request time
    and are also retried.  ``StreamStallError`` (a silent provider,
    enforced by the loop's inactivity watchdog) is retried too.
    Bad-request, content-filter and auth errors are NOT retried here — they
    are the SDK's / inbox's concern, and 429/5xx are already retried by the
    SDK internally.
    """
    if isinstance(exc, StreamStallError):
        return True
    for _lib, _base in (
        ("httpx2", "TransportError"),
        ("httpcore2", "NetworkError"),
        ("httpx", "TransportError"),
        ("httpcore", "NetworkError"),
    ):
        try:
            _mod = importlib.import_module(_lib)
        except ImportError:
            continue
        _exc_type = getattr(_mod, _base, None)
        if _exc_type is not None and isinstance(exc, _exc_type):
            return True
    try:
        import openai
    except ImportError:
        openai = None
    if openai is not None and isinstance(
        exc, (openai.APIConnectionError, openai.APITimeoutError),
    ):
        return True
    try:
        import anthropic
    except ImportError:
        anthropic = None
    if anthropic is not None and isinstance(
        exc, (anthropic.APIConnectionError, anthropic.APITimeoutError),
    ):
        return True
    return False


# ── Types ──────────────────────────────────────────────────────────


@dataclass
class ToolCallInfo:
    """Information about a single tool call from the LLM."""

    id: str
    name: str
    arguments: dict
    args_truncated: bool = False


@dataclass
class AgentResult:
    """Result of running the agent loop."""

    text: str
    usage: TokenUsage
    cancelled: bool = False


class MaxIterationsExceeded(Exception):
    """Raised when the agent loop exceeds the configured iteration limit."""

    def __init__(self, iterations: int):
        self.iterations = iterations
        super().__init__(f"Agent exceeded maximum of {iterations} iterations")


class AgentEventHandler(Protocol):
    """Protocol for handling agent events during streaming.

    Implementations (e.g. a TUI) receive real-time callbacks
    as thinking, text, tool calls, and token usage are produced.
    """

    async def on_thinking_chunk(self, chunk: str) -> None:
        """Called with each reasoning/thinking token as it arrives."""
        ...

    async def on_text_chunk(self, chunk: str) -> None:
        """Called with each text token as it arrives from the LLM."""
        ...

    async def on_tool_call(
        self, tool_call: ToolCallInfo, iteration: int = 0, max_iterations: int = 30
    ) -> None:
        """Called before a tool is executed.

        iteration: 1-based current iteration number.
        max_iterations: configured maximum iterations.
        """
        ...

    async def on_tool_approval(self, tool_call: ToolCallInfo) -> bool:
        """Called before executing a tool that requires user approval.

        Return True to proceed with execution, False to deny.
        Default implementation approves everything — handlers that
        don't implement this method will auto-approve.
        """
        return True

    async def on_tool_result(
        self, tool_call_id: str, result: str, is_error: bool
    ) -> None:
        """Called after a tool finishes executing."""
        ...

    async def on_token_usage(self, usage: TokenUsage) -> None:
        """Called with cumulative token usage after each LLM call."""
        ...

    async def on_stream_retry(self) -> None:
        """Discard any partial text/thinking shown for a retried LLM request.

        The agent loop retries transient transport failures (connection drop
        mid-stream). When partial output was already streamed, the handler
        resets it so the retried request starts visually clean — otherwise
        the user sees the partial output duplicated. Default is a no-op.
        """
        ...

    async def on_max_iterations(self, iterations: int) -> None:
        """Called when the agent loop hits the configured iteration limit.

        The turn still completes as a cancelled turn (persistence-wise),
        but the handler can surface the limit to the user. Default is a
        no-op.
        """
        ...

    def finalize_current(self) -> None:
        """Mark the current (last incomplete) assistant message as complete.

        Called on error/cancel to ensure the TUI spinner stops and the
        chat view does not stay in a permanent loading state.
        """
        ...


# ── Stream accumulator ─────────────────────────────────────────────


@dataclass
class _StreamResult:
    """Accumulated result from processing a single streaming response."""

    content: str
    thinking: str
    usage: TokenUsage
    tool_accum: dict[int, dict]  # index → partial tool call info


# ── Agent loop ─────────────────────────────────────────────────────


class AgentLoop:
    """Core function-calling agent loop with real-time streaming.

    The loop:
      1. Sends history + tools to the LLM via streaming API
      2. Emits thinking and text chunks via callbacks in real-time
      3. Accumulates tool call deltas; if the model requests tools,
         executes them and loops back
      4. If the LLM returns text (no tool calls), returns the final text

    Tracks cumulative token usage across all API calls in the loop.
    """

    def __init__(
        self,
        llm_client: LLMClient,
        tool_registry: ToolRegistry,
        max_iterations: int = 30,
        max_tool_result_chars: int = 0,
        tool_timeout: float | None = None,  # None = registry work.tool_budget
        context_window: int = 0,
        context_ceiling: float = 0.8,
        context_floor: float = 0.2,
        supports_vision: bool = False,
        model_name: str = "",
        input_modalities: str = "",
        presence_provider: Callable[[], list[tuple[float, str]]] | None = None,
        schedule_provider: Callable[[], list[dict]] | None = None,
        a2a_stale_provider: Callable[[], list[dict]] | None = None,
        cutin_enabled: bool = True,
        pending_input_has: "Callable[[], bool] | None" = None,
        drop_context_turns: Callable[[list[int]], Awaitable[bool]] | None = None,
        set_context_turns: Callable[[list[int]], Awaitable[bool]] | None = None,
        clear_context_turns: Callable[[], Awaitable[bool]] | None = None,
        recall_turns: Callable[..., Awaitable[list[int] | None]] | None = None,
        recall_available: Callable[[], bool] | None = None,
        turns_by_ids: Callable[[list[int]], Awaitable[list[dict]]] | None = None,
        rebuild_message: bool = False,
        images_by_turn: dict[int, list[dict]] | None = None,
        stream_timeout: float | None = None,
        stream_max_retries: int | None = None,
        stream_stall_timeout: float | None = None,
        tool_catalog: "ToolCatalogService | None" = None,
        load_threshold: int = 100,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        #: Shared tool catalog — the injection snapshot and the turn-boundary
        #: eviction consult it.  None (no catalog) keeps the historical
        #: all-registered injection with no eviction.
        self.tool_catalog = tool_catalog
        self.load_threshold = load_threshold if load_threshold and load_threshold > 0 else 100
        #: Tool names injectable by the NEXT LLM request (re-read before every
        #: request — see :meth:`_refresh_inject_snapshot`).
        #: None ⇒ fall back to the whole registry (no catalog).
        self._inject_snapshot: AbstractSet[str] | None = None
        self.max_iterations = max_iterations
        self.max_tool_result_chars = max_tool_result_chars
        self.tool_timeout = (
            tool_timeout
            if tool_timeout is not None
            else _timeouts.timeouts.work.tool_budget
        )
        self.context_window = context_window
        self.context_ceiling = context_ceiling
        self.context_floor = context_floor
        #: Wall-clock cap on a single LLM stream call (``None`` = unlimited).
        #: A silent provider stall would otherwise block the ``async for``
        #: forever with no exception — set for subagents so a hang becomes a
        #: catchable ``TimeoutError`` that flows back through the error path.
        self.stream_timeout = stream_timeout
        #: Retries for transient LLM stream failures.  ``None`` inherits the
        #: module default; ``0`` = fail fast (one attempt) — used by
        #: subagents, which have no user to wait on and should surface the
        #: error to the caller instead of retrying a flaky provider.
        self.stream_max_retries = (
            stream_max_retries
            if stream_max_retries is not None
            else _timeouts.timeouts.stream.retries
        )
        #: Inactivity watchdog on LLM streaming: seconds of silence before
        #: a stream is declared stalled (reset on every chunk).  ``None``
        #: inherits the module default; ``0``/negative disables.  Unlike
        #: ``stream_timeout`` this is not a total cap — a slow-but-live
        #: generation streams on, only a dead stream is cut (the TUI would
        #: otherwise sit on "processing" forever with no error visible).
        self.stream_stall_timeout = (
            _timeouts.timeouts.work.stall
            if stream_stall_timeout is None
            else stream_stall_timeout
        )
        #: Persist the live-context start boundary after a trim evicted
        #: *count* oldest turns, so a restart rebuilds the exit-time context
        #: from exactly where the live one now stands.  Best-effort: an
        #: unreachable memdb only leaves the boundary stale (restore becomes
        #: a superset, never a loss).  Wired by AgentService (bound method).
        self.drop_context_turns = drop_context_turns
        self.set_context_turns = set_context_turns
        #: Empties the persisted live-context list — the write behind an empty
        #: recall selection, whose counterpart ``set_context_turns`` refuses
        #: (its empty-list guard protects a *partial* selection; a deliberate
        #: empty one goes through the clear tool that exists for it).
        self.clear_context_turns = clear_context_turns
        self.recall_turns = recall_turns
        #: Whether the memory store can be asked at all.  Gates the
        #: discriminator so a turn is not spent calling a model to decide a
        #: recall that cannot run; ``None`` means unset, and the call is made.
        self.recall_available = recall_available
        self.turns_by_ids = turns_by_ids
        # Per-turn context rebuild (see the recall step in run()).
        self.rebuild_message = rebuild_message
        self._images_by_turn = images_by_turn if images_by_turn is not None else {}
        self.supports_vision = supports_vision
        self.model_name = model_name
        self.input_modalities = input_modalities
        #: Read-and-clear provider for pending A2A peer presence events.
        #: Injected by AgentService so the turn prompt can show what
        #: changed since the last turn.  Returns ``(epoch_seconds, text)``.
        self._presence_provider = presence_provider
        #: Provider for open failed/missed scheduled runs (turn-prompt reminder).
        #: Returns render-ready ``{name, due_at, status}`` items; the loop
        #: injects them into ``_turn_prompt`` each turn.
        self._schedule_provider = schedule_provider
        #: Provider for inbound A2A tasks orphaned by a restart.  Returns
        #: ``{task_id, peer, since}`` items — never completable, so the prompt
        #: tells the model to answer the peer with a plain message instead.
        self._a2a_stale_provider = a2a_stale_provider
        #: Mid-turn input preemption (cut-in mode): when True the loop injects
        #: a pending queued message at each iteration boundary; when False,
        #: messages wait in the queue until the turn ends (the original
        #: behavior).  Toggleable at runtime via ``set_midturn_input``.
        self.cutin_enabled = cutin_enabled
        #: Inbox hook for cut-in mode — ``has_injectable()`` bound by
        #: AgentService after the Inbox is constructed.  Left ``None``
        #: (e.g. subagents, never wired) disables injection regardless of the
        #: flag.  The extraction itself is the ``_check_new_input`` tool's
        #: job (via the ``extract_injectable`` ToolContext hook).
        self.pending_input_has = pending_input_has
        self._cancel_event = asyncio.Event()
        # Last API usage by history identity.  Every inbox message shares
        # the main agent's ONE context (human / wechat / heartbeat /
        # scheduled / subagent all flow into the same history), so this
        # holds a single live entry — the last completed API call's
        # usage, read by _turn_prompt, the TUI status bar, and the turn
        # save.  _last_usage is kept only as the restore-time estimate
        # slot (primed by restore_session).
        # FIFO eviction past _MAX_USAGE_CACHE replaces the manual
        # ``pop(next(iter(...)))`` oldest-entry drop.
        self._usage_by_history: FIFOCache[int, TokenUsage] = FIFOCache(
            maxsize=_MAX_USAGE_CACHE,
        )
        self._last_usage = TokenUsage()
        # Track stable fields — only emit in the turn prompt when they change.
        self._last_cwd: str = ""
        self._last_shell: str = ""
        self._last_model_name: str = ""
        self._last_input_modalities: str = ""
        self._context_time_start: str = ""  # earliest turn date in context; set by restore, advanced by trim
        self._last_context_time_start: str = ""  # change-detection in the turn prompt
        self._context_turn_dates: list[str] = []  # dates of restored turns, oldest-first; consumed by trim
        self._current_turn_start: str = ""  # start date of the running turn (seeds the trim-exhausted turn-prompt anchor)
        #: ``id(history)`` whose restore must not be immediately
        #: shredded by the ceiling trim.  Restore primes the history
        #: up to the ceiling; the first replacement turn would otherwise
        #: compact it straight back to the floor before the user got to
        #: use it.  Consumed on that turn (see :meth:`run`).
        self._just_restored_history: int | None = None

    def set_max_iterations(self, max_iterations: int) -> str:
        """Change the per-turn iteration cap at runtime (0 = unlimited).

        Takes effect from the next turn: the running turn's iteration
        budget was fixed when ``run()`` started, so this only affects
        subsequent ``run()`` calls.  Returns a human-readable confirmation
        or an error string.
        """
        if not isinstance(max_iterations, int) or isinstance(max_iterations, bool):
            return f"Error: max_iterations must be an integer, got {max_iterations!r}."
        if max_iterations < 0:
            return f"Error: max_iterations must be >= 0 (0 = unlimited), got {max_iterations}."
        self.max_iterations = max_iterations
        if max_iterations == 0:
            return "Max iterations set to 0 — unlimited (no cap)."
        return f"Max iterations set to {max_iterations}."

    def cancel(self) -> None:
        """Signal the agent loop to stop at the next safe point."""
        self._cancel_event.set()

    def reset_cancel(self) -> None:
        """Clear the cancel signal for the next run."""
        self._cancel_event.clear()

    def reset_context_time(self) -> None:
        """Clear the tracked context time range and the measured occupancy.

        The next turn re-seeds ``_context_time_start`` from its own start, so
        "Context covers" reflects the fresh context; forgetting the measured
        size keeps ``_turn_prompt`` / the status bar from reporting a context
        that no longer exists.  Called by the rebuild's empty selection — the
        one remaining path that empties the context outright.
        """
        self._context_time_start = ""
        self._context_turn_dates = []
        self._last_context_time_start = ""
        self._usage_by_history.clear()
        self._last_usage = TokenUsage()

    # ── Tool call helpers ──────────────────────────────────────────

    @staticmethod
    def _flag_truncated_args(tc: ToolCallInfo, result: str) -> str:
        """Prepend a visible marker when the provider truncated the tool's
        argument JSON mid-stream and the call ran with no arguments.

        The marker lands in the tool result the LLM and the UI both see —
        a log line alone would let the empty-argument rerun pass silently.
        """
        if not tc.args_truncated:
            return result
        return (
            "⚠ Provider truncated this tool call's arguments (JSON cut "
            "mid-stream) — the tool ran with NO arguments. If arguments "
            "were required, re-issue the call.\n\n" + result
        )

    @staticmethod
    def _truncate_args(args: dict, max_len: int = 80) -> dict:
        """Truncate (and mask) long argument values for readable log output.

        Tool-call arguments can carry secrets the LLM passes through — mask
        before the values reach the session log.
        """
        result = {}
        for k, v in args.items():
            s = sanitize_secrets(str(v))
            if len(s) > max_len:
                s = s[:max_len] + "…"
            result[k] = s
        return result

    @staticmethod
    def _serialize_tool_calls(tool_calls: list[ToolCallInfo]) -> list[dict]:
        """Serialize ToolCallInfo list back to OpenAI API format."""
        return [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(
                        tc.arguments, ensure_ascii=False
                    ),
                },
            }
            for tc in tool_calls
        ]

    @staticmethod
    def _build_tool_calls_from_deltas(
        accum: dict[int, dict],
    ) -> list[ToolCallInfo]:
        """Build ToolCallInfo list from accumulated streaming deltas."""
        result = []
        for idx in sorted(accum.keys()):
            acc = accum[idx]
            try:
                args = (
                    json.loads(acc["arguments"])
                    if acc["arguments"].strip()
                    else {}
                )
            except json.JSONDecodeError:
                # Truncated by max_tokens / provider mid-argument — surface it
                # in the tool result instead of silently running the tool with
                # no arguments (a destructive command would otherwise become a
                # confusing no-op, and a default-arg tool a wrong effect).
                logger.warning(
                    "tool_args_malformed id=%s name=%s raw=%.120s",
                    acc["id"], acc["name"], acc["arguments"],
                )
                args = {}
                truncated = True
            else:
                truncated = False
            result.append(
                ToolCallInfo(
                    id=acc["id"],
                    name=acc["name"],
                    arguments=args,
                    args_truncated=truncated,
                )
            )
        return result

    # ── Context trimming ────────────────────────────────────────────

    def context_tokens_for(self, history: MessageHistory) -> int:
        """Context usage reported by ``_turn_prompt`` and the TUI status bar.

        The **previous turn's** last API call's real ``prompt_tokens +
        completion_tokens`` for this history — the exact token count of the
        saved history as the next request would re-send it.  ``_turn_prompt``
        is auto-invoked before the current turn's first API call, so the
        current round's usage is unknowable by construction; the last
        completed call is the previous round's.  Single source for
        ``_turn_prompt``, the trim decision (``_trim_after_save``), and the
        TUI status bar — one value, no recompute.  Resolution order:

        1. This history's last API call's actual prompt + completion tokens
           (tracked per history, so a heartbeat's small context never
           pollutes the human history's reading).
        2. After a restore, ``_last_usage`` — primed from the latest
           restored turn's **persisted** ``context_tokens`` (the exact
           context size at exit), so a restarted slife still reports the
           previous round's value.
        3. Neither (genuinely fresh start — no previous round) → ``0``.
           Never a chars÷3 estimate presented as real usage.
        """
        usage = self._usage_by_history.get(id(history))
        if usage is None:
            usage = self._last_usage  # restored session: previous round's persisted context_tokens
        if usage.prompt_tokens or usage.completion_tokens:
            return usage.prompt_tokens + usage.completion_tokens
        if usage.total_tokens:
            return usage.total_tokens
        return 0  # fresh start — no previous round's usage yet

    @staticmethod
    def _parse_recall_args(text: str) -> dict | None:
        """Extract the discriminator's parameter object from its reply.

        The reply is asked to be a bare JSON object, but a model may wrap it in
        prose or a fenced block, so the outer ``{…}`` is taken rather than the
        whole string.  Unknown keys are dropped and only string values kept —
        the args reach ``__memory_turn_recall`` directly, bypassing the
        registry's schema validation, so this is the only gate on what a model
        can inject.
        """
        if not text:
            return None
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(text[start:end + 1])
        except Exception:
            return None
        if not isinstance(parsed, dict):
            return None
        return {
            k: v for k, v in parsed.items()
            if k in ("query", "since", "until") and isinstance(v, (str, type(None)))
        }

    async def _discriminate_recall(
        self, history: MessageHistory, user_input: str,
    ) -> dict | None:
        """One call deciding which history this turn needs; None on failure.

        What it is sent is the **system prompt plus the instruction**
        (``rebuild_messages.j2``) — not the conversation, which the selection
        does not need: the answer *overrides* the context, so a turn already
        in it is simply re-selected rather than having to be reported
        (DESIGN.md §2.3).  The instruction quotes the input so
        the model reasons about the input rather than about the conversation it
        is about to rebuild.

        Never persisted, never streamed to the TUI.  It degrades to None rather
        than retrying: a retry would double the latency of the pre-turn path
        for a call whose failure has a perfectly good fallback (keep the
        existing context).
        """
        if self.llm_client is None:
            logger.info("recall_discriminator_skipped reason=no_llm_client")
            return None
        # The selector is the memory plugin's *internal* tool, so nothing in
        # the registry can prove it is reachable — this check does.  Asking a
        # model to decide a recall that cannot run would spend a call every
        # turn for nothing, and the caller reads the skip as "keep the
        # context".
        if self.recall_available is not None and not self.recall_available():
            logger.info("recall_discriminator_skipped reason=store_unavailable")
            return None
        from slife.agent.system_prompt import build_recall_instruction

        # The call is the agent's current context with the instruction in
        # place of the user message — the discriminator judges from the
        # conversation in hand, which is what lets a follow-up's query name
        # the subject it refers to ("人工智能学院是什么时候成立的" after
        # three turns about 首经贸).  Written from the input alone the query
        # drops that subject, retrieves nothing, and — because a selection
        # *replaces* the context — leaves the turn with nothing at all.
        # ``_turn_id`` is a runtime mapping the model must not see; the
        # context is otherwise sent as it stands.
        messages = MessageHistory.strip_turn_ids(history.messages)
        messages.append(
            {"role": "user",
             "content": build_recall_instruction(user_input)},
        )
        # `msgs` / `prompt_chars` are the call's *shape*: what the
        # discriminator was actually asked with, which is not the same thing
        # as what the turn will run on.
        prompt_chars = sum(len(m.get("content") or "") for m in messages)
        logger.debug(
            "recall_discriminator_prompt msgs=%d chars=%d text=%.300s",
            len(messages), prompt_chars, sanitize_secrets(messages[-1]["content"]),
        )
        t0 = _time.monotonic()
        try:
            resp, _usage = await asyncio.wait_for(
                self.llm_client.chat(messages),
                timeout=_timeouts.timeouts.work.recall_discriminator,
            )
            text = (resp.choices[0].message.content or "") if resp else ""
        except Exception:
            logger.warning(
                "recall_discriminator_failed took_ms=%.0f",
                (_time.monotonic() - t0) * 1000, exc_info=True,
            )
            return None
        args = self._parse_recall_args(text)
        took_ms = (_time.monotonic() - t0) * 1000
        if args is None:
            # A reply that is not the requested JSON object — prose, or
            # nothing at all.  The turn keeps its context either way, so this
            # is the only trace of why the recall did not run.
            logger.warning(
                "recall_discriminator_unparsed took_ms=%.0f reply=%.200s",
                took_ms, sanitize_secrets(text),
            )
            return None
        logger.info(
            "recall_discriminated msgs=%d prompt_chars=%d took_ms=%.0f "
            "query=%.60s since=%s until=%s",
            len(messages), prompt_chars, took_ms,
            sanitize_secrets(str(args.get("query") or "")),
            args.get("since"), args.get("until"),
        )
        return args

    def forget_images(self, history: MessageHistory) -> int:
        """Drop every injected image block from *history* and from the store.

        Called when the provider rejected a request that carried attachments
        (:func:`slife.agent.inbox._is_bad_request`).  Clearing the live
        message list is not enough on its own: ``_recall_and_rebuild``
        re-attaches the blocks from ``_images_by_turn`` on the next turn, so
        the same rejection would repeat.  Both structures go, or neither
        does.

        Returns the number of blocks removed from the live history — 0 means
        the request carried none, so the images were not what was rejected
        and the store is left alone.
        """
        removed = history.strip_images()
        if removed:
            # The same dict the service holds as ``_image_by_turn`` — the
            # rebuild's source, and the only other place a block survives.
            self._images_by_turn.clear()
            logger.info("images_forgotten count=%d", removed)
        return removed

    async def _recall_and_rebuild(
        self, history: MessageHistory, user_input: str,
        handler: object | None = None,
    ) -> bool:
        """Rebuild the context from a recall selection — once per turn.

        Returns True when the context was replaced.  The outcomes, and the
        difference is the whole contract:

        * **No parameters** (``{}``) means *no recall is needed* — the
          discriminator judged that what is already in context is enough.  The
          turn runs on it as it stands (and the ceiling still bounds it); no
          store call is made.
        * **The selection** (empty or not) replaces the context.  An empty
          selection is an *answer* — no turn qualified for this turn, so the
          context is the system prompt and nothing else.
        * **No selection at all** (no reply, or the store could not be asked)
          and **a selection that cannot be fetched** leave the context
          untouched: nothing was learned about what this turn needs, and a
          guess is not an improvement on what is already there.
        """
        if not self.rebuild_message or self.recall_turns is None:
            return False

        args = await self._discriminate_recall(history, user_input)
        if args is None:
            # No reply at all — a failure the discriminator already logged.
            logger.info("recall_not_needed reason=no_discriminator_reply")
            return False
        if not any(args.values()):
            # `{}`: the context is judged sufficient.  Nothing to look up, so
            # the store is not asked and the context is not touched.
            logger.info("recall_not_needed reason=context_sufficient")
            return False

        ids = await self.recall_turns(
            str(args.get("query") or ""),
            args.get("since") or None,
            args.get("until") or None,
        )
        if ids is None:
            logger.info("recall_unavailable")
            return False

        turns = await self.turns_by_ids(ids) if (ids and self.turns_by_ids) else []
        if ids and not turns:
            logger.warning("recall_abandoned reason=unfetchable ids=%d", len(ids))
            return False

        # Rebuild before persisting: if the persist fails, the turn still runs
        # on the right context and only the *restart* path is stale — a
        # superset, which is the safe direction.
        # Whether the context held anything is read *before* the rebuild: a
        # cleared context is only news when there was something in it, and
        # announcing a clear over an already-empty one (a fresh store's first
        # turn) would report a change that did not happen.
        had_turns = len(history.messages) > 1
        history.rebuild_messages(
            turns,
            images_by_turn=self._images_by_turn,
            vision=self.supports_vision,
        )
        # Deliberately NOT resetting the cached usage: `context_tokens_for`
        # reports the previous round's real API usage and returns 0 when there
        # is none (it never presents an estimate as real usage), so clearing it
        # would make every turn prompt read 0%.  The previous round's number is
        # one turn behind and bounded by the same budget — a far better read
        # than nothing.  The trim is a consumer too: its ceiling applies in
        # this mode as well (see `_trim_after_save`).
        # "Context covers" comes from the selection now, not from an
        # incremental date list.
        stamps = [t.get("created_at") or "" for t in turns]
        stamps = [s for s in stamps if s]
        self._context_time_start = stamps[0] if stamps else ""
        self._context_turn_dates = list(stamps[1:])
        self._last_context_time_start = ""

        if not turns:
            # An empty selection is persisted by *clearing*: the restore
            # contract is the same list, and it must agree with the history
            # the turn actually ran on.
            if self.clear_context_turns is not None:
                if not await self.clear_context_turns():
                    logger.warning("recall_clear_failed")
            # The context is now the system prompt, so the *measured* size of
            # the one it replaced must go too (unlike a normal rebuild, where
            # the previous round's number is one turn behind and a far better
            # read than nothing).  Without this, `_turn_prompt` and the status
            # bar would report the pre-clear occupancy.
            self.reset_context_time()
        elif self.set_context_turns is not None:
            written = await self.set_context_turns([t["rowid"] for t in turns])
            if not written:
                logger.warning("recall_persist_failed ids=%d", len(turns))
        # Tell the human: the model's context just changed under them.
        on_rebuild = getattr(handler, "on_rebuild", None)
        if on_rebuild is not None and (turns or had_turns):
            try:
                on_rebuild(len(turns))
            except Exception:
                logger.exception("recall_notice_ui_failed")
        logger.info("recall_rebuilt turns=%d", len(turns))
        return True

    async def _trim_after_save(
        self, history: MessageHistory, handler: object | None = None,
    ) -> None:
        """Trim the oldest turns after a turn is saved to memory.

        The ceiling is the window's safety valve and applies in **both** modes
        (`rebuild_message` true or false): a turn that grew past it — tool
        results, above all — is compacted down to the floor regardless of how
        the context was chosen.  In rebuild mode the next turn's recall
        re-selects the context anyway, so the eviction is not a decision, only
        a bound.

        Called by ``save_to_memory`` once the just-completed turn is
        persisted.  By then the last API call's real prompt + completion tokens are
        known (``context_tokens_for`` reads ``_usage_by_history``), so the
        ceiling check uses the true context occupancy — not the estimate
        the loop had at the turn's start.

        *handler* (optional) receives ``on_trim(count)`` so the live TUI
        can show the trim note on the turn's last assistant message —
        mirroring the LLM-side note.

        When occupancy is at/over the ceiling, compacts the history
        down to the floor (oldest complete turns removed) and appends a
        trim note to the last assistant message so the LLM knows how many
        turns were cut from its context.  The note is
        runtime-only — never persisted, discarded on restore (a restored
        session is already the trimmed state; "a past session was
        truncated" is meaningless to the model, only the *current* cut is).

        A freshly-restored history is a legitimate pre-exit state,
        not growth — the ``_just_restored_history`` marker (consumed in
        :meth:`run`) still guards the first replacement turn.
        """
        # A freshly-restored history is a legitimate pre-exit state,
        # not growth — never shred it on the very first replacement turn.
        # Consume the marker so from the second turn on the live rules
        # apply.  (Restore primes the context up to the ceiling; the first
        # turn's save would otherwise immediately compact it to the floor.)
        just_restored = self._just_restored_history == id(history)
        if just_restored:
            self._just_restored_history = None
            return

        # The ceiling is the window's safety valve, not a function of how the
        # context is *chosen*: it applies in both modes.  In rebuild mode the
        # next turn's recall re-selects anyway, so an eviction here costs
        # nothing — while a turn whose tool results ballooned past the ceiling
        # (and, on a smaller window, past the window itself) would otherwise
        # have nothing bounding it at all.
        # Only the just-finished turn exists / nothing to trim — the loop
        # also needs a boundary to not trim a history whose context
        # usage is unmeasurable (no API call yet → estimate fallback).
        current = self.context_tokens_for(history)
        if current < int(self.context_window * self.context_ceiling):
            return

        # Compress to the floor; the current (just-saved) turn is kept by
        # extract_oldest_turns — it only ever removes complete older turns.
        target = int(self.context_window * self.context_floor)
        turns, tokens_freed = history.extract_oldest_turns(target)
        if not turns:
            return

        # Drop the evicted turns from the persisted live-context list
        # (best-effort — see drop_context_turns).  The ids are exact: each
        # turn carries its diary rowid as ``_turn_id`` on its opening user
        # message, set at save and re-stamped on restore.  A turn whose save
        # failed has no id and is simply left on the list (a superset on the
        # next restore, never a loss) — hence ``removed`` counts turns while
        # ``evicted`` counts droppable ones, and the two may differ.
        evicted = [t["turn_id"] for t in turns if t.get("turn_id") is not None]
        if evicted and self.drop_context_turns is not None:
            try:
                await self.drop_context_turns(evicted)
            except Exception:
                logger.exception(
                    "context_turns_drop_failed count=%d", len(evicted),
                )

        # Advance the tracked "Context covers" time range by the same
        # number of removed turns (each complete turn has one user msg).
        removed = len(turns)
        for _ in range(removed):
            if self._context_turn_dates:
                self._context_time_start = self._context_turn_dates.pop(0)
        if removed and not self._context_turn_dates:
            # The trim removed every tracked turn (list exhausted — e.g. a
            # fresh session whose only tracked turn was in
            # _context_time_start, or all later dates got popped).  The
            # context now starts at the current turn (the one this save
            # just finished, which extract_oldest_turns always keeps);
            # don't leave a stale "covers since …" that points at a turn
            # that is no longer in context.  Seed from the current turn's
            # actual start (recorded at run() time), not from wall-clock
            # now — the turn prompt's "covers since HH:MM" must track the turn.
            self._context_time_start = getattr(
                self, "_current_turn_start", "") or format_turn_ts()
        logger.info(
            "context_trimmed_after_save turns=%d ids=%d tokens_freed=%d time_start=%s",
            removed, len(evicted), tokens_freed, self._context_time_start,
        )

        # Tell the LLM how much of its context was just cut.  Runtime-only
        # note appended to the last assistant message (guaranteed present
        # and last by _ensure_turn_consistent in save_to_memory).
        history.append_trim_marker(removed)
        # Mirror it in the live TUI — same trim note on the turn's last
        # assistant message.
        if handler is not None:
            on_trim = getattr(handler, "on_trim", None)
            if on_trim is not None:
                try:
                    on_trim(removed)
                except Exception:
                    logger.exception("trim_marker_ui_failed")

    # ── Harness tool invocation ────────────────────────────────────

    def _turn_prompt_kwargs(self, history: MessageHistory, current: int) -> dict:
        """Build the render kwargs for the ``_turn_prompt`` status tool.

        Time + token always shown; model/CWD/shell only when they
        changed since the last turn.  *current* is the context token
        count — computed once in :meth:`run` for the prompt (the trim
        decision later uses its own reading in ``_trim_after_save``).
        The ``restarted`` flag rides the restore marker (consumed by
        ``_trim_after_save``, not here) so only the very first prompt
        after a restart reports it.
        """
        cwd_now = os.getcwd()
        shell_now = detect_current_shell()
        kwargs: dict = {
            "context_window": self.context_window,
            "last_context_tokens": current,
        }
        if self._just_restored_history == id(history):
            kwargs["restarted"] = True
        if self.model_name != self._last_model_name:
            kwargs["model_name"] = self.model_name
            kwargs["input_modalities"] = self.input_modalities
            self._last_model_name = self.model_name
        if cwd_now != self._last_cwd:
            kwargs["cwd"] = cwd_now
            self._last_cwd = cwd_now
        if shell_now != self._last_shell:
            kwargs["shell"] = shell_now
            self._last_shell = shell_now
        # Context start is reported on the first turn and then only when it
        # changes (restore sets it, trim advances it) — same change-detection
        # as model/CWD/shell.
        if self._context_time_start != self._last_context_time_start:
            kwargs["context_time_start"] = self._context_time_start
            self._last_context_time_start = self._context_time_start
        # presence_events are NOT drained here — _auto_invoke reads them only
        # when the prompt is actually recorded, so a cancelled turn doesn't lose
        # them.
        return kwargs

    async def _auto_invoke(
        self,
        name: str,
        args: dict,
        history: MessageHistory,
    ) -> None:
        """Invoke a declared harness tool on the loop's behalf.

        Records a normal ``assistant(tool_calls)`` + ``tool`` result pair,
        executing the tool **directly** — not through :meth:`_execute_tools`
        (no approval / timeout / async wrapping, and no cancel early-return
        race that could orphan the pair).  The tool is still a real
        schema-declared tool; only *who* invokes it differs from an
        LLM-requested call.
        """
        if self._cancel_event.is_set():
            return
        if name == "_turn_prompt" and self._presence_provider is not None:
            # Drain pending peer-presence events only now that the prompt will
            # actually be recorded — draining them in the _turn_prompt_kwargs args
            # expression while cancelled would lose them.
            args = dict(args)
            args["presence_events"] = self._presence_provider()
        if name == "_turn_prompt" and self._schedule_provider is not None:
            args = dict(args)
            args["schedule_status"] = self._schedule_provider()
        if name == "_turn_prompt" and self._a2a_stale_provider is not None:
            args = dict(args)
            args["a2a_stale_tasks"] = self._a2a_stale_provider()
        tool = self.tool_registry.get(name)
        if tool is None:
            logger.warning("auto_invoke_tool_missing name=%s", name)
            return
        # Harness ids strip a leading underscore if present (_turn_prompt →
        # _harness_turn_prompt_…); non-underscore tools (attach_image) keep
        # their name: _harness_attach_image_…
        stem = name[1:] if name.startswith("_") else name
        tc = ToolCallInfo(
            id=f"_harness_{stem}_{_time.time_ns():x}",
            name=name,
            arguments=args,
        )
        history.add_assistant_message(
            content=None, tool_calls=self._serialize_tool_calls([tc]),
        )
        try:
            # Run the tool against the history the loop is currently
            # processing.  `_ctx.message_history` is set once at startup to the
            # human history, but harness tools are invoked per-source
            # (WeChat / remote-agent turns have their own MessageHistory) — a
            # trim must target the active one, not always the human diary.
            # Swap for the duration of the call and restore afterwards.
            ctx = getattr(tool, "_ctx", None)
            prev_history = None
            if ctx is not None:
                prev_history = ctx.message_history
                ctx.message_history = history
            try:
                result = await tool.execute(**args)
            finally:
                if ctx is not None:
                    ctx.message_history = prev_history
        except Exception as e:
            result = f"Error: Tool '{name}' failed: {type(e).__name__}: {e}."
            logger.warning("auto_invoke_error name=%s err=%s", name, e)
        history.add_tool_result(
            tc.id, sanitize_secrets(result),
            is_error=result.startswith("Error"),
        )

    # ── Stream processing ──────────────────────────────────────────

    async def _consume_stream(
        self,
        stream_iter,
        handler: AgentEventHandler | None,
        content_parts: list[str],
        thinking_parts: list[str],
        tool_accum: dict[int, dict],
        emitted: list[bool],
    ) -> TokenUsage:
        """Consume one LLM stream, accumulating content / thinking / tools.

        Extracted so ``asyncio.wait_for`` can cap the whole iteration — a
        silent provider stall (no chunk, no error) would otherwise block
        ``async for`` forever.  ``emitted`` is a one-element holder the
        caller owns; it is set ``True`` as soon as any chunk arrives, so the
        retry path can tell "partial output was shown" even when the stream
        later raises.  Returns the accumulated usage.
        """
        stream_usage = TokenUsage()
        stall = (
            self.stream_stall_timeout
            if self.stream_stall_timeout is not None and self.stream_stall_timeout > 0
            else None
        )
        while True:
            if stall is not None:
                try:
                    async with asyncio.timeout(stall):
                        try:
                            chunk = await stream_iter.__anext__()
                        except StopAsyncIteration:
                            return stream_usage
                except TimeoutError:
                    # Provider answered but is sending nothing — a stall,
                    # not a slow generation.  The retry ladder in
                    # _process_stream treats it like any other transient
                    # failure (and its message is non-empty by contract).
                    raise StreamStallError(stall) from None
            else:
                try:
                    chunk = await stream_iter.__anext__()
                except StopAsyncIteration:
                    return stream_usage

            emitted[0] = True

            if chunk.thinking:
                thinking_parts.append(chunk.thinking)
                if handler and not self._cancel_event.is_set():
                    await handler.on_thinking_chunk(chunk.thinking)

            if chunk.content:
                content_parts.append(chunk.content)
                if handler and not self._cancel_event.is_set():
                    await handler.on_text_chunk(chunk.content)

            if chunk.tool_deltas:
                for td in chunk.tool_deltas:
                    idx = td["index"]
                    if idx not in tool_accum:
                        tool_accum[idx] = {
                            "id": "",
                            "name": "",
                            "arguments": "",
                        }
                    acc = tool_accum[idx]
                    if td["id"]:
                        acc["id"] = td["id"]
                    if td["function"]["name"]:
                        acc["name"] = td["function"]["name"]
                    if td["function"]["arguments"]:
                        acc["arguments"] += td["function"]["arguments"]

            if chunk.usage:
                stream_usage = chunk.usage

    async def _tools_for_request(self) -> list[dict]:
        """The OpenAI function list for the NEXT LLM request.

        Built from the loaded set :meth:`_refresh_inject_snapshot` just read —
        which is why a tool loaded mid-turn (``func_tool_load``) is in the very
        next request rather than the next turn.

        Schemas are read from the CATALOG DB (the ``schema`` column — the
        single source: builtin tools' descriptors are seeded from their defs at
        session start, mcp/rest-api rows from the reconcile), NOT from tool
        code or a live MCP fetch.  A row that is missing or lacks a schema
        (e.g. a meta tool not yet mirrored while its gateway is down) falls
        back to the materialized instance.  No catalog → the whole registry
        (the historical behavior).
        """
        if self.tool_catalog is None:
            return self.tool_registry.to_openai_functions(projection=self._inject_snapshot)
        if self._inject_snapshot is None:
            return self.tool_registry.to_openai_functions()
        try:
            rows = await self.tool_catalog.store.rows_for_names(self._inject_snapshot)
        except Exception:
            logger.exception("tools_schema_read_failed — falling back to instances")
            return self.tool_registry.to_openai_functions(projection=self._inject_snapshot)
        by_name = {r["name"]: r["schema"] for r in rows}
        result: list[dict] = []
        for tool in self.tool_registry.list_tools():
            if tool.name not in self._inject_snapshot:
                continue
            func = _function_from_schema(tool.name, by_name.get(tool.name))
            result.append(func if func is not None else tool.to_openai_function())
        return result

    async def _refresh_inject_snapshot(self) -> None:
        """Read the catalog's loaded set — called before EVERY LLM request.

        The tool list is per-REQUEST, not per-turn: ``func_tool_load`` (and
        ``_func_tool_unload``) mid-turn must land in the next request, which is
        the whole point of a per-tool load.  The order of the rebuilt list is
        the registry's, so a tool registered mid-turn (an mcp/rest-api proxy is
        materialized at load time) appends at the END — the request's prefix up
        to that point is unchanged, and the prompt cache survives the load.

        Best-effort: a catalog hiccup degrades to the whole registry for that
        request, so a transient db error never folds the tool list.
        """
        if self.tool_catalog is None:
            self._inject_snapshot = None
            return
        try:
            self._inject_snapshot = await self.tool_catalog.snapshot_loaded()
        except Exception:
            logger.exception("inject_snapshot_failed — injecting full registry")
            self._inject_snapshot = None

    async def _maybe_evict(self) -> None:
        """Turn-boundary threshold eviction (harness-side LRU squeeze).

        ONLY the catalog's write owner (the main agent) evicts; a subagent
        worker inherits the shared budget and never squeezes it.  The
        eviction is recorded by ``evict_to_threshold``'s own log line.
        """
        if self.tool_catalog is None:
            return
        try:
            await self.tool_catalog.evict_to_threshold()
        except Exception:
            logger.exception("turn_evict_failed")

    async def _process_stream(
        self,
        history: MessageHistory,
        handler: AgentEventHandler | None,
    ) -> _StreamResult:
        """Consume a single streaming LLM response.

        Emits thinking and text chunks to the handler in real-time.
        Accumulates tool call deltas, content, and usage.

        When cancelled, the stream is closed immediately via
        ``chat_stream(cancel_event=...)`` — the underlying HTTP
        connection is released and no more chunks are consumed.

        Returns a _StreamResult with the response data accumulated
        up to the point of cancellation.
        """
        content_parts: list[str] = []
        thinking_parts: list[str] = []
        tool_accum: dict[int, dict] = {}
        stream_usage = TokenUsage()

        # Transient transport failures (e.g. the peer closing the connection
        # mid-chunked-read) are retried with linear backoff.  The history
        # is untouched while streaming — the assistant message is only added
        # after this method returns — so a retry sends identical messages.
        # ``stream_max_retries == 0`` disables retry entirely (fail fast);
        # ``stream_timeout`` caps a single stream call in TOTAL wall-clock
        # time (subagents), while ``stream_stall_timeout`` cuts a silent
        # provider per-chunk (inactivity, applies to every agent) — see
        # ``_consume_stream``.
        attempts = 0
        max_retries = self.stream_max_retries
        # One-element holder so `emitted_any` survives a mid-stream raise —
        # the retry path needs to know partial output was already shown.
        emitted: list[bool] = [False]
        # The tool list is computed ONCE per REQUEST — every retry attempt of
        # this request sends the identical list (a tools array changing between
        # attempts would defeat the prompt-cache prefix AND reorder tools the
        # model may be calling right now).  The next request recomputes it from
        # a fresh snapshot, so a mid-turn load lands there.
        request_tools = await self._tools_for_request()
        while True:
            attempts += 1
            try:
                # No SDK-level timeout here BY DESIGN: the anthropic/openai
                # clients carry no timeout kwarg of ours, and the real bound
                # is owned two layers up — the loop's per-chunk stall
                # watchdog (registry work.stall) and the tool budget — so a
                # live-but-slow generation is never cut by a transport clock.
                stream_iter = self.llm_client.chat_stream(
                    messages=history.to_openai_messages(
                        thinking_enabled=self.llm_client.model_config.thinking_enabled,
                    ),
                    # Schemas carry business params only; the universal
                    # meta-params (`_timeout`/`_async`/`_approve`) are a
                    # system-prompt contract (slife.j2), not per-tool schema
                    # fields — that saves ~3 params × 60 tools per request.
                    #
                    # With a catalog, only the loaded snapshot is injected, and
                    # the schemas come FROM THE CATALOG DB — the snapshot is
                    # re-read per request (before this call), so the list is
                    # whatever is loaded NOW; without a catalog the historical
                    # all-registered list is sent.  Computed once OUTSIDE the
                    # retry loop so every attempt sends byte-identical tools.
                    tools=request_tools,
                    cancel_event=self._cancel_event,
                )
                if self.stream_timeout is not None:
                    try:
                        stream_usage = await asyncio.wait_for(
                            self._consume_stream(
                                stream_iter, handler, content_parts,
                                thinking_parts, tool_accum, emitted,
                            ),
                            timeout=self.stream_timeout,
                        )
                    except TimeoutError as e:
                        raise TimeoutError(
                            f"LLM stream timed out after {self.stream_timeout}s"
                        ) from e
                else:
                    stream_usage = await self._consume_stream(
                        stream_iter, handler, content_parts, thinking_parts,
                        tool_accum, emitted,
                    )
                break  # stream completed cleanly
            except asyncio.CancelledError:
                # Cancellation is a control-flow signal — never retry it.
                raise
            except Exception as e:
                if not _is_retryable_stream_error(e):
                    raise
                if attempts > max_retries:
                    # Wrap the exhausted-retries error so the surfaced message
                    # is actionable.  RuntimeError is not a BadRequestError, so
                    # the inbox keeps the history intact — correct for a
                    # transient failure.  ``str(e) or type(e).__name__``
                    # guards against provider exceptions that stringify to
                    # empty (e.g. an httpx2 / httpcore2 ReadError wrapping
                    # a message-less BrokenResourceError) — an empty
                    # "Error: " must never reach the user.
                    detail = str(e) or type(e).__name__
                    raise RuntimeError(
                        f"LLM stream failed after {attempts} attempts: {detail}"
                    ) from e
                # Reset partial state + TUI display before retrying, so the
                # retried request starts visually clean.
                if emitted[0] and handler is not None:
                    on_retry = getattr(handler, "on_stream_retry", None)
                    if on_retry is not None:
                        try:
                            await on_retry()
                        except Exception:
                            pass
                content_parts.clear()
                thinking_parts.clear()
                tool_accum.clear()
                stream_usage = TokenUsage()
                logger.warning(
                    "llm_stream_retry attempt=%d max=%d err=%s",
                    attempts, max_retries, e,
                )
                if self._cancel_event.is_set():
                    raise AgentCancelled()
                await asyncio.sleep(_timeouts.timeouts.stream.retry_base_delay * attempts)

        # Remember the last API call's usage on the shared history — every
        # inbox message runs against the main agent's one context.
        if stream_usage.total_tokens > 0:
            # The cache is FIFO-capacity: one entry per history (id), evicting
            # the oldest past _MAX_USAGE_CACHE.  A heartbeat fires every 60s and
            # each A2A remote turn uses a fresh one-shot history, so without the
            # cap the cache grows without bound; evicting also makes an
            # id()-reused history miss (fresh estimate) instead of reading a
            # stale unrelated usage.
            self._usage_by_history[id(history)] = stream_usage

        return _StreamResult(
            content="".join(content_parts),
            thinking="".join(thinking_parts),
            usage=stream_usage,
            tool_accum=tool_accum,
        )

    # ── Tool execution ─────────────────────────────────────────────

    async def _await_approval(self, handler, tc) -> bool:
        """Wait for the user's approval decision OR the turn's cancellation.

        Esc-cancel (``_cancel_event``) must not leave the loop blocked forever
        on an approval prompt that may have lost focus (e.g. the model picker
        stole it) — every later message would queue behind it.  On cancel the
        prompt is denied and the turn's cancellation proceeds.
        """
        cancel_wait = asyncio.create_task(self._cancel_event.wait())
        approve_wait = asyncio.create_task(handler.on_tool_approval(tc))
        pending: set = set()
        try:
            done, pending = await asyncio.wait(
                {cancel_wait, approve_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if cancel_wait in pending:
                cancel_wait.cancel()
        if approve_wait in done:
            return approve_wait.result()
        # Cancelled while the prompt was open — deny it.  on_tool_approval
        # resolves its own prompt future on cancellation so nothing dangles.
        approve_wait.cancel()
        return False

    async def _execute_tools(
        self,
        tool_calls: list[ToolCallInfo],
        history: MessageHistory,
        handler: AgentEventHandler | None,
        iteration: int = 0,
    ) -> None:
        """Execute a batch of tool calls and record results.

        Emits on_tool_call/on_tool_result via the handler.
        Adds tool result messages to the history.

        Tools are executed **concurrently** — when the LLM issues
        multiple independent tool calls (e.g. two subscribe operations),
        they run in parallel via :func:`asyncio.gather`.  Each tool is
        still individually guarded by :attr:`tool_timeout`.

        Tools requiring user approval serialize behind an
        :class:`asyncio.Lock` so only one modal dialog appears at a time.
        """
        # Check cancellation before starting the batch
        if self._cancel_event.is_set():
            logger.info("agent_cancelled phase=before_batch iter=%d", iteration)
            return

        # Any tool that reads ctx.message_history (attach_image) must see the
        # history this loop is processing — not the startup human history —
        # while a WeChat/remote-agent turn is running.
        # All builtin tools share one ToolContext, so a single swap covers the
        # concurrent batch; it is restored in the finally below.
        _ctx = None
        for _tc in tool_calls:
            _t = self.tool_registry.get(_tc.name)
            if _t is None:
                continue
            _ctx = getattr(_t, "_ctx", None)
            if _ctx is not None:
                break
        _prev_conv = None
        if _ctx is not None:
            _prev_conv = _ctx.message_history
            _ctx.message_history = history

        # Serialize approval prompts — concurrent prompts would overlap
        _approval_lock = asyncio.Lock()

        async def _run_one(tc: ToolCallInfo) -> None:
            """Execute a single tool call with timeout, sanitization, and
            handler notifications.  Safe to run concurrently."""
            logger.debug("tool_start name=%s", tc.name)

            # ── Dynamic per-call timeout / async / approval ───────
            # LLM can pass _async (boolean), _timeout (number), or
            # _approve (boolean) on ANY tool call.  _async takes
            # priority — if true, the tool is scheduled in background
            # and we return immediately.
            actual_args = dict(tc.arguments)
            is_async = actual_args.pop("_async", None)
            inline_timeout = actual_args.pop("_timeout", None)
            # A malformed _timeout (the LLM writes natural language like
            # "5 seconds") must degrade to "use the default" for THIS call —
            # never raise out of the concurrent batch and kill the whole turn
            # (raw float() conversion used to escape _execute_tools).
            if inline_timeout is not None:
                try:
                    inline_timeout = float(inline_timeout)
                except (TypeError, ValueError):
                    logger.debug(
                        "tool_timeout_garbage_discarded value=%r", inline_timeout,
                    )
                    inline_timeout = None
            approve_requested = bool(actual_args.pop("_approve", False))

            # ── Native timeout mapping ───────────────────────────
            # Precedence (DESIGN.md §4.7): the
            # agent's positive ``timeout`` overrides ALL defaults — it lands
            # in the tool parameter (native) or becomes the wait_for bound
            # (non-native).  Omission → the tool's own registry value (native)
            # or work.tool_budget (non-native).  Tools with a native
            # ``timeout`` parameter (e.g. execute_shell) handle their own
            # timeout internally.
            # Map _timeout → timeout and let the tool drive — no
            # asyncio.wait_for wrapper.  ``_timeout`` of 0 or negative
            # (or sub-second, which truncates to 0) can NOT mean "no
            # timeout" on this path: the tool's deadline is then the ONLY
            # bound, and a tool reading its ``timeout`` arg into
            # ``wait_for(..., timeout=0)`` fires INSTANTLY (execute_shell)
            # — the opposite of the prompt's "0 = no timeout" (B2/B3).
            # Normalise ≤0 to "omit the arg" so the tool's own default
            # governs — the contract these tools document ("≤0 = default") —
            # and never forward the instant-kill zero.
            tool = self.tool_registry.get(tc.name)
            prop_keys = getattr(tool, 'parameters', {}).get("properties", {})
            has_native_timeout = "timeout" in prop_keys
            if has_native_timeout:
                if inline_timeout is not None:
                    timeout_val = int(inline_timeout)
                    if timeout_val > 0:
                        actual_args["timeout"] = timeout_val
                    else:
                        actual_args.pop("timeout", None)
                elif actual_args.get("timeout") is not None:
                    # A bare ``timeout`` arg is schema-valid here and
                    # normally reaches the tool untouched — but a 0 or
                    # negative spelling is the same instant-kill footgun as
                    # ``_timeout: 0``; pop it so the tool default governs.
                    try:
                        _bare_native = float(actual_args["timeout"])
                    except (TypeError, ValueError):
                        _bare_native = 1.0  # unparseable → keep the arg
                    if _bare_native <= 0:
                        actual_args.pop("timeout", None)

            # ── Bare `timeout` alias ─────────────────────────────
            # LLMs routinely append a bare ``timeout`` arg to tools whose
            # schema defines no such parameter (subagent sends, plugin
            # proxies) — the server's input validation then rejects
            # the WHOLE call with "Unexpected keyword argument" and the
            # turn dies.  Read it as the per-call bound instead: pop it so
            # it never reaches the tool, and let the wait_for logic below
            # enforce it exactly like ``_timeout``.  A tool WITH a native
            # timeout keeps its schema-valid arg untouched above — it
            # enforces it itself.
            if not has_native_timeout and inline_timeout is None:
                bare_timeout = actual_args.pop("timeout", None)
                if bare_timeout is not None:
                    try:
                        inline_timeout = float(bare_timeout)
                    except (TypeError, ValueError):
                        inline_timeout = None  # garbage → default bound

            # ── Approval gate — pure model judgment ────────────────
            # The LLM decides per-call whether the operation needs user
            # confirmation by passing `_approve: true` — a system-prompt
            # meta-parameter contract (slife.j2), not a schema field.
            # The harness no longer hardcodes approval on any tool.
            # Serialised via lock so concurrent approval dialogs never
            # overlap.
            if approve_requested:
                async with _approval_lock:
                    if handler:
                        approved = await self._await_approval(handler, tc)
                    else:
                        approved = True
                if not approved:
                    result = "Error: Tool execution was denied by user."
                    result = sanitize_secrets(result)
                    if handler:
                        await handler.on_tool_result(tc.id, result, is_error=True)
                    history.add_tool_result(tc.id, result, is_error=True)
                    return

            # ── Handler notification — after the approval gate ────
            # The tool call is only surfaced once approved (or when no
            # approval was requested): a denied call returns above and
            # never mounts a tool widget — the approval prompt itself
            # carries the "denied" state.
            if handler:
                await handler.on_tool_call(
                    tc,
                    iteration=iteration,
                    max_iterations=self.max_iterations,
                )

            if is_async:
                # ── Async: schedule background task ─────────────
                from slife.tools.system import schedule as schedule_async

                if _ctx is not None:
                    # The background task runs AFTER _execute_tools' finally
                    # restores ctx.message_history — a tool that reads it (e.g.
                    # attach_image) would otherwise target the wrong
                    # history.  Pin it to the one this turn is processing.
                    _run_conv = history
                    _run_ctx = _ctx

                    async def _run_with_conv():
                        _saved = _run_ctx.message_history
                        _run_ctx.message_history = _run_conv
                        try:
                            return await self.tool_registry.execute(
                                tc.name, **actual_args,
                            )
                        finally:
                            _run_ctx.message_history = _saved

                    coro = _run_with_conv()
                else:
                    coro = self.tool_registry.execute(tc.name, **actual_args)

                # Backgrounded calls are the _async escape hatch: without an
                # explicit agent `_timeout` the tool runs BARE — the chain
                # default (work.tool_budget) never applies to background
                # work; a tool that needs a bound carries its own (native
                # timeout param or internal deadline).  With a positive
                # `_timeout`, the Rule-2 mapping binds: non-native tools get
                # wait_for(agent value); native tools already received it via
                # the `timeout` arg mapped above (they self-enforce — no
                # double timer).
                if not has_native_timeout:
                    if inline_timeout is not None and inline_timeout > 0:
                        coro = asyncio.wait_for(coro, timeout=inline_timeout)

                task_id = schedule_async(coro)
                result = (
                    f"✓ Async task started.\n"
                    f"  task_id: {task_id}\n"
                    f"  tool: {tc.name}\n"
                    f"  Poll with check_async(task_id=\"{task_id}\").\n"
                    f"  Cancel with cancel_async(task_id=\"{task_id}\")."
                )
                result = self._flag_truncated_args(tc, result)
                result = sanitize_secrets(result)
                if handler:
                    await handler.on_tool_result(tc.id, result, is_error=False)
                history.add_tool_result(tc.id, result)
                return

            if has_native_timeout:
                # ── Native timeout: tool handles its own deadline ──
                # _timeout was already mapped to the timeout arg above.
                # The tool is responsible for enforcing its own timeout.
                try:
                    coro = self.tool_registry.execute(tc.name, **actual_args)
                    result = await coro
                except Exception as e:
                    result = (
                        f"Error: Tool '{tc.name}' failed: {type(e).__name__}: {e}."
                    )
                    logger.warning(
                        "tool_error name=%s err=%s", tc.name, e,
                    )
            else:
                # ── Agent Loop timeout: wrap with asyncio.wait_for ──
                # ≤0 / missing is never "no timeout" (and never an instant
                # kill): fall back to the tool-chain default.  DESIGN.md §4.7.
                if inline_timeout is not None and inline_timeout > 0:
                    effective_timeout = inline_timeout
                else:
                    effective_timeout = self.tool_timeout

                try:
                    coro = self.tool_registry.execute(tc.name, **actual_args)
                    result = await asyncio.wait_for(coro, timeout=effective_timeout)
                except asyncio.TimeoutError:
                    result = (
                        f"Error: Tool '{tc.name}' timed out "
                        f"({effective_timeout}s)."
                    )
                    logger.warning(
                        "tool_timeout name=%s timeout=%ds args=%s",
                        tc.name, effective_timeout,
                        self._truncate_args(tc.arguments),
                    )
                except Exception as e:
                    result = (
                        f"Error: Tool '{tc.name}' failed: {type(e).__name__}: {e}."
                    )
                    logger.warning(
                        "tool_error name=%s err=%s", tc.name, e,
                    )

            # Judge failure BEFORE the args-truncation marker prepends its ⚠
            # text — otherwise a failed call whose result began with "Error:"
            # would read as success (is_error false) in every downstream sink.
            is_error = result.startswith("Error")
            # Surface provider-truncated arguments before secrets handling —
            # the marker is plain text and must not be sanitized away.
            result = self._flag_truncated_args(tc, result)
            # Sanitize secrets BEFORE anything else — prevents API keys
            # from reaching the LLM context or TUI display.
            result = sanitize_secrets(result)
            # Truncate oversized tool results so a single large file
            # read doesn't blow up the context window.
            max_chars = self.max_tool_result_chars
            if max_chars > 0 and len(result) > max_chars:
                original_len = len(result)
                result = result[:max_chars] + (
                    f"\n… (truncated: original {original_len} chars — "
                    f"re-run the tool to see the full output)"
                )
                logger.debug("tool_result_truncated name=%s original=%d truncated=%d", tc.name, original_len, max_chars)

            if handler:
                await handler.on_tool_result(tc.id, result, is_error)

            history.add_tool_result(tc.id, result, is_error=is_error)

        try:
            await asyncio.gather(*(_run_one(tc) for tc in tool_calls))
        finally:
            if _ctx is not None:
                _ctx.message_history = _prev_conv

    # ── Main loop ──────────────────────────────────────────────────

    async def run(
        self,
        user_input: str,
        history: MessageHistory,
        images: list[str] | None = None,
        handler: AgentEventHandler | None = None,
    ) -> AgentResult:
        """Run the agent loop for a single user input.

        Uses streaming API so thinking and text appear in real-time.

        Args:
            user_input: The user's message text.
            history: The message history (mutated in place).
            images: Optional list of image file paths to attach.
            handler: Optional event handler for real-time callbacks.

        Returns:
            AgentResult with final text and cumulative token usage.  On
            cancellation or hitting max_iterations, the result carries
            ``cancelled=True`` and is surfaced to the handler via
            ``on_max_iterations`` (it is not raised).
        """
        n_imgs = len(images) if images else 0
        if n_imgs > 0 and not self.supports_vision:
            msg = (
                f"⚠ Current model does not support image input (supports_vision=false), "
                f"but {n_imgs} image(s) were received. "
                f"Use a vision-capable model, or remove the @path attachment."
            )
            logger.warning("vision_unsupported imgs=%d model_vision=%s", n_imgs, self.supports_vision)
            # Add text-only — don't encode images the model can't handle.
            # The warning is recorded as the assistant reply so the
            # history doesn't end on a dangling user message.
            history.add_user_message(user_input)
            history.add_assistant_message(content=msg)
            return AgentResult(text=msg, usage=TokenUsage())

        # @path / programmatic attachments ride the SAME attach_image path
        # the model would use — invoked by the harness so no LLM iteration
        # is spent deciding to attach.  Reuses _auto_invoke (the _turn_prompt
        # machinery): records the assistant(tool_use) + tool result pair, runs
        # the tool directly, and attach_image's execute injects the image
        # content blocks into the user message in memory (never persisted —
        # restore rebuilds text-only).  All images go in ONE call so a single
        # (user, assistant, tool) triple carries the whole batch.
        # The context rebuild runs BEFORE the user message is added: it
        # replaces `messages` wholesale, so anything appended first (the user
        # message, the attach_image blocks) would be destroyed — and the image
        # blocks exist only in memory, so they could not be recovered.  It also
        # stays outside the iteration loop, whose per-iteration work
        # (_check_new_input, the tool-schema refresh) belongs to the turn in
        # progress.
        await self._recall_and_rebuild(history, user_input, handler)

        history.add_user_message(user_input)
        if images:
            await self._auto_invoke(
                "attach_image", {"sources": list(images)}, history,
            )
        total_usage = TokenUsage()
        t_request = _time.monotonic()

        logger.info("req_start msg=%.100s imgs=%d", sanitize_secrets(user_input), n_imgs)

        with request_scope(user_input[:50]):
            try:
                # Track the context time range.  "Context covers" is shown on
                # the first turn, then only when restore or a trim advances it.
                # Invariant: _context_time_start holds the OLDEST turn's date;
                # _context_turn_dates holds the rest (restore seeds dates[1:]).
                # Same 'YYYY-MM-DD HH:MM:SS' wall-clock format restore seeds
                # (_context_time_start), so "Context covers" never flips format.
                turn_start = format_turn_ts()
                # Remember the current turn's start for the trim-exhausted
                # branch (seed _context_time_start from it, not from now).
                self._current_turn_start = turn_start
                if not self._context_time_start:
                    self._context_time_start = turn_start
                else:
                    self._context_turn_dates.append(turn_start)
                    if len(self._context_turn_dates) > _MAX_CONTEXT_DATES:
                        # Keep the OLDEST dates (what a trim consumes) — a
                        # huge window that never trims must not grow the list
                        # without bound.
                        del self._context_turn_dates[_MAX_CONTEXT_DATES:]

                # Threshold eviction BEFORE the first request: the
                # oldest-by-LRU loaded tools over the budget leave the tool
                # list for this turn.  Eviction stays a turn-boundary
                # operation (the injected list itself is rebuilt per
                # request, below).
                await self._maybe_evict()

                # Context usage is computed ONCE and shared: _turn_prompt
                # reports it as the usage %, and the TUI status bar.
                current = self.context_tokens_for(history)
                await self._auto_invoke(
                    "_turn_prompt", self._turn_prompt_kwargs(history, current), history,
                )
                # Context trimming no longer happens here — it moved to
                # _trim_after_save (after each turn is persisted), where the
                # real API usage is known.  The _turn_prompt percentage and the
                # trim decision now come from the same context_tokens_for
                # reading at their respective times.
                # max_iterations = 0 means no cap.  The cap is checked live
                # each iteration, so a mid-turn set_max_iterations applies
                # immediately (and to the next turn too).
                for i in itertools.count():
                    if self.max_iterations > 0 and i >= self.max_iterations:
                        raise MaxIterationsExceeded(self.max_iterations)
                    # Check for cancellation before each iteration
                    if self._cancel_event.is_set():
                        logger.info("agent_cancelled iter=%d", i + 1)
                        raise AgentCancelled()

                    # Cut-in mode: a message arrived mid-turn — inject it at
                    # this safe point so the model addresses it in the same
                    # iteration.  The gate is cheap (queue emptiness); the
                    # extraction itself happens only inside _auto_invoke once
                    # the pair will be recorded, so a cancelled turn never
                    # loses the queued message.
                    if (
                        self.cutin_enabled
                        and self.pending_input_has is not None
                        and self.pending_input_has()
                    ):
                        await self._auto_invoke("_check_new_input", {}, history)

                    # The injected tool list is rebuilt for EVERY request from
                    # the catalog's loaded set, so a load that landed during the
                    # previous iteration (func_tool_load, _func_tool_unload, a
                    # plugin's mirror) is in this request — the next LLM call,
                    # not the next turn.
                    await self._refresh_inject_snapshot()

                    with elapsed("iter", logger, iter=i + 1):
                        result = await self._process_stream(history, handler)

                        # Capture usage even when cancelled — the API call
                        # already consumed tokens regardless of outcome.
                        total_usage = total_usage + result.usage
                        if handler:
                            await handler.on_token_usage(total_usage)

                        # Check for cancellation after stream
                        if self._cancel_event.is_set():
                            logger.info("agent_cancelled phase=after_stream iter=%d", i + 1)
                            raise AgentCancelled()

                        # Tool calls?
                        if result.tool_accum:
                            tool_calls = self._build_tool_calls_from_deltas(
                                result.tool_accum
                            )
                            logger.debug(
                                "tool_calls=%d names=%s",
                                len(tool_calls),
                                [tc.name for tc in tool_calls],
                            )
                            history.add_assistant_message(
                                content=result.content or None,
                                tool_calls=self._serialize_tool_calls(tool_calls),
                                thinking=result.thinking or None,
                            )
                            await self._execute_tools(
                                tool_calls, history, handler, iteration=i + 1
                            )
                            continue

                        # No tool calls — final response
                        history.add_assistant_message(
                            content=result.content or "",
                            thinking=result.thinking or None,
                        )
                        t_total = (_time.monotonic() - t_request) * 1000
                        logger.info(
                            "response tok_p=%d tok_c=%d tok_t=%d took_ms=%.0f text=%.200s",
                            total_usage.prompt_tokens,
                            total_usage.completion_tokens,
                            total_usage.total_tokens,
                            t_total,
                            result.content,
                        )
                        return AgentResult(text=result.content, usage=total_usage)
            except AgentCancelled:
                # Turn consistency is enforced at the single save point
                # (save_to_memory runs unconditionally after every turn) —
                # the history is repaired there, not here.
                return AgentResult(text="", usage=total_usage, cancelled=True)
            except MaxIterationsExceeded:
                logger.warning("max_iterations_exceeded max=%d", self.max_iterations)
                # Surface the limit to the handler (e.g. the TUI) before
                # returning the cancelled result — the turn is saved as a
                # normal cancelled turn, but the user should see why it
                # stopped.
                if handler is not None:
                    on_max = getattr(handler, "on_max_iterations", None)
                    if on_max is not None:
                        try:
                            await on_max(self.max_iterations)
                        except Exception:
                            pass
                return AgentResult(text="", usage=total_usage, cancelled=True)
            except Exception:
                # Re-raise so the caller (inbox) handles the error as before;
                # the history is repaired at the save point.
                raise
        # Unreachable: the loop above only exits via return or raise, but
        # Pylance can't see that itertools.count() is infinite.
        raise RuntimeError("agent loop exited without a result")
