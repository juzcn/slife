"""Textual TUI application for slife — Claude Code CLI style."""

import json

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

        # Recovery state
        self._recovery_info: dict | None = None  # interrupted diary for recovery

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

                # Check for interrupted session
                diary = await self.service.check_interrupted()
                if diary and diary.get("interrupted"):
                    self._show_recovery_prompt(diary)
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

        # Recovery commands
        if raw == "/restore" and self._recovery_info:
            event.input.clear()
            self.run_worker(self._restore_session(), exclusive=True)
            return

        if raw == "/discard" and self._recovery_info:
            event.input.clear()
            self.run_worker(self._discard_session(), exclusive=True)
            return

        if raw == "/preview" and self._recovery_info:
            event.input.clear()
            self.run_worker(self._preview_session(), exclusive=True)
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

    def _show_recovery_prompt(self, diary: dict) -> None:
        """Display the recovery prompt when an interrupted session is found."""
        self._recovery_info = diary

        title = diary.get("title") or "(未命名)"
        turns = diary.get("how_many_turns", 0)
        updated = diary.get("updated_at", "")[:16]
        tokens = diary.get("how_many_tokens", 0)
        status = diary.get("status", "意外中断")

        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_system_message(
            f"⚡ 发现中断的对话\n"
            f"\n"
            f"  「{title}」\n"
            f"  {turns} 轮对话 · {updated}\n"
            f"  状态：{status} · {tokens:,} tokens\n"
            f"\n"
            f"  /restore — 从中断处继续\n"
            f"  /discard — 丢弃，开始新对话\n"
            f"  /preview — 查看对话内容",
            color="#d29922",
        )

    async def _restore_session(self) -> None:
        """Restore the interrupted session."""
        if not self._recovery_info:
            return

        diary = self._recovery_info
        rowid = diary.get("rowid")
        self.service._diary_rowid = rowid

        # Load full diary entry
        try:
            result = await self.service._memory_client.call_tool(
                "memory_open",
                {"rowid": rowid, "author": self.service.config.user},
            )
            full = json.loads(result)
            messages = json.loads(full.get("messages", "[]"))

            # Rebuild Conversation
            from slife.agent.conversation import Conversation
            self.service.conversation = Conversation()
            self.service.conversation.messages = messages

            # Rebuild UI from messages
            chat_view = self.query_one("#chat-view", ChatView)
            from slife.ui.chat import UserMessage, AssistantMessage
            for msg in messages:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if role == "user":
                    chat_view.add_user_message(
                        content or "",
                        images=msg.get("images"),
                        prefix="You> " if self._assistant_prefix else "> ",
                    )
                elif role == "assistant":
                    tool_calls = msg.get("tool_calls")
                    if tool_calls:
                        # Tool call iteration — show header
                        tc_names = [
                            tc.get("function", {}).get("name", "?")
                            for tc in tool_calls
                        ]
                        chat_view.add_system_message(
                            f"🔧 工具调用: {', '.join(tc_names)}",
                            color="#d29922",
                        )
                    if content:
                        am = chat_view.add_assistant_message(
                            name_prefix=self._assistant_prefix,
                        )
                        am.append_text(content)
                        am.finalize()
                elif role == "tool":
                    # Tool result — show as system message
                    short = (content or "")[:100]
                    chat_view.add_system_message(
                        f"  ↳ {short}{'…' if len(content or '') > 100 else ''}",
                        color="#6e7681",
                    )

            self._recovery_info = None
            self._show_system_message("✅ 已恢复对话，继续吧", color="#3fb950")

            # Update status bar with restored token count
            if full.get("how_many_tokens"):
                self.service.session_usage.prompt_tokens = 0
                self.service.session_usage.completion_tokens = 0
                self.service.session_usage.total_tokens = full["how_many_tokens"]
                self._update_status()

        except Exception as e:
            self._show_system_message(f"✗ 恢复失败: {e}", color="#f85149")
            self._recovery_info = None

    async def _discard_session(self) -> None:
        """Discard the interrupted session and start fresh."""
        if not self._recovery_info:
            return

        try:
            rowid = self._recovery_info.get("rowid")
            await self.service._memory_client.call_tool(
                "memory_close_diary",
                {"rowid": rowid, "author": self.service.config.user},
            )
            self._show_system_message("🗑 已丢弃中断的对话", color="#6e7681")
        except Exception:
            pass

        # Start a fresh diary
        self._recovery_info = None
        rowid = await self.service.start_memory()
        self._show_system_message("📖 开始新对话", color="#3fb950")

    async def _preview_session(self) -> None:
        """Preview the interrupted session's messages."""
        if not self._recovery_info:
            return

        try:
            rowid = self._recovery_info.get("rowid")
            result = await self.service._memory_client.call_tool(
                "memory_open",
                {"rowid": rowid, "author": self.service.config.user},
            )
            full = json.loads(result)
            messages = json.loads(full.get("messages", "[]"))

            chat_view = self.query_one("#chat-view", ChatView)
            chat_view.add_system_message(
                f"📋 对话预览 ({len(messages)} 条消息):", color="#8b949e",
            )
            for msg in messages[-6:]:  # Last 6 messages
                role = msg.get("role", "")
                content = msg.get("content", "") or ""
                if role == "user":
                    chat_view.add_system_message(
                        f"  You> {content[:120]}{'…' if len(content) > 120 else ''}",
                        color="#c9d1d9",
                    )
                elif role == "assistant" and content:
                    chat_view.add_system_message(
                        f"  🤖 {content[:120]}{'…' if len(content) > 120 else ''}",
                        color="#8b949e",
                    )

            # Re-show the prompt after preview
            self._show_recovery_prompt(self._recovery_info)

        except Exception as e:
            self._show_system_message(f"✗ 预览失败: {e}", color="#f85149")

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
