"""Chat view widgets for the slife TUI — Claude Code CLI style."""

from rich.markup import escape as _escape
from textual.containers import VerticalScroll
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

    def add_system_message(self, text: str) -> None:
        """Add a system/status message."""
        msg = Static(_escape(text), classes="system-message")
        self.mount(msg)
        self.scroll_end(animate=False)


class UserMessage(Static):
    """User message — "> text" prefix style, no label."""

    def __init__(self, text: str, images: list[str] | None = None):
        parts = [f"[bold #d97706]>[/bold #d97706] {_escape(text)}"]
        if images:
            file_list = ", ".join(images)
            parts.append(f" [dim]# 📎 {_escape(file_list)}[/dim]")
        super().__init__("".join(parts))
        self.add_class("user-message")


class AssistantMessage(Static):
    """Assistant message — clean text with optional thinking block.

    Claude Code style: no "Assistant:" label, thinking in dim italic,
    response text cleanly presented, token usage shown subtly.
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
        """Rebuild the display in Claude Code style."""
        parts = []

        # Thinking block — dim italic, subtle header
        if self._has_thinking:
            thinking_display = _escape(
                self._thinking[:500] + "..."
                if len(self._thinking) > 500
                else self._thinking
            )
            parts.append(
                f"[dim italic]⟐ Thinking…[/dim italic]\n"
                f"[dim]{thinking_display}[/dim]"
            )
            parts.append("")

        # Response text — clean, no label
        if self._buffer:
            parts.append(_escape(self._buffer))
        elif not self._has_thinking:
            parts.append("[dim]…[/dim]")

        # Token usage — very subtle
        if self._usage:
            parts.append(
                f"\n[dim]↑ {self._usage.total_tokens:,} tokens "
                f"(in: {self._usage.prompt_tokens:,}, "
                f"out: {self._usage.completion_tokens:,})[/dim]"
            )

        self.update("\n".join(parts) if parts else "")


