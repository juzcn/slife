"""Textual TUI application for Slife — Claude Code CLI style."""

import json

from textual.app import App, ComposeResult
from textual.widgets import Input, Static

from slife.config import Config
from slife.agent.service import AgentService
from slife.agent.loop import MaxIterationsExceeded
from slife.ui.chat import ChatView
from slife.ui.handler import TUIHandler
from slife.ui.tool_display import ToolCallWidget


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
    """Main Textual application for Slife — an AI agent in the terminal.

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

        # ★ Step 1: Start memory service first (synchronous — fast local startup)
        if self.service.config.memory_config and self.service.config.memory_config.enabled:
            try:
                await self.service.start_memory()

                # Check for restorable session (interrupted or last completed)
                diary = await self.service.check_interrupted()
                if diary:
                    self._recovery_info = diary
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
        if self.service.config.mcp_config.enabled:
            self.run_worker(
                self.service.start_mcp(),
                exclusive=False,
                group="mcp-startup",
            )

        # Step 3: Start A2A P2P mesh in the background
        if self.service.config.a2a_config and self.service.config.a2a_config.enabled:
            self.service.on_a2a_activity(self._on_a2a_activity)
            self.run_worker(
                self.service.start_a2a(
                    handler_factory=lambda: TUIHandler(
                        self, assistant_prefix=self._assistant_prefix
                    ),
                ),
                exclusive=False,
                group="a2a-startup",
            )

        # Step 4: Start subagent manager
        if self.service.config.subagent_config:
            self.run_worker(
                self.service.start_subagent(),
                exclusive=False,
                group="subagent-startup",
            )

    # ── Actions ──────────────────────────────────────────────────

    async def action_quit(self) -> None:
        """Quit the app, shutting down memory, subagent, A2A, and MCP."""
        await self.service.stop_subagent()
        await self.service.stop_a2a()
        await self.service.stop_mcp()
        await self.service.stop_memory()
        await super().action_quit()

    def action_clear_chat(self) -> None:
        """Clear chat history and start a fresh diary entry."""
        # Close current diary and open a new one
        if self.service.memory_enabled and self.service._diary_rowid is not None:
            self.run_worker(
                self._restart_diary(),
                exclusive=True,
                group="memory-restart",
            )
        self.service.clear()
        chat_view = self.query_one("#chat-view", ChatView)
        for child in list(chat_view.children):
            child.remove()
        self._tool_widgets.clear()

    async def _restart_diary(self) -> None:
        """Close the current diary and open a new one."""
        try:
            rowid = await self.service.start_memory()
            if rowid:
                self._show_system_message("📖 新对话已开始", color="#3fb950")
        except Exception as e:
            self._show_system_message(
                f"⚠ 记忆服务异常: {e}", color="#d29922",
            )

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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user pressing Enter in the input field."""
        raw = event.value.strip()
        if not raw:
            return

        event.input.clear()

        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_user_message(raw)

        self.run_worker(
            self._process_message(raw, None, chat_view),
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

    async def _restore_session(self) -> None:
        """Restore a previous session to its exact working context.

        The diary stores the full conversation history (immutable) plus
        a *trim_count* — the cumulative number of messages trimmed from
        the front.  Skipping those messages recovers the exact working
        window the previous session had at its last save point.

        Rebuilds the UI with proper ToolCallWidget and AssistantMessage
        widgets (including thinking blocks) so the restored view matches
        the live conversation appearance.
        """
        if not self._recovery_info:
            return

        diary = self._recovery_info
        rowid = diary.get("rowid")

        # ── Phase 1: Load & pre-process all data ───────────────────
        try:
            result = await self.service._memory_client.call_tool(
                "memory_open",
                {"rowid": rowid, "author": self.service.config.user},
            )
            full = json.loads(result)
            all_messages: list[dict] = json.loads(full.get("messages", "[]"))
            trim_count: int = full.get("trim_count", 0)

            # Recover the exact working context:
            # system prompt (index 0 if present) + messages after skip
            sys_end = 1 if all_messages and all_messages[0].get("role") == "system" else 0
            working = all_messages[:sys_end] + all_messages[sys_end + trim_count:]

            # Build tool-result lookup for matching widgets to results
            tool_results: dict[str, str] = {}
            tool_errors: dict[str, bool] = {}
            for msg in all_messages:
                if msg.get("role") == "tool":
                    tcid = msg.get("tool_call_id", "")
                    if tcid:
                        tool_results[tcid] = msg.get("content", "") or ""
                        tool_errors[tcid] = msg.get("is_error", False)

            # Collect UI descriptors (built before state switch)
            from slife.ui.chat import UserMessage, AssistantMessage
            user_prefix = "You> " if self._assistant_prefix else "> "
            ui_ops: list[dict] = []

            # Track assistant messages for intermediate/final classification:
            # all but the LAST assistant message are intermediate (collapsed thinking)
            assistant_indices = [
                i for i, m in enumerate(all_messages)
                if m.get("role") == "assistant"
            ]
            last_assistant_idx = assistant_indices[-1] if assistant_indices else -1

            for idx, msg in enumerate(all_messages):
                role = msg.get("role", "")
                if role == "system":
                    continue  # not shown in UI

                elif role == "user":
                    ui_ops.append({
                        "type": "user",
                        "content": msg.get("content", "") or "",
                        "images": msg.get("images"),
                        "prefix": user_prefix,
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
                    # Tool results are rendered inside their ToolCallWidget —
                    # but only if the matching tool_call has already been
                    # emitted.  The sequential processing below handles
                    # this via the tool_results lookup built above.
                    pass

        except Exception as e:
            self._show_system_message(f"✗ 恢复失败: {e}", color="#f85149")
            self._recovery_info = None
            return

        # ── Phase 2: Switch state (only after data loaded OK) ──────
        orphan_rowid = self.service._diary_rowid
        if orphan_rowid and orphan_rowid != rowid and self.service._memory_client:
            try:
                await self.service._memory_client.call_tool(
                    "memory_close_diary",
                    {"rowid": orphan_rowid, "author": self.service.config.user},
                )
            except Exception:
                pass

        self.service._diary_rowid = rowid

        from slife.agent.conversation import Conversation
        self.service.conversation = Conversation()
        self.service.conversation.messages = working
        self.service._trim_count = trim_count

        # ── Phase 3: Rebuild UI with proper widgets ────────────────
        chat_view = self.query_one("#chat-view", ChatView)
        from slife.ui.chat import UserMessage, AssistantMessage

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

                # Restore thinking
                thinking = op.get("thinking", "")
                if thinking:
                    am.append_thinking(thinking)

                # Restore text
                text = op.get("content", "")
                if text:
                    am.append_text(text)

                # Finalize: intermediate → collapsed thinking; final → expanded
                am.finalize(intermediate=not op.get("is_final", False))

                # Recreate ToolCallWidget(s) with completed state
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

        # Update status bar with restored token count
        if full.get("how_many_tokens"):
            self.service.session_usage.prompt_tokens = 0
            self.service.session_usage.completion_tokens = 0
            self.service.session_usage.total_tokens = full["how_many_tokens"]
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
