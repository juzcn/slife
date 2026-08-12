"""Session restore — rebuild chat history from persistent memory.

Extracted from ``slife.ui.app`` to keep the TUI application focused on
its primary responsibility: UI event handling and layout.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from slife.agent.llm_client import TokenUsage
from slife.agent.loop import extract_image_markers
from slife.ui.chat import ChatView
from slife.ui.tool_display import ToolCallWidget

if TYPE_CHECKING:
    from slife.config import Config
    from slife.agent.conversation import Conversation
    from slife.ui.app import SlifeApp
    from slife.ui.chat import ChatView

logger = logging.getLogger(__name__)


# ── Turn token estimation ─────────────────────────────────────────────


def estimate_turn_tokens(turn: dict) -> int:
    """Estimate the *incremental* token cost of a single turn.

    Counts the user message plus the stored assistant/tool messages
    using the same chars/3 heuristic as ``Conversation.count_tokens``.
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


# ── Prefix mapping ────────────────────────────────────────────────────


def restore_prefix(channel: str | None, _agent_id: str) -> str:
    """Consistent prefix mapping for restored turns.

    Matches the real-time display prefixes used during live operation:
      - human  → "You> "
      - wechat → "<agent_id>(Wechat)"
      - other   → "<remote_agent_id>(a2a)" (external agent id, A2A peer, etc.)
    """
    ch = channel or ""
    if ch == "human":
        return "You> "
    if ch == "wechat":
        return "You(Wechat)> "
    if ch:
        return f"{ch}(a2a)"
    return "You> "


# ── Image restore (file-exists only — no BLOBs) ──────────────────────
#
# Image markers (``[image: <path>]``) point at files on disk.  On
# session restore the file either still exists → render, or it doesn't
# → text placeholder.  No BLOB table, no DB round-trip.


async def resolve_pending_images(
    pending: list[tuple[str, "ChatView", "ToolCallWidget"]],
) -> list[tuple[str | None, str, "ChatView", "ToolCallWidget"]]:
    """Resolve image markers — file exists → path, otherwise → None."""
    if not pending:
        return []

    result: list[tuple[str | None, str, "ChatView", "ToolCallWidget"]] = []
    for marker, cv, aw in pending:
        p = Path(marker)
        if p.exists() and p.is_file():
            resolved = str(p.resolve())
            result.append((resolved, marker, cv, aw))
            logger.info("restore_image_resolved marker=%s → file", marker)
        else:
            logger.info("restore_image_missing marker=%s — no file on disk", marker)
            result.append((None, marker, cv, aw))
    return result


# ── Safe arg parse ────────────────────────────────────────────────────


def _safe_parse_args(raw: str) -> dict:
    """Parse a tool-call arguments JSON string, falling back gracefully."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw}


# ── Chained image restore ─────────────────────────────────────────────


def _mount_resolved_image(
    resolved_path: str | None,
    marker_path: str,
    chat_view: "ChatView",
    after_widget: "ToolCallWidget | None",
) -> None:
    """Mount one restored image (or its placeholder) in the chat view.

    ``resolved_path=None`` mounts the broken-file placeholder from
    ``safe_image_widget`` using the marker path, so an image that has
    no BLOB and no file still shows as ``⚠ <filename>`` instead of
    silently disappearing.

    Does NOT scroll — the single restore scroll happens once, after the
    last image mounts (see :func:`_schedule_image_mounts`).
    """
    from slife.ui.image_utils import safe_image_widget

    widget = safe_image_widget(
        resolved_path or marker_path, css_class="chat-image",
    )
    if after_widget is not None:
        chat_view.mount(widget, after=after_widget)
        # Mounting with 'after=' can leave HalfcellImage at zero
        # height because the surrounding layout wasn't invalidated
        # for the insert position.  Explicit refresh fixes it.
        widget.refresh(layout=True)
    else:
        chat_view.mount(widget)
    logger.info(
        "image_mount widget=%s resolved=%s",
        type(widget).__name__, bool(resolved_path),
    )


def _schedule_image_mounts(
    app: "SlifeApp",
    chat_view: "ChatView",
    resolved: list[tuple[str | None, str, "ChatView", "ToolCallWidget"]],
) -> None:
    """Mount restored images one per compositor cycle, then scroll ONCE.

    ``textual-image`` only paints an image if it gets its own compositor
    cycle — mounting several images in a single pass lays them out but
    paints at most the last one (they never echo).

    Timers scheduled all-at-once can still land in the same message-pump
    batch.  Instead, each mount schedules the *next* timer from within
    its own callback.  This guarantees each ``HalfcellImage`` is mounted
    in a separate event-loop tick, giving Textual idle time for a
    compositor cycle between images.

    Jitter is avoided by NOT scrolling per image: the caller has already
    suppressed ``ChatView`` auto-scroll, so these mounts do not move the
    viewport at all.  Exactly one scroll-to-end is scheduled after the
    final image mounts.

    All DB I/O already happened in the resolve phase — callbacks only
    mount pre-resolved widgets.  Does NOT block.
    """
    n = len(resolved)
    _GAP = 0.06  # seconds between mounts — enough for a compositor tick

    def _schedule_next(i: int) -> None:
        if i >= n:
            return
        path, marker, _cv, after_widget = resolved[i]
        is_last = (i == n - 1)
        logger.info(
            "restore_mount_step i=%d/%d path=%s is_last=%s",
            i + 1, n, path, is_last,
        )
        _mount_resolved_image(path, marker, chat_view, after_widget)
        if is_last:
            chat_view.call_after_refresh(
                chat_view.scroll_end, animate=False,
            )
        else:
            app.set_timer(_GAP, lambda: _schedule_next(i + 1))

    if n > 0:
        logger.info("restore_mount_start count=%d gap=%.2fs", n, _GAP)
        _schedule_next(0)
    else:
        chat_view.scroll_end(animate=False)


# ── Main restore orchestrator ─────────────────────────────────────────


async def restore_session(
    app: "SlifeApp",
    recovery_info: dict,
    conversation: "Conversation",
    config: "Config",
    agent_id: str,
    assistant_prefix: str,
) -> None:
    """Restore a previous session from turn-based memory.

    Loads only the most recent turns that fit within ``context_floor``
    of the model's context window.  Older turns stay in the memory DB
    and can be retrieved via ``memory_search`` if needed.

    This function is self-contained — it reads recovery_info, rebuilds
    the conversation message list, and reconstructs the chat UI.
    """
    all_turns: list[dict] = recovery_info.get("turns", [])
    if not all_turns:
        return

    # ── Select turns within token budget (newest-first, cap at floor) ──
    context_window = config.active_model.context_window
    context_floor = config.context_floor
    token_budget = int(context_window * context_floor)

    turns: list[dict] = []
    tokens_selected = 0
    for turn in reversed(all_turns):
        t = estimate_turn_tokens(turn)
        if turns and tokens_selected + t > token_budget:
            break
        turns.append(turn)
        tokens_selected += t
    turns.reverse()

    skipped = len(all_turns) - len(turns)
    if skipped > 0:
        logger.debug(
            "session_restore_trimmed loaded=%d skipped=%d budget=%d selected=%d",
            len(turns), skipped, token_budget, tokens_selected,
        )

    # ── Phase 1: Reconstruct message list from selected turns ─────────
    try:
        sys_msg = (
            conversation.messages[0]
            if conversation.messages
            and conversation.messages[0].get("role") == "system"
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
            all_messages.append({
                "role": "user",
                "content": user_msg_text,
            })
            all_messages.extend(turn_msgs)

        # Build tool-result lookup
        tool_results: dict[str, str] = {}
        tool_errors: dict[str, bool] = {}
        tool_images: dict[str, list[str]] = {}
        for msg in all_messages:
            if msg.get("role") == "tool":
                tcid = msg.get("tool_call_id", "")
                if tcid:
                    content = msg.get("content", "") or ""
                    tool_results[tcid] = content
                    tool_errors[tcid] = msg.get("is_error", False)
                    # Extract markers WITHOUT an existence check —
                    # resolve_pending_images later resolves each path
                    # against the filesystem (file exists → render,
                    # file gone → ⚠ placeholder).
                    imgs = extract_image_markers(content)
                    if imgs:
                        tool_images[tcid] = imgs
                        logger.info(
                            "restore_markers_found tcid=%s count=%d paths=%s",
                            tcid, len(imgs), imgs,
                        )

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
        for idx, msg in enumerate(all_messages):
            role = msg.get("role", "")
            if role == "system":
                continue
            elif role == "user":
                turn_idx += 1
                ch = _channel_by_row.get(turn_idx, "")
                prefix = restore_prefix(ch, agent_id)
                raw = msg.get("content", "") or ""
                ui_ops.append({
                    "type": "user",
                    "content": raw,
                    "images": msg.get("images"),
                    "prefix": prefix,
                })
            elif role == "assistant":
                # Harness messages (_sys_note, _sys_trim) exist so the
                # LLM sees system status in context.  They are never
                # visible in the live TUI — skip their widgets here.
                tcs = msg.get("tool_calls") or []
                if (
                    msg.get("content") in (None, "")
                    and not msg.get("thinking")
                    and tcs
                    and all(
                        tc.get("function", {}).get("name", "").startswith("_")
                        for tc in tcs
                    )
                ):
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
                })
            elif role == "tool":
                pass

        # The very last assistant message in the restored conversation
        # should mirror live-session behaviour: thinking expanded, reply
        # visible.  Walk backwards through ui_ops and tag the last one.
        for op in reversed(ui_ops):
            if op.get("type") == "assistant":
                op["is_final"] = True
                break

    except Exception as e:
        _show_system_message(app, f"✗ 恢复失败: {e}", color="#f85149")
        return

    # ── Phase 2: Replace conversation messages ────────────────────────
    conversation.messages = all_messages
    # Load-side invariant: a restored turn may contain an orphaned tool_call
    # (persisted by a pre-ensure session) — repair it and keep roles
    # alternating, mirroring the save-side ensure in save_to_memory.
    conversation._ensure_turn_consistent()

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

    # Collect image paths to render one-at-a-time after the batch.
    # textual-image needs its own refresh cycle per image — mounting
    # several in a single pass paints at most the last one.
    _pending_images: list[tuple[str, "ChatView", "ToolCallWidget"]] = []

    with app.batch_update():
        for op in ui_ops:
            if op["type"] == "user":
                chat_view.add_user_message(
                    op["content"],
                    images=op.get("images"),
                    prefix=op["prefix"],
                )
            elif op["type"] == "assistant":
                am = chat_view.add_assistant_message(
                    name_prefix=op.get("name_prefix"),
                )
                thinking = op.get("thinking", "")
                if thinking:
                    am.append_thinking(thinking)
                text = op.get("content", "")
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
                    for img_path in tool_images.get(tcid, []):
                        _pending_images.append((img_path, chat_view, widget))
                    logger.debug(
                        "restore_pending_add tcid=%s tool=%s imgs=%d",
                        tcid, tc.get("name", "?"), len(tool_images.get(tcid, [])),
                    )

    # ── Post-restore setup ────────────────────────────────────────────
    # Still under suppressed auto-scroll — the system message must not
    # scroll by itself; the single final scroll below covers it.
    if skipped > 0:
        _show_system_message(
            app,
            f"✅ 已恢复最近 {len(turns)} 轮对话"
            f"（{skipped} 轮旧记录未加载，可用 memory_search 查找）",
            color="#3fb950",
        )
    else:
        _show_system_message(app, "✅ 已恢复对话，继续吧", color="#3fb950")

    # Auto-scroll is live again; settle the view with ONE scroll.
    chat_view._autoscroll = True
    if _pending_images:
        logger.info(
            "restore_pending_total count=%d paths=%s",
            len(_pending_images),
            [p for p, _, _ in _pending_images],
        )
        # Phase 3b: resolve markers, then stagger-mount the images (one
        # compositor cycle each so textual-image paints them); the last
        # mount performs the single scroll-to-end.
        resolved_images = await resolve_pending_images(_pending_images)
        _schedule_image_mounts(app, chat_view, resolved_images)
    else:
        chat_view.scroll_end(animate=False)

    # Reset session token counter — session starts fresh
    app.service.session_usage.total_tokens = 0

    # Prime the context footer with the restored token estimate.  This is
    # the estimated context size after restore (computed above to decide
    # how many turns to restore) — on the first round we have no real API
    # usage yet, so `context_tokens_for` / the status bar fall back to it.
    # Stored as prompt_tokens because that is semantically what it is.
    if tokens_selected > 0:
        app.service.agent_loop._last_usage = TokenUsage(
            prompt_tokens=tokens_selected,
            total_tokens=tokens_selected,
        )
    app._update_status()


def _show_system_message(app: "SlifeApp", text: str, color: str | None = None) -> None:
    """Show a system message in the chat view."""
    chat_view = app.query_one("#chat-view", ChatView)
    chat_view.add_system_message(text, color=color)
