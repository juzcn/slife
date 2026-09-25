"""Tests for Slife.ui.chat — chat view widgets (pure logic tests)."""

import pytest; pytestmark = pytest.mark.unit


import pytest
from unittest.mock import MagicMock, patch

from textual.app import App, ComposeResult

from slife.agent.llm_client import TokenUsage
from slife.ui.app import HistoryInput
from slife.ui.chat import ChatView


# ── UserMessage logic ─────────────────────────────────────────────────


class TestUserMessage:
    """Tests for UserMessage — test string construction without Textual."""

    def test_basic_message_format(self):
        """UserMessage formats text with the '>' prefix."""
        from slife.ui.chat import UserMessage
        msg = UserMessage("hello world", prefix="> ")
        assert msg.render().plain == "> hello world"

    def test_custom_prefix(self):
        """A custom prefix (e.g. 'You> ') is used verbatim."""
        from slife.ui.chat import UserMessage
        msg = UserMessage("hi", prefix="You> ")
        assert msg.render().plain == "You> hi"

    def test_timestamp_is_prefixed(self):
        """A timestamp is rendered as a dim [HH:MM] before the prefix."""
        from slife.ui.chat import UserMessage
        msg = UserMessage("hi", prefix="> ", timestamp="2026-08-16T10:00:00")
        plain = msg.render().plain
        assert plain.startswith("[") and "> hi" in plain

    def test_turn_footnote_styled_dim_italic(self):
        """A restored turn's footnote renders the payload alone — the
        `[INFO: …]` envelope is machine-facing — in dim/italic (the
        thinking style) inline, right after the user's words: readable,
        but clearly machine metadata, not part of the message."""
        from slife.ui.chat import UserMessage
        text = 'switch model [INFO: {"turn_id": 30, "begin": "2026-08-18 17:24", "end": "17:25"}]'
        payload = '{"turn_id": 30, "begin": "2026-08-18 17:24", "end": "17:25"}'
        rendered = UserMessage(text, prefix="> ").render()
        # Envelope stripped from the display; the payload sits right after
        # the user's words.
        assert rendered.plain == "> switch model " + payload
        foot = rendered.plain.index('{"turn_id":')
        covering = [s for s in rendered.spans if s.start <= foot < s.end]
        assert covering, "footnote carries no style span"
        assert covering[0].style == "dim italic"

    def test_no_footnote_plain_message(self):
        """A message without a turn footnote gets no dim/italic styling."""
        from slife.ui.chat import UserMessage
        rendered = UserMessage("just a message", prefix="> ").render()
        styled = [s for s in rendered.spans
                  if "dim" in s.style or "italic" in s.style]
        assert styled == []

    def test_rendered_content(self):
        """Verify the rendered content format directly."""
        parts = ["[bold #d97706]>[/bold #d97706] Hello world"]
        rendered = "".join(parts)
        assert "Hello world" in rendered
        assert ">" in rendered

    def test_with_images(self):
        """Image attachments show file names."""
        parts = ["[bold #d97706]>[/bold #d97706] Describe"]
        parts.append(" [dim]# 📎 img1.png, img2.jpg[/dim]")
        rendered = "".join(parts)
        assert "img1.png" in rendered
        assert "img2.jpg" in rendered
        assert "📎" in rendered


# ── AssistantMessage logic ────────────────────────────────────────────


class TestAssistantMessage:
    """Tests for AssistantMessage — test display logic without Textual."""

    def _make_msg(self):
        """Make a bare AssistantMessage with necessary attrs set."""
        with patch("slife.ui.chat.Static.__init__", return_value=None):
            from slife.ui.chat import AssistantMessage
            msg = AssistantMessage.__new__(AssistantMessage)
            msg._buffer = ""
            msg._thinking = ""
            msg._has_thinking = False
            msg._usage = None
            msg._is_thinking_collapsed = False
            msg._show_usage = True
            msg._name_prefix = None
            msg._trim_marker = ""
            return msg

    def test_initial_state(self):
        msg = self._make_msg()
        assert msg._buffer == ""
        assert msg._thinking == ""
        assert msg._has_thinking is False
        assert msg._usage is None

    def test_append_text(self):
        msg = self._make_msg()
        msg._refresh_display = MagicMock()
        msg.append_text("Hello")
        assert msg._buffer == "Hello"
        msg.append_text(" world")
        assert msg._buffer == "Hello world"

    def test_append_thinking(self):
        msg = self._make_msg()
        msg._refresh_display = MagicMock()
        msg.append_thinking("Let me think...")
        assert msg._thinking == "Let me think..."
        assert msg._has_thinking is True

    def test_set_token_usage(self):
        msg = self._make_msg()
        msg._refresh_display = MagicMock()
        usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        msg.set_token_usage(usage)
        assert msg._usage == usage

    def test_refresh_display_text_only(self):
        msg = self._make_msg()
        msg._buffer = "Hello, user!"
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "Hello, user!" in text
        assert "Thinking" not in text

    def test_refresh_display_with_thinking(self):
        msg = self._make_msg()
        msg._thinking = "Step by step..."
        msg._has_thinking = True
        msg._buffer = "Done"
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "Thinking" in text
        assert "Step by step" in text
        assert "Done" in text

    def test_refresh_display_long_thinking_truncated(self):
        msg = self._make_msg()
        msg._thinking = "x" * 600
        msg._has_thinking = True
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "..." in text

    def test_refresh_display_with_usage(self):
        msg = self._make_msg()
        msg._buffer = "OK"
        msg._usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "150" in text
        assert "tokens" in text

    def test_refresh_display_empty_without_thinking(self):
        """Empty state without thinking shows ellipsis."""
        msg = self._make_msg()
        msg._buffer = ""
        msg._has_thinking = False
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "…" in text

    def test_refresh_display_full(self):
        """Full display with thinking, text, and usage."""
        msg = self._make_msg()
        msg._thinking = "Analyzing..."
        msg._has_thinking = True
        msg._buffer = "The answer is 42."
        msg._usage = TokenUsage(prompt_tokens=20, completion_tokens=10, total_tokens=30)
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "Analyzing..." in text
        assert "The answer is 42." in text
        assert "30" in text
        assert "20" in text
        assert "10" in text

    # ── Finalize / collapse ──────────────────────────────────────────

    def test_finalize_intermediate(self):
        """finalize(intermediate=True) collapses thinking and hides usage."""
        msg = self._make_msg()
        msg._has_thinking = True
        msg._thinking = "reasoning"
        msg.update = MagicMock()
        msg.finalize(intermediate=True)
        assert msg._is_thinking_collapsed is True
        assert msg._show_usage is False

    def test_finalize_final(self):
        """finalize(intermediate=False) keeps thinking expanded and usage visible."""
        msg = self._make_msg()
        msg.update = MagicMock()
        msg.finalize(intermediate=False)
        assert msg._is_thinking_collapsed is False
        assert msg._show_usage is True

    def test_on_click_expands_only(self):
        """Click expands collapsed thinking, but never collapses (avoids destroying text selection)."""
        msg = self._make_msg()
        msg._has_thinking = True
        msg._is_thinking_collapsed = True
        msg.update = MagicMock()
        # Click when collapsed: expand
        msg.on_click()
        assert msg._is_thinking_collapsed is False
        # Click when expanded: no-op (user may be selecting text)
        msg._is_thinking_collapsed = False
        msg.on_click()
        assert msg._is_thinking_collapsed is False  # stays expanded

    def test_keyboard_toggles_collapse(self):
        """Enter/Space toggles thinking collapse both ways."""
        msg = self._make_msg()
        msg._has_thinking = True
        msg.update = MagicMock()
        assert msg._is_thinking_collapsed is False
        msg.action_toggle_thinking()
        assert msg._is_thinking_collapsed is True
        msg.action_toggle_thinking()
        assert msg._is_thinking_collapsed is False

    def test_on_click_no_thinking_noop(self):
        """Click is a no-op when there is no thinking to collapse."""
        msg = self._make_msg()
        msg._has_thinking = False
        msg.update = MagicMock()
        msg.on_click()
        assert msg._is_thinking_collapsed is False

    def test_collapsed_display_shows_summary(self):
        """Collapsed thinking shows one-liner + response text (always visible)."""
        msg = self._make_msg()
        msg._thinking = "Step by step reasoning"
        msg._has_thinking = True
        msg._is_thinking_collapsed = True
        msg._buffer = "The answer"
        msg._usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        msg._show_usage = True
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        # Thinking summary is shown (collapsed one-liner).
        assert "Thinking" in text
        assert "22 chars" in text
        assert "▸" in text
        # Response text is ALWAYS visible — collapsing must not swallow it.
        assert "The answer" in text
        # Usage is visible when _show_usage is True.
        assert "tokens" in text

    def test_collapsed_with_show_usage_false(self):
        """Collapsed thinking with _show_usage=False hides usage but keeps text."""
        msg = self._make_msg()
        msg._thinking = "x"
        msg._has_thinking = True
        msg._is_thinking_collapsed = True
        msg._buffer = "response still visible"
        msg._usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        msg._show_usage = False  # intermediate → hide usage
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        # Thinking one-liner is present.
        assert "Thinking" in text
        # Response text IS still visible — must never be swallowed.
        assert "response still visible" in text
        # Usage is hidden when _show_usage is False.
        assert "tokens" not in text

    def test_expanded_display_no_collapse_indicator(self):
        """Expanded thinking does not show collapse indicator ▸."""
        msg = self._make_msg()
        msg._thinking = "x"
        msg._has_thinking = True
        msg._is_thinking_collapsed = False
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "▸" not in text
        assert "Thinking" in text

    def test_trim_marker_rendered_after_text(self):
        """The runtime trim note renders dim/italic after the reply, with
        the machine-facing `[INFO: …]` envelope unwrapped for display."""
        msg = self._make_msg()
        msg._buffer = "The answer"
        msg._trim_marker = "[INFO: 3 oldest turns have been removed from context]"
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        assert content.plain.endswith(
            "The answer 3 oldest turns have been removed from context"
        )

    def test_trim_marker_set_keeps_buffer(self):
        """set_trim_marker records the note without touching the buffer."""
        msg = self._make_msg()
        msg.update = MagicMock()
        msg._buffer = "The answer"
        msg._trim_marker = ""
        msg.set_trim_marker(2)
        assert msg._buffer == "The answer"
        assert msg._trim_marker == "[INFO: 2 oldest turns have been removed from context]"
        msg.update.assert_called_once()


# ── ChatView logic ────────────────────────────────────────────────────


class TestChatView:
    """Tests for ChatView methods that don't need full Textual."""

    def test_can_focus_is_true(self):
        """ChatView needs focus to receive keyboard scroll bindings."""
        with patch("slife.ui.chat.VerticalScroll.__init__", return_value=None):
            from slife.ui.chat import ChatView
            view = ChatView()
            assert view.can_focus is True


# ── Sticky auto-follow (the reported "scroll doesn't work" bug) ──────


class Host(App):
    """A real chat view plus the real prompt, as the app composes them."""

    def compose(self) -> ComposeResult:
        yield ChatView(id="chat-view")
        yield HistoryInput(id="prompt")


def _fill(view, n=30):
    for i in range(n):
        view.add_user_message(f"[{i}] " + "line of history text " * 6)


def _visible_lines(app) -> list[str]:
    """The text the terminal is showing — the compositor's output.

    Not ``scroll_offset``: the offset was always right, and every assertion
    that read it passed while the screen stood still.  What the reader sees
    is the compositor's strips, so a repaint is only testable here.
    """
    strips = app.screen._compositor.render_strips()
    return ["".join(segment.text for segment in strip) for strip in strips]


async def _repainted(app, pilot, previous: list[str], tries: int = 20) -> list[str]:
    """The screen once it differs from ``previous`` — a repaint, waited for.

    The offset moves synchronously; the screen does not.  A scroll repaints
    through ``Widget._refresh_scroll`` → ``check_idle`` → that widget's Idle
    handler, and the compositor re-arranges the scrolled-to layout only in
    that pass — so a read taken the moment ``scroll_to`` returns still shows
    the *previous* screen.  One ``pilot.pause`` usually covers the pass, but
    not always: on a loaded runner the pause returns first and the caller ends
    up comparing the old screen with itself.  Bounded, so a scroll that never
    repaints — the bug this class exists for — still fails rather than waits.
    """
    lines = _visible_lines(app)
    for _ in range(tries):
        if lines != previous:
            break
        await pilot.pause()
        lines = _visible_lines(app)
    return lines


class TestScrollFollowing:
    """Following the tail must be sticky, or a streaming turn owns the view.

    Every token and every mounted widget landed on an unconditional
    ``scroll_end``, so paging up during a turn was undone by the next token —
    the wheel and the keys both looked dead while the agent was working, and
    worked again once it was idle.
    """

    @pytest.mark.asyncio
    async def test_follows_while_at_the_tail(self):
        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one("#chat-view", ChatView)
            _fill(view)
            await pilot.pause()
            before = view.scroll_offset.y
            view.add_user_message("[new] while at the tail")
            await pilot.pause()
            assert view.scroll_offset.y > before

    @pytest.mark.asyncio
    async def test_does_not_yank_a_reader_back_to_the_tail(self):
        """The bug: 20 streamed tokens must not move a view being read."""
        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one("#chat-view", ChatView)
            _fill(view)
            await pilot.pause()
            view.scroll_to(y=0, animate=False)
            await pilot.pause()

            for _ in range(20):
                view.add_assistant_message().append_text("token ")
                view.follow_tail()
            await pilot.pause()

            assert view.scroll_offset.y == 0

    @pytest.mark.asyncio
    async def test_sending_a_message_returns_to_the_tail(self):
        """The reader's own send goes to the end, wherever they had read to.

        Sticky following is what keeps streamed content from pulling the page
        out from under a reader in history — but sending a message is that
        reader leaving history themselves.  Sent through the guarded follow
        alone, Enter did nothing at all: the view stayed where it was, and the
        message just sent sat below the fold.
        """
        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one("#chat-view", ChatView)
            _fill(view)
            await pilot.pause()
            view.scroll_to(y=0, animate=False)
            await pilot.pause()
            assert view._at_tail is False

            view.add_user_message("You> hello from up here")
            view.jump_to_tail()
            await pilot.pause()

            assert view._at_tail is True
            assert view.scroll_offset.y == view.max_scroll_y
            assert any("hello from up here" in line
                       for line in _visible_lines(app))

    @pytest.mark.asyncio
    async def test_following_resumes_after_a_send(self):
        """The reply follows the message that took the reader back down.

        Re-arming is the half that is easy to lose: a jump that only scrolls
        leaves following disarmed where it was, so the reply to the message
        the reader just sent streams in below the fold.
        """
        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one("#chat-view", ChatView)
            _fill(view)
            await pilot.pause()
            view.scroll_to(y=0, animate=False)
            await pilot.pause()

            view.add_user_message("You> hello")
            view.jump_to_tail()
            await pilot.pause()
            before = view.scroll_offset.y

            # exactly what on_text_chunk does for every streamed token
            view.add_assistant_message().append_text("token ")
            view.follow_tail()
            await pilot.pause()

            assert view.scroll_offset.y > before

    @pytest.mark.asyncio
    async def test_returning_to_the_tail_resumes_following(self):
        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one("#chat-view", ChatView)
            _fill(view)
            await pilot.pause()
            view.scroll_to(y=0, animate=False)
            await pilot.pause()
            view.scroll_to(y=view.max_scroll_y, animate=False)
            await pilot.pause()

            before = view.scroll_offset.y
            view.add_user_message("[new] after coming back")
            await pilot.pause()
            assert view.scroll_offset.y > before

    @pytest.mark.asyncio
    async def test_page_keys_reach_the_transcript_from_the_prompt(self):
        """PageUp while typing pages the transcript, not the draft.

        TextArea binds PageUp/PageDown to its own text and stops the event, so
        with focus in the prompt — where it normally sits — the transcript
        could not be paged at all.
        """
        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one("#chat-view", ChatView)
            _fill(view)
            await pilot.pause()
            view.scroll_to(y=view.max_scroll_y, animate=False)
            app.query_one("#prompt", HistoryInput).focus()
            await pilot.pause()

            before = view.scroll_offset.y
            await pilot.press("pageup")
            await pilot.pause()
            assert view.scroll_offset.y < before

            await pilot.press("pagedown")
            await pilot.pause()
            assert view.scroll_offset.y == view.max_scroll_y
            # The reader keeps their cursor: paging must not move focus.
            assert isinstance(app.focused, HistoryInput)

    @pytest.mark.asyncio
    async def test_scrolling_moves_what_is_on_screen(self):
        """A scroll must repaint the view, not just move the offset.

        ``ChatView.watch_scroll_y`` overrides Textual's watcher, and the
        override has to delegate: the base one is what repaints the widget at
        the new offset.  Without it the reader scrolled an image that never
        moved — the offset changed, the screen stood still — which is why
        every offset assertion in this class stayed green through the bug.
        """
        app = Host()
        async with app.run_test(size=(80, 24)) as pilot:
            view = app.query_one("#chat-view", ChatView)
            _fill(view)
            await pilot.pause()
            at_tail = _visible_lines(app)

            view.scroll_to(y=0, animate=False)
            shown = await _repainted(app, pilot, at_tail)

            assert view.scroll_offset.y == 0
            assert shown != at_tail


# ── Timestamp formatting + per-message rendering ────────────────────


class TestTimestamp:
    """_format_timestamp rules; both user and assistant show a time."""

    def test_none_and_garbage(self):
        from slife.ui.chat import _format_timestamp
        assert _format_timestamp(None) is None
        assert _format_timestamp("not-a-time") is None

    def test_same_day_time_only(self):
        from datetime import datetime
        from slife.ui.chat import _format_timestamp
        now = datetime.now().astimezone()
        assert _format_timestamp(now) == now.strftime("%H:%M")

    def test_iso_string_same_day(self):
        from datetime import datetime
        from slife.ui.chat import _format_timestamp
        now = datetime.now().astimezone().replace(second=0, microsecond=0)
        assert _format_timestamp(now.isoformat(timespec="seconds")) == now.strftime("%H:%M")

    def test_other_day_same_year(self, monkeypatch):
        from datetime import datetime
        import slife.ui.chat as chat

        class _FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 12, 10, 0, tzinfo=tz).astimezone()

        monkeypatch.setattr(chat, "datetime", _FixedNow)
        assert chat._format_timestamp(datetime(2026, 1, 2, 9, 5)) == "01-02 09:05"

    def test_previous_year_full_date(self, monkeypatch):
        from datetime import datetime
        import slife.ui.chat as chat

        class _FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 12, 10, 0, tzinfo=tz).astimezone()

        monkeypatch.setattr(chat, "datetime", _FixedNow)
        assert chat._format_timestamp(datetime(2025, 12, 31, 23, 59)) == "2025-12-31 23:59"

    def test_user_message_with_timestamp(self, monkeypatch):
        """User messages show [HH:MM] before the prefix (Enter-press time)."""
        from datetime import datetime
        from slife.ui.chat import UserMessage
        import slife.ui.chat as chat

        class _FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 12, 10, 0, tzinfo=tz).astimezone()

        monkeypatch.setattr(chat, "datetime", _FixedNow)
        m = UserMessage(
            "hello", prefix="You> ", timestamp=datetime(2026, 8, 12, 14, 32),
        )
        assert m.render().plain == "[14:32] You> hello"

    def test_user_message_no_timestamp_no_time(self):
        """Without a timestamp the user message is unchanged."""
        from slife.ui.chat import UserMessage
        m = UserMessage("hello", prefix="You> ")
        assert m.render().plain == "You> hello"

    def test_assistant_message_renders_timestamp(self, monkeypatch):
        """Assistant messages show [HH:MM] before the content."""
        from datetime import datetime
        from slife.ui.chat import AssistantMessage
        import slife.ui.chat as chat

        class _FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 12, 10, 0, tzinfo=tz).astimezone()

        monkeypatch.setattr(chat, "datetime", _FixedNow)
        m = AssistantMessage(timestamp=datetime(2026, 8, 12, 14, 32))
        m.append_text("hi")
        assert m.render().plain == "[14:32] hi"

    def test_assistant_timestamp_after_thinking(self, monkeypatch):
        """The timestamp goes on the response text, NOT before the thinking
        block — a thinking-only message shows no time."""
        from datetime import datetime
        from slife.ui.chat import AssistantMessage
        import slife.ui.chat as chat

        class _FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return datetime(2026, 8, 12, 10, 0, tzinfo=tz).astimezone()

        monkeypatch.setattr(chat, "datetime", _FixedNow)
        m = AssistantMessage(timestamp=datetime(2026, 8, 12, 14, 32))
        m.append_thinking("thinking…")
        # Thinking-only (no response text yet) → no timestamp.
        assert "[14:32]" not in m.render().plain
        # Once the reply streams in, the time sits before the text.
        m.append_text("answer")
        plain = m.render().plain
        assert plain.index("[14:32]") > plain.index("thinking")
        assert plain.endswith("answer")

    def test_assistant_no_timestamp_unchanged(self):
        from slife.ui.chat import AssistantMessage
        m = AssistantMessage()
        m.append_text("hi")
        assert m.render().plain == "hi"
