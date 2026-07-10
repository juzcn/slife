"""Textual TUI application for slife — Claude Code CLI style."""

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from slife.config import Config
from slife.agent.service import AgentService
from slife.agent.loop import MaxIterationsExceeded
from slife.agent.multimodal import parse_file_attachments
from slife.ui.chat import ChatView, AssistantMessage
from slife.ui.handler import TUIHandler
from slife.ui.tool_display import ToolCallWidget


# ── Status bar ─────────────────────────────────────────────────────


class StatusBar(Static):
    """Thin status bar showing model, tokens, and key bindings.

    Claude Code style: minimal, dim, informative.
    """

    def update_info(
        self,
        model: str = "",
        tokens: int = 0,
        thinking: bool = False,
    ) -> None:
        """Update the status bar display."""
        parts = []

        if model:
            parts.append(f"[#8b949e]{model}[/#8b949e]")

        if thinking:
            parts.append("[#d29922]⚡ thinking[/#d29922]")

        if tokens > 0:
            parts.append(f"[#6e7681]↑ {tokens:,} tokens[/#6e7681]")

        parts.append(
            "[#484f58]│ Ctrl+C quit  Ctrl+L clear  Esc focus[/#484f58]"
        )

        self.update("  ".join(parts))


# ── Main TUI app ───────────────────────────────────────────────────


class SlifeApp(App):
    """Main Textual application for slife — an AI agent in the terminal.

    Claude Code CLI style: minimal chrome, dark theme, clean message display.
    Owns the UI; delegates agent orchestration to AgentService.
    """

    CSS_PATH = "slife.tcss"

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear"),
        ("escape", "focus_input", "Focus Input"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.service = AgentService(config)

        # TUI state for tracking active widgets during streaming
        self._tool_widgets: dict[str, ToolCallWidget] = {}
        self._active_assistant: AssistantMessage | None = None

    def compose(self) -> ComposeResult:
        """Minimal layout: chat fills screen, input + status docked at bottom."""
        yield ChatView(id="chat-view")
        yield Input(
            placeholder="Message slife…",
            id="user-input",
        )
        yield StatusBar(id="status-bar")

    def on_mount(self) -> None:
        """Initialize status bar with model info."""
        status = self.query_one("#status-bar", StatusBar)
        status.update_info(
            model=self.service.model_display_name,
            thinking=self.service.thinking_enabled,
        )

    # ── Actions ──────────────────────────────────────────────────

    def action_clear_chat(self) -> None:
        """Clear chat history and conversation."""
        self.service.clear()
        chat_view = self.query_one("#chat-view", ChatView)
        for child in list(chat_view.children):
            child.remove()
        self._tool_widgets.clear()

    def action_focus_input(self) -> None:
        """Focus the input field."""
        self.query_one("#user-input").focus()

    # ── Status bar ───────────────────────────────────────────────

    def _update_status(self) -> None:
        """Refresh the status bar with current session info."""
        status = self.query_one("#status-bar", StatusBar)
        status.update_info(
            model=self.service.model_display_name,
            tokens=self.service.session_usage.total_tokens,
            thinking=self.service.thinking_enabled,
        )

    # ── Input handling ────────────────────────────────────────────

    def on_input_submitted(self, event) -> None:
        """Handle user pressing Enter in the input field."""
        from textual.widgets import Input

        if not isinstance(event, Input.Submitted):
            return

        raw = event.value.strip()
        if not raw:
            return

        event.input.clear()

        # Parse /file directives for multimodal
        text, image_paths = parse_file_attachments(raw)

        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_user_message(
            text or raw, images=image_paths if image_paths else None
        )

        self.run_worker(
            self._process_message(text or raw, image_paths, chat_view),
            exclusive=True,
            group="agent",
        )

    # ── Agent interaction ─────────────────────────────────────────

    async def _process_message(
        self,
        text: str,
        images: list[str],
        chat_view: ChatView,
    ) -> None:
        """Run the agent loop and stream results to the TUI."""
        # Create the assistant message widget that will receive streaming content
        self._active_assistant = chat_view.add_assistant_message()
        self._tool_widgets.clear()

        handler = TUIHandler(self)

        try:
            await self.service.process_message(
                user_input=text,
                images=images if images else None,
                handler=handler,
            )
        except MaxIterationsExceeded as e:
            chat_view.add_system_message(f"[#f85149]✗ {e}[/#f85149]")
        except Exception as e:
            chat_view.add_system_message(f"[#f85149]✗ Error: {e}[/#f85149]")
        finally:
            self._active_assistant = None
