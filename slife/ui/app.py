"""Textual TUI application for slife."""

from textual.app import App, ComposeResult
from textual.widgets import Footer, Header

from slife.config import Config
from slife.agent.llm_client import LLMClient, TokenUsage
from slife.agent.conversation import Conversation
from slife.agent.loop import (
    AgentLoop,
    ToolCallInfo,
    AgentResult,
    MaxIterationsExceeded,
)
from slife.agent.multimodal import parse_file_attachments
from slife.tools.factory import create_tools_from_config
from slife.ui.chat import ChatView, InputBar
from slife.ui.tool_display import ToolCallWidget


class SlifeApp(App):
    """Main Textual application for slife — an AI agent in the terminal.

    Features:
      - Multi-model support (configured via slife.toml)
      - Config-driven tools
      - Token usage tracking per response and session
      - Thinking/reasoning display (DeepSeek V4)
      - Multimodal: attach images via /file <path>
    """

    CSS_PATH = "slife.tcss"

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
        ("ctrl+l", "clear_chat", "Clear Chat"),
        ("escape", "focus_input", "Focus Input"),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.config = config

        # Load tools from config
        self.tool_registry = create_tools_from_config(config.tools)

        # Shared conversation
        self.conversation = Conversation(
            system_prompt=config.system_prompt
        )

        # LLM client from active model
        self.llm_client = LLMClient(config.active_model)

        # Agent loop
        self.agent_loop = AgentLoop(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            max_iterations=config.max_iterations,
        )

        # Session token usage accumulator
        self.session_usage = TokenUsage()

        # Track tool widgets by tool_call_id
        self._tool_widgets: dict[str, ToolCallWidget] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield ChatView(id="chat-view")
        yield InputBar(id="input-bar")
        yield Footer()

    # ── Actions ──────────────────────────────────────────────────

    def action_clear_chat(self) -> None:
        """Clear chat history and conversation."""
        self.conversation.clear()
        chat_view = self.query_one("#chat-view", ChatView)
        for child in list(chat_view.children):
            child.remove()
        self._tool_widgets.clear()

    def action_focus_input(self) -> None:
        """Focus the input field."""
        self.query_one("#user-input").focus()

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
        has_images = bool(image_paths)

        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_user_message(
            text or raw, images=image_paths if has_images else None
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
        assistant_msg = chat_view.add_assistant_message()

        async def on_thinking_chunk(chunk: str) -> None:
            assistant_msg.append_thinking(chunk)

        async def on_text_chunk(char: str) -> None:
            assistant_msg.append_char(char)

        async def on_tool_call(tc: ToolCallInfo) -> None:
            widget = ToolCallWidget(
                tool_name=tc.name,
                tool_args=tc.arguments,
                tool_call_id=tc.id,
            )
            widget.set_running()
            chat_view.mount(widget)
            chat_view.scroll_end(animate=False)
            self._tool_widgets[tc.id] = widget

        async def on_tool_result(
            tc_id: str, result: str, is_error: bool
        ) -> None:
            widget = self._tool_widgets.get(tc_id)
            if widget:
                widget.set_complete(result, is_error)

        async def on_token_usage(usage: TokenUsage) -> None:
            self.session_usage = usage
            assistant_msg.set_token_usage(usage)

        try:
            result: AgentResult = await self.agent_loop.run(
                user_input=text,
                conversation=self.conversation,
                images=images if images else None,
                on_thinking_chunk=on_thinking_chunk,
                on_text_chunk=on_text_chunk,
                on_tool_call=on_tool_call,
                on_tool_result=on_tool_result,
                on_token_usage=on_token_usage,
            )
        except MaxIterationsExceeded as e:
            chat_view.add_system_message(f"[red]{e}[/red]")
        except Exception as e:
            chat_view.add_system_message(f"[red]Error: {e}[/red]")
