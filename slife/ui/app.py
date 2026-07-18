"""Textual TUI application for Slife — Claude Code CLI style."""

import json
import logging

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Static

from slife.config import Config
from slife.agent.service import AgentService
from slife.agent.loop import MaxIterationsExceeded
from slife.ui.chat import ChatView
from slife.ui.handler import TUIHandler
from slife.ui.tool_display import ToolCallWidget

logger = logging.getLogger(__name__)


def _safe_parse_args(raw: str) -> dict:
    """Parse a tool-call arguments JSON string, falling back gracefully."""
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"_raw": raw}


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
        inbox_busy: bool = False,
        inbox_pending: int = 0,
    ) -> None:
        """Update the status bar display."""
        parts = []

        if model:
            parts.append(f"[#8b949e]{model}[/#8b949e]")

        if thinking:
            parts.append("[#d29922]⚡ thinking[/#d29922]")

        if inbox_busy:
            parts.append("[#d29922]⏳ processing[/#d29922]")
        elif inbox_pending > 0:
            parts.append(f"[#6e7681]⏳ {inbox_pending} queued[/#6e7681]")

        if tokens > 0:
            parts.append(f"[#6e7681]↑ {tokens:,} tokens[/#6e7681]")

        parts.append(
            "[#484f58]│ Ctrl+C quit  Esc cancel  Ctrl+L focus  Home/End scroll[/#484f58]"
        )

        self.update("  ".join(parts))


# ── Main TUI app ───────────────────────────────────────────────────


def _restore_prefix(channel: str, assistant_prefix: str) -> str:
    """Consistent prefix mapping for restored turns.

    Matches the real-time display prefixes used during live operation:
      - human  → "You> " / "> "
      - wechat → "Wechat> "
      - other   → "<channel>> " (external agent id, A2A peer, etc.)
    """
    if channel == "human":
        return "You> " if assistant_prefix else "> "
    if channel == "wechat":
        return "Wechat> "
    if channel:
        return f"{channel}> "
    # Backward compat: old turns saved before channel was introduced
    return "You> " if assistant_prefix else "> "


class SlifeApp(App):
    """Main Textual application for Slife — an AI agent in the terminal.

    Claude Code CLI style: minimal chrome, dark theme, clean message display.
    Owns the UI; delegates agent orchestration to AgentService.
    """

    CSS_PATH = "slife.tcss"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel agent loop", priority=True),
        Binding("ctrl+l", "focus_input", "Focus Input"),
        Binding("home", "scroll_home", "Scroll to top", priority=True),
        Binding("end", "scroll_end", "Scroll to bottom", priority=True),
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

        # Recovery state
        self._recovery_info: dict | None = None  # interrupted diary for recovery

    def compose(self) -> ComposeResult:
        """Minimal layout: chat fills screen, input + status docked at bottom."""
        yield ChatView(id="chat-view")
        yield Input(
            placeholder="Message Slife…",
            id="user-input",
        )
        yield StatusBar(id="status-bar")

    async def on_mount(self) -> None:
        """Initialize status bar and start Memory + MCP + A2A + Subagent."""
        status = self.query_one("#status-bar", StatusBar)
        status.update_info(
            model=self.service.model_display_name,
            thinking=self.service.thinking_enabled,
        )

        # Focus input on startup
        self.query_one("#user-input").focus()

        # ★ Step 0: Start the unified message queue first.
        # All input (human, A2A, WeChat) flows through this inbox.
        await self.service.start_inbox()

        # ★ Step 1: Start memory service first (synchronous — fast local startup)
        if self.service.config.memory_config:
            try:
                await self.service.start_memory()

                # Check for recent turns to restore
                turns = await self.service.get_recent_turns()
                if turns:
                    self._recovery_info = {"turns": turns}
                    self.run_worker(
                        self._restore_session(),
                        exclusive=True,
                        group="restore-session",
                    )
            except Exception as e:
                self._show_system_message(
                    f"⚠ 记忆服务启动失败: {e}", color="#d29922",
                )

        # Step 2: Start MCP wrapper in the background
        if self.service.config.mcp_config:
            self.run_worker(
                self.service.start_mcp(),
                exclusive=False,
                group="mcp-startup",
            )

        # Step 3: Register unified activity callbacks + handler factory.
        # These serve ALL input channels — A2A, WeChat, etc. —
        # not just A2A.  Must run BEFORE any channel starts polling
        # so messages are never dropped before the UI is listening.
        self.service.on_a2a_activity(self._on_a2a_activity)
        self.service.inbox._conversations.set_default_handler_factory(
            lambda: TUIHandler(self, assistant_prefix=self._assistant_prefix)
        )

        # Step 4: Start A2A P2P mesh in the background
        if self.service.config.a2a_config and self.service.config.a2a_config.enabled:
            self.run_worker(
                self.service.start_a2a(),
                exclusive=False,
                group="a2a-startup",
            )

        # Step 5: Start subagent manager
        if self.service.config.subagent_config:
            self.run_worker(
                self.service.start_subagent(),
                exclusive=False,
                group="subagent-startup",
            )

        # Step 6: Start WeChat plugin (if enabled in config)
        if self.service.config.wechat_config and self.service.config.wechat_config.enabled:
            self.run_worker(
                self.service.start_wechat(),
                exclusive=False,
                group="wechat-startup",
            )

    # ── Actions ──────────────────────────────────────────────────

    def action_quit(self) -> None:
        """Quit the app — cancel workers, then fire-and-forget cleanup."""
        import asyncio

        for worker in list(self.workers):
            try:
                worker.cancel()
            except Exception:
                pass

        async def _cleanup():
            stop_coros = {
                "subagent": self.service.stop_subagent,
                "a2a": self.service.stop_a2a,
                "mcp": self.service.stop_mcp,
                "memory": self.service.stop_memory,
                "wechat": self.service.stop_wechat,
                "inbox": self.service.stop_inbox,
            }
            tasks = [_stop_one(n, f()) for n, f in stop_coros.items()]
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        async def _stop_one(name: str, coro) -> None:
            try:
                await asyncio.wait_for(coro, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("shutdown_timeout service=%s", name)
            except Exception:
                pass

        asyncio.get_running_loop().create_task(_cleanup())
        self.exit()

    def action_cancel(self) -> None:
        """Cancel the currently running agent loop.  No-op if idle."""
        if not self.service.inbox.busy:
            return
        self.service.inbox.cancel()
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_system_message("⏹ 已中断", color="#d29922")

    def action_focus_input(self) -> None:
        """Focus the input field."""
        self.query_one("#user-input").focus()

    def action_scroll_home(self) -> None:
        """Scroll chat view to the top."""
        self.query_one("#chat-view", ChatView).scroll_home(animate=False)

    def action_scroll_end(self) -> None:
        """Scroll chat view to the bottom."""
        self.query_one("#chat-view", ChatView).scroll_end(animate=False)

    # ── Status bar ───────────────────────────────────────────────

    def _update_status(self) -> None:
        """Refresh the status bar with current session info."""
        status = self.query_one("#status-bar", StatusBar)
        inbox = self.service.inbox
        status.update_info(
            model=self.service.model_display_name,
            tokens=self.service.session_usage.total_tokens,
            thinking=self.service.thinking_enabled,
            inbox_busy=inbox.busy if inbox else False,
            inbox_pending=inbox.pending if inbox else 0,
        )

    # ── Input handling ────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user pressing Enter in the input field.

        Posts the message to the unified inbox queue — never cancels
        a running agent loop.  If the queue is empty and no loop is
        running, processing starts immediately.
        """
        raw = event.value.strip()
        if not raw:
            return

        event.input.clear()

        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_user_message(raw)

        # _process_message just enqueues and returns immediately
        # (handler is attached to the message, inbox streams later).
        self.run_worker(
            self._process_message(raw, None, chat_view),
            exclusive=False,
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
                extra = f" ({card.agent_id})" if card.display_name and card.display_name != card.agent_id else ""
                chat_view.add_system_message(
                    f"⚡ {name}{extra} online [{card.status}]",
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

        elif kind == "peer_message":
            # Peer terminal (WeChat etc.) — show with channel prefix
            source = kwargs.get("source", "wechat")
            content = kwargs.get("content", "").strip()
            chat_view.add_user_message(content, prefix="Wechat> ")

        elif kind == "task_completed":
            source = kwargs.get("source", "unknown")
            result = kwargs.get("result", "")
            chat_view.add_system_message(
                f"✓ task from {source} completed", color="#3fb950",
            )

    # ── Recovery UI ───────────────────────────────────────────────

    def _show_system_message(self, text: str, color: str | None = None) -> None:
        """Show a system message in the chat view."""
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_system_message(text, color=color)

    # ── Restore helpers ──────────────────────────────────────────────

    async def _restore_session(self) -> None:
        """Restore a previous session from turn-based memory.

        Loads all turns for the last session_id, concatenates their
        messages, and rebuilds the UI.  Each turn is saved individually
        so there's no trim_count — we load all turns and reconstruct
        the full conversation from scratch.
        """
        if not self._recovery_info:
            return

        info = self._recovery_info
        turns: list[dict] = info.get("turns", [])

        if not turns:
            self._recovery_info = None
            return

        # ── Phase 1: Reconstruct full message list from turns ──────
        try:
            # Get system prompt from current conversation
            sys_msg = self.service.conversation.messages[0] if self.service.conversation.messages else None

            all_messages: list[dict] = []
            if sys_msg and sys_msg.get("role") == "system":
                all_messages.append(dict(sys_msg))

            for turn in turns:
                user_msg_text = turn.get("user_message", "")
                turn_messages_json = turn.get("messages", "[]")
                turn_msgs: list[dict] = (
                    json.loads(turn_messages_json)
                    if isinstance(turn_messages_json, str) else turn_messages_json
                )

                all_messages.append({
                    "role": "user",
                    "content": user_msg_text,
                })
                all_messages.extend(turn_msgs)

            # Build tool-result lookup
            tool_results: dict[str, str] = {}
            tool_errors: dict[str, bool] = {}
            for msg in all_messages:
                if msg.get("role") == "tool":
                    tcid = msg.get("tool_call_id", "")
                    if tcid:
                        tool_results[tcid] = msg.get("content", "") or ""
                        tool_errors[tcid] = msg.get("is_error", False)

            # Build UI ops
            from slife.ui.chat import UserMessage, AssistantMessage
            ui_ops: list[dict] = []

            assistant_indices = [
                i for i, m in enumerate(all_messages)
                if m.get("role") == "assistant"
            ]
            last_assistant_idx = assistant_indices[-1] if assistant_indices else -1

            # Build a channel→prefix lookup so every user message gets the
            # correct prefix per turn (human → "You> ", wechat → "Wechat> ",
            # remote agent → "<agent_id>> ").
            _channel_by_row: dict[int, str] = {}
            for i, turn in enumerate(turns):
                ch = turn.get("channel", "")
                # Count user messages up to this turn (each turn adds
                # exactly one user message after the system prompt).
                _channel_by_row[i] = ch

            turn_idx = -1
            for idx, msg in enumerate(all_messages):
                role = msg.get("role", "")
                if role == "system":
                    continue

                elif role == "user":
                    turn_idx += 1
                    ch = _channel_by_row.get(turn_idx, "")
                    prefix = _restore_prefix(ch, self._assistant_prefix)
                    ui_ops.append({
                        "type": "user",
                        "content": msg.get("content", "") or "",
                        "images": msg.get("images"),
                        "prefix": prefix,
                    })

                elif role == "assistant":
                    is_final = (idx == last_assistant_idx)
                    thinking = msg.get("thinking") or ""
                    content = msg.get("content") or ""
                    tcs = msg.get("tool_calls") or []

                    ui_ops.append({
                        "type": "assistant",
                        "thinking": thinking,
                        "content": content,
                        "tool_calls": [
                            {
                                "id": tc.get("id", ""),
                                "name": tc.get("function", {}).get("name", "?"),
                                "arguments": _safe_parse_args(
                                    tc.get("function", {}).get("arguments", "{}")
                                ),
                            }
                            for tc in tcs
                        ],
                        "is_final": is_final,
                        "name_prefix": self._assistant_prefix,
                    })

                elif role == "tool":
                    pass

        except Exception as e:
            self._show_system_message(f"✗ 恢复失败: {e}", color="#f85149")
            self._recovery_info = None
            return

        # ── Phase 2: Switch state ──────────────────────────────────
        from slife.agent.conversation import Conversation
        self.service.conversation = Conversation()
        self.service.conversation.messages = all_messages

        # ── Phase 3: Rebuild UI ────────────────────────────────────
        chat_view = self.query_one("#chat-view", ChatView)
        from slife.ui.chat import UserMessage, AssistantMessage

        with self.batch_update():
            for op in ui_ops:
                if op["type"] == "user":
                    chat_view.add_user_message(
                        op["content"],
                        images=op.get("images"),
                        prefix=op["prefix"],
                    )

                elif op["type"] == "assistant":
                    am = chat_view.add_assistant_message(
                        name_prefix=op.get("name_prefix"),
                    )
                    thinking = op.get("thinking", "")
                    if thinking:
                        am.append_thinking(thinking)
                    text = op.get("content", "")
                    if text:
                        am.append_text(text)
                    am.finalize(intermediate=not op.get("is_final", False))

                    for tc in op.get("tool_calls", []):
                        tcid = tc["id"]
                        result = tool_results.get(tcid, "")
                        is_error = tool_errors.get(tcid, False)
                        widget = ToolCallWidget(
                            tool_name=tc["name"],
                            tool_args=tc["arguments"],
                            tool_call_id=tcid,
                        )
                        chat_view.mount(widget)
                        widget.set_complete(result, is_error)

            self._recovery_info = None
            self._show_system_message("✅ 已恢复对话，继续吧", color="#3fb950")

        # Update status bar with token estimate from turns
        total_tokens = sum(t.get("token_count", 0) for t in turns)
        if total_tokens > 0:
            self.service.session_usage.total_tokens = total_tokens
            self._update_status()

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
