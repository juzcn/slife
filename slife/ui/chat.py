"""Chat view widgets for the Slife TUI — Claude Code CLI style."""

import re

from rich.style import Style as RichStyle
from rich.text import Text as RichText
from textual.containers import VerticalScroll
from textual.content import Content
from textual.events import Key
from textual.widgets import Static

from slife.agent.llm_client import TokenUsage
from slife.ui.image_utils import safe_image_widget

# ── Clickable link detection ──────────────────────────────────────────
# Detects URIs and absolute paths in assistant output so links are
# actually clickable in the TUI (Content.from_text does NOT auto-link).

# Standard URI schemes: https://, http://, file:///, ws://, wss://
_URI_RE = re.compile(
    r"(?<!\w)((?:https?|file|ws|wss)://[^\s<>\[\]{}|\\^`\"']+)"
)

# Windows absolute paths ending in a common image / document extension.
# Link target becomes file:/// with forward slashes.
_WIN_PATH_RE = re.compile(
    r"(?<!\w)([A-Za-z]:\\(?:[^\s<>\[\]]+\\)*[^\s<>\[\]]+"
    r"\.(?:png|jpg|jpeg|gif|webp|bmp|svg|tiff|tif))",
    re.IGNORECASE,
)


def _linkify(plain: str) -> Content:
    """Return *plain* as Content with clickable links for detected URIs / paths."""
    if not plain:
        return Content("")
    rt = RichText(plain)

    # URIs → clickable (the URI itself is the link target)
    rt.highlight_regex(
        _URI_RE,
        style=lambda m: RichStyle(link=m),
    )

    # Windows paths → clickable via file:/// link
    def _file_link(m: str) -> RichStyle:
        return RichStyle(link="file:///" + m.replace("\\", "/"))

    rt.highlight_regex(_WIN_PATH_RE, style=_file_link)

    return Content.from_rich_text(rt)


class ChatView(VerticalScroll):
    """Scrollable container for chat messages.

    can_focus is True so the ScrollView itself can receive focus and
    process keyboard scroll bindings (PageUp/PageDown/Home/End).
    Tab is intercepted at the Screen level to always focus the input.
    """

    can_focus = True

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        # When False, the add_* helpers do not auto-scroll to the end.
        # Session restore turns this off, rebuilds the whole history, and
        # scrolls exactly once at the end — scrolling on every widget (the
        # normal live behaviour) is what made the restore jitter.
        self._autoscroll: bool = True

    def _follow(self) -> None:
        """Scroll to the end unless auto-scroll is suppressed (restore)."""
        if self._autoscroll:
            self.scroll_end(animate=False)

    def _follow_after_refresh(self) -> None:
        """Deferred :meth:`_follow` — used after mounting a widget whose
        full height is only known once it has been laid out (images)."""
        if self._autoscroll:
            self.call_after_refresh(self.scroll_end, animate=False)

    async def _on_key(self, event: Key) -> None:
        """Redirect printable keys to the input field."""
        if event.is_printable:
            inp = self.screen.query_one("#user-input")
            if inp is not None and not inp.has_focus:
                inp.focus()
                await inp._on_key(event)
                event.stop()
                return
        await super()._on_key(event)

    def add_user_message(
        self,
        text: str,
        images: list[str] | None = None,
        prefix: str = "> ",
    ) -> "UserMessage":
        """Add and return a user message widget.

        Image attachments are mounted as sibling widgets below the
        message text so they render inline in the chat scroll.
        """
        msg = UserMessage(text, images=images, prefix=prefix)
        self.mount(msg)
        if images:
            for img_path in images:
                self.add_image_to_chat(img_path, thumb=True)
        self._follow()
        return msg

    def add_image_to_chat(
        self, file_path: str, *, thumb: bool = False
    ):  # returns HalfcellImage or fallback Static
        """Mount an inline image widget in the chat view.

        Uses ``safe_image_widget`` — never raises, always returns a
        widget (Image or text fallback).

        Args:
            file_path: Path to an image file.
            thumb: Use small thumbnail size when True.
        """
        css = "chat-image-thumb" if thumb else "chat-image"
        widget = safe_image_widget(file_path, css_class=css)
        self.mount(widget)
        # Defer scroll so the image widget has time to render its full
        # height before we compute the scroll position.
        self._follow_after_refresh()
        return widget

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
        self._follow()
        return msg

    def add_system_message(self, text: str, color: str | None = None) -> None:
        """Add a system/status message."""
        content = Content.from_text(text, markup=False)
        if color:
            content = content.stylize(color)
        msg = Static(content, classes="system-message")
        self.mount(msg)
        self._follow()

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
        self._follow()
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
        # Image rendering is handled by ChatView.add_user_message()
        # which mounts InlineImage siblings — no text fallback here.
        super().__init__(content)
        self.add_class("user-message")
        self._image_paths: list[str] = images or []


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

    async def _on_key(self, event: Key) -> None:
        """Redirect printable keys to the input field."""
        if event.is_printable:
            inp = self.screen.query_one("#user-input")
            if inp is not None and not inp.has_focus:
                inp.focus()
                await inp._on_key(event)
                event.stop()
                return
        await super()._on_key(event)

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
        self._image_paths: list[str] = []  # images to render below text

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

    def append_image(self, source: str) -> None:
        """Record an image to be rendered below the response text.

        The actual widget mounting is handled externally (by
        TUIHandler / ChatView) — this just tracks the source
        so session restore can reconstruct the image list.
        """
        self._image_paths.append(source)

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
        """Build response text with optional name prefix styling.

        URLs and absolute file paths are auto-detected and rendered as
        clickable links (via Rich :class:`~rich.text.Text` highlight).
        """
        if self._name_prefix:
            full = f"{self._name_prefix}{self._buffer}"
            rt = RichText(full)
            rt.highlight_regex(
                _URI_RE,
                style=lambda m: RichStyle(link=m),
            )
            rt.highlight_regex(
                _WIN_PATH_RE,
                style=lambda m: RichStyle(link="file:///" + m.replace("\\", "/")),
            )
            rt.stylize("bold #d97706", 0, len(self._name_prefix))
            return Content.from_rich_text(rt)
        return _linkify(self._buffer)

    def _build_usage_line(self) -> Content:
        """Token usage footer line."""
        assert self._usage is not None
        return Content.from_markup(
            f"\n[dim]↑ {self._usage.total_tokens:,} tokens "
            f"(in: {self._usage.prompt_tokens:,}, "
            f"out: {self._usage.completion_tokens:,})[/dim]"
        )

    def _refresh_display(self) -> None:
        """Rebuild the display in Claude Code style using safe Content objects.

        Thinking may collapse to a one-liner, but the response text is
        ALWAYS rendered — collapsing thinking must never swallow the
        agent's reply (regression the user can hit on restore/streaming).
        """
        content = Content()

        # Thinking block: expanded, or collapsed to a one-liner.
        if self._has_thinking:
            if self._is_thinking_collapsed:
                content = content + self._build_thinking_collapsed()
                if self._buffer:
                    content = content + Content.from_text("\n", markup=False)
            else:
                content = content + self._build_thinking_expanded()

        # Response text — always visible, never collapsed away.
        # Placeholder only when there is neither text nor thinking yet.
        if self._buffer:
            content = content + self._build_response_text()
        elif not self._has_thinking:
            content = content + Content.from_markup("[dim]…[/dim]")

        # Token usage footer
        if self._usage and self._show_usage:
            content = content + self._build_usage_line()

        self.update(content if content else "")
