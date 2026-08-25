"""Session restore — rebuild chat history from persistent memory.

Extracted from ``slife.ui.app`` to keep the TUI application focused on
its primary responsibility: UI event handling and layout.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from slife.agent.message_history import turn_header
from slife.agent.schedules import is_autonomous_trigger, is_schedule_trigger
from slife.agent.llm_client import TokenUsage
from slife.ui.chat import ChatView
from slife.ui.i18n import t
from slife.ui.tool_display import ToolCallWidget

if TYPE_CHECKING:
    from slife.config import Config
    from slife.agent.message_history import MessageHistory
    from slife.ui.app import SlifeApp
    from slife.ui.chat import ChatView

logger = logging.getLogger(__name__)


# ── Turn token estimation ─────────────────────────────────────────────


def estimate_turn_tokens(turn: dict) -> int:
    """Estimate the *incremental* token cost of a single turn.

    Counts the user message plus the stored assistant/tool messages
    using the same chars/3 heuristic as ``MessageHistory.count_tokens``.
    Returns at least 1 so that a zero-content turn still counts.
    """
    user = turn.get("user_message", "") or ""
    messages = turn.get("messages", "[]")
    if isinstance(messages, str):
        messages = json.loads(messages)
    body = (
        json.dumps(messages, ensure_ascii=False)
        if isinstance(messages, list)
        else str(messages)
    )
    return max(len(user) // 3 + len(body) // 3, 1)


# ── Turn header (restore-time annotation) ────────────────────────────
#
# Every restored user message gets a compact `[Turn: N · …]` footnote,
# concatenated into the message text — the LLM needs to tell old turns
# apart: which turn (rowid), when it started, when it finished.  Without
# it the whole restored history reads as "just happened".  The builder
# lives in ``history.turn_header`` so the save path annotates
# completed live turns with the same format.  The current in-flight turn
# gets nothing (it is the one that IS now), and the human reads the
# footnote in the TUI.  Heartbeat turns are excluded: their user message
# is a synthetic `[Heartbeat]` trigger, not a real query.


# ── Prefix mapping ────────────────────────────────────────────────────


def restore_prefix(channel: str | None, _agent_name: str) -> str:
    """Consistent prefix mapping for restored turns.

    Matches the real-time display prefixes used during live operation:
      - human  → "You> "
      - wechat → "<agent_name>(Wechat)"
      - other   → "<remote_agent_name>(a2a)" (external agent id, A2A peer, etc.)
    """
    ch = channel or ""
    if ch == "human":
        return "You> "
    if ch == "wechat":
        return "You(Wechat)> "
    if ch:
        return f"{ch}(a2a)"
    return "You> "


# ── Safe arg parse ────────────────────────────────────────────────────


def _safe_parse_args(raw: str) -> dict:
    """Parse a tool-call arguments JSON string, falling back gracefully."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw}


def tool_result_is_error(msg: dict) -> bool:
    """Error state of a restored ``tool`` message.

    The persisted ``is_error`` flag wins — it is the loop's recorded
    verdict.  Legacy turns (saved before the flag existed) fall back to
    the same heuristic the live loop uses, so old errors don't render
    as successful.
    """
    if "is_error" in msg:
        return bool(msg["is_error"])
    content = msg.get("content", "") or ""
    return isinstance(content, str) and content.startswith("Error")


# ── Main restore orchestrator ─────────────────────────────────────────


async def restore_session(
    app: "SlifeApp",
    recovery_info: dict,
    history: "MessageHistory",
    config: "Config",
    agent_name: str,
    assistant_prefix: str,
) -> None:
    """Restore a previous session from turn-based memory.

    Rebuilds the **exit-time context**: ``get_recent_turns`` already
    selected the turns recorded after the persisted live-context boundary,
    within the context-ceiling token budget.  This re-select pass only
    guards legacy ``recovery_info`` that carries an untrimmed list; the
    current path arrives pre-fitted.  Older turns stay in the memory DB
    and can be retrieved via ``turn_search`` if needed.

    This function is self-contained — it reads recovery_info, rebuilds
    the history message list, and reconstructs the chat UI.
    """
    all_turns: list[dict] = recovery_info.get("turns", [])
    if not all_turns:
        return

    # ── Reuse the exit-time context verbatim ──────────────────────────
    # get_recent_turns already returns every turn after the persisted
    # live-context boundary — the exact slice that was live at exit.  No
    # re-slicing against the ceiling: restore replays the exit state so the
    # agent picks up exactly where it left off.  (Legacy recovery_info that
    # predates this carries an untrimmed list — kept whole here too.)
    turns = all_turns

    # ── Phase 1: Reconstruct message list from selected turns ─────────
    try:
        sys_msg = (
            history.messages[0]
            if history.messages
            and history.messages[0].get("role") == "system"
            else None
        )

        all_messages: list[dict] = []
        if sys_msg:
            all_messages.append(dict(sys_msg))

        for turn in turns:
            user_msg_text = turn.get("user_message", "")
            turn_messages_json = turn.get("messages", "[]")
            turn_msgs: list[dict] = (
                json.loads(turn_messages_json)
                if isinstance(turn_messages_json, str)
                else turn_messages_json
            )
            # (The images column is gone — image blocks live only in the
            # in-memory user message and are never persisted; restore is
            # text-only.  The user message here is plain text + turn header.)
            # Autonomous turns (heartbeat / schedule) carry a synthetic
            # trigger as the user message, not a real query — no turn header
            # (restore also filters them from the TUI).
            header = (
                "" if is_autonomous_trigger(user_msg_text)
                else turn_header(turn)
            )
            # The turn header is an inline footnote concatenated onto the end
            # of the user-message text (not a separate part or line) — so
            # both the LLM context and the restored TUI bubble carry it,
            # reading as metadata right after the user's words.
            if header:
                user_msg_text = user_msg_text + " " + header
            user_msg: dict = {"role": "user", "content": user_msg_text}
            all_messages.append(user_msg)
            all_messages.extend(turn_msgs)

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

        _channel_by_row: dict[int, str] = {}
        for i, turn in enumerate(turns):
            _channel_by_row[i] = turn.get("channel", "")

        turn_idx = -1
        # Per-turn synthetic-trigger flags, set on the user message and read
        # on the assistant messages that follow it.  Initialized here so the
        # assistant branch is provably bound even if an assistant message
        # somehow appears before any user message (defaults: treat as real).
        is_synthetic = False
        is_schedule = False
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
                if is_synthetic:
                    continue
                ch = _channel_by_row.get(turn_idx, "")
                prefix = restore_prefix(ch, agent_name)
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
                # (_sys_note — LLM context only, never in the live
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
                            t("schedule_prefix") if is_schedule
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

    except Exception as e:
        _show_system_message(app, t("restore_failed", err=e), color="#f85149")
        return

    # ── Phase 2: Replace history messages ────────────────────────
    # (messages were already assigned + repaired in Phase 1, before the
    # tool-result lookup was built, so the UI and LLM context agree.)
    history.messages = all_messages

    # The restored context is a legitimate pre-exit state, not growth —
    # mark it so the loop does NOT compact it to the floor on the very
    # first replacement turn (the marker is consumed in AgentLoop.run).
    if turns:
        app.service.agent_loop._just_restored_history = id(history)

    # Prime the context time range so _sys_note shows the LLM
    # what time window its current context covers.  The start date is
    # advanced by the agent loop after each trim.
    if turns:
        dates = [
            t.get("created_at", "")[:19].replace("T", " ")
            for t in turns if t.get("created_at")
        ]
        if dates:
            app.service.agent_loop._context_time_start = dates[0]
            app.service.agent_loop._context_turn_dates = dates[1:]  # reserve for trim

    # ── Phase 3: Rebuild UI ───────────────────────────────────────────
    chat_view = app.query_one("#chat-view", ChatView)

    # Suppress per-widget auto-scroll while rebuilding: the whole history
    # is mounted first, then the view scrolls to the end exactly once.
    # Scrolling on every widget (the live behaviour) is what made the
    # restore jitter.
    chat_view._autoscroll = False

    with app.batch_update():
        for op in ui_ops:
            if op["type"] == "user":
                chat_view.add_user_message(
                    op["content"],
                    prefix=op["prefix"],
                    timestamp=op.get("created_at"),
                )
            elif op["type"] == "assistant":
                # Live semantics: a message widget exists only once thinking
                # or text streamed.  A tool-iteration message without either
                # is kept in storage purely for the LLM context — render its
                # tool widgets, but never an empty "…" placeholder the live
                # TUI couldn't have shown.
                thinking = op.get("thinking", "")
                text = op.get("content", "")
                if thinking or text:
                    am = chat_view.add_assistant_message(
                        name_prefix=op.get("name_prefix"),
                        timestamp=op.get("completed_at"),
                    )
                    if thinking:
                        am.append_thinking(thinking)
                    if text:
                        am.append_text(text)
                    am.finalize(intermediate=not op.get("is_final", False))

                for tc in op.get("tool_calls", []):
                    # Skip harness notifications (_trim_context, _context_status).
                    # They are system-injected, not LLM actions — showing them
                    # as tool widgets confuses the human user.
                    if tc.get("name", "").startswith("_"):
                        continue
                    tcid = tc["id"]
                    result = tool_results.get(tcid, "")
                    is_error = tool_errors.get(tcid, False)
                    widget = ToolCallWidget(
                        tool_name=tc["name"],
                        tool_args=tc["arguments"],
                        tool_call_id=tcid,
                    )
                    chat_view.mount(widget)
                    widget.set_complete(result, is_error)

    # ── Post-restore setup ────────────────────────────────────────────
    # Still under suppressed auto-scroll — the system message must not
    # scroll by itself; the single final scroll below covers it.
    skipped = recovery_info.get("skipped", 0)  # legacy field, now always 0
    if skipped > 0:
        _show_system_message(
            app,
            t("restored_partial", n=len(turns), skipped=skipped),
            color="#3fb950",
        )
    else:
        _show_system_message(app, t("restored_ok"), color="#3fb950")

    # Auto-scroll is live again; settle the view with ONE scroll.
    chat_view._autoscroll = True
    chat_view.scroll_end(animate=False)

    # Reset session token counter — session starts fresh
    app.service.session_usage.total_tokens = 0

    # Prime the context footer with the restored context size.  On the
    # first round we have no real API usage yet, so `context_tokens_for` /
    # the status bar fall back to `_last_usage`.  Use the **latest restored
    # turn's persisted prompt_tokens** — the exact context size at exit
    # (what _sys_note would have reported) — instead of an estimate.
    # Legacy turns predate the column → fall back to the token estimate.
    last_turn = turns[-1] if turns else {}
    prompt = last_turn.get("prompt_tokens") or 0
    if prompt <= 0:
        prompt = estimate_turn_tokens(last_turn) if last_turn else 0
    if prompt > 0:
        app.service.agent_loop._last_usage = TokenUsage(
            prompt_tokens=prompt,
            total_tokens=prompt,
        )
    app._update_status()


def _show_system_message(app: "SlifeApp", text: str, color: str | None = None) -> None:
    """Show a system message in the chat view."""
    chat_view = app.query_one("#chat-view", ChatView)
    chat_view.add_system_message(text, color=color)
