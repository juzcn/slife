"""Chat view widgets for the slife TUI."""

from textual.containers import Container, VerticalScroll
from textual.widgets import Input, Static

from slife.agent.llm_client import TokenUsage


class ChatView(VerticalScroll):
    """Scrollable container for chat messages."""

    can_focus = False

    def add_user_message(self, text: str, images: list[str] | None = None) -> "UserMessage":
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
        msg = Static(text, classes="system-message")
        self.mount(msg)
        self.scroll_end(animate=False)


class UserMessage(Static):
    """Displays a user message with optional file attachment indicators."""

    def __init__(self, text: str, images: list[str] | None = None):
        parts = ["[bold]You:[/bold]"]
        if images:
            file_list = ", ".join(images)
            parts.append(f" [dim][📎 {file_list}][/dim]")
        parts.append(f" {text}")
        super().__init__("".join(parts))
        self.add_class("user-message")


class AssistantMessage(Static):
    """Displays an assistant message with thinking and token usage."""

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

    def append_char(self, char: str) -> None:
        """Append a character to the visible response."""
        self._buffer += char
        self._refresh_display()

    def set_token_usage(self, usage: TokenUsage) -> None:
        """Set token usage to display after the response."""
        self._usage = usage
        self._refresh_display()

    def _refresh_display(self) -> None:
        """Rebuild the display from thinking, response, and usage."""
        parts = []

        if self._has_thinking:
            thinking_display = (
                self._thinking[:500] + "..."
                if len(self._thinking) > 500
                else self._thinking
            )
            parts.append(
                f"[dim italic]Thought:[/dim italic] [dim]{thinking_display}[/dim]"
            )
            parts.append("")

        if self._buffer:
            parts.append(f"[bold]Assistant:[/bold] {self._buffer}")
        else:
            if not self._has_thinking:
                parts.append("[bold]Assistant:[/bold] [dim]...[/dim]")

        if self._usage:
            parts.append(
                f"[dim]↖ {self._usage.total_tokens:,} tokens "
                f"(in: {self._usage.prompt_tokens:,}, "
                f"out: {self._usage.completion_tokens:,})[/dim]"
            )

        self.update("\n".join(parts) if parts else "")


class InputBar(Container):
    """Bottom input bar with text input field."""

    def compose(self):
        yield Input(
            placeholder=(
                "Type your message... (/file <path> to attach, "
                "Ctrl+C to quit, Ctrl+L to clear)"
            ),
            id="user-input",
        )
