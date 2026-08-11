"""Tests for the wechat server poll loop — incoming message dedup (REVIEW C6).

context_token is per-conversation, so the old ``from_user_id + context_token``
key collided across every message in one conversation and non-text items
burned the key.  The key now includes the text and is recorded only after the
empty-text check, so distinct messages get through while true re-deliveries
are still deduped.
"""

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
        client.is_logged_in = True
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
        # Only the text message recorded its key.
        assert ws._seen_keys == {"u1::ctx1::hello"}

    @pytest.mark.asyncio
    async def test_exact_duplicate_still_deduped(self):
        """True re-delivery (same sender + conversation + text) is still dropped."""
        queued = await self._run([[
            _msg("u1", "ctx1", "hello"),
            _msg("u1", "ctx1", "hello"),  # re-delivery
        ]])
        assert [q["text"] for q in queued] == ["hello"]
        assert len(ws._seen_keys) == 1
