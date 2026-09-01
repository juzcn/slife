"""iLink ClawBot protocol client for WeChat message bridge.

Adapted from SiverKing/weixin-ClawBot-API (MIT License).
Handles QR login, long-poll message receive, and message send.

All terminal-rendering code stripped — this runs headless inside the
slife-wechat MCP server process.  QR content is returned as a string
for the LLM to relay to the user.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
import time
from urllib.parse import quote

import aiohttp

logger = logging.getLogger("slife_wechat")

BASE_URL = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "2.4.3"
ILINK_APP_ID = "bot"
ILINK_APP_CLIENT_VERSION = str((2 << 16) | (4 << 8) | 3)
BOT_AGENT = "slife-wechat/1.0.0 (python)"


def _make_headers(token: str | None = None) -> dict:
    uin = str(random.randint(0, 0xFFFFFFFF))
    headers: dict = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(uin.encode()).decode(),
        "iLink-App-Id": ILINK_APP_ID,
        "iLink-App-ClientVersion": ILINK_APP_CLIENT_VERSION,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _base_info() -> dict:
    return {
        "channel_version": CHANNEL_VERSION,
        "bot_agent": BOT_AGENT,
    }


def _error_envelope(result: dict) -> bool:
    """True when a getupdates envelope carries an error (no ``msgs``).

    The iLink API signals a rejected/revoked token with a 200 response whose
    body is ``{"errno": …, "error": …}`` (or ``retcode``) instead of
    ``{"msgs": …}``.  Treating that as "no messages" is what made the poll
    loop silently spin while status claimed ``logged_in``.
    """
    return bool(result.get("errno") or result.get("error") or result.get("retcode"))


def _envelope_error(result: dict) -> str:
    """Human-readable error from a getupdates error envelope."""
    err = result.get("error") or result.get("errno") or result.get("retcode")
    if err:
        return f"session rejected by server: {err}"
    return ""


# ── Client ────────────────────────────────────────────────────────────────


class WechatClawbotClient:
    """Async client for the WeChat iLink ClawBot protocol.

    Usage::

        client = WechatClawbotClient()
        if not await client.try_restore_session(saved):
            result = await client.login()
            await client.start(result["bot_token"], result.get("baseurl", ""))
            # save client.get_session_dict()

        while True:
            for msg in await client.poll_updates():
                text = msg["item_list"][0]["text_item"]["text"]
                await client.send_message(
                    msg["from_user_id"], msg["context_token"], f"echo: {text}"
                )

        await client.stop()
    """

    SESSION_MAX_AGE = 23 * 3600  # re-login if older than 23 hours

    def __init__(self) -> None:
        self._bot_token: str = ""
        self._base_url: str = BASE_URL
        self._get_updates_buf: str = ""
        self._typing_tickets: dict[str, str] = {}
        self._ilink_user_id: str = ""   # bot's own iLink user ID (from login)
        self._ilink_bot_id: str = ""    # bot's iLink bot ID (from login)
        self.last_contact: dict[str, str | None] = {
            "from_id": None, "context_token": None,
        }
        # Session health, surfaced to the LLM by wechat_check_status:
        # ``last_error`` records the most recent poll/validation failure (None
        # when healthy); ``auth_failed`` flags a token the server rejected
        # (getupdates error envelope) — the session needs a fresh wechat_login.
        self.last_error: str | None = None
        self.auth_failed: bool = False

    # ── Login ─────────────────────────────────────────────────────────

    async def login(self, base_url: str = "") -> dict:
        """Full QR login flow.

        Returns a dict with ``qrcode`` (the QR content string) and
        ``status`` — one of ``"confirmed"`` (login succeeded),
        ``"expired"``, ``"timeout"``, or ``"error"``.

        On success the dict also contains ``bot_token`` and ``baseurl``.
        The caller should display *qrcode* to the user.
        """
        url = base_url or BASE_URL
        refresh_count = 0
        max_refresh = 3

        while True:
            data = await self._fetch_qrcode(url)
            qrcode = data["qrcode"]
            qrcode_img = data.get("qrcode_img_content", "")

            logger.debug("qr_fetched qrcode=%s", qrcode)

            result = await self._wait_login_confirmation(qrcode, url)
            if result.get("bot_token"):
                result["qrcode"] = str(qrcode_img or qrcode)
                result["status"] = "confirmed"
                return result
            if result.get("already_connected"):
                logger.debug("qr_server_connected action=refresh")
            elif result.get("expired"):
                logger.info("qr_expired action=refresh")
            elif result.get("verify_code_blocked"):
                logger.warning("qr_verify_blocked action=refresh")
            elif result.get("timeout"):
                logger.info("qr_timeout action=refresh")

            refresh_count += 1
            if refresh_count >= max_refresh:
                return {"status": "error", "error": "二维码多次失效，请稍后重试"}

    async def _fetch_qrcode(self, base_url: str) -> dict:
        body = {"local_token_list": []}
        data = await self._api_post(
            "ilink/bot/get_bot_qrcode?bot_type=3", body, base_url,
        )
        if data.get("qrcode"):
            return data
        logger.debug("qr_post_empty action=get_fallback")
        return await self._api_get(
            "ilink/bot/get_bot_qrcode?bot_type=3", base_url,
        )

    async def _poll_login_status(
        self, qrcode: str, base_url: str, verify_code: str | None = None,
    ) -> dict:
        endpoint = f"ilink/bot/get_qrcode_status?qrcode={quote(qrcode, safe='')}"
        if verify_code:
            endpoint += f"&verify_code={quote(verify_code, safe='')}"
        status = await self._api_get(endpoint, base_url)
        state = status.get("status", "")

        if state == "confirmed" or status.get("bot_token"):
            return {
                "bot_token": status.get("bot_token"),
                "baseurl": status.get("baseurl") or status.get("base_url") or base_url,
                "ilink_bot_id": status.get("ilink_bot_id"),
                "ilink_user_id": status.get("ilink_user_id"),
            }
        if state == "binded_redirect" or status.get("binded_redirect"):
            return {"already_connected": True}
        if state == "expired":
            return {"expired": True}
        if state == "scaned_but_redirect":
            redirect_host = status.get("redirect_host")
            if redirect_host:
                return {"redirect_base": f"https://{redirect_host}"}
            return {}
        if state == "scaned":
            return {"scanned": True, "verify_code_accepted": bool(verify_code)}
        if state in ("need_verifycode", "verify_code_blocked") or status.get("need_verifycode"):
            if state == "verify_code_blocked":
                return {"verify_code_blocked": True}
            return {"need_verifycode": True, "retry_verifycode": bool(verify_code)}
        if state and state != "wait":
            logger.debug("login_poll status=%s raw=%.200s", state, status)

        return {}

    async def _wait_login_confirmation(
        self, qrcode: str, base_url: str, timeout: float = 600,
    ) -> dict:
        deadline = asyncio.get_event_loop().time() + timeout
        current_base_url = base_url
        pending_verify_code: str | None = None

        while True:
            if asyncio.get_event_loop().time() >= deadline:
                return {"timeout": True}

            try:
                result = await self._poll_login_status(
                    qrcode, current_base_url, pending_verify_code,
                )
            except Exception as e:
                logger.debug("login_poll_failed err=%s", e)
                await asyncio.sleep(1)
                continue

            if result.get("bot_token"):
                return result
            if result.get("already_connected") or result.get("expired"):
                return result
            if result.get("verify_code_blocked"):
                return result
            if result.get("redirect_base"):
                current_base_url = result["redirect_base"]
                logger.debug("poll_node_switch url=%s", current_base_url)
                continue
            if result.get("scanned"):
                if pending_verify_code and result.get("verify_code_accepted"):
                    pending_verify_code = None
                logger.info("qr_scanned action=wait_confirmation")
            if result.get("need_verifycode"):
                # Headless mode — can't prompt for verify code.
                # Just wait; most logins don't require it.
                logger.warning("qr_verify_required mode=headless")
                continue

            await asyncio.sleep(1)

    # ── Session lifecycle ──────────────────────────────────────────────

    async def start(
        self, bot_token: str, base_url: str = "",
        ilink_user_id: str = "", ilink_bot_id: str = "",
    ) -> None:
        """Set credentials. Call after ``login()``.

        Each API call creates its own aiohttp session (following the
        official weixin-ClawBot-API pattern) so there is no persistent
        session to manage here.
        """
        self._bot_token = bot_token
        self._base_url = base_url or BASE_URL
        self._ilink_user_id = ilink_user_id
        self._ilink_bot_id = ilink_bot_id

    async def stop(self) -> None:
        """Clean up (no persistent session to close)."""

    # ── Session persistence ────────────────────────────────────────────

    async def try_restore_session(self, saved: dict | None = None) -> bool:
        """Restore session from a saved dict. Returns True if valid."""
        if not saved:
            return False

        saved_at = saved.get("saved_at", 0)
        if time.time() - saved_at > self.SESSION_MAX_AGE:
            logger.debug("session_expired age_hours=%s", self.SESSION_MAX_AGE // 3600)
            return False

        bot_token = saved.get("bot_token")
        base_url = saved.get("base_url", BASE_URL)
        if not bot_token:
            return False

        await self.start(
            bot_token, base_url,
            ilink_user_id=saved.get("ilink_user_id", ""),
            ilink_bot_id=saved.get("ilink_bot_id", ""),
        )
        logger.debug("session_restored")
        return True

    async def validate_session(self) -> bool:
        """Probe the API to confirm the restored token still works.

        :meth:`try_restore_session` only checks the ``saved_at`` age — a token
        invalidated server-side (re-login elsewhere) passes and then silently
        spins on API errors while status reports ``logged_in``.  A getupdates
        response that is an error envelope (no ``msgs`` plus an error/errno/
        retcode key) marks the session invalid (``auth_failed``).

        Returns ``False`` (and records the reason in ``last_error``) for any
        failure — both a rejected token and a network time-out.  The caller
        distinguishes them via ``auth_failed``: a rejected token needs a new
        login, a network error is transient.
        """
        try:
            result = await self._api_post(
                "ilink/bot/getupdates",
                {"get_updates_buf": self._get_updates_buf, "base_info": _base_info()},
            )
            if not isinstance(result, dict):
                self.last_error = f"unexpected getupdates envelope: {result!r}"
                return False
            if "msgs" in result:
                self.last_error = None
                return True
            if _error_envelope(result):
                self.auth_failed = True
                self.last_error = _envelope_error(result) or "session rejected"
                return False
            return True  # ambiguous envelope — don't false-revoke
        except Exception as e:
            self.last_error = str(e)
            return False

    def get_session_dict(self) -> dict:
        """Return current session data suitable for persistence."""
        return {
            "bot_token": self._bot_token,
            "base_url": self._base_url,
            "ilink_user_id": self._ilink_user_id,
            "ilink_bot_id": self._ilink_bot_id,
            "saved_at": time.time(),
        }

    @property
    def is_logged_in(self) -> bool:
        """Whether the client has valid credentials."""
        return bool(self._bot_token)

    def reject_session(self) -> None:
        """Drop credentials after the stored session failed validation.

        ``try_restore_session`` loads the saved token before validation; when
        validation rejects it, the token must go so ``is_logged_in`` reads
        false and status reports ``not_logged_in`` instead of a phantom
        ``logged_in``.  The config file is not touched here — the caller
        decides whether to forget the stale session.
        """
        self._bot_token = ""
        self._base_url = BASE_URL
        self._get_updates_buf = ""
        self._ilink_user_id = ""
        self._ilink_bot_id = ""
        self.last_contact = {"from_id": None, "context_token": None}
        self.last_error = None
        self.auth_failed = False

    def clear_session_faults(self) -> None:
        """Clear reported health flags when the session recovers (login/retry)."""
        self.last_error = None
        self.auth_failed = False

    # ── Message operations ─────────────────────────────────────────────

    async def poll_updates(self) -> list[dict]:
        """Long-poll ``/ilink/bot/getupdates``. Returns list of message dicts.

        Each message dict has: ``from_user_id``, ``context_token``,
        ``item_list[0].text_item.text``, ``message_type``.

        A response that is an error envelope (no ``msgs`` plus an
        error/errno/retcode key) means the token was revoked server-side —
        the session sets ``auth_failed`` so the poll loop can stop and the
        status tool can tell the LLM to re-login.
        """
        try:
            result = await self._api_post(
                "ilink/bot/getupdates",
                {"get_updates_buf": self._get_updates_buf, "base_info": _base_info()},
            )
        except Exception as e:
            self.last_error = str(e)
            return []
        if not isinstance(result, dict):
            return []
        self._get_updates_buf = result.get("get_updates_buf") or self._get_updates_buf
        if _error_envelope(result):
            self.auth_failed = True
            self.last_error = _envelope_error(result) or "session rejected"
            return []
        self.last_error = None
        return result.get("msgs") or []

    async def send_message(
        self, to_user_id: str, context_token: str, text: str,
    ) -> dict:
        """Send a text message to the WeChat user."""
        client_id = f"slife-wechat-{random.randint(0, 0xFFFFFFFF):08x}"
        return await self._api_post(
            "ilink/bot/sendmessage",
            {
                "msg": {
                    "from_user_id": "",
                    "to_user_id": to_user_id,
                    "client_id": client_id,
                    "message_type": 2,
                    "message_state": 2,
                    "context_token": context_token,
                    "item_list": [{"type": 1, "text_item": {"text": text}}],
                },
                "base_info": _base_info(),
            },
        )

    async def send_typing(
        self, to_user_id: str, context_token: str, status: int = 1,
    ) -> dict | None:
        """Send typing indicator. ``status=1`` to show, ``status=2`` to hide."""
        ticket = await self._ensure_typing_ticket(to_user_id, context_token)
        if not ticket:
            return None
        return await self._api_post(
            "ilink/bot/sendtyping",
            {
                "ilink_user_id": to_user_id,
                "typing_ticket": ticket,
                "status": status,
                "base_info": _base_info(),
            },
        )

    async def _ensure_typing_ticket(
        self, user_id: str, context_token: str,
    ) -> str:
        # Return cached ticket if valid (non-empty)
        if user_id in self._typing_tickets and self._typing_tickets[user_id]:
            return self._typing_tickets[user_id]

        # getconfig is a bot-level endpoint — it needs the bot's own
        # ilink_user_id (returned at login), NOT the message sender's ID.
        # The sender's ID goes to sendtyping as the target.
        bot_id = self._ilink_user_id or self._ilink_bot_id
        body: dict = {"base_info": _base_info()}
        if bot_id:
            body["ilink_user_id"] = bot_id
        # context_token is per-conversation and may help the API identify
        # the right session for the typing ticket
        if context_token:
            body["context_token"] = context_token

        cfg = await self._api_post("ilink/bot/getconfig", body)
        ticket = cfg.get("typing_ticket", "")
        if ticket:
            self._typing_tickets[user_id] = ticket
        else:
            logger.debug(
                "_ensure_typing_ticket_empty ilink_user_id=%s context_token=%s "
                "cfg_keys=%s", bot_id, context_token, list(cfg.keys()),
            )
        return ticket

    # ── Internal helpers ───────────────────────────────────────────────

    async def _api_get(self, path: str, base_url: str = "") -> dict:
        url = f"{base_url or self._base_url}/{path}"
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                url, headers=_make_headers(self._bot_token),
            ) as res:
                text = await res.text()
                logger.debug("http_request method=GET url=%s status=%s body=%.200s", path, res.status, text)
                try:
                    return json.loads(text)
                except Exception:
                    return {}

    async def _api_post(
        self, path: str, body: dict, base_url: str = "",
    ) -> dict:
        url = f"{base_url or self._base_url}/{path}"
        timeout = aiohttp.ClientTimeout(total=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                url, json=body, headers=_make_headers(self._bot_token),
            ) as res:
                text = await res.text()
                logger.debug("http_request method=POST url=%s status=%s body=%.200s", path, res.status, text)
                try:
                    return json.loads(text)
                except Exception:
                    return {}
