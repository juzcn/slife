"""Session restore — put a previous session back on the screen.

The rebuild itself lives in :mod:`slife.agent.session`: the headless host
restores the same history from the same exit-time context and has no widgets
to put it in, so the part that is neither UI nor agent moved out of here.
What remains is the rendering — turning the plan's ops into mounted widgets,
in one batch, with the scroll handled once at the end.
"""

from __future__ import annotations

import logging

from slife.agent.session import restore_context
from slife.ui.chat import ChatView
from slife.ui.i18n import t
from slife.ui.tool_display import ToolCallWidget

logger = logging.getLogger(__name__)


# ── Main restore orchestrator ─────────────────────────────────────────


async def restore_session(
    app,
    turns: list[dict],
    history,
    assistant_prefix: str,
) -> None:
    """Rebuild the chat view from *turns* (the resolved exit-time context).

    *turns* is the **exit-time context**, already resolved by
    ``get_exit_context_turns`` — the turns named by the persisted
    live-context list, in that list's order.  :func:`restore_context` replays
    it into *history* and hands back the rendering plan; this function mounts
    that plan.
    """
    if not turns:
        return

    try:
        plan = restore_context(app.service, turns, history, assistant_prefix)
    except Exception as e:
        # The red line reaches the reader; ``restore_context`` already logged
        # the traceback for whoever has to explain it.  Returning normally
        # also means the caller's own handler never sees the exception, so
        # without this the only trace of a failed rebuild was a sentence in
        # the transcript and a log that simply stopped — which is what made an
        # earlier report of a dead transcript un-diagnosable.
        app._show_system_message(t("restore_failed", err=e), color="#f85149")
        return

    # ── Rebuild the view from the plan ────────────────────────────────
    chat_view = app.query_one("#chat-view", ChatView)

    # Suppress per-widget auto-scroll while rebuilding: the whole history
    # is mounted first, then the view scrolls to the end exactly once.
    # Scrolling on every widget (the live behaviour) is what made the
    # restore jitter.
    chat_view._autoscroll = False

    with app.batch_update():
        for op in plan.ui_ops:
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
                    # Skip harness notifications (_trim_context, _turn_prompt).
                    # They are system-injected, not LLM actions — showing them
                    # as tool widgets confuses the human user.
                    if tc.get("name", "").startswith("_"):
                        continue
                    tcid = tc["id"]
                    result = plan.tool_results.get(tcid, "")
                    is_error = plan.tool_errors.get(tcid, False)
                    widget = ToolCallWidget(
                        tool_name=tc["name"],
                        tool_args=tc["arguments"],
                    )
                    chat_view.mount(widget)
                    widget.set_complete(result, is_error)

    # ── Post-restore setup ────────────────────────────────────────────
    # Still under suppressed auto-scroll — the system message must not
    # scroll by itself; the single final scroll below covers it.
    app._show_system_message(t("restored_ok"), color="#3fb950")

    # Auto-scroll is live again; settle the view with ONE scroll.  The re-arm
    # is stated here for the reason ``jump_to_tail`` states it: ``scroll_end``
    # lands a refresh later, so content arriving in between would find
    # following still off.  Restore rebuilds from the top, so the reader is
    # put back at the tail whether or not they were there at exit.
    chat_view._autoscroll = True
    chat_view._at_tail = True
    chat_view.scroll_end(animate=False)

    app._update_status()
