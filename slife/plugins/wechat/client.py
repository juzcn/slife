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
        self.last_contact: dict[str, str | None] = {
            "from_id": None, "context_token": None,
        }

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

            logger.info("qrcode=%s", qrcode)

            result = await self._wait_login_confirmation(qrcode, url)
            if result.get("bot_token"):
                result["qrcode"] = str(qrcode_img or qrcode)
                result["status"] = "confirmed"
                return result
            if result.get("already_connected"):
                logger.debug("Server reports already connected; refreshing QR")
            elif result.get("expired"):
                logger.info("QR code expired, refreshing…")
            elif result.get("verify_code_blocked"):
                logger.warning("Verify code blocked, refreshing QR…")
            elif result.get("timeout"):
                logger.info("Login timeout, refreshing QR…")

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
        logger.debug("POST did not return qrcode, trying GET fallback")
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
            logger.debug("Login status: %s, raw: %s", state, status)

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
                logger.debug("Poll login status failed: %s", e)
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
                logger.debug("Switching poll node to: %s", current_base_url)
                continue
            if result.get("scanned"):
                if pending_verify_code and result.get("verify_code_accepted"):
                    pending_verify_code = None
                logger.info("QR code scanned, waiting for phone confirmation…")
            if result.get("need_verifycode"):
                # Headless mode — can't prompt for verify code.
                # Just wait; most logins don't require it.
                logger.warning("Verify code required but running headless — waiting…")
                continue

            await asyncio.sleep(1)

    # ── Session lifecycle ──────────────────────────────────────────────

    async def start(self, bot_token: str, base_url: str = "") -> None:
        """Set credentials. Call after ``login()``.

        Each API call creates its own aiohttp session (following the
        official weixin-ClawBot-API pattern) so there is no persistent
        session to manage here.
        """
        self._bot_token = bot_token
        self._base_url = base_url or BASE_URL

    async def stop(self) -> None:
        """Clean up (no persistent session to close)."""

    # ── Session persistence ────────────────────────────────────────────

    async def try_restore_session(self, saved: dict | None = None) -> bool:
        """Restore session from a saved dict. Returns True if valid."""
        if not saved:
            return False

        saved_at = saved.get("saved_at", 0)
        if time.time() - saved_at > self.SESSION_MAX_AGE:
            logger.debug("Saved session expired (>%sh old)", self.SESSION_MAX_AGE // 3600)
            return False

        bot_token = saved.get("bot_token")
        base_url = saved.get("base_url", BASE_URL)
        if not bot_token:
            return False

        await self.start(bot_token, base_url)
        logger.debug("Session restored from saved dict")
        return True

    def get_session_dict(self) -> dict:
        """Return current session data suitable for persistence."""
        return {
            "bot_token": self._bot_token,
            "base_url": self._base_url,
            "saved_at": time.time(),
        }

    @property
    def is_logged_in(self) -> bool:
        """Whether the client has valid credentials."""
        return bool(self._bot_token)

    # ── Message operations ─────────────────────────────────────────────

    async def poll_updates(self) -> list[dict]:
        """Long-poll ``/ilink/bot/getupdates``. Returns list of message dicts.

        Each message dict has: ``from_user_id``, ``context_token``,
        ``item_list[0].text_item.text``, ``message_type``.
        """
        result = await self._api_post(
            "ilink/bot/getupdates",
            {"get_updates_buf": self._get_updates_buf, "base_info": _base_info()},
        )
        self._get_updates_buf = result.get("get_updates_buf") or self._get_updates_buf
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
        if user_id in self._typing_tickets:
            return self._typing_tickets[user_id]
        cfg = await self._api_post(
            "ilink/bot/getconfig",
            {
                "ilink_user_id": user_id,
                "context_token": context_token,
                "base_info": _base_info(),
            },
        )
        ticket = cfg.get("typing_ticket", "")
        self._typing_tickets[user_id] = ticket
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
                logger.debug("[GET %s] HTTP %s → %s", path, res.status, text[:200])
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
                logger.debug("[POST %s] HTTP %s → %s", path, res.status, text[:200])
                try:
                    return json.loads(text)
                except Exception:
                    return {}
