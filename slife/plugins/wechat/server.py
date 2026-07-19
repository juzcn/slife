"""slife-wechat server — FastMCP server for WeChat iLink ClawBot messaging.

Bidirectional WeChat integration:
  - Auto-restores session from ``wechat_<user>.json5`` on startup.
  - Background poll loop fetches incoming messages continuously.
  - LLM tools: login, send_message, check_messages, check_status, logout.

Usage:
    uv run python -m slife.plugins.wechat.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from pathlib import Path

from fastmcp import FastMCP

from slife.plugins.wechat.client import WechatClawbotClient, BASE_URL
from slife.plugins.wechat.config import (
    load_wechat_config,
    save_wechat_config,
    clear_wechat_config,
)
from slife.server_utils import setup_server_logging, ok_json, error_json

SESSION_MAX_AGE = WechatClawbotClient.SESSION_MAX_AGE

logger = logging.getLogger("slife_wechat")

_log_path = setup_server_logging("slife_wechat")

# ── QR code rendering ────────────────────────────────────────────────────


def _render_qr_ascii(content: str) -> str:
    """Render a string as a compact terminal-scannable QR code.

    Uses single-width Unicode block characters to stay within the
    typical TUI chat view width (~80 cols).  A QR v3 with border=1
    produces ~31 chars wide — easily scannable.
    """
    if not content:
        return ""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1, box_size=1)
        qr.add_data(content)
        qr.make(fit=True)
        # Use half-block pairs: two QR rows → one output row
        # ▄ = lower half block (top row dark), ▀ = upper half block (bottom row dark)
        # █ = full block (both rows dark), ' ' = both rows light
        matrix = qr.get_matrix()
        size = len(matrix)
        lines: list[str] = []
        for y in range(0, size, 2):
            row_chars: list[str] = []
            for x in range(size):
                top = matrix[y][x] if y < size else False
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
        logger.debug("qrcode_lib_unavailable — returning raw URL")
        return content

# ── Global state ─────────────────────────────────────────────────────────

_client = WechatClawbotClient()
_agent_id: str = os.environ.get("SLIFE_AGENT_ID", "slife")
_work_dir: Path = Path(os.environ.get("SLIFE_CONFIG_DIR", "."))

# Background polling
_poll_task: asyncio.Task | None = None
_pending: deque[dict] = deque()
_seen_keys: set[str] = set()
_MAX_QUEUED = 200  # keep at most 200 pending messages

# QR login state (non-blocking)
_qr_task: asyncio.Task | None = None
_qr_status: str = ""  # "" | "waiting" | "scanned" | "confirmed" | "expired" | "error"
_qr_content: str = ""
_qr_error: str = ""
_QR_POLL_INTERVAL = 2.0  # seconds between QR status checks
_QR_MAX_REFRESH = 3

# ── FastMCP server ──────────────────────────────────────────────────────

mcp = FastMCP(
    "slife-wechat",
    instructions=(
        "slife-wechat — bidirectional WeChat messaging. "
        "LLM tools: login (QR scan), send_message (reply), "
        "check_messages (incoming), check_status, logout."
    ),
)

# ═══════════════════════════════════════════════════════════════════════════
# Background polling
# ═══════════════════════════════════════════════════════════════════════════


def _msg_key(msg: dict) -> str:
    """Unique key for dedup: from_user_id + context_token."""
    return f"{msg.get('from_user_id', '')}::{msg.get('context_token', '')}"


async def _poll_loop(poll_interval: float = 3.0) -> None:
    """Continuously poll WeChat for new messages, queueing them for the LLM."""
    global _pending, _seen_keys

    # Flush after every log so we can debug poll activity in real time
    logger.info("poll_loop_start interval=%.1fs", poll_interval)
    _flush_logs()

    backoff = poll_interval

    while _client.is_logged_in:
        try:
            msgs = await _client.poll_updates()
            _flush_logs()  # ensure POST debug lines hit disk
            new_count = 0
            for m in msgs:
                key = _msg_key(m)
                if key in _seen_keys:
                    continue
                _seen_keys.add(key)

                text = ""
                item_list = m.get("item_list", [])
                if item_list:
                    text_item = item_list[0].get("text_item", {})
                    text = text_item.get("text", "")

                if not text.strip():
                    continue

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
                # Keep the set from growing unbounded
                # Remove oldest ~half
                to_remove = list(_seen_keys)[:_MAX_QUEUED]
                for k in to_remove:
                    _seen_keys.discard(k)

            if new_count:
                logger.debug("poll_new msgs=%d queued=%d", new_count, len(_pending))

            backoff = poll_interval  # reset on success
        except Exception as e:
            logger.debug("poll_error err=%s", e)
            backoff = min(backoff * 1.5, 30.0)  # back off on errors
        await asyncio.sleep(backoff)

    logger.info("poll_loop_stop")


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
    """Cancel the background poll task."""
    global _poll_task, _pending
    if _poll_task is not None and not _poll_task.done():
        _poll_task.cancel()
    _poll_task = None
    _pending.clear()


# ═══════════════════════════════════════════════════════════════════════════
# LLM-visible tools
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
            save_wechat_config(_agent_id, session_dict, _work_dir)
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
                    logger.exception("qr_refresh_failed")
                    _qr_status = "error"
                    _qr_error = f"QR refresh failed: {e}"
                    return
            else:
                _qr_status = "expired"
                _qr_error = "QR code expired after 3 refreshes. Call login again."
                return

        if data.get("scanned"):
            _qr_status = "scanned"

        if data.get("verify_code_blocked"):
            _qr_status = "error"
            _qr_error = "Verify code blocked. Call login again."
            return

        await asyncio.sleep(_QR_POLL_INTERVAL)

    _qr_status = "error"
    _qr_error = "Login timed out (10 min). Call login again."


@mcp.tool(
    name="login",
    description=(
        "Generate a WeChat QR code for the user to scan. "
        "Returns IMMEDIATELY with a QR code link — does NOT block. "
        "Tell the user to open WeChat on their phone and scan the QR code. "
        "Then call check_status to see when login completes. "
        "On success, the session is auto-saved and message polling starts. "
        "Session validity: ~23 hours."
    ),
)
async def wechat_login() -> str:
    global _client, _qr_task, _qr_status, _qr_content, _qr_error

    if _client.is_logged_in:
        return json.dumps({
            "status": "already_logged_in",
            "hint": "Already logged in. Call logout first to switch accounts.",
        }, ensure_ascii=False, indent=2)

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

    return json.dumps({
        "status": "qr_ready",
        "qrcode_url": _qr_content,
        "qr_ascii": qr_ascii,
        "hint": (
            "QR code is ready! Show the ASCII QR above to the user. "
            "Tell them: '请使用微信扫描以下二维码登录' (scan the QR code with WeChat). "
            "Then call check_status every few seconds until login completes. "
            "The QR expires after ~10 minutes and auto-refreshes up to 3 times."
        ),
    }, ensure_ascii=False, indent=2)


@mcp.tool(
    name="send_message",
    description=(
        "Send a text message to the logged-in WeChat user. "
        "Get the to_user_id and context_token from check_status.last_contact. "
        "Example: call check_status → copy to_user_id → "
        "call send_message(to_user_id='...', text='你好'). "
        "The context_token can be empty for the first message."
    ),
)
async def wechat_send_message(
    to_user_id: str = "",
    context_token: str = "",
    text: str = "",
) -> str:
    global _client

    if not _client.is_logged_in:
        return error_json("Not logged in. Call login first.")

    if not to_user_id.strip() or not text.strip():
        return error_json("Both to_user_id and text are required and must be non-empty.")

    try:
        await _client.send_message(to_user_id, context_token or "", text)
        # Hide typing indicator after reply
        try:
            await _client.send_typing(to_user_id, context_token or "", status=2)
        except Exception:
            pass
        logger.debug("sent to=%s len=%d", to_user_id, len(text))
        return json.dumps({
            "status": "sent",
            "to_user_id": to_user_id,
            "text_length": len(text),
        }, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("send_failed to=%s", to_user_id)
        return error_json(str(e))


@mcp.tool(
    name="send_typing",
    description=(
        "Show or hide the typing indicator on the WeChat user's phone. "
        "Call with status=1 BEFORE processing a WeChat message to show "
        "'typing…', and status=2 AFTER sending the reply to hide it. "
        "This gives the WeChat user visual feedback that the agent is working."
    ),
)
async def wechat_send_typing(
    to_user_id: str = "",
    context_token: str = "",
    status: int = 1,
) -> str:
    global _client

    if not _client.is_logged_in:
        return error_json("Not logged in. Call login first.")

    if not to_user_id.strip():
        return error_json("to_user_id is required.")

    try:
        result = await _client.send_typing(to_user_id, context_token or "", status)
        if result is None:
            logger.debug(
                "send_typing_no_ticket to_user_id=%s context_token=%s",
                to_user_id, context_token,
            )
            return error_json(
                "Typing indicator not sent — could not obtain typing ticket. "
                "The getconfig API call may have failed."
            )
        return ok_json(
            status="sent",
            typing_status=status,
        )
    except Exception as e:
        logger.debug("send_typing_error err=%s", e)
        return error_json(str(e))


@mcp.tool(
    name="check_messages",
    description=(
        "Check for new incoming WeChat messages. "
        "Returns queued messages that have arrived since the last check. "
        "Each message includes from_user_id and context_token — save these "
        "to reply with send_message. "
        "Call this periodically to see if anyone has sent a WeChat message "
        "to the agent. Messages are consumed (not returned again)."
    ),
)
async def wechat_check_messages() -> str:
    global _pending

    if not _client.is_logged_in:
        return json.dumps({
            "messages": [],
            "status": "not_logged_in",
            "hint": "Not logged in. Call login first.",
        }, ensure_ascii=False, indent=2)

    # Drain pending messages
    msgs = list(_pending)
    _pending.clear()

    if not msgs:
        return json.dumps({
            "messages": [],
            "status": "ok",
            "hint": "No new messages.",
        }, ensure_ascii=False, indent=2)

    return json.dumps({
        "messages": msgs,
        "count": len(msgs),
        "status": "ok",
        "hint": (
            f"{len(msgs)} new message(s). "
            "Reply using send_message with the to_user_id and context_token "
            "from each message above."
        ),
    }, ensure_ascii=False, indent=2)


@mcp.tool(
    name="check_status",
    description=(
        "Check the current WeChat connection status. "
        "Returns whether logged in, session age, remaining time (~23h max), "
        "and whether background polling is active. "
        "Call this before send_message or when the user asks about WeChat."
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
                "QR code is waiting to be scanned. "
                "Tell the user to open WeChat and scan the QR code. "
                f"QR link: {_qr_content}"
            )
        elif _qr_status == "scanned":
            qr_info["hint"] = (
                "QR code has been scanned! Waiting for the user to "
                "confirm on their phone. This usually takes a few seconds."
            )
        elif _qr_status == "expired":
            qr_info["hint"] = (
                "QR code expired. Call login again to generate a new one."
            )
        elif _qr_status == "error":
            qr_info["hint"] = f"QR login error: {_qr_error}. Call login again."
        return json.dumps(qr_info, ensure_ascii=False, indent=2)

    if not _client.is_logged_in:
        saved = load_wechat_config(_agent_id, _work_dir)
        if saved.get("bot_token"):
            try:
                restored = await _client.try_restore_session(saved)
                if restored:
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
                        "last_contact": {
                            "from_user_id": ilink_uid,
                            "context_token": "",
                        } if ilink_uid else None,
                        "hint": f"Session restored. {remaining/3600:.1f}h remaining.",
                    }, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.debug("restore_failed err=%s", e)

        return json.dumps({
            "status": "not_logged_in",
            "polling": False,
            "hint": "Not logged in. Call login to start the QR login flow.",
        }, ensure_ascii=False, indent=2)

    # Track last contact for proactive messaging
    last_contact = _client.last_contact if hasattr(_client, "last_contact") else {}
    last_from_id = last_contact.get("from_id", "")
    last_ctx = last_contact.get("context_token", "")

    session = _client.get_session_dict()
    age = time.time() - session.get("saved_at", time.time())
    remaining = max(0, SESSION_MAX_AGE - age)

    resp = {
        "status": "logged_in",
        "session_age_hours": round(age / 3600, 1),
        "remaining_hours": round(remaining / 3600, 1),
        "polling": _poll_task is not None and not _poll_task.done(),
        "queued_messages": len(_pending),
    }
    if last_from_id:
        resp["last_contact"] = {
            "to_user_id": last_from_id,
            "context_token": last_ctx,
        }

    if last_from_id:
        hint = (
            f"Logged in — {remaining/3600:.1f}h remaining, "
            f"{len(_pending)} messages queued. "
            f"To send a WeChat message, call send_message with "
            f'to_user_id="{last_from_id}" and context_token="{last_ctx or ""}".'
            if remaining > 0 else
            "Session EXPIRED. Call login to re-scan QR code."
        )
    else:
        hint = (
            f"Logged in — {remaining/3600:.1f}h remaining, "
            f"{len(_pending)} messages queued. "
            "No contacts yet — ask the WeChat user to send a message first."
            if remaining > 0 else
            "Session EXPIRED. Call login to re-scan QR code."
        )

    return json.dumps({
        **resp,
        "hint": hint,
    }, ensure_ascii=False, indent=2)


@mcp.tool(
    name="logout",
    description=(
        "Log out of WeChat and clear the saved session. "
        "Stops background message polling. "
        "After this, login must be called again to reconnect. "
        "Use when switching accounts or troubleshooting."
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
    clear_wechat_config(_agent_id, _work_dir)

    return json.dumps({
        "status": "logged_out",
        "config_cleared": True,
        "hint": "Logged out. Call login to reconnect.",
    }, ensure_ascii=False, indent=2)


# ── Entry point ──────────────────────────────────────────────────────────


def main():
    """Run the slife-wechat server on stdio transport.

    Session restore happens lazily on the first check_status call,
    inside FastMCP's own event loop — this avoids the aiohttp session
    being bound to a temporary loop that gets closed.
    """
    from slife.logfmt import elapsed

    logger.info(
        "wechat_start agent_id=%s transport=stdio log=%s pid=%s",
        _agent_id, _log_path, os.getpid(),
    )
    with elapsed("wechat_run", logger, level=logging.INFO, agent_id=_agent_id):
        mcp.run(transport="stdio")
    logger.info("wechat_stop agent_id=%s", _agent_id)


if __name__ == "__main__":
    main()
