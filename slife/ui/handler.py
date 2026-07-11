"""TUI event handler — bridges AgentEventHandler callbacks to Textual widgets.

Receives real-time streaming events from the agent loop and updates
TUI widgets (chat view, tool call widgets, status bar).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from slife.agent.llm_client import TokenUsage
from slife.agent.loop import ToolCallInfo
from slife.ui.tool_display import ToolCallWidget

if TYPE_CHECKING:
    from slife.ui.app import SlifeApp


class TUIHandler:
    """Bridges AgentEventHandler callbacks to the Textual TUI.

    Implements the AgentEventHandler protocol — receives real-time
    streaming events from the agent loop and updates TUI widgets.
    """

    def __init__(self, app: SlifeApp):
        self._app = app
        self._chat_view = app.query_one("#chat-view")  # ChatView

    async def on_thinking_chunk(self, chunk: str) -> None:
        """Stream a thinking/reasoning token to the active assistant widget."""
        if self._app._active_assistant:
            self._app._active_assistant.append_thinking(chunk)
            self._chat_view.scroll_end(animate=False)

    async def on_text_chunk(self, chunk: str) -> None:
        """Stream a text token to the active assistant widget."""
        if self._app._active_assistant:
            self._app._active_assistant.append_text(chunk)
            self._chat_view.scroll_end(animate=False)

    async def on_tool_call(
        self, tool_call: ToolCallInfo, iteration: int = 0, max_iterations: int = 10
    ) -> None:
        """Mount a tool call widget in the chat view."""
        widget = ToolCallWidget(
            tool_name=tool_call.name,
            tool_args=tool_call.arguments,
            tool_call_id=tool_call.id,
            iteration=iteration,
            max_iterations=max_iterations,
        )
        self._chat_view.mount(widget)
        widget.set_running()
        self._chat_view.scroll_end(animate=False)
        self._app._tool_widgets[tool_call.id] = widget

    async def on_tool_result(
        self, tool_call_id: str, result: str, is_error: bool
    ) -> None:
        """Update a tool call widget with its result."""
        widget = self._app._tool_widgets.get(tool_call_id)
        if widget:
            widget.set_complete(result, is_error)
            self._chat_view.scroll_end(animate=False)

    async def on_token_usage(self, usage: TokenUsage) -> None:
        """Update session usage and refresh status bar."""
        self._app.service.session_usage = usage
        if self._app._active_assistant:
            self._app._active_assistant.set_token_usage(usage)
        self._app._update_status()
        self._chat_view.scroll_end(animate=False)
