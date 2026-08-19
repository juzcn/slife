"""TUI event handler — bridges AgentEventHandler callbacks to Textual widgets.

Receives real-time streaming events from the agent loop and updates
TUI widgets (chat view, tool call widgets, status bar).

Manages per-iteration AssistantMessage lifecycle:
  - Creates a new AssistantMessage when a new iteration begins
    (detected by thinking/text chunks arriving after tool results).
  - Collapses thinking in intermediate (tool-calling) messages.
  - Keeps the final response expanded at the bottom of the chat.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING

from slife.agent.llm_client import TokenUsage
from slife.agent.loop import ToolCallInfo
from slife.ui.chat import AssistantMessage, ChatView
from slife.ui.tool_display import ToolCallWidget

if TYPE_CHECKING:
    from slife.ui.app import SlifeApp


class TUIHandler:
    """Bridges AgentEventHandler callbacks to the Textual TUI.

    Implements the AgentEventHandler protocol — receives real-time
    streaming events from the agent loop and updates TUI widgets.

    Owns the per-iteration AssistantMessage lifecycle. Each iteration
    of the agent loop gets its own message widget; intermediate
    iterations have their thinking collapsed so the final response
    is always at the bottom of the chat.
    """

    def __init__(
        self,
        app: SlifeApp,
        assistant_prefix: str | None = None,
        timestamp: datetime | None = None,
    ):
        self._app = app
        self._chat_view: ChatView = app.query_one("#chat-view")  # type: ignore[assignment]
        self._assistant_prefix = assistant_prefix
        # Turn timestamp — the user's Enter-press moment when threaded from
        # the app, else captured now.  Rendered on assistant messages AND
        # threaded into the diary ``created_at`` via the inbox →
        # save_to_memory path, so restore shows the same [HH:MM] as live.
        self._timestamp: datetime = timestamp or datetime.now().astimezone()
        self._current_assistant: AssistantMessage | None = None
        self._iteration_needs_new_message: bool = False
        # Every AssistantMessage widget created for this turn — updated to
        # the completion time by set_completed_at when the turn ends.
        self._turn_assistants: list[AssistantMessage] = []
        # True when the current assistant reply is a bare "." — silence, so
        # the message is discarded at finalize instead of shown.
        self._silent_dot: bool = False

    # ── Assistant message lifecycle ──────────────────────────────────

    def _ensure_assistant(self) -> None:
        """Ensure a current AssistantMessage exists for streaming chunks.

        Creates a new message when:
          - This is the very first chunk (no message exists yet).
          - A new iteration has started (tool results were received
            in the previous iteration, signaled by the flag).
        """
        if self._iteration_needs_new_message or self._current_assistant is None:
            # Collapse the previous message (intermediate iteration)
            if self._current_assistant is not None:
                self._current_assistant.finalize(intermediate=True)
            # Create fresh message for the new iteration
            self._current_assistant = self._chat_view.add_assistant_message(
                name_prefix=self._assistant_prefix,
                timestamp=self._timestamp,
            )
            self._turn_assistants.append(self._current_assistant)
            self._iteration_needs_new_message = False
            self._silent_dot = False

    def set_completed_at(self, dt: datetime) -> None:
        """Stamp the turn's assistant messages with the completion time.

        Called by the service when the turn finishes persisting (the
        ``now`` captured after ``_ensure_turn_consistent``); updates every
        assistant message of this turn so the live [HH:MM] equals the
        ``completed_at`` that restore will read.
        """
        for msg in self._turn_assistants:
            msg._timestamp = dt
            msg._refresh_display()

    def finalize_current(self) -> None:
        """Mark the current assistant message as the final response.

        Called after the agent loop completes (success, max iterations,
        or error). Keeps thinking expanded and shows token usage.  A bare
        "." reply is silence — discard it instead of showing it.
        """
        if self._current_assistant is not None:
            if self._silent_dot:
                self._discard_current_assistant()
            else:
                self._current_assistant.finalize(intermediate=False)

    def on_trim(self, count: int) -> None:
        """Show the runtime ``[TrimContext: N]`` note on the turn's last
        assistant message.

        Called by the loop after a trim (which happens after this turn was
        saved).  The marker is appended to the last assistant message of the
        turn — the same message whose text carries the LLM-side marker.
        """
        if self._turn_assistants:
            self._turn_assistants[-1].set_trim_marker(count)

    def _discard_current_assistant(self) -> None:
        """Hide a silent (".") assistant message — it never appears in chat."""
        msg = self._current_assistant
        self._current_assistant = None
        self._silent_dot = False
        if msg is not None:
            if msg in self._turn_assistants:
                self._turn_assistants.remove(msg)
            try:
                msg.display = False
            except Exception:
                pass

    async def on_stream_retry(self) -> None:
        """Discard partial streamed text/thinking so a retried LLM request
        starts visually clean (no duplicated output in the chat view)."""
        if self._current_assistant is not None:
            self._current_assistant.reset_stream()

    async def on_max_iterations(self, iterations: int) -> None:
        """Surface the iteration limit being hit — the loop stops silently
        otherwise (cancelled turn, no final text)."""
        self._chat_view.add_system_message(
            f"✗ Agent exceeded maximum of {iterations} iterations",
            color="#f85149",
        )

    async def on_memory_save_warning(self, message: str) -> None:
        """Surface a diary-save warning — same style as the max-iterations
        notice: a red ✗ system line, since the save may not have landed."""
        self._chat_view.add_system_message(
            f"✗ {message}",
            color="#f85149",
        )

    # ── AgentEventHandler implementation ─────────────────────────────

    async def on_thinking_chunk(self, chunk: str) -> None:
        """Stream a thinking/reasoning token to the active assistant widget."""
        self._ensure_assistant()
        if self._current_assistant:
            self._current_assistant.append_thinking(chunk)
            self._chat_view.scroll_end(animate=False)

    async def on_text_chunk(self, chunk: str) -> None:
        """Stream a text token to the active assistant widget.

        A lone "." is silence — never render it; the message is discarded
        at finalize (covers heartbeats, autonomous a2a notifications, and
        any other event where the model replies ".").
        """
        self._ensure_assistant()
        if self._current_assistant:
            if (
                not self._current_assistant._buffer.strip()
                and chunk.strip() == "."
            ):
                self._silent_dot = True
                return
            self._current_assistant.append_text(chunk)
            self._chat_view.scroll_end(animate=False)

    async def on_tool_call(
        self, tool_call: ToolCallInfo, iteration: int = 0, max_iterations: int = 30
    ) -> None:
        """Mount a tool call widget in the chat view.

        Harness tools (``_``-prefixed) are skipped — they are synthetic
        notifications injected by the system, not visible LLM actions.
        """
        if tool_call.name.startswith("_"):
            return
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

    async def on_tool_approval(self, tool_call: ToolCallInfo) -> bool:
        """Ask user to approve a tool call before execution.

        Claude Code style: an inline row in the chat stream (no modal
        overlay).  The row takes focus and waits for Y / N / Esc; focus
        returns to the input bar once the user decides.  Returns True
        (approved) or False (denied).
        """
        from slife.ui.approval_prompt import ApprovalPrompt

        # If the model picker is open, it would hold focus — this prompt's
        # focus() would steal it and the picker's future would never resolve
        # (leaked task, Ctrl+S dead forever).  Dismiss the picker first.
        if getattr(self._app, "_model_picker_open", False):
            self._app._dismiss_model_picker()

        future: asyncio.Future[bool] = asyncio.Future()
        prompt = ApprovalPrompt(tool_call, future)
        self._chat_view.mount(prompt)
        self._chat_view.scroll_end(animate=False)
        prompt.focus()
        try:
            approved = await future
        except asyncio.CancelledError:
            # The turn was cancelled (Esc) while the prompt was open — the
            # loop's approval-wait cancels this coroutine.  Resolve the prompt
            # as denied so its future doesn't dangle, then re-raise so the
            # loop's cancel flow proceeds.
            prompt._decide(approved=False)
            raise
        self._refocus_input()
        return approved

    def _refocus_input(self) -> None:
        """Return focus to the user input bar after an inline decision."""
        try:
            self._app.query_one("#user-input").focus()
        except Exception:
            pass  # input missing (e.g. app tearing down) — nothing to restore

    async def on_tool_result(
        self, tool_call_id: str, result: str, is_error: bool
    ) -> None:
        """Update a tool call widget with its result."""
        widget = self._app._tool_widgets.get(tool_call_id)
        if widget:
            widget.set_complete(result, is_error)
            self._chat_view.scroll_end(animate=False)
        # Signal that the next thinking/text chunk starts a new iteration
        self._iteration_needs_new_message = True

    async def on_token_usage(self, usage: TokenUsage) -> None:
        """Update per-message token display and refresh status bar.

        *usage* is the turn's cumulative total — the dialog shows the
        growing total on each assistant message.  Session accumulation
        happens at turn end (see inbox.py) to avoid double-counting.
        """
        if self._current_assistant:
            self._current_assistant.set_token_usage(usage)
        self._app._update_status()
        self._chat_view.scroll_end(animate=False)

    async def on_image(self, source: str) -> None:
        """Render an image produced by the agent in the chat view."""
        import logging
        logger = logging.getLogger(__name__)
        logger.info("handler_on_image path=%s", source)
        self._chat_view.add_image_to_chat(source)
