"""What a host does to a service — the two operations neither host owns.

Two hosts run an agent: the TUI (``slife/ui/app.py``) and the headless agent
(``slife/headless.py``, `slife --headless`).  They differ in everything the
user sees and in nothing else, so the operations that are neither UI nor agent
live here rather than in whichever host needed them first:

* :func:`restore_context` — rebuild the history from the persisted exit-time
  context, and prime the loop from it.  It lived in ``slife/ui/restore.py``,
  which made *session continuity* a property of having a screen: a process
  without one could not pick up where it left off.
* :func:`shutdown_session` — the bounded teardown order.  It lived in
  ``SlifeApp._stop_plugins``, which made "no plugin child outlives the
  session" a property of the TUI's exit path.

Both are order-sensitive, and the order is the reason they are shared rather
than copied: a restore that primes the loop after the widgets are built, or a
teardown that stops the catalog before the plugins that write to it, is wrong
in a way that shows up somewhere else entirely.

Importable without Textual.  It does import ``slife.ui.i18n`` for the
restored-turn prefixes — the same inversion ``slife/agent/service.py``
documents (the harness composes user-facing text, so it localizes it too), and
``i18n`` is stdlib-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, NamedTuple

import slife.timeouts as _timeouts  # module ref — call-time lookup, patch-safe
from slife.a2a.identity import Channel
from slife.agent.llm_client import TokenUsage
from slife.agent.message_history import messages_from_turns
from slife.agent.schedules import is_autonomous_trigger, is_schedule_trigger
from slife.agent.timer import is_timer_trigger
from slife.ui.i18n import t

if TYPE_CHECKING:
    from slife.agent.message_history import MessageHistory
    from slife.agent.service import AgentService

logger = logging.getLogger(__name__)


# ── Prefix mapping ────────────────────────────────────────────────────


def restore_prefix(channel: Channel) -> str | None:
    """Consistent prefix mapping for restored turns.

    Delegates entirely to the channel type's display prefix — the single
    implementation live and restored bubbles share (the subagent branch is
    i18n-aware there, so the two can never diverge by language):
      - human     → "You> "
      - wechat    → "Wechat> "
      - subagent  → "Subagent(<name>)> " (local worker completion, routed
        into the human history — not an A2A peer)
      - a2a       → "A2A(<peer name>)"
      - system    → None (filtered from the chat view)
    """
    return channel.display_prefix()


def _safe_parse_args(raw: str) -> dict:
    """Parse a tool-call arguments JSON string, falling back gracefully."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw}


def tool_result_is_error(msg: dict) -> bool:
    """Error state of a restored ``tool`` message.

    The loop persists its ``is_error`` verdict on every tool result —
    read it directly, never re-derive from content.
    """
    return bool(msg.get("is_error", False))


# ── Restore ───────────────────────────────────────────────────────────


class RestorePlan(NamedTuple):
    """What the rebuild produced, for the host that asked for it.

    ``ui_ops`` is the turn-by-turn rendering plan and the two maps are the
    tool results that hang on its widgets — a shape only a screen uses.  A
    host without one discards all three and keeps only the rebuilt history,
    which is why the rebuild and the plan are one function: they are one walk
    of one message list, and splitting them would mean walking it twice and
    risking two orders.
    """

    ui_ops: list[dict]
    tool_results: dict[str, str]
    tool_errors: dict[str, bool]


def restore_context(
    service: AgentService,
    turns: list[dict],
    history: MessageHistory,
    assistant_prefix: str,
) -> RestorePlan:
    """Rebuild *history* from the persisted exit-time context.

    *turns* is the **exit-time context**, already resolved: the caller read it
    with ``get_exit_context_turns``, which returns the turns named by the
    persisted live-context list, in that list's order, with no ceiling
    re-slicing — the list already encodes the trimmed state.  Restore replays
    it verbatim so the agent picks up exactly where it left off; older turns
    stay in the memory DB and can be retrieved via ``turn_search`` if needed.

    *history* is the caller's, not ``service.message_history``: which history
    a restore writes into is the host's decision, and the rebuild is driven
    against that object.  *assistant_prefix* is the host's name for the
    agent's own replies, stamped on every assistant op (the headless host,
    which renders nothing, passes ``""``).  Raises if the rebuild fails, after
    the partial assignment already happened — the caller decides how to say so.
    """
    if not turns:
        return RestorePlan([], {}, {})

    try:
        sys_msg = (
            history.messages[0]
            if history.messages
            and history.messages[0].get("role") == "system"
            else None
        )

        # One turn→messages builder, shared with the per-turn rebuild, so a
        # restored turn and a rebuilt one render identically.
        all_messages = messages_from_turns(turns, system_message=sys_msg)

        # Repair orphaned tool_calls (persisted by a pre-ensure session)
        # BEFORE building the tool-result lookup and UI ops — otherwise the
        # restored UI shows "done + empty result" while the repaired LLM
        # context carries "(Tool execution interrupted)".
        history.messages = all_messages
        history._ensure_turn_consistent()

        # Build tool-result lookup
        tool_results: dict[str, str] = {}
        tool_errors: dict[str, bool] = {}
        for msg in all_messages:
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id", "")
                if tcid:
                    content = msg.get("content", "") or ""
                    tool_results[tcid] = content
                    tool_errors[tcid] = tool_result_is_error(msg)

        # Build UI ops
        ui_ops: list[dict] = []
        assistant_indices = [
            i for i, m in enumerate(all_messages)
            if m.get("role") == "assistant"
            and not (
                m.get("content") in (None, "")
                and not m.get("thinking")
                and (m.get("tool_calls") or [])
                and all(
                    tc.get("function", {}).get("name", "").startswith("_")
                    for tc in (m.get("tool_calls") or [])
                )
            )
        ]
        last_assistant_idx = assistant_indices[-1] if assistant_indices else -1

        _channel_by_row: dict[int, Channel] = {}
        for i, turn in enumerate(turns):
            _channel_by_row[i] = Channel.from_db(
                turn.get("channel", ""), turn.get("channel_data", "{}"),
            )

        turn_idx = -1
        # Per-turn synthetic-trigger flags, set on the user message and read
        # on the assistant messages that follow it.  Initialized here so the
        # assistant branch is provably bound even if an assistant message
        # somehow appears before any user message (defaults: treat as real).
        is_synthetic = False
        is_schedule = False
        is_timer = False
        cur_created = ""
        cur_completed = ""
        for idx, msg in enumerate(all_messages):
            role = msg.get("role", "")
            if role == "system":
                continue
            elif role == "user":
                turn_idx += 1
                # Per-turn timestamps: created_at = user input time (shown
                # on the user message), completed_at = assistant completion
                # (shown on every assistant message of this turn).
                if turn_idx < len(turns):
                    cur_created = turns[turn_idx].get("created_at", "")
                    cur_completed = (
                        turns[turn_idx].get("completed_at") or cur_created
                    )
                else:
                    cur_created = ""
                    cur_completed = ""
                content = msg.get("content", "") or ""
                raw = (
                    "".join(
                        p.get("text", "") for p in content if p.get("type") == "text"
                    )
                    if isinstance(content, list)
                    else content
                )
                # Synthetic-trigger turns (heartbeat / schedule): the trigger
                # is a marked system message, not a real user message — filter
                # the whole turn (the reply renders as ⚡ autonomous or
                # 📅 scheduled below, or not at all if quiet).
                is_synthetic = is_autonomous_trigger(raw)
                is_schedule = is_schedule_trigger(raw)
                is_timer = is_timer_trigger(raw)
                if is_synthetic:
                    continue
                ch = _channel_by_row.get(turn_idx)
                if ch is None:
                    # No persisted channel row (shouldn't happen — every
                    # turn carries one) — degrade to a human message.
                    ch = Channel.human()
                prefix = restore_prefix(ch)
                if prefix is None:
                    # System channel — filtered from the chat view.
                    continue
                ui_ops.append({
                    "type": "user",
                    "content": raw,
                    "prefix": prefix,
                    "created_at": cur_created,
                })
            elif role == "assistant":
                # "." uniformly means silence — never restore a bare-dot
                # reply, from any turn source (heartbeat, autonomous a2a
                # notification, or anything else).
                if (msg.get("content") or "").strip() == ".":
                    continue
                # Nothing to show → skip.  Covers harness messages
                # (_turn_prompt — LLM context only, never in the live
                # TUI) AND genuinely empty messages.  An empty tool-iteration
                # message with REAL tool calls stays: its ToolCallWidgets
                # render the work even without a message body.
                tcs = msg.get("tool_calls") or []
                visible_calls = [
                    tc for tc in tcs
                    if not tc.get("function", {}).get("name", "").startswith("_")
                ]
                if (
                    not (msg.get("content") or "")
                    and not (msg.get("thinking") or "")
                    and not visible_calls
                ):
                    continue
                if is_synthetic:
                    # Synthetic-trigger beat (heartbeat / schedule): show
                    # real content as ⚡ autonomous or 📅 scheduled.  A bare "." is
                    # already skipped by the general silence filter above;
                    # here we only drop empty messages.
                    content = msg.get("content") or ""
                    if not content.strip():
                        continue
                    ui_ops.append({
                        "type": "assistant",
                        "thinking": "",
                        "content": content,
                        "tool_calls": [],
                        "is_final": False,
                        "name_prefix": (
                            t("timer_prefix") if is_timer
                            else t("schedule_prefix") if is_schedule
                            else t("autonomous_prefix")
                        ),
                        "completed_at": cur_completed,
                    })
                    continue
                is_final = (idx == last_assistant_idx)
                thinking = msg.get("thinking") or ""
                content = msg.get("content") or ""
                tcs = msg.get("tool_calls") or []
                ui_ops.append({
                    "type": "assistant",
                    "thinking": thinking,
                    "content": content,
                    "tool_calls": [
                        {
                            "id": tc.get("id", ""),
                            "name": tc.get("function", {}).get("name", "?"),
                            "arguments": _safe_parse_args(
                                tc.get("function", {}).get("arguments", "{}")
                            ),
                        }
                        for tc in tcs
                    ],
                    "is_final": is_final,
                    "name_prefix": assistant_prefix,
                    "completed_at": cur_completed,
                })
            elif role == "tool":
                pass

        # The very last assistant message in the restored history
        # should mirror live-session behaviour: thinking expanded, reply
        # visible.  Walk backwards through ui_ops and tag the last one.
        for op in reversed(ui_ops):
            if op.get("type") == "assistant":
                op["is_final"] = True
                break

    except Exception:
        # LOGGED, not just shown.  The red line reaches the reader (the caller
        # draws it); this reaches whoever has to explain it.  Re-raised so the
        # caller can, because only it knows what a failed restore means for its
        # own surface.
        logger.exception("session_restore_failed turns=%d", len(turns))
        raise

    # ── Phase 2: the loop's view of the restored context ──────────────
    # (messages were already assigned + repaired in Phase 1, before the
    # tool-result lookup was built, so the UI and LLM context agree.)
    history.messages = all_messages

    # The restored context is a legitimate pre-exit state, not growth —
    # mark it so the loop does NOT compact it to the floor on the very
    # first replacement turn (the marker is consumed in AgentLoop.run).
    if turns:
        service.agent_loop._just_restored_history = id(history)

    # Prime the context time range so _turn_prompt shows the LLM
    # what time window its current context covers.  The start date is
    # advanced by the agent loop after each trim.
    if turns:
        dates = [
            t.get("created_at", "")[:19].replace("T", " ")
            for t in turns if t.get("created_at")
        ]
        if dates:
            service.agent_loop._context_time_start = dates[0]
            service.agent_loop._context_turn_dates = dates[1:]  # reserve for trim

    # Reset session token counter — session starts fresh
    service.session_usage.total_tokens = 0

    # Prime the turn prompt with the restored context size.  On the
    # first round we have no real API usage yet, so `context_tokens_for` /
    # the status bar fall back to `_last_usage`.  Use the **latest restored
    # turn's persisted context_tokens** — the last call's prompt+completion,
    # i.e. the exact context size at exit (what _turn_prompt would have
    # reported).
    #
    # A missing/zero value (e.g. a cancelled turn) primes nothing: the report
    # is then genuinely unknown, and `context_tokens_for` returns 0 for that.
    # Substituting an estimate here would present a guess as the real exit-time
    # occupancy — the one thing that function is written never to do.
    last_turn = turns[-1] if turns else {}
    prompt = last_turn.get("context_tokens") or 0
    if prompt > 0:
        service.agent_loop._last_usage = TokenUsage(
            prompt_tokens=prompt,
            total_tokens=prompt,
        )

    return RestorePlan(ui_ops, tool_results, tool_errors)


# ── Teardown ──────────────────────────────────────────────────────────


async def shutdown_session(service: AgentService) -> None:
    """Stop the inbox, the workers and every plugin, then the catalog.

    The order is the point, and it is shared so it can only be got wrong once:

    * the inbox first — it completes any in-flight message, and the agent loop
      must not keep firing tool calls into an MCP client we are about to
      disconnect;
    * then the workers and every registered plugin in parallel, each under the
      shutdown grace bound, so one wedged child cannot hang the exit (a plugin
      that owns poll/drain tasks declares them on its lifecycle, so a uniform
      stop is enough — there are no per-plugin stop methods to keep in step);
    * the shared tool catalog last — a late reconcile must never write to a
      closed DB, and the aiosqlite worker thread would otherwise block exit.
    """
    async def _stop_one(name: str, coro) -> None:
        try:
            await asyncio.wait_for(coro, timeout=_timeouts.timeouts.grace.shutdown)
        except asyncio.TimeoutError:
            logger.warning("shutdown_timeout service=%s", name)
        except Exception:
            pass

    await _stop_one("inbox", service.stop_inbox())
    await asyncio.gather(
        _stop_one("subagent", service.stop_subagent()),
        # The plugins' own grace-bounded stop, shared with the worker's exit
        # (AgentService.stop_plugins) — never a second copy of it here.
        service.stop_plugins(),
        return_exceptions=True,
    )
    await _stop_one("catalog", service.close_catalog())
