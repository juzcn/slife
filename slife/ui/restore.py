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


# ── Image BLOB restore ────────────────────────────────────────────────
#
# Design contract (see DESIGN.md "Session Restore"): image cache files
# are ephemeral — on restart images are reconstructed from the
# diary_images BLOB table, never from the cache directory.  The
# resolution chain per ``[image: <path>]`` marker is:
#
#   1. BLOB by image_id (= marker filename stem) → write back to the
#      canonical images dir → render;
#   2. no BLOB, but the marker path exists on disk (legacy markers
#      that predate BLOB storage) → render the original file;
#   3. neither → text placeholder — never silently drop the image.

# MIME → extension map for BLOBs whose file_name carries no suffix.
_MIME_EXT: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}


async def _load_blobs(
    image_ids: list[str], db_path: Path | None = None,
) -> dict[str, tuple[bytes, str, str]]:
    """Batch-read BLOBs for *image_ids* — one connection, one query.

    Returns ``{image_id: (data, mime_type, file_name)}``.  Any DB
    failure yields an empty dict so callers degrade to the disk /
    placeholder fallbacks instead of aborting the restore.
    """
    import aiosqlite

    if not image_ids:
        return {}

    resolved = db_path or _resolve_db_path()
    if not resolved or not resolved.is_file():
        return {}

    try:
        conn = await aiosqlite.connect(str(resolved))
        try:
            placeholders = ",".join("?" for _ in image_ids)
            cursor = await conn.execute(
                f"SELECT image_id, data, mime_type, file_name "
                f"FROM diary_images WHERE image_id IN ({placeholders})",
                image_ids,
            )
            rows = await cursor.fetchall()
            return {
                row[0]: (row[1], row[2] or "", row[3] or "")
                for row in rows
            }
        finally:
            await conn.close()
    except Exception as e:
        logger.debug("blob_restore_error stage=read err=%s", e)
        return {}


def _blob_extension(file_name: str, mime_type: str, marker_suffix: str) -> str:
    """Pick an output extension for a restored BLOB.

    Preference: original file_name suffix → mime_type map → the marker
    path's suffix → ``.png``.
    """
    name_ext = Path(file_name).suffix.lower() if file_name else ""
    if name_ext:
        return name_ext
    if mime_type in _MIME_EXT:
        return _MIME_EXT[mime_type]
    return marker_suffix or ".png"


async def resolve_pending_images(
    pending: list[tuple[str, "ChatView", "ToolCallWidget"]],
    db_path: Path | None = None,
) -> list[tuple[str | None, str, "ChatView", "ToolCallWidget"]]:
    """Resolve pending image markers to renderable file paths.

    Runs the resolution chain (BLOB → original file → ``None``
    placeholder) for every ``(marker_path, chat_view, after_widget)``
    spec with a single DB round-trip.  BLOBs are written back to the
    canonical images dir once per unique image_id.

    Returns one ``(resolved_path_or_None, marker_path, chat_view,
    after_widget)`` entry per input spec, in input order.
    """
    from slife.paths import get_images_dir

    if not pending:
        return []

    # Unique image ids for the batch query; unique marker paths so a
    # marker repeated across turns is resolved (and written) once.
    image_ids: list[str] = []
    seen_ids: set[str] = set()
    for marker, _cv, _aw in pending:
        iid = Path(marker).stem
        if iid and iid not in seen_ids:
            image_ids.append(iid)
            seen_ids.add(iid)

    blobs = await _load_blobs(image_ids, db_path)

    resolved_by_marker: dict[str, str | None] = {}
    written_by_id: dict[str, str] = {}
    for marker, _cv, _aw in pending:
        if marker in resolved_by_marker:
            continue

        p = Path(marker)
        image_id = p.stem
        blob = blobs.get(image_id)

        if blob is not None:
            data, mime_type, file_name = blob
            if image_id in written_by_id:
                resolved_by_marker[marker] = written_by_id[image_id]
                continue
            try:
                # Always write restored images to the canonical cache
                # directory, not the original marker path (which may be
                # from an old format or an arbitrary filesystem location).
                images_dir = get_images_dir()
                images_dir.mkdir(parents=True, exist_ok=True)
                ext = _blob_extension(file_name, mime_type, p.suffix.lower())
                output_path = images_dir / f"{image_id}{ext}"
                output_path.write_bytes(data)
                out = str(output_path.resolve())
                written_by_id[image_id] = out
                resolved_by_marker[marker] = out
                logger.debug(
                    "blob_restore_ok image_id=%s size=%d path=%s",
                    image_id, len(data), output_path,
                )
            except Exception as e:
                logger.debug(
                    "blob_restore_error image_id=%s err=%s", image_id, e,
                )
                resolved_by_marker[marker] = None
            continue

        # No BLOB — fall back to the marker path itself when the file
        # still exists (legacy markers predating BLOB storage).
        if p.exists() and p.is_file():
            logger.debug(
                "blob_restore_missing image_id=%s fallback=original", image_id,
            )
            resolved_by_marker[marker] = str(p.resolve())
        else:
            logger.debug(
                "blob_restore_missing image_id=%s fallback=placeholder", image_id,
            )
            resolved_by_marker[marker] = None

    return [
        (resolved_by_marker[marker], marker, cv, aw)
        for marker, cv, aw in pending
    ]


def _resolve_db_path() -> Path | None:
    """Resolve the memory DB path from env or the data directory."""
    import os as _os
    from slife.paths import get_data_dir

    env_db = _os.environ.get("SLIFE_MEMORY_DB")
    if env_db:
        return Path(env_db)
    return get_data_dir() / f"{_os.environ.get('SLIFE_AGENT_ID', 'slife')}.db"


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
    """
    from slife.ui.image_utils import safe_image_widget

    widget = safe_image_widget(
        resolved_path or marker_path, css_class="chat-image",
    )
    if after_widget is not None:
        chat_view.mount(widget, after=after_widget)
    else:
        chat_view.mount(widget)
    chat_view.call_after_refresh(chat_view.scroll_end, animate=False)
    logger.debug(
        "image_mount widget=%s resolved=%s",
        type(widget).__name__, bool(resolved_path),
    )


def _schedule_image_mounts(
    app: "SlifeApp",
    resolved: list[tuple[str | None, str, "ChatView", "ToolCallWidget"]],
) -> None:
    """Schedule image widget mounts with staggered timers so each
    HalfcellImage gets its own compositor cycle.

    All DB I/O already happened in the resolve phase — timers only
    mount pre-resolved widgets.  Does NOT block."""
    for i, (path, marker, cv, after_widget) in enumerate(resolved):
        app.set_timer(
            0.5 + i * 0.2,
            lambda p=path, m=marker, c=cv, a=after_widget:
                _mount_resolved_image(p, m, c, a),
        )


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
                    # Extract markers WITHOUT an existence check — the
                    # cache files are ephemeral; the BLOB table is the
                    # source of truth (resolve_pending_images handles
                    # the BLOB → file → placeholder chain).
                    imgs = extract_image_markers(content)
                    if imgs:
                        tool_images[tcid] = imgs

        # Build UI ops
        ui_ops: list[dict] = []
        assistant_indices = [
            i for i, m in enumerate(all_messages)
            if m.get("role") == "assistant"
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
                ui_ops.append({
                    "type": "user",
                    "content": msg.get("content", "") or "",
                    "images": msg.get("images"),
                    "prefix": prefix,
                })
            elif role == "assistant":
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

    except Exception as e:
        _show_system_message(app, f"✗ 恢复失败: {e}", color="#f85149")
        return

    # ── Phase 2: Replace conversation messages ────────────────────────
    conversation.messages = all_messages

    # ── Phase 3: Rebuild UI ───────────────────────────────────────────
    chat_view = app.query_one("#chat-view", ChatView)

    # Collect image paths to render one-at-a-time after the batch.
    # HalfcellImage needs its own refresh cycle — mounting multiple
    # instances inside a single batch_update causes only the last
    # one to render (textual-image known issue).
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

    # Phase 3b: Resolve images from the BLOB table (single DB
    # round-trip), then schedule staggered widget mounts.  Does not
    # block — timers fire after the app is running.
    if _pending_images:
        resolved_images = await resolve_pending_images(_pending_images)
        _schedule_image_mounts(app, resolved_images)

    # ── Post-restore setup ────────────────────────────────────────────
    if skipped > 0:
        _show_system_message(
            app,
            f"✅ 已恢复最近 {len(turns)} 轮对话"
            f"（{skipped} 轮旧记录未加载，可用 memory_search 查找）",
            color="#3fb950",
        )
    else:
        _show_system_message(app, "✅ 已恢复对话，继续吧", color="#3fb950")

    # Reset session token counter — session starts fresh
    app.service.session_usage.total_tokens = 0

    # Prime the context footer with the restored token estimate
    if tokens_selected > 0:
        app.service.agent_loop._last_usage = TokenUsage(
            total_tokens=tokens_selected,
        )
    app._update_status()


def _show_system_message(app: "SlifeApp", text: str, color: str | None = None) -> None:
    """Show a system message in the chat view."""
    chat_view = app.query_one("#chat-view", ChatView)
    chat_view.add_system_message(text, color=color)
