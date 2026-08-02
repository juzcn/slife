"""Textual TUI application for Slife — Claude Code CLI style."""

import json
import logging
import re
from pathlib import Path

from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, Static

from slife.config import Config
from slife.agent.service import AgentService
from slife.agent.loop import MaxIterationsExceeded
from slife.ui.chat import ChatView
from slife.ui.handler import TUIHandler
from slife.ui.tool_display import ToolCallWidget
from slife.ui.image_utils import is_image_file
from slife.agent.loop import _scan_for_images

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


# ── Main TUI app ───────────────────────────────────────────────────


def _restore_prefix(channel: str | None, _agent_id: str) -> str:
    """Consistent prefix mapping for restored turns.

    Matches the real-time display prefixes used during live operation:
      - human  → "You> "
      - wechat → "<agent_id>(Wechat)"
      - other   → "<remote_agent_id>(a2a)" (external agent id, A2A peer, etc.)
    """
    # Normalise None → "" (JSON null values, missing keys)
    ch = channel or ""
    if ch == "human":
        return "You> "
    if ch == "wechat":
        return "You(Wechat)> "
    if ch:
        return f"{ch}(a2a)"
    # Backward compat: old turns saved before channel was introduced
    return "You> "


# ── Image attachment parsing ──────────────────────────────────────

# Matches @ followed by an image file path (quoted or unquoted).
# Supports: @path/img.png  @"path/with spaces/img.jpg"  @'path/img.gif'
_IMAGE_ATTACH_RE = re.compile(
    r"""@(?:"([^"]+)"|'([^']+)'|(\S+))""",
)


def _parse_images_from_input(raw: str) -> tuple[str, list[str]]:
    """Extract ``@path`` image directives from user input.

    Returns ``(cleaned_text, [absolute_paths])``.  Paths that don't
    exist or have non-image extensions are left in the text unchanged.
    """
    images: list[str] = []
    parts: list[str] = []
    last_end = 0

    for match in _IMAGE_ATTACH_RE.finditer(raw):
        # Text before this @directive
        parts.append(raw[last_end:match.start()])
        file_path = match.group(1) or match.group(2) or match.group(3)
        p = Path(file_path)
        if p.exists() and p.is_file() and is_image_file(file_path):
            images.append(str(p.resolve()))
        else:
            # Not a valid image — leave the @directive as-is
            parts.append(raw[match.start():match.end()])
        last_end = match.end()

    parts.append(raw[last_end:])
    cleaned = "".join(parts).strip()
    return cleaned, images


async def _restore_from_blob(cache_path: str, chat_view: "ChatView") -> bool:
    """Reconstruct an image from the diary_images BLOB table.

    Extracts the ``image_id`` from the cache filename (stem = UUID),
    reads the BLOB, writes it to the cache dir, and renders in chat.
    """
    import aiosqlite
    import os as _os
    from slife.paths import get_data_dir

    p = Path(cache_path)
    image_id = p.stem

    env_db = _os.environ.get("SLIFE_MEMORY_DB")
    db_path = (
        Path(env_db) if env_db
        else get_data_dir() / f"{_os.environ.get('SLIFE_AGENT_ID', 'slife')}.db"
    )
    if not db_path.is_file():
        return False

    try:
        conn = await aiosqlite.connect(str(db_path))
        try:
            cursor = await conn.execute(
                "SELECT data FROM diary_images WHERE image_id = ?",
                (image_id,),
            )
            row = await cursor.fetchone()
            if row is None:
                return False
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(row[0])
            chat_view.add_image_to_chat(str(p.resolve()))
            return True
        finally:
            await conn.close()
    except Exception:
        return False


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
        same mechanism as native tools.  Built-in plugins (memory, mcp,
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

        self.run_worker(
            self._start_a2a_safe(),
            exclusive=False, group="a2a-startup",
        )
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
                logger.warning("shutdown_timeout service=%s", name)
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
            _stop_one("memory", self.service.stop_memory()),
            _stop_one("wechat", self.service.stop_wechat()),
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
            tokens=self.service.session_usage.total_tokens,
            thinking=self.service.thinking_enabled,
            inbox_busy=inbox.busy if inbox else False,
            inbox_pending=inbox.pending if inbox else 0,
        )

    # ── Plugin startup helpers ────────────────────────────────────

    async def _start_plugin_safe(self, name: str, coro) -> None:
        """Start a plugin and show unified success/failure in chat."""
        try:
            result = await coro
            if result is False:
                self._show_system_message(
                    f"⚠ 插件启动失败: {name}", color="#d29922",
                )
            else:
                self._show_system_message(
                    f"🔌 插件已加载: {name}", color="#3fb950",
                )
        except Exception as e:
            self._show_system_message(
                f"⚠ 插件启动失败 ({name}): {e}", color="#d29922",
            )

    async def _start_a2a_safe(self) -> None:
        """Start A2A mesh and show status — only notifies on success.

        A2A is not a plugin (it is discovered via broker probe, not
        ``discover_plugins``), so it gets its own startup helper.
        When the broker is unreachable we stay silent — that is the
        expected default when Mosquitto is not running.
        """
        try:
            result = await self.service.start_a2a()
            if result is True:
                a2a_cfg = self.service.config.a2a_config
                agent_id = a2a_cfg.agent_id if a2a_cfg else "slife"
                self._show_system_message(
                    f"🔌 多Agent已就绪: {agent_id}", color="#3fb950",
                )
        except Exception as e:
            self._show_system_message(
                f"⚠ A2A 启动失败: {e}", color="#d29922",
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
            _result = kwargs.get("result", "")
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

        Loads only the most recent turns that fit within ``context_floor``
        of the model's context window.  Older turns stay in the memory DB
        and can be retrieved via ``memory_search`` if needed.
        """
        if not self._recovery_info:
            return

        info = self._recovery_info
        all_turns: list[dict] = info.get("turns", [])

        if not all_turns:
            self._recovery_info = None
            return

        # ── Select turns within token budget (newest-first, cap at floor) ──
        context_window = self.service.config.active_model.context_window
        context_floor = self.service.config.context_floor
        token_budget = int(context_window * context_floor)

        # token_count is the cumulative total from the API (input + output
        # for the entire conversation up to that turn).  Comparing each
        # turn's cumulative value directly against the budget avoids
        # double-counting — the newest kept turn's token_count already
        # accounts for all older turns that will be loaded alongside it.
        turns: list[dict] = []
        for turn in reversed(all_turns):
            t = turn.get("token_count", 0) or 0
            # Always keep at least one turn; otherwise stop at budget
            if turns and t > token_budget:
                break
            turns.append(turn)
        turns.reverse()  # restore oldest-first order

        skipped = len(all_turns) - len(turns)
        if skipped > 0:
            logger.debug(
                "session_restore_trimmed loaded=%d skipped=%d budget=%d max_cumulative=%d",
                len(turns), skipped, token_budget,
                turns[-1].get("token_count", 0) if turns else 0,
            )

        # ── Phase 1: Reconstruct message list from selected turns ────
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
            tool_images: dict[str, list[str]] = {}  # tcid → [image_paths]
            for msg in all_messages:
                if msg.get("role") == "tool":
                    tcid = msg.get("tool_call_id", "")
                    if tcid:
                        content = msg.get("content", "") or ""
                        tool_results[tcid] = content
                        tool_errors[tcid] = msg.get("is_error", False)
                        imgs = _scan_for_images(content)
                        if imgs:
                            tool_images[tcid] = imgs

            # Build UI ops
            ui_ops: list[dict] = []

            assistant_indices = [
                i for i, m in enumerate(all_messages)
                if m.get("role") == "assistant"
            ]
            last_assistant_idx = assistant_indices[-1] if assistant_indices else -1

            # Build a channel→prefix lookup so every user message gets the
            # correct prefix per turn (human → "You> ", wechat → "You(Wechat)",
            # remote agent / a2a → "<agent_id>(a2a)").
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
                    prefix = _restore_prefix(ch, self._agent_id)
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
        # Replace messages on the existing conversation object so the
        # inbox's ConversationStore (which holds a reference to the same
        # object via _convs[HUMAN]) sees the restored history.
        self.service.conversation.messages = all_messages

        # ── Phase 3: Rebuild UI ────────────────────────────────────
        chat_view = self.query_one("#chat-view", ChatView)

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
                        # Restore images from BLOB (source of truth)
                        for img_path in tool_images.get(tcid, []):
                            await _restore_from_blob(img_path, chat_view)

            self._recovery_info = None
            if skipped > 0:
                self._show_system_message(
                    f"✅ 已恢复最近 {len(turns)} 轮对话"
                    f"（{skipped} 轮旧记录未加载，可用 memory_search 查找）",
                    color="#3fb950",
                )
            else:
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
