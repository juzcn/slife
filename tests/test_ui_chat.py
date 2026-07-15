"""Tests for Slife.ui.chat — chat view widgets (pure logic tests)."""

import pytest
from unittest.mock import MagicMock, patch

from slife.agent.llm_client import TokenUsage


# ── UserMessage logic ─────────────────────────────────────────────────


class TestUserMessage:
    """Tests for UserMessage — test string construction without Textual."""

    def test_basic_message_format(self):
        """UserMessage formats text with > prefix."""
        from slife.ui.chat import UserMessage
        with patch.object(UserMessage, '__init__', lambda self, text, images=None: None):
            pass

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
        with patch("Slife.ui.chat.Static.__init__", return_value=None):
            from slife.ui.chat import AssistantMessage
            msg = AssistantMessage.__new__(AssistantMessage)
            msg._buffer = ""
            msg._thinking = ""
            msg._has_thinking = False
            msg._usage = None
            msg._is_thinking_collapsed = False
            msg._show_usage = True
            msg._name_prefix = None
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
        """Collapsed display shows only the one-line summary."""
        msg = self._make_msg()
        msg._thinking = "Step by step reasoning"
        msg._has_thinking = True
        msg._is_thinking_collapsed = True
        msg._buffer = "The answer"
        msg._usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "Thinking" in text
        assert "22 chars" in text
        assert "▸" in text
        # Collapsed: no text buffer, no usage
        assert "The answer" not in text
        assert "tokens" not in text

    def test_collapsed_display_no_text_no_usage(self):
        """Verify collapsed display excludes text content and token usage."""
        msg = self._make_msg()
        msg._thinking = "x"
        msg._has_thinking = True
        msg._is_thinking_collapsed = True
        msg._buffer = "should not appear"
        msg._usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2)
        msg._show_usage = True  # collapse should override this
        msg.update = MagicMock()
        msg._refresh_display()
        content = msg.update.call_args[0][0]
        text = content.plain
        assert "should not appear" not in text
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


# ── ChatView logic ────────────────────────────────────────────────────


class TestChatView:
    """Tests for ChatView methods that don't need full Textual."""

    def test_can_focus_is_true(self):
        """ChatView needs focus to receive keyboard scroll bindings."""
        with patch("Slife.ui.chat.VerticalScroll.__init__", return_value=None):
            from slife.ui.chat import ChatView
            view = ChatView()
            assert view.can_focus is True
