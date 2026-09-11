"""Tests for the wechat server poll loop — incoming message dedup (REVIEW C6).

context_token is per-conversation, so the old ``from_user_id + context_token``
key collided across every message in one conversation and non-text items
burned the key.  The key now includes the text and is recorded only after the
empty-text check, so distinct messages get through while true re-deliveries
are still deduped.
"""

import json
import time

import pytest; pytestmark = pytest.mark.unit


from unittest.mock import AsyncMock, MagicMock, patch

from slife.plugins.wechat import server as ws


def _msg(from_id: str, ctx: str, text: str) -> dict:
    return {
        "from_user_id": from_id,
        "context_token": ctx,
        "item_list": [{"text_item": {"text": text}}],
    }


class TestMsgKey:
    def test_includes_text(self):
        assert ws._msg_key(_msg("u1", "ctx", "hello"), "hello") == "u1::ctx::hello"

    def test_distinct_texts_distinct_keys(self):
        m = _msg("u1", "ctx", "ignored")
        assert ws._msg_key(m, "one") != ws._msg_key(m, "two")


class TestPollLoopDedup:
    """Dedup must not drop distinct messages from one conversation (C6)."""

    def setup_method(self):
        ws._pending.clear()
        ws._seen_keys.clear()

    async def _run(self, batches):
        """Drive _poll_loop once per batch, then log out."""
        client = MagicMock()
        client._base_url = "https://ilinkai.weixin.qq.com"
        client.is_logged_in = True
        client.auth_failed = False
        client.last_error = None
        client.last_contact = None

        def _poll():
            if not batches:
                client.is_logged_in = False
                return []
            return batches.pop(0)

        client.poll_updates = AsyncMock(side_effect=_poll)

        original = ws._client
        ws._client = client
        try:
            with patch.object(ws, "_flush_logs", lambda: None):
                # Run until is_logged_in flips False.
                await ws._poll_loop(poll_interval=0.01)
        finally:
            ws._client = original

        return list(ws._pending)

    @pytest.mark.asyncio
    async def test_same_conversation_different_texts_both_queued(self):
        """Two real text messages in one conversation both get through —
        previously the second was dropped (same sender+context_token key)."""
        queued = await self._run([[
            _msg("u1", "ctx1", "first"),
            _msg("u1", "ctx1", "second"),
        ]])
        assert [q["text"] for q in queued] == ["first", "second"]
        assert len(ws._seen_keys) == 2

    @pytest.mark.asyncio
    async def test_non_text_does_not_burn_key(self):
        """An image/sticker (empty text) must not record the key — the next
        real text in the conversation must NOT be dropped."""
        non_text = dict(_msg("u1", "ctx1", ""))  # empty text
        queued = await self._run([[
            non_text,
            _msg("u1", "ctx1", "hello"),
        ]])
        assert [q["text"] for q in queued] == ["hello"]
        # Only the text message recorded its key (the map value is a timestamp).
        assert set(ws._seen_keys) == {"u1::ctx1::hello"}

    @pytest.mark.asyncio
    async def test_cross_poll_redelivery_still_deduped(self):
        """A true re-delivery ACROSS polls (same sender + conversation + text)
        is still dropped — the key is only deduped per poll (REVIEW §1-9)."""
        queued = await self._run([
            [_msg("u1", "ctx1", "hello")],
            [_msg("u1", "ctx1", "hello")],  # same message returned again
        ])
        assert [q["text"] for q in queued] == ["hello"]
        assert len(ws._seen_keys) == 1

    @pytest.mark.asyncio
    async def test_same_text_twice_in_one_poll_both_queued(self):
        """Two genuine same-text messages in ONE poll both get through — a user
        sending 'ok' twice in a row must not have the second dropped."""
        queued = await self._run([[
            _msg("u1", "ctx1", "hello"),
            _msg("u1", "ctx1", "hello"),
        ]])
        assert [q["text"] for q in queued] == ["hello", "hello"]
        assert len(ws._seen_keys) == 1  # one distinct key for both


class TestPollLoopBackoff:
    """A failed poll (network error recorded as ``last_error``, then a clean
    ``poll_updates`` return of []) must drive exponential backoff, not an
    unchanged-interval hot loop against a down endpoint."""

    def setup_method(self):
        ws._pending.clear()
        ws._seen_keys.clear()

    @pytest.mark.asyncio
    async def test_last_error_backs_off_then_resets(self):
        import asyncio as _asyncio

        client = MagicMock()
        client.is_logged_in = True
        client.auth_failed = False
        client.last_error = None
        client.last_contact = None
        ws._client = client

        sleeps = []
        calls = 0

        async def _poll():
            nonlocal calls
            calls += 1
            if calls == 1:
                client.last_error = "simulated network failure"
            else:
                client.last_error = None
                client.is_logged_in = False  # stop after the recovery poll
            return []

        client.poll_updates = AsyncMock(side_effect=_poll)

        async def _sleep(delay):
            sleeps.append(delay)

        with (
            patch.object(ws, "_flush_logs", lambda: None),
            patch.object(_asyncio, "sleep", AsyncMock(side_effect=_sleep)),
        ):
            await ws._poll_loop(poll_interval=3.0)

        # First sleep (after the failed poll) must be the enlarged backoff
        # (3.0 * 1.5 = 4.5), NOT the base interval — that is the bug: the
        # swallow in poll_updates() previously reset backoff to 3.0.
        assert calls == 2
        assert sleeps and sleeps[0] == pytest.approx(4.5)


class TestCheckStatusLastContactShape:
    """last_contact must have the same shape — both from_user_id and
    to_user_id — on the live-polling AND session-restore paths.

    It previously flipped keys between the paths, so an LLM following the
    schema doc (to_user_id from last_contact) could copy from_user_id into
    to_user_id and the reply would not send (BUGS.md #1).
    """

    def setup_method(self):
        ws._qr_status = ""

    @pytest.mark.asyncio
    async def test_logged_in_polling_path(self):
        client = MagicMock()
        client.is_logged_in = True
        client.last_contact = {"from_id": "u1", "context_token": "ctx1"}
        client.get_session_dict.return_value = {"saved_at": time.time()}
        client.auth_failed = False
        client.last_error = None
        original = ws._client
        ws._client = client
        try:
            with patch.object(
                ws, "_poll_task", MagicMock(done=MagicMock(return_value=False))
            ):
                resp = json.loads(await ws.wechat_check_status())
        finally:
            ws._client = original
        lc = resp["last_contact"]
        assert resp["status"] == "logged_in"
        assert lc["peer_wechat_id"] == "u1"
        assert lc["context_token"] == "ctx1"

    @pytest.mark.asyncio
    async def test_logged_in_but_link_down_reports_degraded(self):
        """With a live poll loop error, status must say degraded — not a
        misleading 'logged_in' — so the LLM knows messages are not arriving."""
        client = MagicMock()
        client.is_logged_in = True
        client.last_contact = {"from_id": "u1", "context_token": "ctx1"}
        client.get_session_dict.return_value = {"saved_at": time.time()}
        client.auth_failed = False
        client.last_error = "Cannot connect to host ilinkai.weixin.qq.com:443"
        original = ws._client
        ws._client = client
        try:
            with patch.object(
                ws, "_poll_task", MagicMock(done=MagicMock(return_value=False))
            ):
                resp = json.loads(await ws.wechat_check_status())
        finally:
            ws._client = original
        assert resp["status"] == "degraded"
        # the LLM-visible hint carries the reason messages are not arriving
        assert "Cannot connect" in resp["hint"]

    @pytest.mark.asyncio
    async def test_restore_rejected_token_reports_not_logged_in(self):
        """A restore whose token the server rejects must clear the stale
        session and report not_logged_in — never a phantom logged_in."""
        client = MagicMock()
        client.is_logged_in = False
        client.try_restore_session = AsyncMock(return_value=True)
        client.validate_session = AsyncMock(return_value=False)
        client.auth_failed = True
        client.last_error = "session rejected by server: 3"
        client.reject_session = MagicMock()
        client.clear_session_faults = MagicMock()
        original = ws._client
        ws._client = client
        try:
            with patch.object(ws, "load_wechat_config", return_value={
                "bot_token": "tok",
                "ilink_user_id": "u1",
                "saved_at": time.time(),
            }), patch.object(ws, "clear_wechat_config", return_value=True):
                resp = json.loads(await ws.wechat_check_status())
        finally:
            ws._client = original
        assert resp["status"] == "not_logged_in"
        assert "rejected" in resp["hint"]
        client.reject_session.assert_called_once()
        client.clear_session_faults.assert_not_called()

    @pytest.mark.asyncio
    async def test_restore_path(self):
        client = MagicMock()
        client.is_logged_in = False
        client.try_restore_session = AsyncMock(return_value=True)
        client.validate_session = AsyncMock(return_value=True)
        client.clear_session_faults = AsyncMock()
        client.get_session_dict.return_value = {"saved_at": time.time()}
        client.auth_failed = False
        client.last_error = None
        original = ws._client
        ws._client = client
        try:
            with patch.object(ws, "load_wechat_config", return_value={
                "bot_token": "tok",
                "ilink_user_id": "u1",
                "saved_at": time.time(),
            }):
                with patch.object(ws, "_start_polling", lambda: None):
                    resp = json.loads(await ws.wechat_check_status())
        finally:
            ws._client = original
        assert resp["status"] == "restored"
        lc = resp["last_contact"]
        assert lc["peer_wechat_id"] == "u1"
        assert lc["context_token"] == ""

    @pytest.mark.asyncio
    async def test_restore_network_failure_reports_degraded(self):
        """A restore blocked by a network error keeps the saved session but
        reports degraded, so the LLM learns the link is the problem."""
        client = MagicMock()
        client.is_logged_in = False
        client.try_restore_session = AsyncMock(return_value=True)
        client.validate_session = AsyncMock(return_value=False)
        client.auth_failed = False
        client.last_error = "Cannot connect to host ilinkai.weixin.qq.com:443"
        original = ws._client
        ws._client = client
        try:
            with patch.object(ws, "load_wechat_config", return_value={
                "bot_token": "tok",
                "ilink_user_id": "u1",
                "saved_at": time.time(),
            }):
                resp = json.loads(await ws.wechat_check_status())
        finally:
            ws._client = original
        assert resp["status"] == "degraded"
        assert "unreachable" in resp["hint"]


class TestWechatSendMessage:
    """D7: a send must not report "sent" when iLink returns a 200 error
    envelope (rejected/revoked token) — only the getupdates path checked
    `_error_envelope` before."""

    def _client(self):
        client = MagicMock()
        client.is_logged_in = True
        client.auth_failed = False
        client.last_error = None
        client.send_message = AsyncMock()
        client.send_typing = AsyncMock()
        return client

    @pytest.mark.asyncio
    async def test_error_envelope_not_reported_as_sent(self):
        client = self._client()
        client.send_message.return_value = {"errno": 3000, "error": "token rejected"}
        original = ws._client
        ws._client = client
        try:
            resp = json.loads(
                await ws.wechat_send_message(
                    peer_wechat_id="u1", context_token="", text="hi",
                ),
            )
        finally:
            ws._client = original
        assert resp.get("status") != "sent"
        assert "rejected" in resp.get("error", "")
        assert client.auth_failed is True

    @pytest.mark.asyncio
    async def test_success_reports_sent(self):
        client = self._client()
        client.send_message.return_value = {"message_id": "m-42"}
        original = ws._client
        ws._client = client
        try:
            resp = json.loads(
                await ws.wechat_send_message(
                    peer_wechat_id="u1", context_token="", text="hi",
                ),
            )
        finally:
            ws._client = original
        assert resp["status"] == "sent"
        assert resp["message_id"] == "m-42"


class TestSyncBufPersist:
    """D6: the getupdates ack is persisted (once per change) so a restart can
    resume ack'ing — replayed messages aren't re-ingested as duplicates."""

    def test_persists_on_change_and_dedups(self):
        persisted = []
        original = ws.update_wechat_updates_buf
        ws.update_wechat_updates_buf = lambda _u, buf, _wd=None: persisted.append(buf)
        try:
            ws._persisted_sync_buf = ""
            ws._persist_updates_buf("buf-1")
            ws._persist_updates_buf("buf-1")  # unchanged → no second write
            ws._persist_updates_buf("buf-2")
        finally:
            ws.update_wechat_updates_buf = original
            ws._persisted_sync_buf = ""
        assert persisted == ["buf-1", "buf-2"]

    def test_non_string_buf_skipped(self):
        persisted = []
        original = ws.update_wechat_updates_buf
        ws.update_wechat_updates_buf = lambda _u, buf, _wd=None: persisted.append(buf)
        try:
            ws._persisted_sync_buf = ""
            ws._persist_updates_buf(None)  # test fakes expose MagicMock attrs
            ws._persist_updates_buf("")
        finally:
            ws.update_wechat_updates_buf = original
            ws._persisted_sync_buf = ""
        assert persisted == []
