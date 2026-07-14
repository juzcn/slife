"""Chat view widgets for the slife TUI — Claude Code CLI style."""

from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Static

from slife.agent.llm_client import TokenUsage


class ChatView(VerticalScroll):
    """Scrollable container for chat messages.

    can_focus is True so the ScrollView itself can receive focus and
    process keyboard scroll bindings (PageUp/PageDown/Home/End).
    When focusable children exist inside the scroll container, Textual
    may route arrow keys to focus navigation instead of scrolling;
    keeping the container itself focusable ensures its scroll bindings
    are always active in the key-binding resolution chain.
    """

    can_focus = True

    def add_user_message(
        self,
        text: str,
        images: list[str] | None = None,
        prefix: str = "> ",
    ) -> "UserMessage":
        """Add and return a user message widget."""
        msg = UserMessage(text, images=images, prefix=prefix)
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg

    def add_assistant_message(
        self, name_prefix: str | None = None
    ) -> "AssistantMessage":
        """Add and return an assistant message widget (initially empty).

        Args:
            name_prefix: Optional prefix like ``"Jack> "`` shown before
                         the response text.  ``None`` means no prefix.
        """
        msg = AssistantMessage(name_prefix=name_prefix)
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg

    def add_system_message(self, text: str, color: str | None = None) -> None:
        """Add a system/status message."""
        content = Content.from_text(text, markup=False)
        if color:
            content = content.stylize(color)
        msg = Static(content, classes="system-message")
        self.mount(msg)
        self.scroll_end(animate=False)

    def add_a2a_task_message(self, source: str, text: str) -> "UserMessage":
        """Add an incoming A2A task from another agent.

        Uses a left-arrow prefix and the source agent id so the operator
        can see who delegated the task.  ``source`` is the remote agent's
        id (e.g. ``"desk-01"``).
        """
        content = (
            Content.from_text("← ", markup=False).stylize("bold #7c3aed")
            + Content.from_text(source, markup=False).stylize("bold italic #a78bfa")
            + Content.from_text(": ", markup=False).stylize("dim")
            + Content.from_text(text, markup=False)
        )
        msg = UserMessage.__new__(UserMessage)
        Static.__init__(msg, content)
        msg.add_class("user-message")
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg


class UserMessage(Static):
    """User message — ``prefix> text``, default prefix ``>``."""

    def __init__(
        self,
        text: str,
        images: list[str] | None = None,
        prefix: str = "> ",
    ):
        # Build as single string then style only the prefix portion.
        # Avoids Content concatenation quirks that can insert newlines.
        prefix_len = len(prefix)
        content = Content.from_text(
            f"{prefix}{text}", markup=False,
        ).stylize("bold #d97706", start=0, end=prefix_len)
        if images:
            file_list = ", ".join(images)
            content = (
                content
                + Content.from_text(" # 📎 ", markup=False).stylize("dim")
                + Content.from_text(file_list, markup=False).stylize("dim")
            )
        super().__init__(content)
        self.add_class("user-message")


class AssistantMessage(Static):
    """Assistant message — clean text with optional thinking block.

    Claude Code style: no "Assistant:" label, thinking in dim italic,
    response text cleanly presented, token usage shown subtly.

    All user-facing text goes through Content.from_text(markup=False)
    so special characters (&, [, ]) are rendered literally — no
    MarkupError from URLs or code in the assistant's output.

    Lifecycle:
      - Created by TUIHandler per iteration, receives streaming chunks.
      - After tool calls complete, handler calls finalize(intermediate=True)
        to collapse thinking and hide token usage for non-final iterations.
      - The final iteration stays expanded so the user sees the answer.
      - Click to toggle thinking collapse/expand.
    """

    can_focus = True

    BINDINGS = [
        ("enter", "toggle_thinking", "Toggle thinking"),
        ("space", "toggle_thinking", "Toggle thinking"),
    ]

    def __init__(self, name_prefix: str | None = None):
        super().__init__("")
        self.add_class("assistant-message")
        self._name_prefix = name_prefix  # e.g. "Jack> " or None
        self._buffer = ""
        self._thinking = ""
        self._has_thinking = False
        self._usage: TokenUsage | None = None
        self._is_thinking_collapsed: bool = False
        self._show_usage: bool = True

    def append_thinking(self, chunk: str) -> None:
        """Append a chunk of reasoning/thinking content."""
        self._thinking += chunk
        self._has_thinking = True
        self._refresh_display()

    def append_text(self, text: str) -> None:
        """Append text to the visible response."""
        self._buffer += text
        self._refresh_display()

    def set_token_usage(self, usage: TokenUsage) -> None:
        """Set token usage to display after the response."""
        self._usage = usage
        self._refresh_display()

    def finalize(self, intermediate: bool = False) -> None:
        """Mark this message as complete.

        Args:
            intermediate: True for non-final iterations (collapse thinking,
                          hide usage). False for the final response (keep
                          thinking expanded, show usage).
        """
        if intermediate:
            self._is_thinking_collapsed = True
            self._show_usage = False
        self._refresh_display()

    def on_click(self) -> None:
        """Expand thinking on click (never collapse — avoids destroying text selection).

        When collapsed, any click expands so the user can read the content.
        When expanded, clicks do nothing — the user may be selecting text
        to copy. Use Enter/Space to toggle collapse instead.
        """
        if self._has_thinking and self._is_thinking_collapsed:
            self._is_thinking_collapsed = False
            self._refresh_display()

    def action_toggle_thinking(self) -> None:
        """Toggle thinking collapse/expand via keyboard."""
        if self._has_thinking:
            self._is_thinking_collapsed = not self._is_thinking_collapsed
            self._refresh_display()

    # ── Content builders (composed by _refresh_display) ──────────────

    def _build_thinking_collapsed(self) -> Content:
        """One-line thinking summary with character count."""
        n = len(self._thinking)
        return Content.from_markup(
            f"[dim italic]⟐ Thinking ({n} chars) ▸[/dim italic]"
        )

    def _build_thinking_expanded(self) -> Content:
        """Expanded thinking block, truncated at 500 chars."""
        content = Content.from_markup("[dim italic]⟐ Thinking…[/dim italic]\n")
        display = (
            self._thinking[:500] + "..."
            if len(self._thinking) > 500
            else self._thinking
        )
        content = content + Content.from_text(display, markup=False).stylize("dim")
        content = content + Content.from_text("\n\n", markup=False)
        return content

    def _build_response_text(self) -> Content:
        """Build response text with optional name prefix styling."""
        if self._name_prefix:
            prefix_len = len(self._name_prefix)
            return Content.from_text(
                f"{self._name_prefix}{self._buffer}", markup=False,
            ).stylize("bold #d97706", start=0, end=prefix_len)
        return Content.from_text(self._buffer, markup=False)

    def _build_usage_line(self) -> Content:
        """Token usage footer line."""
        return Content.from_markup(
            f"\n[dim]↑ {self._usage.total_tokens:,} tokens "
            f"(in: {self._usage.prompt_tokens:,}, "
            f"out: {self._usage.completion_tokens:,})[/dim]"
        )

    def _refresh_display(self) -> None:
        """Rebuild the display in Claude Code style using safe Content objects."""
        # Collapsed thinking: show one-liner only, nothing else
        if self._has_thinking and self._is_thinking_collapsed:
            self.update(self._build_thinking_collapsed())
            return

        content = Content()

        # Expanded thinking block
        if self._has_thinking:
            content = content + self._build_thinking_expanded()

        # Response text — or placeholder if no thinking shown
        if self._buffer:
            content = content + self._build_response_text()
        elif not self._has_thinking:
            content = content + Content.from_markup("[dim]…[/dim]")

        # Token usage footer
        if self._usage and self._show_usage:
            content = content + self._build_usage_line()

        self.update(content if content else "")
