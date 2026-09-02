"""slife-wechat server — FastMCP server for WeChat iLink ClawBot messaging.

Bidirectional WeChat integration:
  - Auto-restores session from ``wechat_<user>.json5`` on startup.
  - Background poll loop fetches incoming messages continuously.
  - LLM tools: wechat_login, wechat_send_message, wechat_check_status, wechat_logout.

Usage:
    uv run python -m slife.plugins.wechat.server       # auto-assigned port (Streamable HTTP)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from slife.plugins.wechat.client import WechatClawbotClient, BASE_URL
from slife.plugins.wechat.config import (
    load_wechat_config,
    save_wechat_config,
    clear_wechat_config,
)
from slife.server_utils import create_plugin_server
from slife.logfmt import error_json

SESSION_MAX_AGE = WechatClawbotClient.SESSION_MAX_AGE

@asynccontextmanager
async def _wechat_lifespan(_app):
    """Graceful shutdown: stop the poll / QR / typing background tasks
   ."""
    try:
        yield
    finally:
        global _poll_task, _qr_task, _typing_tasks
        for t in list(_typing_tasks.values()):
            t.cancel()
        _typing_tasks.clear()
        if _qr_task is not None and not _qr_task.done():
            _qr_task.cancel()
        if _poll_task is not None and not _poll_task.done():
            _poll_task.cancel()


mcp, _log_path, logger = create_plugin_server(
    "slife-wechat",
    instructions=(
        "slife-wechat — bidirectional WeChat messaging. "
        "LLM tools: wechat_login (QR scan), wechat_send_message (proactive "
        "send), wechat_check_status, wechat_logout."
    ),
    lifespan=_wechat_lifespan,
)

# ── QR code rendering ────────────────────────────────────────────────────


def _render_qr_ascii(content: str) -> str:
    """Render a string as a compact terminal-scannable ASCII QR code.

    Uses qrcode library with half-block Unicode characters (█▀▄) to combine
    two QR rows per output row.
    """
    if not content:
        return ""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1, box_size=1)
        qr.add_data(content)
        qr.make(fit=True)
        matrix = qr.get_matrix()
        size = len(matrix)
        lines: list[str] = []
        for y in range(0, size, 2):
            row_chars: list[str] = []
            for x in range(size):
                top = matrix[y][x]
                bot = matrix[y + 1][x] if y + 1 < size else False
                if top and bot:
                    row_chars.append("█")
                elif top:
                    row_chars.append("▀")
                elif bot:
                    row_chars.append("▄")
                else:
                    row_chars.append(" ")
            lines.append("".join(row_chars))
        return "\n".join(lines)
    except ImportError:
        logger.warning("qrcode_lib_unavailable hint=install_qrcode")
        return content

# ── Global state ─────────────────────────────────────────────────────────

_client = WechatClawbotClient()
_agent_name: str = os.environ.get("SLIFE_AGENT_NAME", "slife")
from slife.paths import get_data_dir as _get_data_dir
_work_dir: Path = _get_data_dir()

# Background polling
_poll_task: asyncio.Task | None = None
_pending: deque[dict] = deque()
# Dedup key → last-seen monotonic time.  A windowed set: a repeated key is
# only a "re-delivery" (WeChat re-sends recent messages until the sync buffer
# advances) if seen within the window — a genuine repeat message sent later
# (e.g. "收到" twice minutes apart) is NOT dropped.  The old forever-set
# dropped every same-text repeat after the first, across polls.
_seen_keys: dict[str, float] = {}
_DEDUP_WINDOW = 30.0  # seconds — re-deliveries arrive well within this
_MAX_QUEUED = 200  # keep at most 200 pending messages

# Typing indicator keep-alive — per-conversation tasks managed by the server
_typing_tasks: dict[str, asyncio.Task] = {}
_TYPING_REFRESH = 8.0  # seconds between typing indicator refreshes
_TYPING_MAX_LIFETIME = 300.0  # keepalive bound — stops if the agent never replies

# QR login state (non-blocking)
_qr_task: asyncio.Task | None = None
_qr_status: str = ""  # "" | "waiting" | "scanned" | "confirmed" | "expired" | "error"
_qr_content: str = ""
_qr_error: str = ""
_QR_POLL_INTERVAL = 2.0  # seconds between QR status checks
_QR_MAX_REFRESH = 3

# ═══════════════════════════════════════════════════════════════════════════
# Background polling
# ═══════════════════════════════════════════════════════════════════════════


def _msg_key(msg: dict, text: str) -> str:
    """Dedup key: ``from_user_id + context_token + text``.

    ``context_token`` is per-conversation, so sender+token alone collides
    across every message in one conversation — a second real message would be
    dropped as a "duplicate".  Including the text makes only true
    re-deliveries (same sender, same conversation, same text) dedupe.
    """
    return (
        f"{msg.get('from_user_id', '')}::{msg.get('context_token', '')}::{text}"
    )


async def _poll_loop(poll_interval: float = 3.0) -> None:
    """Continuously poll WeChat for new messages, queueing them for the LLM."""
    global _pending, _seen_keys

    # Flush after every log so we can debug poll activity in real time
    logger.info("poll_loop_start interval=%.1fs", poll_interval)
    _flush_logs()

    backoff = poll_interval

    while _client.is_logged_in and not _client.auth_failed:
        try:
            msgs = await _client.poll_updates()
            _flush_logs()  # ensure POST debug lines hit disk
            new_count = 0
            # Keys seen in THIS poll — two genuine same-text messages in one
            # poll must both be delivered (a user sending "ok" twice in a row),
            # so only re-deliveries across polls are deduped.
            batch_seen: set[str] = set()
            for m in msgs:
                text = ""
                item_list = m.get("item_list", [])
                if item_list:
                    text_item = item_list[0].get("text_item", {})
                    text = text_item.get("text", "")

                # Non-text items (empty text) are skipped BEFORE the dedup key
                # is recorded — otherwise they'd burn the conversation key and
                # drop the next real message.
                if not text.strip():
                    continue

                key = _msg_key(m, text)
                now = time.monotonic()
                last_seen = _seen_keys.get(key)
                if (
                    last_seen is not None
                    and now - last_seen <= _DEDUP_WINDOW
                    and key not in batch_seen
                ):
                    continue  # true re-delivery seen within the window
                batch_seen.add(key)
                _seen_keys[key] = now

                from_id = m.get("from_user_id", "")
                ctx_token = m.get("context_token", "")

                # Remember last contact so send_message knows who to reply to
                _client.last_contact = {
                    "from_id": from_id,
                    "context_token": ctx_token,
                }

                _pending.append({
                    "to_user_id": from_id,
                    "context_token": ctx_token,
                    "text": text,
                    "message_type": m.get("message_type", 0),
                })
                new_count += 1

            # Trim if too many queued
            while len(_pending) > _MAX_QUEUED:
                _pending.popleft()
            while len(_seen_keys) > _MAX_QUEUED * 3:
                # Keep the map from growing unbounded — dict preserves
                # insertion order, so the oldest entries are first.
                for k in list(_seen_keys)[:_MAX_QUEUED]:
                    _seen_keys.pop(k, None)

            if new_count:
                logger.debug("poll_new msgs=%d queued=%d", new_count, len(_pending))

            backoff = poll_interval  # reset on success
        except Exception as e:
            logger.debug("poll_error err=%s", e)
            # Surface to the LLM: status reports degraded until the link
            # recovers (a successful poll clears the fault).
            _client.last_error = str(e)
            backoff = min(backoff * 1.5, 30.0)  # back off on errors
        await asyncio.sleep(backoff)

    logger.info("poll_loop_stop interval=%.1fs", poll_interval)


def _flush_logs() -> None:
    """Flush all log handlers to disk (for debugging poll activity)."""
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass


def _start_polling() -> None:
    """Launch the background poll task if not already running."""
    global _poll_task
    if _poll_task is not None and not _poll_task.done():
        return
    _poll_task = asyncio.create_task(_poll_loop())


def _stop_polling() -> None:
    """Cancel the background poll task and all typing keep-alives."""
    global _poll_task, _pending, _typing_tasks
    if _poll_task is not None and not _poll_task.done():
        _poll_task.cancel()
    _poll_task = None
    _pending.clear()
    # Cancel all typing keep-alive tasks
    for task in _typing_tasks.values():
        if not task.done():
            task.cancel()
    _typing_tasks.clear()


# ═══════════════════════════════════════════════════════════════════════════
# Typing indicator keep-alive (server-managed)
# ═══════════════════════════════════════════════════════════════════════════


def _start_typing_keepalive(from_id: str, ctx_token: str) -> None:
    """Start a background task that refreshes the typing indicator every ~8 s.

    The WeChat iLink typing indicator auto-expires after ~10-20 s.  This
    keep-alive runs inside the plugin process so the harness (AgentService)
    doesn't need to know about typing at all.
    """
    global _typing_tasks

    # Don't double-start for the same conversation
    if from_id in _typing_tasks and not _typing_tasks[from_id].done():
        return

    async def _keep_typing(uid: str, tok: str) -> None:
        # Bound the keepalive: the reply dispatch stops it for a normal turn,
        # but if the agent never replies (crash, or the stop is missed) it must
        # not loop forever.
        deadline = asyncio.get_event_loop().time() + _TYPING_MAX_LIFETIME
        while asyncio.get_event_loop().time() < deadline:
            try:
                await asyncio.sleep(_TYPING_REFRESH)
                await _client.send_typing(uid, tok, status=1)
            except asyncio.CancelledError:
                break
            except Exception:
                pass

    _typing_tasks[from_id] = asyncio.create_task(_keep_typing(from_id, ctx_token))


def _stop_typing_keepalive(from_id: str) -> None:
    """Cancel and remove the typing keep-alive task for a conversation."""
    global _typing_tasks
    task = _typing_tasks.pop(from_id, None)
    if task is not None and not task.done():
        task.cancel()


# ═══════════════════════════════════════════════════════════════════════════
# LLM-visible tools
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# Harness tools (programmatic only — not exposed to LLM)
# ═══════════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="__wechat_drain_incoming",
    description="Drain queued incoming WeChat messages. Internal — called by the agent service poll loop.",
)
async def __wechat_drain_incoming() -> str:
    """Return all queued incoming messages and auto-start typing for each.

    Called by the AgentService poll loop.  Each returned message gets
    a typing keep-alive started automatically so the WeChat user sees
    "对方正在输入…" while the agent processes the message.
    """
    global _pending

    if not _client.is_logged_in:
        return json.dumps({
            "messages": [],
            "status": "not_logged_in",
        }, ensure_ascii=False)

    msgs = list(_pending)
    _pending.clear()

    # Auto-start typing for each conversation that has a new message
    for m in msgs:
        from_id = m.get("to_user_id", "")
        ctx_token = m.get("context_token", "")
        if from_id:
            _start_typing_keepalive(from_id, ctx_token)
            # Fire an initial typing indicator immediately
            try:
                await _client.send_typing(from_id, ctx_token, status=1)
            except Exception:
                pass

    return json.dumps({
        "messages": msgs,
        "count": len(msgs),
        "status": "ok",
    }, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════════
# QR code login helpers
# ═══════════════════════════════════════════════════════════════════════════


async def _qr_poll_loop(qrcode: str, base_url: str, refresh_count: int = 0) -> None:
    """Background task: poll QR status until scanned, expired, or error."""
    global _client, _qr_status, _qr_content, _qr_error

    _qr_status = "waiting"
    deadline = asyncio.get_event_loop().time() + 600

    while asyncio.get_event_loop().time() < deadline:
        try:
            data = await _client._poll_login_status(qrcode, base_url)
        except Exception as e:
            logger.debug("qr_poll_error err=%s", e)
            await asyncio.sleep(_QR_POLL_INTERVAL)
            continue

        if data.get("bot_token"):
            bot_token = data["bot_token"]
            bu = data.get("baseurl", base_url)
            ilink_user_id = data.get("ilink_user_id", "")
            ilink_bot_id = data.get("ilink_bot_id", "")
            await _client.start(
                bot_token, bu,
                ilink_user_id=ilink_user_id,
                ilink_bot_id=ilink_bot_id,
            )
            # Store the user's WeChat ID so the LLM knows who to message
            if ilink_user_id:
                _client.last_contact = {
                    "from_id": ilink_user_id,
                    "context_token": "",
                }
            # Save with ilink_user_id for session-restore across restarts
            session_dict = _client.get_session_dict()
            session_dict["ilink_user_id"] = ilink_user_id
            save_wechat_config(_agent_name, session_dict, _work_dir)
            _client.clear_session_faults()
            _start_polling()
            _qr_status = "confirmed"
            logger.info("qr_login_confirmed user_id=%s", ilink_user_id)
            return

        if data.get("expired"):
            if refresh_count < _QR_MAX_REFRESH:
                logger.info("qr_expired refreshing %d/%d", refresh_count + 1, _QR_MAX_REFRESH)
                try:
                    new_data = await _client._fetch_qrcode(base_url)
                    new_qr = new_data.get("qrcode", "")
                    img = new_data.get("qrcode_img_content", "")
                    _qr_content = str(img or new_qr)
                    _qr_status = "waiting"
                    # Recurse with refreshed QR
                    await _qr_poll_loop(new_qr, base_url, refresh_count + 1)
                    return
                except Exception as e:
                    logger.exception("qr_refresh_failed refresh_count=%d", refresh_count)
                    _qr_status = "error"
                    _qr_error = f"QR refresh failed: {e}"
                    return
            else:
                _qr_status = "expired"
                _qr_error = "QR code expired after 3 refreshes. Call wechat_login again."
                return

        if data.get("scanned"):
            _qr_status = "scanned"

        if data.get("verify_code_blocked"):
            _qr_status = "error"
            _qr_error = "Verify code blocked. Call wechat_login again."
            return

        await asyncio.sleep(_QR_POLL_INTERVAL)

    _qr_status = "error"
    _qr_error = "Login timed out (10 min). Call wechat_login again."


@mcp.tool(
    name="wechat_login",
    description=(
        "Generate a WeChat login QR code. Copy the ASCII QR block verbatim "
        "into your reply — the user cannot see tool output. Then poll "
        "wechat_check_status until login completes."
    ),
)
async def wechat_login() -> str:
    global _client, _qr_task, _qr_status, _qr_content, _qr_error

    if _client.is_logged_in and not _client.auth_failed:
        return json.dumps({
            "status": "already_logged_in",
            "hint": "Already logged in. Call wechat_logout first to switch accounts.",
        }, ensure_ascii=False, indent=2)
    if _client.auth_failed:
        # Stale rejected token — drop it so a fresh QR scan can proceed.
        _client.reject_session()
        clear_wechat_config(_agent_name, _work_dir)

    # Reset QR state
    _qr_status = ""
    _qr_content = ""
    _qr_error = ""

    try:
        data = await _client._fetch_qrcode(BASE_URL)
    except Exception as e:
        logger.exception("qr_fetch_failed")
        return error_json(str(e))

    qrcode = data.get("qrcode", "")
    img = data.get("qrcode_img_content", "")
    _qr_content = str(img or qrcode)
    logger.info("qr_fetched qrcode=%s", qrcode)

    # Start background QR polling
    if _qr_task is not None and not _qr_task.done():
        _qr_task.cancel()
    _qr_task = asyncio.create_task(_qr_poll_loop(qrcode, BASE_URL))

    qr_ascii = _render_qr_ascii(_qr_content)

    return "\n".join([
        "SHOW THIS QR IN YOUR REPLY:",
        qr_ascii,
        "Scan with WeChat. Call wechat_check_status to track. ~10min expiry.",
    ])


@mcp.tool(
    name="wechat_send_message",
    description=(
        "Send a text message to the WeChat user you are chatting with. "
        "peer_wechat_id/context_token from wechat_check_status.last_contact; "
        "context_token may be empty for the first message."
    ),
)
async def wechat_send_message(
    peer_wechat_id: str = "",
    context_token: str = "",
    text: str = "",
) -> str:
    """Send a text message to the WeChat user you are chatting with.

    Args:
        peer_wechat_id: The WeChat user id you are talking to (from
            wechat_check_status.last_contact / the [WECHAT: ...] input marker).
        context_token: Conversation token for replying in a thread; may be empty for the first message.
        text: The message body.
    """
    global _client

    if not _client.is_logged_in:
        return error_json("Not logged in. Call wechat_login first.")

    if not peer_wechat_id.strip() or not text.strip():
        return error_json("Both peer_wechat_id and text are required and must be non-empty.")

    # Stop the typing keep-alive for this conversation — the reply is going
    # out now; without this the "正在输入…" indicator would keep refreshing
    # past the sent message until its keep-alive bound.
    _stop_typing_keepalive(peer_wechat_id)

    try:
        result = await _client.send_message(peer_wechat_id, context_token or "", text)
        # Hide typing indicator after reply
        try:
            await _client.send_typing(peer_wechat_id, context_token or "", status=2)
        except Exception:
            pass
        logger.debug("sent to=%s len=%d", peer_wechat_id, len(text))
        out = {"status": "sent"}
        if isinstance(result, dict) and result.get("message_id"):
            out["message_id"] = result["message_id"]
        return json.dumps(out, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("send_failed to=%s", peer_wechat_id)
        return error_json(str(e))


@mcp.tool(
    name="__check",
    description=(
        "WeChat login/session raw status: logged_in, auth_failed, last_error, "
        "session facts. Internal — probed by the harness's system_health, "
        "never exposed to the LLM."
    ),
)
async def __check() -> str:
    """Return raw WeChat session facts for the harness's health check.

    Unlike ``wechat_check_status`` — which can trigger a session restore —
    this probe only reads current state.  Internal: probed by the harness's
    ``system_health``; never exposed to the LLM.  Facts only — no health
    levels, no remediation hints.
    """
    saved = load_wechat_config(_agent_name, _work_dir)
    session: dict = {
        "saved": False,
        "saved_at": 0.0,
        "age_h": 0.0,
        "max_age_h": round(SESSION_MAX_AGE / 3600, 1),
    }
    if _client.is_logged_in:
        s = _client.get_session_dict()
        saved_at = s.get("saved_at", time.time())
        session["saved"] = True
        session["saved_at"] = saved_at
        session["age_h"] = round(max(0, time.time() - saved_at) / 3600, 1)
    elif saved.get("bot_token"):
        saved_at = saved.get("saved_at", time.time())
        session["saved"] = True
        session["saved_at"] = saved_at
        session["age_h"] = round(max(0, time.time() - saved_at) / 3600, 1)
    facts = {
        "logged_in": _client.is_logged_in,
        "auth_failed": bool(_client.auth_failed),
        "last_error": _client.last_error or "",
        "session": session,
    }
    return json.dumps(facts, ensure_ascii=False, indent=2)


def _last_contact_entry(user_id: str, ctx: str | None = "") -> dict | None:
    """Stable ``last_contact`` shape for wechat_check_status.

    ``peer_wechat_id`` is the WeChat user id of the person who last messaged
    the bot — exactly who a reply goes back to.  One semantic key (not
    from_user_id/to_user_id flipping between paths) so an LLM can pass it
    straight to ``wechat_send_message.peer_wechat_id`` regardless of whether
    the status came from a session restore or live polling.
    """
    if not user_id:
        return None
    return {
        "peer_wechat_id": user_id,
        "context_token": ctx,
    }


@mcp.tool(
    name="wechat_check_status",
    description=(
        "WeChat connection status: logged in?, time remaining, polling "
        "active, last_contact (peer_wechat_id + context_token)."
    ),
)
async def wechat_check_status() -> str:
    global _client, _poll_task, _qr_status, _qr_content, _qr_error

    # If a QR login is in progress, report its state
    if _qr_status and not _client.is_logged_in:
        qr_info = {
            "status": "qr_pending",
            "qr_state": _qr_status,
        }
        if _qr_status == "waiting":
            qr_info["hint"] = (
                "QR code waiting to be scanned — tell the user to open WeChat "
                "and scan. " + (f"QR link: {_qr_content}" if _qr_content else "")
            )
        elif _qr_status == "scanned":
            qr_info["hint"] = (
                "QR code scanned — waiting for confirmation on the phone."
            )
        elif _qr_status == "expired":
            qr_info["hint"] = "QR code expired. Call wechat_login again."
        elif _qr_status == "error":
            qr_info["hint"] = f"QR login error: {_qr_error}. Call wechat_login again."
        return json.dumps(qr_info, ensure_ascii=False, indent=2)

    if not _client.is_logged_in:
        saved = load_wechat_config(_agent_name, _work_dir)
        if saved.get("bot_token"):
            try:
                restored = await _client.try_restore_session(saved)
                # try_restore_session only checks age — confirm the token
                # actually works before reporting "restored"/logged_in.
                if restored and not await _client.validate_session():
                    if _client.auth_failed:
                        # Server rejected the token — forget it so the next
                        # status read is not a phantom "logged_in".
                        logger.warning(
                            "wechat_restore_invalid_token — re-login required",
                        )
                        _client.reject_session()
                        clear_wechat_config(_agent_name, _work_dir)
                        return json.dumps({
                            "status": "not_logged_in",
                            "polling": False,
                            "hint": ("WeChat session was rejected by the server — "
                                     "call wechat_login to re-scan the QR code."),
                        }, ensure_ascii=False, indent=2)
                    # Network/API failure while validating — keep the saved
                    # session but tell the LLM the link is currently down.
                    return json.dumps({
                        "status": "degraded",
                        "polling": False,
                        "hint": ("WeChat server unreachable during session check "
                                 "- retry, or call wechat_login if it persists. "
                                 f"Last error: {_client.last_error}"),
                    }, ensure_ascii=False, indent=2)
                if restored:
                    _client.clear_session_faults()
                    # Restore last contact so the LLM knows who to message
                    ilink_uid = saved.get("ilink_user_id", "")
                    if ilink_uid:
                        _client.last_contact = {
                            "from_id": ilink_uid,
                            "context_token": "",
                        }
                    _start_polling()
                    session = _client.get_session_dict()
                    age = time.time() - session.get("saved_at", time.time())
                    remaining = max(0, SESSION_MAX_AGE - age)
                    return json.dumps({
                        "status": "restored",
                        "remaining_hours": round(remaining / 3600, 1),
                        "polling": _poll_task is not None and not _poll_task.done(),
                        "last_contact": _last_contact_entry(ilink_uid, ""),
                    }, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.debug("restore_failed err=%s", e)

        return json.dumps({
            "status": "not_logged_in",
            "polling": False,
            "hint": "Not logged in. Call wechat_login to start the QR login flow.",
        }, ensure_ascii=False, indent=2)

    # Track last contact for proactive messaging
    last_contact = _client.last_contact if hasattr(_client, "last_contact") else {}
    last_from_id = last_contact.get("from_id", "")
    last_ctx = last_contact.get("context_token", "")

    session = _client.get_session_dict()
    age = time.time() - session.get("saved_at", time.time())
    remaining = max(0, SESSION_MAX_AGE - age)

    # Tell the LLM the truth when the link is broken: a revoked token needs
    # a fresh login, a network failure is transient.  A stale ``logged_in``
    # made the agent believe messaging worked while the poll loop spun on
    # connection errors.
    if _client.auth_failed:
        _client.reject_session()
        clear_wechat_config(_agent_name, _work_dir)
        return json.dumps({
            "status": "not_logged_in",
            "polling": False,
            "hint": ("WeChat session was rejected by the server — "
                     "call wechat_login to re-scan the QR code. "
                     f"Last error: {_client.last_error}"),
        }, ensure_ascii=False, indent=2)
    if _client.last_error:
        return json.dumps({
            "status": "degraded",
            "remaining_hours": round(remaining / 3600, 1),
            "polling": _poll_task is not None and not _poll_task.done(),
            "last_contact": (
                _last_contact_entry(last_from_id, last_ctx) if last_from_id else None
            ),
            "hint": ("WeChat link is down — messages will not arrive until it "
                     "recovers. "
                     f"Last error: {_client.last_error}"),
        }, ensure_ascii=False, indent=2)

    resp = {
        "status": "logged_in",
        "remaining_hours": round(remaining / 3600, 1),
        "polling": _poll_task is not None and not _poll_task.done(),
    }
    if last_from_id:
        resp["last_contact"] = _last_contact_entry(last_from_id, last_ctx)
    else:
        resp["hint"] = "No contacts yet — ask the WeChat user to send a message first."
    if remaining <= 0:
        resp["hint"] = "Session expired — call wechat_login to re-scan the QR code."

    return json.dumps(resp, ensure_ascii=False, indent=2)


@mcp.tool(
    name="wechat_logout",
    description=(
        "Log out of WeChat, clear the saved session, stop polling. "
        "Call wechat_login to reconnect."
    ),
)
async def wechat_logout() -> str:
    global _client

    _stop_polling()

    try:
        await _client.stop()
    except Exception as e:
        logger.debug("stop_error err=%s", e)

    _client = WechatClawbotClient()
    clear_wechat_config(_agent_name, _work_dir)

    return json.dumps({
        "status": "logged_out",
        "config_cleared": True,
        "hint": "Logged out. Call wechat_login to reconnect.",
    }, ensure_ascii=False, indent=2)


# ── Entry point ──────────────────────────────────────────────────────────


def main():
    """Run the slife-wechat server on Streamable HTTP transport.

    Session restore happens lazily on the first wechat_check_status call,
    inside FastMCP's own event loop — this avoids the aiohttp session
    being bound to a temporary loop that gets closed.
    """
    from slife.server_utils import run_plugin_server, shutdown_server_logging

    logger.info("wechat_start agent_name=%s log=%s pid=%s",
                _agent_name, _log_path, os.getpid())
    try:
        run_plugin_server(mcp)
    finally:
        logger.info("wechat_stop agent_name=%s", _agent_name)
        shutdown_server_logging()


if __name__ == "__main__":
    main()
