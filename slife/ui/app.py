"""Textual TUI application for slife — Claude Code CLI style."""

from textual.app import App, ComposeResult
from textual.widgets import Static

from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.conversation import Conversation
from slife.agent.loop import (
    AgentLoop,
    AgentEventHandler,
    ToolCallInfo,
    AgentResult,
    MaxIterationsExceeded,
)
from slife.agent.multimodal import parse_file_attachments
from slife.tools.factory import create_tools_from_config
from slife.ui.chat import ChatView, InputBar, AssistantMessage
from slife.ui.tool_display import ToolCallWidget


# ── Agent service layer ────────────────────────────────────────────


class AgentService:
    """Wires together LLM client, tools, conversation, and agent loop.

    Owns the agent's runtime state. The TUI delegates to this service
    rather than directly managing agent internals.
    """

    def __init__(self, config: Config):
        self.config = config
        self.tool_registry = create_tools_from_config(config.tools)
        self.llm_client = LLMClient(config.active_model)
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
        )
        self.conversation = Conversation(system_prompt=config.system_prompt)
        self.session_usage = TokenUsage()

    @property
    def model_display_name(self) -> str:
        """Human-readable name of the active model."""
        return self.config.active_model.display_name

    @property
    def thinking_enabled(self) -> bool:
        """Whether thinking/reasoning mode is active."""
        return self.config.active_model.thinking_enabled

    def clear(self) -> None:
        """Reset conversation history and session usage."""
        self.conversation.clear()
        self.session_usage = TokenUsage()

    async def process_message(
        self,
        user_input: str,
        images: list[str] | None,
        handler: AgentEventHandler,
    ) -> AgentResult:
        """Run the agent loop for a user message via streaming."""
        return await self.agent_loop.run(
            user_input=user_input,
            conversation=self.conversation,
            images=images,
            handler=handler,
        )


# ── TUI → Agent event bridge ───────────────────────────────────────


class _TUIHandler:
    """Bridges AgentEventHandler callbacks to the Textual TUI.

    Implements the AgentEventHandler protocol — receives real-time
    streaming events from the agent loop and updates TUI widgets.
    """

    def __init__(self, app: "SlifeApp"):
        self._app = app

    async def on_thinking_chunk(self, chunk: str) -> None:
        """Stream a thinking/reasoning token to the active assistant widget."""
        if self._app._active_assistant:
            self._app._active_assistant.append_thinking(chunk)

    async def on_text_chunk(self, chunk: str) -> None:
        """Stream a text token to the active assistant widget."""
        if self._app._active_assistant:
            self._app._active_assistant.append_text(chunk)

    async def on_tool_call(self, tool_call: ToolCallInfo) -> None:
        """Mount a tool call widget in the chat view."""
        widget = ToolCallWidget(
            tool_name=tool_call.name,
            tool_args=tool_call.arguments,
            tool_call_id=tool_call.id,
        )
        widget.set_running()
        chat_view = self._app.query_one("#chat-view", ChatView)
        chat_view.mount(widget)
        chat_view.scroll_end(animate=False)
        self._app._tool_widgets[tool_call.id] = widget

    async def on_tool_result(
        self, tool_call_id: str, result: str, is_error: bool
    ) -> None:
        """Update a tool call widget with its result."""
        widget = self._app._tool_widgets.get(tool_call_id)
        if widget:
            widget.set_complete(result, is_error)

    async def on_token_usage(self, usage: TokenUsage) -> None:
        """Update session usage and refresh status bar."""
        self._app.service.session_usage = usage
        if self._app._active_assistant:
            self._app._active_assistant.set_token_usage(usage)
        self._app._update_status()


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
        yield InputBar(id="input-bar")
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

        handler = _TUIHandler(self)

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
