"""Textual TUI application for Slife — Claude Code CLI style."""

import logging
import re
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Static

from slife.config import Config
from slife.a2a.card import format_presence_line
from slife.agent.service import AgentService
from slife.agent.plugins import PluginStartStatus
from slife.agent.loop import MaxIterationsExceeded
from slife.ui.chat import ChatView
from slife.ui.handler import TUIHandler
from slife.ui.image_utils import is_image_file
from slife.ui.restore import restore_session
from slife.ui.tool_display import ToolCallWidget

logger = logging.getLogger(__name__)


# ── Status bar ─────────────────────────────────────────────────────


class StatusBar(Static):
    """Thin status bar showing model, tokens, and key bindings.

    Claude Code style: minimal, dim, informative.
    """

    def update_info(
        self,
        model: str = "",
        context_tokens: int = 0,
        context_window: int = 0,
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

        if context_window > 0:
            pct = context_tokens / context_window * 100 if context_tokens else 0.0
            parts.append(
                f"[#6e7681]↑ {context_tokens:,} ({pct:.1f}%)[/#6e7681]"
            )
        elif context_tokens > 0:
            parts.append(f"[#6e7681]↑ {context_tokens:,} tokens[/#6e7681]")

        parts.append(
            "[#484f58]│ Ctrl+C quit  Esc cancel  Ctrl+L focus  Home/End scroll[/#484f58]"
        )

        self.update("  ".join(parts))


# ── History-aware Input ────────────────────────────────────────────


class HistoryInput(Input):
    """Single-line input with up/down history navigation (like readline).

    Stores submitted entries in a bounded list.  Up arrow cycles to older
    entries; down arrow returns to newer entries and eventually restores
    the in-progress draft.
    """

    _MAX_HISTORY: int = 256

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._input_history: list[str] = []
        self._history_idx: int = -1    # -1 = not navigating; 0+ = offset from newest
        self._saved_draft: str = ""    # what was typed before pressing ↑

    def add_history(self, text: str) -> None:
        """Record a submitted entry.  Consecutive duplicates are deduplicated."""
        text = text.strip()
        if not text:
            return
        if self._input_history and self._input_history[-1] == text:
            return
        self._input_history.append(text)
        if len(self._input_history) > self._MAX_HISTORY:
            self._input_history = self._input_history[-self._MAX_HISTORY:]
        self._history_idx = -1

    async def _on_key(self, event: events.Key) -> None:
        """Intercept up/down for history navigation; delegate rest to Input."""
        if event.key == "up":
            if not self._input_history:
                event.stop()
                return
            if self._history_idx < len(self._input_history) - 1:
                if self._history_idx == -1:
                    self._saved_draft = self.value
                self._history_idx += 1
                self.value = self._input_history[-(self._history_idx + 1)]
                self.cursor_position = len(self.value)
            event.stop()
            return

        if event.key == "down":
            if self._history_idx > 0:
                self._history_idx -= 1
                self.value = self._input_history[-(self._history_idx + 1)]
                self.cursor_position = len(self.value)
                event.stop()
                return
            if self._history_idx == 0:
                self._history_idx = -1
                self.value = self._saved_draft
                self.cursor_position = len(self.value)
                self._saved_draft = ""
                event.stop()
                return
            # Not navigating — let Input handle down normally

        await super()._on_key(event)


# ── Image attachment parsing ──────────────────────────────────────

# Matches @ followed by an image file path (quoted or unquoted).
# Supports: @path/img.png  @"path/with spaces/img.jpg"  @'path/img.gif'
_IMAGE_ATTACH_RE = re.compile(
    r"""@(?:"([^"]+)"|'([^']+)'|(\S+))""",
)


def _parse_images_from_input(raw: str) -> tuple[str, list[str]]:
    """Extract ``@path`` and ``@url`` image directives from user input.

    Supports:
      - ``@path/img.png``          → local file
      - ``@https://example.com/...`` → remote URL

    Returns ``(cleaned_text, [paths_or_urls])``.  Items that are neither
    a valid local image file nor an HTTPS URL are left in the text.
    """
    images: list[str] = []
    parts: list[str] = []
    last_end = 0

    for match in _IMAGE_ATTACH_RE.finditer(raw):
        parts.append(raw[last_end:match.start()])
        value = match.group(1) or match.group(2) or match.group(3)

        if value.startswith(("http://", "https://")):
            images.append(value)
        else:
            p = Path(value)
            if p.exists() and p.is_file() and is_image_file(value):
                images.append(str(p.resolve()))
            else:
                # Not a valid image — leave the @directive as-is
                parts.append(raw[match.start():match.end()])
        last_end = match.end()

    parts.append(raw[last_end:])
    cleaned = "".join(parts).strip()
    return cleaned, images


class SlifeApp(App):
    """Main Textual application for Slife — an AI agent in the terminal.

    Claude Code CLI style: minimal chrome, dark theme, clean message display.
    Owns the UI; delegates agent orchestration to AgentService.
    """

    CSS_PATH = "slife.tcss"

    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        # NOTE: NOT priority — Textual's priority pass checks the App before
        # the focused widget (reversed binding chain), so a priority escape
        # here would steal Esc from the ApprovalPrompt's priority deny binding
        # and the loop would cancel instead of denying, leaving the prompt
        # unresolved (C7).  A non-priority escape still cancels when no
        # approval prompt is focused.
        Binding("escape", "cancel", "Cancel agent loop"),
        Binding("ctrl+l", "focus_input", "Focus Input"),
        Binding("home", "scroll_home", "Scroll to top", priority=True),
        Binding("end", "scroll_end", "Scroll to bottom", priority=True),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.service = AgentService(config)

        # Resolve assistant name prefix once (set on first user message)
        a2a = config.a2a_config
        agent_name = a2a.agent_name if a2a else ""
        self._agent_id: str = config.agent_id
        self._assistant_prefix: str = (
            f"{agent_name}> " if agent_name else f"{self._agent_id}> "
        )

        # TUI state for tracking active widgets during streaming
        self._tool_widgets: dict[str, ToolCallWidget] = {}

        # Recovery state
        self._recovery_info: dict | None = None  # interrupted diary for recovery

    def compose(self) -> ComposeResult:
        """Minimal layout: chat fills screen, input + status docked at bottom."""
        yield ChatView(id="chat-view")
        yield HistoryInput(
            placeholder="Message Slife…",
            id="user-input",
        )
        yield StatusBar(id="status-bar")

    async def on_mount(self) -> None:
        """Initialize status bar and start all plugins via auto-discovery.

        Plugins are discovered by scanning ``slife.plugins.*`` — the
        same mechanism as native tools.  Built-in plugins (memdb, mcp,
        wechat) get their post-connect hooks; third-party plugins are
        started with the generic :meth:`AgentService.start_plugin_server`.
        """
        from slife.plugins import discover_plugins

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

        plugins = discover_plugins()

        # ── Step 1: Restore session from SQLite (pure read, no services needed) ─
        # get_recent_turns reads the DB directly via aiosqlite —
        # completely independent of the memory plugin process.
        try:
            turns = await self.service.get_recent_turns()
            if turns:
                self._recovery_info = {"turns": turns}
                await self._restore_session()
        except Exception as e:
            logger.debug("session_restore_skip err=%s", e)

        # ── Step 2: Start all plugins in parallel (auto-discovered) ──────
        for name, module in plugins:
            self.run_worker(
                self._start_plugin_safe(
                    name, self.service.start_plugin_server(name, module),
                ),
                exclusive=False, group="plugin-startup",
            )

        # ── Step 3: A2A + Subagent + callbacks (unchanged) ────────────
        self.service.on_a2a_activity(self._on_a2a_activity)
        self.service.inbox._conversations.set_default_handler_factory(
            lambda: TUIHandler(self, assistant_prefix=self._assistant_prefix)
        )

        # A2A now starts as a plugin via the discovery loop above
        # (start_plugin_server("a2a") → start_a2a, idempotent).
        self.run_worker(
            self.service.start_subagent(),
            exclusive=False, group="subagent-startup",
        )


    # ── Actions ──────────────────────────────────────────────────

    async def action_quit(self) -> None:
        """Quit the app — cancel the agent loop immediately, then
        clean up child processes.  Order matters: inbox/loop must
        stop first so MCP wrapper isn't mid-request during shutdown."""
        import asyncio

        async def _stop_one(name: str, coro) -> None:
            try:
                await asyncio.wait_for(coro, timeout=3.0)
            except asyncio.TimeoutError:
                logger.debug("shutdown_timeout service=%s", name)
            except Exception:
                pass

        # Cancel the agent loop RIGHT NOW — don't let it keep firing
        # tool calls into the MCP wrapper while we're trying to stop.
        self.service.inbox.cancel()

        for worker in list(self.workers):
            try:
                worker.cancel()
            except Exception:
                pass

        # Stop inbox first — completes any in-flight message.
        await _stop_one("inbox", self.service.stop_inbox())
        # Then kill remaining services in parallel.
        await asyncio.gather(
            _stop_one("subagent", self.service.stop_subagent()),
            _stop_one("a2a", self.service.stop_a2a()),
            _stop_one("mcp", self.service.stop_mcp()),
            _stop_one("memdb", self.service.stop_memdb()),
            _stop_one("wechat", self.service.stop_wechat()),
            _stop_one("memfiles", self.service.stop_memfiles()),
            return_exceptions=True,
        )

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
            context_tokens=self.service.current_context_tokens,
            context_window=self.service.context_window,
            thinking=self.service.thinking_enabled,
            inbox_busy=inbox.busy if inbox else False,
            inbox_pending=inbox.pending if inbox else 0,
        )

    # ── Plugin startup helpers ────────────────────────────────────

    async def _start_plugin_safe(self, name: str, coro) -> None:
        """Start a plugin and show success / skip / failure in chat.

        ``SKIPPED`` is an expected no-op (e.g. mqtt without a running MQTT
        broker, or a plugin disabled in config) — shown as neutral info,
        never as an error warning.
        """
        try:
            status = await coro
            if status is PluginStartStatus.STARTED:
                self._show_system_message(
                    f"🔌 插件已加载: {name}", color="#3fb950",
                )
            elif status is PluginStartStatus.SKIPPED:
                logger.debug("plugin_skipped name=%s", name)
                self._show_system_message(
                    f"ℹ️ 插件未加载: {name}", color="#8b949e",
                )
            else:
                self._show_system_message(
                    f"⚠ 插件启动失败: {name}", color="#d29922",
                )
        except Exception as e:
            self._show_system_message(
                f"⚠ 插件启动失败 ({name}): {e}", color="#d29922",
            )


    # ── Input handling ────────────────────────────────────────────

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle user pressing Enter in the input field.

        Posts the message to the unified inbox queue — never cancels
        a running agent loop.  If the queue is empty and no loop is
        running, processing starts immediately.

        Image attachments via ``@path/to/img.jpg`` syntax are
        extracted, validated, and passed to the agent pipeline.
        """
        raw = event.value.strip()
        if not raw:
            return

        if isinstance(event.input, HistoryInput):
            event.input.add_history(raw)
        event.input.clear()

        # Extract @path image directives
        cleaned_text, image_paths = _parse_images_from_input(raw)

        chat_view = self.query_one("#chat-view", ChatView)
        # Display the original raw text (with @path markers visible)
        # … but pass cleaned text + image paths to the agent.
        chat_view.add_user_message(raw, images=image_paths or None, prefix="You> ")

        # _process_message just enqueues and returns immediately
        # (handler is attached to the message, inbox streams later).
        self.run_worker(
            self._process_message(cleaned_text, image_paths or None, chat_view),
            exclusive=False,
        )

    # ── A2A activity (chat notifications) ───────────────────────────

    async def _on_a2a_activity(self, kind: str, **kwargs) -> None:
        """Handle A2A events by updating the chat view."""
        chat_view = self.query_one("#chat-view", ChatView)

        if kind == "agent_change":
            card = kwargs.get("card")
            if card is None:
                return
            event = kwargs.get("event", "")
            line = format_presence_line(card, event)
            if line is None:
                return  # status_change (heartbeat) etc. — not user-visible
            color = {"online": "#7c3aed", "offline": "#6e7681", "timeout": "#d29922"}.get(
                event, "#6e7681"
            )
            chat_view.add_system_message(line, color=color)

        elif kind == "task_received":
            source = kwargs.get("source", "unknown")
            content = kwargs.get("content", "").strip()
            # Show source agent ID as prefix so user knows who sent the task
            chat_view.add_user_message(content, prefix=f"{source}(a2a)")

        elif kind == "peer_message":
            # Peer terminal (WeChat etc.) — show with channel prefix
            source = kwargs.get("source", "wechat")
            content = kwargs.get("content", "").strip()
            chat_view.add_user_message(content, prefix="You(Wechat)> ")

        elif kind == "loop_error":
            error = kwargs.get("error", "")
            chat_view.add_system_message(f"✗ {error}", color="#f85149")

        elif kind == "task_completed":
            source = kwargs.get("source", "unknown")
            chat_view.add_system_message(
                f"✓ task from {source} completed", color="#3fb950",
            )

        elif kind == "idle":
            # Agent loop finished — refresh status bar to clear
            # the "⏳ processing" indicator.
            self._update_status()

    # ── Recovery UI ───────────────────────────────────────────────

    def _show_system_message(self, text: str, color: str | None = None) -> None:
        """Show a system message in the chat view."""
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_system_message(text, color=color)

    # ── Restore helpers ──────────────────────────────────────────────

    async def _restore_session(self) -> None:
        """Restore a previous session from turn-based memory.

        Delegates to :func:`slife.ui.restore.restore_session`.
        """
        if not self._recovery_info:
            return

        await restore_session(
            app=self,
            recovery_info=self._recovery_info,
            conversation=self.service.conversation,
            config=self.service.config,
            agent_id=self._agent_id,
            assistant_prefix=self._assistant_prefix,
        )
        self._recovery_info = None

    # ── Agent interaction ─────────────────────────────────────────

    async def _process_message(
        self,
        text: str,
        images: list[str] | None,
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
