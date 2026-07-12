"""Chat view widgets for the slife TUI — Claude Code CLI style."""

from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Static

from slife.agent.llm_client import TokenUsage


class ChatView(VerticalScroll):
    """Scrollable container for chat messages."""

    can_focus = False

    def add_user_message(
        self, text: str, images: list[str] | None = None
    ) -> "UserMessage":
        """Add and return a user message widget."""
        msg = UserMessage(text, images=images)
        self.mount(msg)
        self.scroll_end(animate=False)
        return msg

    def add_assistant_message(self) -> "AssistantMessage":
        """Add and return an assistant message widget (initially empty)."""
        msg = AssistantMessage()
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


class UserMessage(Static):
    """User message — "> text" prefix style, no label."""

    def __init__(self, text: str, images: list[str] | None = None):
        # Build safe Content: ">" prefix styled, text as literal
        content = Content.from_text("> ", markup=False).stylize("bold #d97706")
        content = content + Content.from_text(text, markup=False)
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
    """

    def __init__(self):
        super().__init__("")
        self.add_class("assistant-message")
        self._buffer = ""
        self._thinking = ""
        self._has_thinking = False
        self._usage: TokenUsage | None = None

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

    def _refresh_display(self) -> None:
        """Rebuild the display in Claude Code style using safe Content objects."""
        content = Content()

        # Thinking block — dim italic, subtle header
        if self._has_thinking:
            content = content + Content.from_markup("[dim italic]⟐ Thinking…[/dim italic]\n")
            thinking_display = (
                self._thinking[:500] + "..."
                if len(self._thinking) > 500
                else self._thinking
            )
            content = content + Content.from_text(thinking_display, markup=False).stylize("dim")
            content = content + Content.from_text("\n\n", markup=False)

        # Response text — clean, no label, safe from markup parsing
        if self._buffer:
            content = content + Content.from_text(self._buffer, markup=False)
        elif not self._has_thinking:
            content = content + Content.from_markup("[dim]…[/dim]")

        # Token usage — very subtle
        if self._usage:
            content = content + Content.from_markup(
                f"\n[dim]↑ {self._usage.total_tokens:,} tokens "
                f"(in: {self._usage.prompt_tokens:,}, "
                f"out: {self._usage.completion_tokens:,})[/dim]"
            )

        self.update(content if content else "")
