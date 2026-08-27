"""Textual TUI application for Slife — Claude Code CLI style."""

import asyncio
import logging
import re
from datetime import datetime

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.message import Message
from textual.widgets import Static, TextArea

from slife.config import Config
from slife.a2a.card import format_presence_line
from slife.agent.service import AgentService, MemoryDatabaseError
from slife.agent.plugins import PluginStartStatus
from slife.ui.chat import ChatView
from slife.ui.handler import TUIHandler
from slife.ui.i18n import t
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
        heartbeat: str = "",
        heartbeat_color: str = "",
        starting: bool = False,
        inbox_busy: bool = False,
        inbox_pending: int = 0,
    ) -> None:
        """Update the status bar display."""
        parts = []

        if model:
            # Escape `[` so a config-derived model name can't be parsed as
            # markup (crash / styling injection in the status bar).
            parts.append(f"[#8b949e]{model.replace('[', '[[')}[/#8b949e]")

        if thinking:
            parts.append("[#d29922]thinking[/#d29922]")

        if heartbeat:
            color = heartbeat_color or "#d29922"
            parts.append(f"[{color}]{heartbeat}[/{color}]")

        if starting:
            # Plugin startup in progress — the service is not open for
            # input yet (input is disabled until startup converges).
            parts.append(f"[#d29922]{t('status_starting')}[/#d29922]")
        elif inbox_busy:
            parts.append(f"[#d29922]{t('status_processing')}[/#d29922]")
        elif inbox_pending > 0:
            parts.append(f"[#6e7681]{t('status_queued', n=inbox_pending)}[/#6e7681]")

        if context_window > 0:
            pct = context_tokens / context_window * 100 if context_tokens else 0.0
            parts.append(
                f"[#6e7681]↑ {context_tokens:,} ({pct:.1f}%)[/#6e7681]"
            )
        elif context_tokens > 0:
            parts.append(f"[#6e7681]↑ {context_tokens:,} tokens[/#6e7681]")

        parts.append(
            f"[#484f58]{t('status_keybinds')}[/#484f58]"
        )

        self.update("  ".join(parts))


# ── History-aware input (multi-line TextArea) ─────────────────────


class HistoryInput(TextArea):
    """Multi-line prompt with up/down history navigation (like readline).

    Built on TextArea instead of the single-line Input because Input swallows
    everything after the first newline on paste — long multi-line text was
    silently truncated.  Enter submits; Shift+Enter inserts a literal newline
    so pasted / composed text can span lines.
    """

    BINDINGS = [
        Binding("shift+enter", "insert_newline", "Insert newline", show=False),
        Binding("up", "up_or_history", show=False),
        Binding("down", "down_or_history", show=False),
    ]

    _MAX_HISTORY: int = 256

    class Submitted(Message):
        """Carries the submitted message text (mirrors ``Input.Submitted``)."""

        def __init__(self, input: "HistoryInput", value: str) -> None:
            super().__init__()
            self.input = input
            self.value = value

    def __init__(self, placeholder: str = "", **kwargs):
        super().__init__(
            placeholder=placeholder,
            # Keep Tab as focus movement and leave Escape un-consumed so the
            # app-level Esc→cancel binding still fires while typing.
            tab_behavior="focus",
            **kwargs,
        )
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

    def action_insert_newline(self) -> bool:
        """Shift+Enter inserts a literal newline (Enter submits instead)."""
        start, end = self.selection
        self._replace_via_keyboard("\n", start, end)
        return True

    async def _on_key(self, event: events.Key) -> None:
        """Enter submits the message; everything else goes to TextArea.

        TextArea consumes Enter in its own ``_on_key`` (inserts a newline)
        before any binding could fire, so submission must be intercepted here.
        """
        if event.key == "enter":
            event.stop()
            value = self.text
            if value.strip():
                self.post_message(self.Submitted(self, value))
            return
        await super()._on_key(event)

    def action_up_or_history(self) -> bool:
        """Up on the first line walks history; otherwise moves the cursor."""
        if self.cursor_location[0] == 0:
            self._history_previous()
        else:
            self.action_cursor_up()
        self.focus()
        return True

    def action_down_or_history(self) -> bool:
        """Down on the last line walks history; otherwise moves the cursor."""
        if self.cursor_location[0] == self.document.line_count - 1:
            self._history_next()
        else:
            self.action_cursor_down()
        self.focus()
        return True

    def _history_previous(self) -> None:
        if not self._input_history:
            return
        if self._history_idx < len(self._input_history) - 1:
            if self._history_idx == -1:
                self._saved_draft = self.text
            self._history_idx += 1
            self.text = self._input_history[-(self._history_idx + 1)]
            self.move_cursor(self.document.end)

    def _history_next(self) -> None:
        if self._history_idx > 0:
            self._history_idx -= 1
            self.text = self._input_history[-(self._history_idx + 1)]
            self.move_cursor(self.document.end)
        elif self._history_idx == 0:
            self._history_idx = -1
            self.text = self._saved_draft
            self.move_cursor(self.document.end)
            self._saved_draft = ""


# ── Image attachment parsing ──────────────────────────────────────
#
# Two-phase regex parsing:
#   1. LOCATE  — _AT_RE finds every '@' in the input.
#   2. EXTRACT — on the slice after each '@', _SOURCE_RE matches the
#      image source by pattern (data URI / URL / quoted / bracketed /
#      bare path), reading up to the next whitespace, quote, or '@'.
#
# Pure pattern extraction — NO filesystem checks here.  A matched source
# is returned as-is; whether it actually exists is validated later by
# attach_image (is_image_source / is_file).  This keeps the parser a
# simple grammar and the existence check in one place downstream.

_AT_RE = re.compile("@")

# Characters that END a token in the parser.  Whitespace, quotes, and '@'
# (the next directive) are always boundaries.  CJK characters are too —
# a natural word boundary when typing "@url和@url" or "@a.png和@b.png" —
# so an adjacent directive isn't swallowed.  URLs additionally stop at a
# comma (the "@url,@url" separator); data URIs keep commas (base64) and
# bare paths keep their image-extension gate.
_TOKEN_END = r'\s"\'@一-鿿'
_URL_END = _TOKEN_END + ","

# Image extensions gate the bare-path shape: a bare @token is only a
# source when it "looks like" an image (ends in a known extension), so
# @someone is skipped.  URLs / data URIs are self-identifying via their
# scheme and need no extension gate.
_IMAGE_EXTS = (
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp",
    ".svg", ".ico", ".avif", ".tiff", ".heic",
)
_EXT_ALT = "|".join(re.escape(e.lstrip(".")) for e in _IMAGE_EXTS)

# One source per @, matched INSIDE the slice after the '@'.  Delimiters:
#   data:…             → to next token-end (commas are base64 — kept)
#   http(s)://…        → to next token-end OR comma (self-identifying,
#                        may have query strings / fragments / no ext)
#   "..."/'...'        → quoted, may contain spaces
#   [...]/ {...} /(…)  → bracketed, may contain spaces
#   bare               → token-end run ending in an image extension —
#                        "@a.png@b.png" cuts cleanly at each '@'.
_SOURCE_RE = re.compile(r"""
    (?: data: [^%s]+ | https?:// [^%s]+ )          # data URI / URL
  | "(?P<dq>[^"]+)"                                # @"path with spaces"
  | '(?P<sq>[^']+)'                                # @'path'
  | \[(?P<br>[^\]\s]+)\]                           # @[path]
  | \{(?P<bc>[^}\s]+)\}                            # @{path}
  | \((?P<bp>[^)\s]+)\)                            # @(path)
  | (?P<bare>[^%s]*\.(?:%s))                       # bare path w/ img ext
""" % (_TOKEN_END, _URL_END, _TOKEN_END, _EXT_ALT), re.VERBOSE)


def _parse_images_from_input(raw: str) -> list[str]:
    """Extract ``@path`` / ``@url`` / ``@data:...`` directive sources from
    user input — one per ``@`` directive, in any mix of shapes.

    Supports, in any combination on one input::

        @path/img.png
        @"path/with spaces/img.jpg"
        @'path/img.gif'
        @[path/a.png] / @{path/a.png} / @(path/a.png)
        @path/a.png@path/b.png      (adjacent, no spaces)
        @https://example.com/x.png
        @data:image/png;base64,AAAA

    Two-phase regex: locate every ``@``, then match a source pattern on
    the slice after it (no filesystem checks — existence is validated
    downstream by ``attach_image``).  The user message itself is passed
    through verbatim (``@`` markers stay visible like any text);
    non-image ``@tokens`` (e.g. ``@someone``) are not attachments and
    are simply not attached (the ``@`` is skipped, the text stays).
    """
    sources: list[str] = []
    i = 0
    while True:
        at = _AT_RE.search(raw, i)
        if at is None:
            break
        m = _SOURCE_RE.match(raw, at.end())
        if m:
            value = m.group("dq") or m.group("sq") or \
                m.group("br") or m.group("bc") or m.group("bp") \
                or m.group(0)
            if value:
                sources.append(value)
            i = m.end()
        else:
            i = at.start() + 1  # skip just the '@', keep scanning
    return sources


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
        # Model switching key — ctrl+s ("s" = switch).  Not ctrl+m (Textual
        # aliases it to enter), not ctrl+g (VSCode's goto-line steals it).
        # Earlier "keys become Home" reports were the picker-mount scroll bug
        # (now fixed with a deferred scroll), not the key itself.
        Binding("ctrl+s", "switch_model", "Switch model"),
        Binding("home", "scroll_home", "Scroll to top", priority=True),
        Binding("end", "scroll_end", "Scroll to bottom", priority=True),
    ]

    def __init__(self, config: Config):
        super().__init__()
        self.service = AgentService(config)

        # Resolve assistant name prefix once (set on first user message)
        self._agent_name: str = config.agent_name
        self._assistant_prefix: str = f"{self._agent_name}> "

        # TUI state for tracking active widgets during streaming
        self._tool_widgets: dict[str, ToolCallWidget] = {}

        # Autonomous heartbeat status-bar indicator — cycles colour per beat so
        # consecutive beats are distinguishable.
        self._heartbeat_indicator: str = ""
        self._heartbeat_color: str = ""
        self._heartbeat_beat: int = 0

        # Recovery state
        self._recovery_info: dict | None = None  # interrupted diary for recovery

        # Model picker re-entry guard — a second Ctrl+S while one is open
        # would stack a second picker and leak the first's await-task.
        self._model_picker_open = False
        self._model_picker_future: "asyncio.Future | None" = None

        # Fatal startup failure (memory DB broken, required plugin failed).
        # Stored so main() can surface it to the terminal AFTER the TUI has
        # torn down its alternate screen — a stderr print during on_mount is
        # wiped by Textual's screen restore, leaving a silent exit.
        self._fatal_message: str | None = None

    def compose(self) -> ComposeResult:
        """Minimal layout: chat fills screen, input + status docked at bottom."""
        yield ChatView(id="chat-view")
        yield HistoryInput(
            placeholder=t("input_placeholder"),
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

        # The service opens for user input only after every plugin spawn
        # converged — keep the input disabled (status bar shows "⏳
        # starting…") until _open_service_when_ready() fires.
        self.query_one("#user-input").disabled = True

        plugins = discover_plugins(external=self.service.config.plugins_external)

        # ── Step 1: Restore session from SQLite (pure read, no services needed) ─
        # get_recent_turns reads the DB directly via aiosqlite —
        # completely independent of the memory plugin process.
        try:
            turns, skipped, budget = await self.service.get_recent_turns()
            if turns:
                self._recovery_info = {
                    "turns": turns, "skipped": skipped, "budget": budget,
                }
                await self._restore_session()
        except MemoryDatabaseError as e:
            # Memory is core — a broken memory DB must not start a
            # memory-less session.  Surface the error and abort startup.
            await self._fatal_exit(t("memdb_unavailable", err=e))
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
        self.service.inbox._histories.set_default_handler_factory(
            lambda: TUIHandler(self, assistant_prefix=self._assistant_prefix)
        )

        # Autonomous heartbeat — surface ⚡ autonomous messages + status pulse.
        self.service.on_autonomous(self._on_autonomous_message)
        self.service.on_heartbeat(self._on_heartbeat)
        # Scheduler-driven output (cron fires / backfill) — surface
        # 📅 scheduled messages.
        self.service.on_schedule(self._on_schedule_message)
        # Fatal memory-save failure — persistent red banner (memory is core).
        self.service.on_memory_broken(self._on_memory_broken)
        # File-sharing tunnel down (harness-probed after sharefile loads) —
        # warning in chat, main-process owned.
        self.service.on_tunnel_down(self._on_tunnel_down)

        # A2A now starts as a plugin via the discovery loop above
        # (start_plugin_server("a2a") → start_a2a, idempotent).
        self.run_worker(
            self.service.start_subagent(),
            exclusive=False, group="subagent-startup",
        )

        # ── Step 4: open the service for input ──────────────────────
        # Enable the TUI input the moment every plugin spawn has converged
        # (the inbox consumer gates on the same event).  Until then the
        # status bar shows "⏳ starting…" and Enter does nothing.
        self.run_worker(
            self._open_service_when_ready(),
            exclusive=False, group="startup-gate",
        )

    async def _open_service_when_ready(self) -> None:
        """Enable user input once all plugin spawns have converged.

        Awaits the same readiness gate as the inbox consumer, so user
        input can never race ahead of core services — the very first
        processed message is whatever was posted before the input gate
        opened (e.g. a [Schedule <name>] trigger or a heartbeat), with the
        normal processing indicator.
        """
        await self.service.wait_startup_settled()
        try:
            self.query_one("#user-input").disabled = False
            self.query_one("#user-input").focus()
        except Exception:
            pass
        self._update_status()

    # ── Actions ──────────────────────────────────────────────────

    async def action_quit(self) -> None:
        """Quit the app — cancel the agent loop immediately, then
        clean up child processes.  Order matters: inbox/loop must
        stop first so MCP wrapper isn't mid-request during shutdown."""
        # Cancel the agent loop RIGHT NOW — don't let it keep firing
        # tool calls into the MCP wrapper while we're trying to stop.
        self.service.inbox.cancel()

        for worker in list(self.workers):
            try:
                worker.cancel()
            except Exception:
                pass

        await self._stop_plugins()
        self.exit()

    async def _fatal_exit(self, message: str) -> None:
        """Abort startup on a fatal component failure — never silently.

        Records the message so ``main()`` can re-print it to stderr after
        the TUI has fully shut down (a print inside ``on_mount`` is erased
        by Textual's alternate-screen restore), and exits with a non-zero
        return code so the shell sees the failure.  Also stops plugins so
        no child process is orphaned.
        """
        self._fatal_message = message
        logger.error("fatal_exit msg=%s", message)
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_system_message(message, color="#f85149")
        await self._stop_plugins()
        self.exit(return_code=1)

    async def _stop_plugins(self) -> None:
        """Stop the inbox and every plugin service with a bounded wait.

        Shared by ``action_quit`` (normal exit) and the fatal
        required-component path (memdb load failure) so child processes
        are never orphaned when the app goes down.
        """

        async def _stop_one(name: str, coro) -> None:
            try:
                await asyncio.wait_for(coro, timeout=3.0)
            except asyncio.TimeoutError:
                logger.warning("shutdown_timeout service=%s", name)
            except Exception:
                pass

        # Stop inbox first — completes any in-flight message.
        await _stop_one("inbox", self.service.stop_inbox())
        # Then kill remaining services in parallel.
        await asyncio.gather(
            _stop_one("subagent", self.service.stop_subagent()),
            _stop_one("a2a", self.service.stop_a2a()),
            _stop_one("mcp", self.service.stop_plugin("mcp")),
            _stop_one("memdb", self.service.stop_memdb()),
            _stop_one("wechat", self.service.stop_wechat()),
            _stop_one("memfiles", self.service.stop_memfiles()),
            _stop_one("sharefile", self.service.stop_sharefile()),
            return_exceptions=True,
        )

    def action_cancel(self) -> None:
        """Cancel the currently running agent loop.  No-op if idle."""
        if not self.service.inbox.busy:
            return
        self.service.inbox.cancel()
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_system_message(t("interrupted"), color="#d29922")

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
            heartbeat=self._heartbeat_indicator,
            heartbeat_color=self._heartbeat_color,
            starting=not self.service.startup_settled,
            inbox_busy=inbox.busy if inbox else False,
            inbox_pending=inbox.pending if inbox else 0,
        )

    # ── Model switching (Ctrl+S) ──────────────────────────────────

    def action_switch_model(self) -> None:
        """Open the inline model picker — works even when the LLM is down.

        Switching is config + runtime only (no API call), so this is the
        escape hatch when the current model is unavailable.  Rendered in
        the chat stream, same style as the approval prompt: type a number
        to pick, Esc to cancel.

        NOTE — must be SYNC: binding actions run inside the key-event
        handler (`App._on_key` → `_check_bindings`), so awaiting the
        user's choice here would block the message pump and deadlock the
        TUI (the picker needs key events to resolve).  The post-decision
        work runs as a background task instead.
        """
        if self._model_picker_open:
            return  # a picker is already showing — ignore re-entrant Ctrl+S
        import asyncio

        from slife.ui.model_picker import ModelPicker

        # A pending approval prompt must not be left blocking the agent loop
        # while the picker takes focus (its Esc would now hit the picker, and
        # the loop would hang on the approval future forever).  Deny it first.
        try:
            prompt = self.query_one(".approval-prompt")
            decide = getattr(prompt, "_decide", None)
            if decide is not None:
                decide(approved=False)
        except Exception:
            pass

        models = list(self.service.config.models)
        if not models:
            self.query_one("#chat-view", ChatView).add_system_message(
                t("no_models"), color="#f85149"
            )
            return

        chat_view = self.query_one("#chat-view", ChatView)
        future: asyncio.Future = asyncio.Future()
        picker = ModelPicker(
            models,
            self.service.config.active_model_ref,
            future,
        )
        chat_view.mount(picker)
        # Scroll to the picker AFTER it is laid out.  An immediate scroll_end
        # runs against the pre-mount content, and the picker's insertion can
        # leave the view pinned at the top — the picker ends up below the
        # fold (the user only finds it by pressing End).  Same deferred
        # pattern as ChatView._follow_after_refresh (images).
        chat_view.call_after_refresh(chat_view.scroll_end, animate=False)
        picker.focus()
        self._model_picker_open = True
        self._model_picker_future = future

        asyncio.create_task(self._finish_model_switch(chat_view, future))

    def _dismiss_model_picker(self) -> None:
        """Cancel an open model picker so its future resolves.

        Called when an approval prompt is about to take focus while the picker
        is open — without this the picker's future never resolves, the
        ``_finish_model_switch`` task leaks, and ``_model_picker_open`` stays
        True so every later Ctrl+S is dead.
        """
        fut = self._model_picker_future
        if fut is not None and not fut.done():
            fut.set_result(None)
        self._model_picker_open = False
        self._model_picker_future = None

    async def _finish_model_switch(self, chat_view, future) -> None:
        """Apply the picker's decision off the key-event handler."""
        model = await future
        self._model_picker_open = False
        self._model_picker_future = None
        self.query_one("#user-input").focus()
        if model is None:
            return  # canceled — the picker already shows the status
        try:
            msg = self.service.switch_model(model.ref)
        except ValueError as exc:
            chat_view.add_system_message(str(exc), color="#f85149")
            return
        self._update_status()
        chat_view.add_system_message(msg, color="#3fb950")

    # ── Memory health (✗ core failure) ─────────────────────────────

    def _on_memory_broken(self, error: str) -> None:
        """Persistent memory-save failure — show a persistent red banner.

        Memory is core; the inbox is frozen (no new turns) until the DB is
        fixed and the agent restarted.
        """
        chat_view = self.query_one("#chat-view", ChatView)
        chat_view.add_system_message(
            t("memory_broken", err=error),
            color="#f85149",
        )

    # ── File-sharing tunnel (⚠ unavailable) ────────────────────────

    def _on_tunnel_down(self, message: str) -> None:
        """File-sharing tunnel failed to start — show a warning in chat.

        Surfaced by the harness after it probes the sharefile plugin's
        ``__tunnel_status`` (main-process owned; the plugin never talks to
        the TUI).  ngrok free tier allows one online agent per token, so a
        second slife instance legitimately cannot start a second tunnel.
        """
        self._show_system_message(message, color="#d29922")

    # ── Autonomous heartbeat (⚡ autonomous) ───────────────────────

    async def _on_autonomous_message(self, text: str) -> None:
        """Mount an autonomous (heartbeat) message in the chat — ⚡ autonomous."""
        self.query_one("#chat-view", ChatView).add_assistant_message(
            name_prefix=t("autonomous_prefix"),
            timestamp=datetime.now().astimezone(),
        ).append_text(text)

    async def _on_schedule_message(self, text: str) -> None:
        """Mount a scheduler-driven message in the chat — 📅 scheduled."""
        self.query_one("#chat-view", ChatView).add_assistant_message(
            name_prefix=t("schedule_prefix"),
            timestamp=datetime.now().astimezone(),
        ).append_text(text)

    async def _on_heartbeat(self, outcome: str) -> None:
        """Update the status-bar heartbeat indicator, cycling its colour so
        consecutive beats are distinguishable (● act / · quiet).

        The act glyph is a dot, not the ⚡ bolt used by the thinking badge —
        a heartbeat firing would otherwise be indistinguishable from the
        agent thinking.
        """
        self._heartbeat_beat += 1
        palette = ["#3fb950", "#58a6ff", "#bc8cff", "#d29922", "#f0883e"]
        self._heartbeat_color = palette[self._heartbeat_beat % len(palette)]
        self._heartbeat_indicator = "●" if outcome == "act" else "·"
        self._update_status()

    # ── Plugin startup helpers ────────────────────────────────────

    async def _start_plugin_safe(self, name: str, coro) -> None:
        """Start a plugin and show its readiness outcome in chat.

        All plugins are equal peers under the readiness contract — there is no
        required set.  A plugin that returns ``STARTED`` completed its MCP
        ``initialize`` handshake, which is its ready declaration (the
        per-plugin serving requirement was encoded server-side in the
        lifespan).  ``SKIPPED`` is an expected no-op (e.g. a2a without a
        running MQTT broker) and stays neutral; ``FAILED`` is a warning —
        the missing service is surfaced where it is used, and a broken
        memory backend freezes the inbox with a red banner the first time a
        turn cannot be saved.  The spawn hang-guard lives in
        ``AgentService.start_plugin_server``, so a stuck child still lets
        startup convergence fire.
        """
        try:
            status = await coro
        except Exception as e:
            self._show_system_message(
                t("plugin_start_failed", name=name, err=e), color="#d29922",
            )
            return
        if status is PluginStartStatus.STARTED:
            # STARTED means the MCP initialize handshake completed — the
            # plugin is ready (its serving requirement was encoded in the
            # server's lifespan, not probed afterwards).
            self._show_system_message(
                t("plugin_ready", name=name), color="#3fb950",
            )
        elif status is PluginStartStatus.SKIPPED:
            logger.debug("plugin_skipped name=%s", name)
            self._show_system_message(
                t("plugin_skipped", name=name), color="#8b949e",
            )
        else:
            self._show_system_message(
                t("plugin_ready_failed", name=name), color="#d29922",
            )

    async def _abort_required_plugin(self, name: str, reason: str) -> None:
        """Abort startup because a required component failed to load.

        Mirrors the memory-restore-fatal path: a red chat message plus an
        stderr line (the TUI message never renders before exit tears the
        app down), a bounded plugin shutdown (no orphaned children), then
        exit — a required component missing is never silent.
        """
        msg = t("required_failed", name=name, reason=reason)
        await self._fatal_exit(msg)


    # ── Input handling ────────────────────────────────────────────

    def on_history_input_submitted(self, event: HistoryInput.Submitted) -> None:
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

        # Extract @path image directive sources — the message itself stays
        # VERBATIM (the @ reference is visible like any other user text);
        # the sources ride to the loop, which auto-invokes attach_image
        # for each (no LLM iteration spent deciding to attach).
        image_paths = _parse_images_from_input(raw)

        chat_view = self.query_one("#chat-view", ChatView)
        # Display the original raw text — exactly what the agent sees.
        # The timestamp is the Enter-press moment — shown on the user
        # message and threaded into the turn's diary created_at so the
        # assistant reply (and restore) show the same time.
        now = datetime.now().astimezone()
        chat_view.add_user_message(
            raw, prefix="You> ", timestamp=now,
        )

        # _process_message just enqueues and returns immediately
        # (handler is attached to the message, inbox streams later).
        self.run_worker(
            self._process_message(
                raw, image_paths or None, chat_view, turn_time=now,
            ),
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
            chat_view.add_user_message(
                content,
                prefix=f"A2A({source})",
                timestamp=datetime.now().astimezone(),
            )

        elif kind == "peer_message":
            # Peer terminal (WeChat etc.) — show with channel prefix
            source = kwargs.get("source", "wechat")
            content = kwargs.get("content", "").strip()
            chat_view.add_user_message(
                content,
                prefix="Wechat> ",
                timestamp=datetime.now().astimezone(),
            )

        elif kind == "subagent_message":
            # Local worker completion — same `⚙️ subagent> ` bubble session
            # restore shows, so live and restored turns read identically.
            content = kwargs.get("content", "").strip()
            if content:
                chat_view.add_user_message(
                    content,
                    prefix=t("subagent_prefix", name=kwargs.get("name") or "subagent"),
                    timestamp=datetime.now().astimezone(),
                )

        elif kind == "loop_error":
            error = kwargs.get("error", "")
            chat_view.add_system_message(t("loop_error", err=error), color="#f85149")

        elif kind == "task_completed":
            source = kwargs.get("source", "unknown")
            chat_view.add_system_message(
                t("task_completed", source=source), color="#3fb950",
            )

        elif kind == "busy":
            # A turn started processing (incl. autonomous heartbeat) —
            # refresh the status bar to show "⏳ processing".
            self._update_status()

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
            history=self.service.message_history,
            config=self.service.config,
            agent_name=self._agent_name,
            assistant_prefix=self._assistant_prefix,
        )
        self._recovery_info = None

    # ── Agent interaction ─────────────────────────────────────────

    async def _process_message(
        self,
        text: str,
        images: list[str] | None,
        chat_view: ChatView,
        turn_time: datetime | None = None,
    ) -> None:
        """Run the agent loop and stream results to the TUI."""
        handler = TUIHandler(
            self, assistant_prefix=self._assistant_prefix, timestamp=turn_time,
        )

        try:
            await self.service.process_message(
                user_input=text,
                images=images if images else None,
                handler=handler,
            )
            handler.finalize_current()
        except Exception as e:
            handler.finalize_current()
            chat_view.add_system_message(t("turn_error", err=e), color="#f85149")
        finally:
            # Clear the tool-widget map only after this turn has finished —
            # clearing at enqueue time (a follow-up worker can start while the
            # previous turn is still streaming) orphaned the in-flight tool
            # widgets, leaving their rows stuck on "◌ running" with the
            # results silently dropped.
            self._tool_widgets.clear()
