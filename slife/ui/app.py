"""Textual TUI application for slife — Claude Code CLI style."""

from textual.app import App, ComposeResult
from textual.message import Message
from textual.widgets import Input, Static

from slife.config import Config
from slife.agent.service import AgentService
from slife.agent.loop import MaxIterationsExceeded
from slife.agent.multimodal import parse_file_attachments
from slife.ui.chat import ChatView
from slife.ui.command_palette import CommandPalette
from slife.ui.handler import TUIHandler
from slife.ui.tool_display import ToolCallWidget


# ── Custom input with slash-command completion ────────────────────


class CommandInput(Input):
    """Input widget with Tab-to-complete for slash commands."""

    BINDINGS = [
        ("tab", "complete_suggestion", "Complete"),
    ]

    def action_complete_suggestion(self) -> None:
        """Complete the current slash suggestion, or pass Tab through."""
        if self.value.startswith("/"):
            self.post_message(CompleteSuggestion())
        else:
            # Not a slash command — let Tab do default focus navigation
            self.app.action_focus_next()


class CompleteSuggestion(Message):
    """Posted when Tab is pressed — parent should complete the suggestion."""


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

        # Resolve assistant name prefix once (set on first user message)
        a2a = config.a2a_config
        agent_name = a2a.agent_name if a2a else None
        self._assistant_prefix: str = (
            f"{agent_name}> " if agent_name else "> "
        )

        # TUI state for tracking active widgets during streaming
        self._tool_widgets: dict[str, ToolCallWidget] = {}

    def compose(self) -> ComposeResult:
        """Minimal layout: chat fills screen, input + status docked at bottom."""
        yield ChatView(id="chat-view")
        yield CommandPalette(id="command-palette")
        yield CommandInput(
            placeholder="Message slife…",
            id="user-input",
        )
        yield StatusBar(id="status-bar")

    async def on_mount(self) -> None:
        """Initialize status bar and start MCP + A2A + Subagent integration."""
        status = self.query_one("#status-bar", StatusBar)
        status.update_info(
            model=self.service.model_display_name,
            thinking=self.service.thinking_enabled,
        )

        # Focus input on startup
        self.query_one("#user-input").focus()

        # Start MCP wrapper in the background
        if self.service.config.mcp_config.enabled:
            self.run_worker(
                self.service.start_mcp(),
                exclusive=False,
                group="mcp-startup",
            )

        # Start A2A P2P mesh in the background (MQTT — remote peers)
        if self.service.config.a2a_config and self.service.config.a2a_config.enabled:
            self.service.on_a2a_activity(self._on_a2a_activity)
            # Pass a handler factory so remote tasks stream to chat
            # just like human-typed messages — fresh TUIHandler per task.
            self.run_worker(
                self.service.start_a2a(
                    handler_factory=lambda: TUIHandler(
                        self, assistant_prefix=self._assistant_prefix
                    ),
                ),
                exclusive=False,
                group="a2a-startup",
            )

        # Start subagent manager (local stdin/stdout — always available)
        if self.service.config.subagent_config:
            self.run_worker(
                self.service.start_subagent(),
                exclusive=False,
                group="subagent-startup",
            )

    # ── Actions ──────────────────────────────────────────────────

    async def action_quit(self) -> None:
        """Quit the app, shutting down subagent, A2A, and MCP."""
        await self.service.stop_subagent()
        await self.service.stop_a2a()
        await self.service.stop_mcp()
        await super().action_quit()

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

    # ── Slash-command completion ─────────────────────────────────

    def on_input_changed(self, event: Input.Changed) -> None:
        """Update the command palette when input value changes."""
        palette = self.query_one("#command-palette", CommandPalette)
        palette.show_suggestions(event.value)

    def on_complete_suggestion(self) -> None:
        """Complete the current slash command with the top suggestion."""
        palette = self.query_one("#command-palette", CommandPalette)
        if not palette.visible:
            return

        completion = palette.selected_text()
        if not completion:
            return

        inp = self.query_one("#user-input", CommandInput)
        inp.value = completion
        inp.cursor_position = len(completion)
        # Refresh palette with the completed value
        palette.show_suggestions(completion)

    # ── Input handling ────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user pressing Enter in the input field."""
        # Hide palette on submit
        self.query_one("#command-palette", CommandPalette).hide()

        if not isinstance(event, Input.Submitted):
            return

        raw = event.value.strip()
        if not raw:
            return

        # /exit — quit the app
        if raw == "/exit":
            self.run_worker(self.action_quit(), exclusive=True)
            return

        event.input.clear()

        # Parse /file directives for multimodal
        text, image_paths = parse_file_attachments(raw)

        chat_view = self.query_one("#chat-view", ChatView)
        # Use agent name for assistant prefix, "You" for user prefix
        user_prefix = "You> " if self._assistant_prefix else "> "
        chat_view.add_user_message(
            text or raw,
            images=image_paths if image_paths else None,
            prefix=user_prefix,
        )

        self.run_worker(
            self._process_message(text or raw, image_paths, chat_view),
            exclusive=True,
            group="agent",
        )

    # ── A2A activity (chat notifications) ───────────────────────────

    async def _on_a2a_activity(self, kind: str, **kwargs) -> None:
        """Handle A2A events by updating the chat view."""
        chat_view = self.query_one("#chat-view", ChatView)

        if kind == "agent_change":
            card = kwargs.get("card")
            event = kwargs.get("event", "")
            if event == "online":
                name = card.display_name or card.agent_id
                chat_view.add_system_message(
                    f"⚡ {name} ({card.agent_id}) online [{card.status}]",
                    color="#7c3aed",
                )
            elif event == "offline":
                chat_view.add_system_message(
                    f"✗ {card.agent_id} offline", color="#6e7681",
                )
            elif event == "timeout":
                chat_view.add_system_message(
                    f"⏱ {card.agent_id} timed out", color="#d29922",
                )

        elif kind == "task_received":
            source = kwargs.get("source", "unknown")
            content = kwargs.get("content", "").strip()
            # Show as a normal user message with source as prefix
            chat_view.add_user_message(content, prefix=f"{source}> ")

        elif kind == "task_completed":
            source = kwargs.get("source", "unknown")
            result = kwargs.get("result", "")
            chat_view.add_system_message(
                f"✓ task from {source} completed", color="#3fb950",
            )

    # ── Agent interaction ─────────────────────────────────────────

    async def _process_message(
        self,
        text: str,
        images: list[str],
        chat_view: ChatView,
    ) -> None:
        """Run the agent loop and stream results to the TUI."""
        self._tool_widgets.clear()

        handler = TUIHandler(self, assistant_prefix=self._assistant_prefix)

        try:
            await self.service.process_message(
                user_input=text,
                images=images if images else None,
                handler=handler,
            )
            handler.finalize_current()
        except MaxIterationsExceeded as e:
            handler.finalize_current()
            chat_view.add_system_message(f"✗ {e}", color="#f85149")
        except Exception as e:
            handler.finalize_current()
            chat_view.add_system_message(f"✗ Error: {e}", color="#f85149")
