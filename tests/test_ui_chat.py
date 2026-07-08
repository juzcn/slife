"""Tests for chat UI widgets (slife.ui.chat).

Tests widget construction, state management, and display logic without
requiring a running Textual application. Uses monkey-patching on
self.update() to capture rendered content.
"""

import pytest

from slife.agent.llm_client import TokenUsage
from slife.ui.chat import UserMessage, AssistantMessage, InputBar


def _get_rendered(widget) -> str:
    """Capture what update() was last called with on a Static widget."""
    captured = []

    def capture(text=""):
        captured.append(text)
        # Also set on the widget so _refresh_display can call self.update
        # but we've already captured at that point

    widget.update = capture
    # If widget has a _refresh_display that calls self.update(),
    # we need the initial content too. For UserMessage, content is set
    # in __init__ via super().__init__("..."), not via update().
    # So for widgets that set content via super().__init__(), we check
    # the superclass init argument.
    return captured[0] if captured else ""


def _get_widget_text(widget) -> str:
    """Get rendered text from a Static widget.

    For AssistantMessage which uses self.update(), intercept update().
    For UserMessage which uses super().__init__(content), use widget.content.
    """
    captured = []

    def capture_update(text=""):
        captured.append(text)

    # Replace update with our capture
    widget.update = capture_update
    # Trigger a display refresh if available (for AssistantMessage)
    if hasattr(widget, '_refresh_display'):
        widget._refresh_display()
    if captured:
        return captured[0]

    # Fallback: use widget.content for Static widgets
    content = getattr(widget, 'content', '')
    return str(content) if content else ""


# ══════════════════════════════════════════════════════════════════════
# UserMessage
# ══════════════════════════════════════════════════════════════════════


class TestUserMessage:
    """Tests for UserMessage widget."""

    def test_plain_text(self):
        """UserMessage with plain text displays the text."""
        msg = UserMessage("Hello, world!")
        text = _get_widget_text(msg)
        assert "Hello, world!" in text

    def test_with_images(self):
        """With images, attachment info is shown."""
        msg = UserMessage("Look at this", images=["photo.png", "doc.jpg"])
        text = _get_widget_text(msg)
        assert "Look at this" in text
        assert "📎" in text
        assert "photo.png" in text
        assert "doc.jpg" in text

    def test_with_no_images(self):
        """Without images, no attachment indicator is shown."""
        msg = UserMessage("Plain message", images=None)
        text = _get_widget_text(msg)
        assert "📎" not in text

    def test_with_empty_images_list(self):
        """Empty images list shows no attachment indicator."""
        msg = UserMessage("Plain", images=[])
        text = _get_widget_text(msg)
        assert "📎" not in text

    def test_css_class(self):
        """UserMessage has 'user-message' CSS class."""
        msg = UserMessage("test")
        assert "user-message" in msg.classes

    def test_empty_text(self):
        """Empty text message is allowed."""
        msg = UserMessage("")
        text = _get_widget_text(msg)
        assert isinstance(text, str)

    def test_special_characters(self):
        """Special characters in text are handled."""
        msg = UserMessage("Hello <world> & \"friends\"")
        text = _get_widget_text(msg)
        assert "Hello" in text


# ══════════════════════════════════════════════════════════════════════
# AssistantMessage
# ══════════════════════════════════════════════════════════════════════


class TestAssistantMessage:
    """Tests for AssistantMessage widget."""

    def test_initial_state(self):
        """Initial state has empty buffer, no thinking, no usage."""
        msg = AssistantMessage()
        assert msg._buffer == ""
        assert msg._thinking == ""
        assert msg._has_thinking is False
        assert msg._usage is None

    def test_css_class(self):
        """Has 'assistant-message' CSS class."""
        msg = AssistantMessage()
        assert "assistant-message" in msg.classes

    def test_append_text(self):
        """append_text accumulates text and refreshes display."""
        msg = AssistantMessage()
        msg.append_text("Hello")
        msg.append_text(" world")
        assert msg._buffer == "Hello world"
        text = _get_widget_text(msg)
        assert "Hello world" in text

    def test_append_thinking(self):
        """append_thinking accumulates thinking and shows it."""
        msg = AssistantMessage()
        msg.append_thinking("Let me analyze this...")
        assert msg._thinking == "Let me analyze this..."
        assert msg._has_thinking is True
        text = _get_widget_text(msg)
        assert "Let me analyze this..." in text
        assert "Thinking" in text

    def test_thinking_truncated_at_500_chars(self):
        """Thinking display is truncated at 500 characters."""
        msg = AssistantMessage()
        long_thinking = "x" * 600
        msg.append_thinking(long_thinking)

        text = _get_widget_text(msg)
        assert "..." in text  # Truncation indicator
        displayed = long_thinking[:500] + "..."
        assert displayed in text

    def test_thinking_under_500_not_truncated(self):
        """Thinking under 500 chars is displayed fully."""
        msg = AssistantMessage()
        short_thinking = "Short thought"
        msg.append_thinking(short_thinking)
        text = _get_widget_text(msg)
        assert short_thinking in text
        assert "..." not in text  # No truncation ellipsis (except in "Thinking…")

    def test_set_token_usage(self):
        """set_token_usage stores and displays usage."""
        msg = AssistantMessage()
        msg.append_text("Response")
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        msg.set_token_usage(usage)

        text = _get_widget_text(msg)
        assert "150" in text  # total tokens
        assert "100" in text  # prompt tokens
        assert "50" in text   # completion tokens
        assert "tokens" in text

    def test_empty_state_shows_ellipsis(self):
        """When no thinking or text, shows '…' indicator."""
        msg = AssistantMessage()
        text = _get_widget_text(msg)
        assert "…" in text

    def test_thinking_shown_before_text(self):
        """Thinking block appears before response text in display."""
        msg = AssistantMessage()
        msg.append_thinking("Thinking...")
        msg.append_text("Response text")

        text = _get_widget_text(msg)
        think_pos = text.index("Thinking")
        response_pos = text.index("Response")
        assert think_pos < response_pos

    def test_multiple_appends(self):
        """Multiple appends accumulate correctly."""
        msg = AssistantMessage()
        for chunk in ["chunk1", "chunk2", "chunk3"]:
            msg.append_text(chunk)
        assert msg._buffer == "chunk1chunk2chunk3"

    def test_usage_update_overwrites(self):
        """Setting usage twice overwrites the previous usage."""
        msg = AssistantMessage()
        msg.append_text("test")
        msg.set_token_usage(TokenUsage(total_tokens=10))
        msg.set_token_usage(TokenUsage(total_tokens=20))

        text = _get_widget_text(msg)
        assert "20" in text
        assert "10" not in text


# ══════════════════════════════════════════════════════════════════════
# InputBar
# ══════════════════════════════════════════════════════════════════════


class TestInputBar:
    """Tests for InputBar widget."""

    def test_compose_has_input(self):
        """InputBar composes an Input widget."""
        bar = InputBar()
        widgets = list(bar.compose())
        assert len(widgets) == 1
        from textual.widgets import Input
        assert isinstance(widgets[0], Input)
        assert widgets[0].id == "user-input"

    def test_input_placeholder(self):
        """Input has a placeholder text."""
        bar = InputBar()
        input_widget = next(bar.compose())
        assert "Message slife" in input_widget.placeholder


# ══════════════════════════════════════════════════════════════════════
# ChatView
# ══════════════════════════════════════════════════════════════════════


class TestChatView:
    """Tests for ChatView widget methods (with mocked DOM)."""

    def test_add_user_message(self):
        """add_user_message mounts a UserMessage and returns it."""
        from slife.ui.chat import ChatView
        from unittest.mock import MagicMock

        view = ChatView()
        view.mount = MagicMock()
        view.scroll_end = MagicMock()

        msg = view.add_user_message("Hello!")
        assert isinstance(msg, UserMessage)
        view.mount.assert_called_once()
        view.scroll_end.assert_called_once()
        assert "Hello!" in msg.content

    def test_add_user_message_with_images(self):
        """add_user_message with images attaches info."""
        from slife.ui.chat import ChatView
        from unittest.mock import MagicMock

        view = ChatView()
        view.mount = MagicMock()
        view.scroll_end = MagicMock()

        msg = view.add_user_message("Look", images=["a.png"])
        assert isinstance(msg, UserMessage)
        assert "📎" in msg.content

    def test_add_assistant_message(self):
        """add_assistant_message mounts and returns an AssistantMessage."""
        from slife.ui.chat import ChatView
        from unittest.mock import MagicMock

        view = ChatView()
        view.mount = MagicMock()
        view.scroll_end = MagicMock()

        msg = view.add_assistant_message()
        assert isinstance(msg, AssistantMessage)
        view.mount.assert_called_once()
        view.scroll_end.assert_called_once()

    def test_add_system_message(self):
        """add_system_message mounts a Static with system-message class."""
        from slife.ui.chat import ChatView
        from unittest.mock import MagicMock

        view = ChatView()
        view.mount = MagicMock()
        view.scroll_end = MagicMock()

        view.add_system_message("Error occurred")
        view.mount.assert_called_once()
        view.scroll_end.assert_called_once()
        # Check the mounted widget
        mounted = view.mount.call_args[0][0]
        assert "system-message" in mounted.classes
        assert "Error occurred" in mounted.content
