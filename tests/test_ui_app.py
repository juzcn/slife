"""Tests for Slife.ui.app — AgentService, event handler, StatusBar logic."""

import pytest; pytestmark = pytest.mark.unit


import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.config import Config, ModelConfig
from slife.agent.llm_client import TokenUsage
from slife.agent.loop import ToolCallInfo, AgentResult
from slife.agent.service import AgentService
from slife.ui.app import StatusBar, _parse_images_from_input
from slife.ui.handler import TUIHandler


# ── AgentService ──────────────────────────────────────────────────────


class TestAgentService:
    """Tests for AgentService — pure logic, no Textual needed."""

    def test_construction(self, sample_config):
        service = AgentService(sample_config)
        assert service.config == sample_config
        assert service.llm_client is not None
        assert service.agent_loop is not None
        assert service.conversation is not None
        assert service.session_usage.total_tokens == 0

    def test_mcp_disabled_initially(self, sample_config):
        """MCP is not enabled until start_mcp is called."""
        service = AgentService(sample_config)
        assert service.mcp_enabled is False
        assert service._plugins["mcp"].client is None
        assert service._plugins["mcp"].process is None

    def test_model_display_name(self, sample_config):
        service = AgentService(sample_config)
        assert service.model_display_name == "deepseek/deepseek-v4-flash"

    def test_thinking_enabled_false(self, sample_config):
        service = AgentService(sample_config)
        assert service.thinking_enabled is False

    def test_thinking_enabled_true(self):
        config = Config(
            models=[ModelConfig(
                ref="deepseek/pro",
                provider="deepseek",
                api_model="pro",
                display_name="Pro",
                api_key="k",
                thinking_enabled=True,
            )],
            active_model_ref="deepseek/pro",
            tools=[],
        )
        service = AgentService(config)
        assert service.thinking_enabled is True

    def test_clear(self, sample_config):
        service = AgentService(sample_config)
        service.session_usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        service.clear()
        assert service.session_usage.total_tokens == 0

    def test_context_window_property(self, sample_config):
        service = AgentService(sample_config)
        assert service.context_window == sample_config.active_model.context_window

    def test_current_context_tokens_fresh_session(self, sample_config):
        """No API call yet and no restore → live conversation estimate."""
        service = AgentService(sample_config)
        assert service.current_context_tokens == service.conversation.count_tokens()

    def test_current_context_tokens_restore_estimate(self, sample_config):
        """First round after restore → the precomputed estimate (no recompute)."""
        service = AgentService(sample_config)
        service.agent_loop._last_usage = TokenUsage(
            prompt_tokens=5000, total_tokens=5000,
        )
        assert service.current_context_tokens == 5000

    def test_current_context_tokens_last_call_actual(self, sample_config):
        """After the first API call → the last call's real prompt_tokens."""
        service = AgentService(sample_config)
        service.agent_loop._last_usage = TokenUsage(
            prompt_tokens=4321, completion_tokens=99, total_tokens=4420,
        )
        assert service.current_context_tokens == 4321

    @pytest.mark.asyncio
    async def test_process_message(self, sample_config):
        """process_message routes through unified inbox — handler on message."""
        service = AgentService(sample_config)
        handler = AsyncMock()

        # Replace inbox with mock so we don't need a running background task
        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        result = await service.process_message("hello", None, handler)

        mock_inbox.post.assert_awaited_once()
        msg = mock_inbox.post.call_args[0][0]
        assert msg.content == "hello"
        assert msg.handler is handler
        assert result.text == ""  # placeholder when using inbox

    @pytest.mark.asyncio
    async def test_process_message_with_images(self, sample_config):
        service = AgentService(sample_config)
        handler = AsyncMock()

        mock_inbox = MagicMock()
        mock_inbox.post = AsyncMock()
        service.inbox = mock_inbox

        result = await service.process_message("describe", ["img.png"], handler)

        mock_inbox.post.assert_awaited_once()
        msg = mock_inbox.post.call_args[0][0]
        assert msg.images == ["img.png"]


# ── AgentService MCP ───────────────────────────────────────────────────


class TestAgentServiceMCP:
    """Tests for AgentService MCP start/stop methods."""

    @pytest.mark.asyncio
    async def test_start_mcp_with_empty_servers(self, sample_config):
        """start_mcp runs even with no servers (wrapper always starts)."""
        service = AgentService(sample_config)

        with patch.object(service, "_connect_mcp_wrapper", AsyncMock()), \
             patch.object(service, "_register_plugin_tools", AsyncMock()), \
             patch.object(service, "_auto_connect_mcp_servers", AsyncMock()):
            await service.start_mcp()

        assert service._plugins["mcp"].client is None  # mocked, so no real connection

    @pytest.mark.asyncio
    async def test_stop_mcp_nothing_running(self, sample_config):
        """stop_mcp is safe when nothing is connected."""
        service = AgentService(sample_config)

        await service.stop_mcp()

        assert service._plugins["mcp"].client is None
        assert service._plugins["mcp"].process is None

    @pytest.mark.asyncio
    async def test_stop_mcp_with_client(self, sample_config):
        """stop_mcp disconnects client and stops process cleanly."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_process = AsyncMock()
        mock_process.stop = AsyncMock()

        service._plugins["mcp"].client = mock_client
        service._plugins["mcp"].process = mock_process

        await service.stop_mcp()

        mock_client.disconnect.assert_awaited_once()
        mock_process.stop.assert_awaited_once()
        assert service._plugins["mcp"].client is None
        assert service._plugins["mcp"].process is None

    @pytest.mark.asyncio
    async def test_stop_mcp_handles_errors(self, sample_config):
        """stop_mcp handles disconnect/stop errors gracefully."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock(side_effect=RuntimeError("oops"))
        mock_process = AsyncMock()
        mock_process.stop = AsyncMock(side_effect=OSError("fail"))

        service._plugins["mcp"].client = mock_client
        service._plugins["mcp"].process = mock_process

        # Should not raise
        await service.stop_mcp()

        assert service._plugins["mcp"].client is None
        assert service._plugins["mcp"].process is None


# ── TUIHandler ───────────────────────────────────────────────────────


class TestTUIHandler:
    """Tests for TUIHandler — uses fully mocked app."""

    def _make_app_mock(self):
        app = MagicMock()
        app._tool_widgets = {}
        mock_chat_view = MagicMock()
        mock_chat_view.add_assistant_message.return_value = MagicMock()
        app.query_one.return_value = mock_chat_view
        return app

    def _handler_with_assistant(self):
        """Create a handler with a pre-existing current assistant."""
        app = self._make_app_mock()
        mock_assistant = MagicMock()
        app.query_one.return_value.add_assistant_message.return_value = mock_assistant
        handler = TUIHandler(app)
        handler._current_assistant = mock_assistant
        return handler, app, mock_assistant

    @pytest.mark.asyncio
    async def test_ensure_assistant_creates_on_first_chunk(self):
        """First thinking chunk creates a new AssistantMessage."""
        app = self._make_app_mock()
        mock_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.return_value = mock_assistant

        handler = TUIHandler(app)
        await handler.on_thinking_chunk("Hmm...")

        mock_chat_view.add_assistant_message.assert_called_once()
        mock_assistant.append_thinking.assert_called_once_with("Hmm...")
        assert handler._current_assistant is mock_assistant

    @pytest.mark.asyncio
    async def test_bare_dot_reply_is_silent(self):
        """A bare "." reply is never rendered and the message is discarded."""
        handler, app, mock_assistant = self._handler_with_assistant()
        mock_assistant._buffer = ""
        handler._silent_dot = False

        await handler.on_text_chunk(".")
        assert handler._silent_dot is True
        mock_assistant.append_text.assert_not_called()

        handler.finalize_current()
        assert handler._current_assistant is None
        assert mock_assistant.display is False
        mock_assistant.finalize.assert_not_called()

    @pytest.mark.asyncio
    async def test_non_dot_reply_renders(self):
        """A normal text reply renders; silent_dot stays False."""
        handler, app, mock_assistant = self._handler_with_assistant()
        mock_assistant._buffer = ""
        handler._silent_dot = False

        await handler.on_text_chunk("Hello")
        assert handler._silent_dot is False
        mock_assistant.append_text.assert_called_once_with("Hello")

        handler.finalize_current()
        mock_assistant.finalize.assert_called_once_with(intermediate=False)

    @pytest.mark.asyncio
    async def test_ensure_assistant_creates_new_after_tool_result(self):
        """After tool result, next chunk creates new message and collapses old."""
        app = self._make_app_mock()
        old_assistant = MagicMock()
        new_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.side_effect = [old_assistant, new_assistant]

        handler = TUIHandler(app)
        await handler.on_thinking_chunk("Hmm...")  # creates old_assistant
        await handler.on_tool_result("c1", "result", False)  # sets flag
        await handler.on_text_chunk("Next iteration...")  # collapses old, creates new

        old_assistant.finalize.assert_called_once_with(intermediate=True)
        assert handler._current_assistant is new_assistant
        assert handler._iteration_needs_new_message is False

    @pytest.mark.asyncio
    async def test_ensure_assistant_reuses_existing(self):
        """Consecutive chunks in same iteration reuse the same assistant."""
        app = self._make_app_mock()
        mock_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.return_value = mock_assistant

        handler = TUIHandler(app)
        await handler.on_thinking_chunk("chunk 1")
        await handler.on_text_chunk("chunk 2")

        # Same assistant used for both (no tool result between them)
        assert mock_chat_view.add_assistant_message.call_count == 1
        mock_assistant.append_text.assert_called_once_with("chunk 2")

    @pytest.mark.asyncio
    async def test_finalize_current(self):
        """finalize_current delegates to the assistant with intermediate=False."""
        app = self._make_app_mock()
        mock_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.return_value = mock_assistant

        handler = TUIHandler(app)
        await handler.on_thinking_chunk("x")  # creates assistant
        handler.finalize_current()

        mock_assistant.finalize.assert_called_once_with(intermediate=False)

    def test_finalize_current_no_assistant(self):
        """finalize_current is safe when no assistant was ever created."""
        app = self._make_app_mock()
        handler = TUIHandler(app)
        handler.finalize_current()  # should not raise

    @pytest.mark.asyncio
    async def test_set_completed_at_updates_turn_assistants(self):
        """set_completed_at stamps every assistant message of the turn with
        the completion time so the live [HH:MM] matches diary completed_at."""
        from datetime import datetime

        app = self._make_app_mock()
        mock_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.return_value = mock_assistant

        handler = TUIHandler(app)
        await handler.on_thinking_chunk("x")  # creates + records an assistant

        dt = datetime(2026, 8, 12, 14, 35, 0)
        handler.set_completed_at(dt)

        assert mock_assistant._timestamp == dt
        mock_assistant._refresh_display.assert_called()

    @pytest.mark.asyncio
    async def test_on_trim_marks_turn_last_assistant(self):
        """on_trim shows [TrimContext: N] on the turn's last assistant."""
        app = self._make_app_mock()
        mock_a1 = MagicMock()
        mock_a2 = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.side_effect = [mock_a1, mock_a2]

        handler = TUIHandler(app)
        await handler.on_thinking_chunk("x")   # creates a1
        handler._iteration_needs_new_message = True
        await handler.on_thinking_chunk("y")   # creates a2 (new iteration)
        assert handler._turn_assistants == [mock_a1, mock_a2]

        handler.on_trim(4)
        mock_a2.set_trim_marker.assert_called_once_with(4)
        mock_a1.set_trim_marker.assert_not_called()

    def test_on_trim_no_assistant_noop(self):
        """on_trim is safe when the turn produced no assistant message."""
        app = self._make_app_mock()
        handler = TUIHandler(app)
        handler.on_trim(2)  # should not raise

    @pytest.mark.asyncio
    async def test_on_token_usage_updates_current_assistant(self):
        """on_token_usage sets usage on the current assistant message."""
        app = self._make_app_mock()
        mock_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.return_value = mock_assistant

        handler = TUIHandler(app)
        await handler.on_thinking_chunk("x")  # creates assistant
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        await handler.on_token_usage(usage)

        mock_assistant.set_token_usage.assert_called_once_with(usage)
        app._update_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_iteration_needs_new_message_set_on_tool_result(self):
        """on_tool_result sets the flag for the next iteration boundary."""
        app = self._make_app_mock()
        mock_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.return_value = mock_assistant
        app._tool_widgets = {"c1": MagicMock()}

        handler = TUIHandler(app)
        await handler.on_thinking_chunk("x")
        await handler.on_tool_result("c1", "result", False)

        assert handler._iteration_needs_new_message is True

    @pytest.mark.asyncio
    async def test_on_thinking_chunk_no_current_assistant(self):
        """When no current assistant, chunk still works (creates one)."""
        app = self._make_app_mock()
        mock_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.return_value = mock_assistant

        handler = TUIHandler(app)
        # No pre-existing current assistant
        await handler.on_thinking_chunk("Hmm...")

        mock_assistant.append_thinking.assert_called_once_with("Hmm...")

    @pytest.mark.asyncio
    async def test_on_text_chunk_no_current_assistant(self):
        """When no current assistant, text chunk creates one."""
        app = self._make_app_mock()
        mock_assistant = MagicMock()
        mock_chat_view = app.query_one.return_value
        mock_chat_view.add_assistant_message.return_value = mock_assistant

        handler = TUIHandler(app)
        await handler.on_text_chunk("text")

        mock_assistant.append_text.assert_called_once_with("text")

    # ── Tool call tests (mostly unchanged logic) ─────────────────────

    @pytest.mark.asyncio
    async def test_on_tool_call(self):
        app = self._make_app_mock()
        app._tool_widgets = {}
        mock_chat_view = app.query_one.return_value

        with patch("slife.ui.handler.ToolCallWidget") as mock_widget_cls:
            mock_widget = MagicMock()
            mock_widget.tool_name = "web_search"
            mock_widget.tool_call_id = "c1"
            mock_widget_cls.return_value = mock_widget

            handler = TUIHandler(app)
            tc = ToolCallInfo(id="c1", name="web_search", arguments={"query": "cats"})
            await handler.on_tool_call(tc)

            assert "c1" in app._tool_widgets
            assert mock_widget.set_running.called

    @pytest.mark.asyncio
    async def test_on_tool_result_success(self):
        app = self._make_app_mock()
        mock_widget = MagicMock()
        app._tool_widgets = {"c1": mock_widget}
        handler = TUIHandler(app)
        await handler.on_tool_result("c1", "Search results", is_error=False)
        mock_widget.set_complete.assert_called_once_with("Search results", False)
        assert handler._iteration_needs_new_message is True

    @pytest.mark.asyncio
    async def test_on_tool_result_error(self):
        app = self._make_app_mock()
        mock_widget = MagicMock()
        app._tool_widgets = {"c1": mock_widget}
        handler = TUIHandler(app)
        await handler.on_tool_result("c1", "Error: failed", is_error=True)
        mock_widget.set_complete.assert_called_once_with("Error: failed", True)
        assert handler._iteration_needs_new_message is True

    @pytest.mark.asyncio
    async def test_on_tool_result_missing_widget(self):
        app = self._make_app_mock()
        app._tool_widgets = {}
        handler = TUIHandler(app)
        await handler.on_tool_result("unknown", "result", False)


# ── StatusBar logic ───────────────────────────────────────────────────


class TestStatusBar:
    """Tests for StatusBar.update_info — pure logic test."""

    def test_update_info_minimal(self):
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info(model="GPT-4o")
        text = bar.update.call_args[0][0]
        assert "GPT-4o" in text

    def test_update_info_full(self):
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info(
            model="DeepSeek V4",
            context_tokens=1500,
            context_window=100000,
            thinking=True,
        )
        text = bar.update.call_args[0][0]
        assert "DeepSeek V4" in text
        assert "1,500 (1.5%)" in text
        assert "thinking" in text

    def test_update_info_heartbeat_uses_dot_not_bolt(self):
        """Heartbeat renders as a colored dot — the status bar carries no ⚡."""
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info(thinking=True, heartbeat="●", heartbeat_color="#3fb950")
        text = bar.update.call_args[0][0]
        assert "thinking" in text     # thinking badge keeps its label
        assert "⚡" not in text        # no bolt anywhere (badge or heartbeat)
        assert "●" in text            # heartbeat is a dot
        assert "#3fb950" in text      # colored

    @pytest.mark.asyncio
    async def test_on_heartbeat_glyph_is_dot(self):
        """_on_heartbeat picks a dot glyph (● act / · quiet), cycling colour."""
        from slife.ui.app import SlifeApp

        app = object.__new__(SlifeApp)
        app._heartbeat_beat = 0
        app._heartbeat_color = ""
        app._heartbeat_indicator = ""
        app._update_status = MagicMock()

        await app._on_heartbeat("act")
        assert app._heartbeat_indicator == "●"
        assert app._heartbeat_color  # a cycling palette colour
        assert "⚡" not in app._heartbeat_indicator

        await app._on_heartbeat("quiet")
        assert app._heartbeat_indicator == "·"
        assert app._heartbeat_color != ""

    def test_update_info_no_model(self):
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info()
        text = bar.update.call_args[0][0]
        assert "Ctrl+C quit" in text

    def test_update_info_no_context_hides_ratio(self):
        """No context info yet → no ↑ token line at all."""
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info(model="Test")
        text = bar.update.call_args[0][0]
        assert "tokens" not in text
        assert "↑" not in text

    def test_update_info_zero_context_with_window(self):
        """Window known but no tokens yet → shows 0 with 0.0% (first round)."""
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info(model="Test", context_tokens=0, context_window=100000)
        text = bar.update.call_args[0][0]
        assert "0 (0.0%)" in text

    def test_status_bar_hints_include_ctrl_g(self):
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info(model="Test")
        assert "Ctrl+S model" in bar.update.call_args[0][0]


# ── Model switching (Ctrl+S) ────────────────────────────────────────────


class TestModelSwitchBinding:
    def test_app_binds_ctrl_s_to_switch_model(self):
        from slife.ui.app import SlifeApp

        matches = [b for b in SlifeApp.BINDINGS if b.key == "ctrl+s"]
        assert len(matches) == 1
        assert matches[0].action == "switch_model"

    def test_action_switch_model_is_sync_not_async(self):
        """Regression: binding actions run inside the key-event handler
        (`App._on_key` → `_check_bindings`).  An async action that awaits
        the picker's future there blocks the message pump and deadlocks
        the TUI — the picker needs key events to resolve, which never
        arrive.  The action must stay sync and defer the await to a task.
        """
        import inspect

        from slife.ui.app import SlifeApp

        assert not inspect.iscoroutinefunction(SlifeApp.action_switch_model)


# ── Fatal startup failure (never silent) ────────────────────────────────


class TestFatalExit:
    """A fatal component failure (broken memory DB, failed required plugin)
    must record a message for post-teardown printing and exit non-zero —
    never a silent exit-0 from the shell's perspective."""

    def _app(self, sample_config):
        from slife.ui.app import SlifeApp

        with patch("slife.ui.app.Static.__init__", return_value=None):
            app = SlifeApp(sample_config)
        app.service.kill_child_processes = MagicMock()
        return app

    @pytest.mark.asyncio
    async def test_fatal_exit_records_message_and_nonzero_code(self, sample_config):
        app = self._app(sample_config)
        app.query_one = MagicMock()
        app._stop_plugins = AsyncMock()

        await app._fatal_exit("✗ Memory database unavailable: boom")

        assert app._fatal_message == "✗ Memory database unavailable: boom"
        assert app._return_code == 1
        app._stop_plugins.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_abort_required_plugin_routes_through_fatal_exit(self, sample_config):
        app = self._app(sample_config)
        app.query_one = MagicMock()
        app._stop_plugins = AsyncMock()

        await app._abort_required_plugin("memdb", "status=failed")

        assert app._fatal_message is not None
        assert "memdb" in app._fatal_message
        assert app._return_code == 1


# ── Image attachment parsing ───────────────────────────────────────


class TestParseImagesFromInput:
    """_parse_images_from_input — multiple @directives, mixed shapes."""

    def test_no_at_no_images(self):
        assert _parse_images_from_input("just text") == []

    def test_single_bare_path(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        assert _parse_images_from_input(f"看 @{img}") == [str(img)]

    def test_non_image_at_token_not_attached(self):
        assert _parse_images_from_input("hi @someone how are you") == []

    def test_multiple_bare_paths(self, tmp_path):
        a = tmp_path / "a.png"; a.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        b = tmp_path / "b.jpg"; b.write_bytes(b"\xff\xd8\xff\xe0JFIF")
        got = _parse_images_from_input(f"看图 @{a} 和 @{b} 哪个好")
        assert got == [str(a), str(b)]

    def test_adjacent_no_space(self, tmp_path):
        """@a.png和@b.png — two directives with no whitespace between them."""
        a = tmp_path / "a.png"; a.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        b = tmp_path / "b.png"; b.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f"对比@{a}和@{b}")
        assert got == [str(a), str(b)]

    def test_comma_separated(self, tmp_path):
        a = tmp_path / "a.png"; a.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        b = tmp_path / "b.png"; b.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f"@{a},@{b}")
        assert got == [str(a), str(b)]

    def test_quoted_with_spaces(self, tmp_path):
        img = tmp_path / "has space.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f'看图 @"{img}" 谢谢')
        assert got == [str(img)]

    def test_single_quoted(self, tmp_path):
        img = tmp_path / "a.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f"@'{img}'")
        assert got == [str(img)]

    def test_quoted_then_bare(self, tmp_path):
        a = tmp_path / "has space.png"; a.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        b = tmp_path / "b.png"; b.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f'@"{a}" 和 @{b}')
        assert got == [str(a), str(b)]

    def test_mixed_url_path(self, tmp_path):
        img = tmp_path / "a.png"; img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(
            f"@{img} @https://example.com/x.png @http://e.com/y.jpg"
        )
        assert got == [str(img), "https://example.com/x.png", "http://e.com/y.jpg"]

    def test_data_uri(self):
        got = _parse_images_from_input("@data:image/png;base64,AAAA")
        assert got == ["data:image/png;base64,AAAA"]

    def test_data_uri_adjacent_to_path(self, tmp_path):
        img = tmp_path / "a.png"; img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f"@{img} @data:image/png;base64,AAAA")
        assert got == [str(img), "data:image/png;base64,AAAA"]

    def test_bracketed(self, tmp_path):
        img = tmp_path / "a.png"; img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f"@[{img}] 和 @{{{img}}}")
        assert got == [str(img), str(img)]

    def test_unterminated_quote_no_crash(self):
        assert _parse_images_from_input('看 @"C:\foo\a.png') == []

    def test_at_alone(self):
        assert _parse_images_from_input("plain @ nothing") == []
        assert _parse_images_from_input("@") == []

    def test_path_and_non_image_together(self, tmp_path):
        img = tmp_path / "a.png"; img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f"@{img} @everyone thanks")
        assert got == [str(img)]

    def test_data_uri_with_comma_adjacent(self):
        """data URIs contain commas — must not split at them."""
        got = _parse_images_from_input(
            "@data:image/png;base64,AAAA @data:image/jpeg;base64,BBBB"
        )
        assert got == [
            "data:image/png;base64,AAAA",
            "data:image/jpeg;base64,BBBB",
        ]

    def test_duplicate_directives_kept(self, tmp_path):
        img = tmp_path / "a.png"; img.write_bytes(b"\x89PNG\r\n\x1a\nfake")
        got = _parse_images_from_input(f"@{img} 再来一次 @{img}")
        assert got == [str(img), str(img)]

    def test_url_adjacent_no_space(self):
        """URLs don't end in an image extension — whitespace-exact slicing
        still splits adjacent directives (URLs never contain spaces)."""
        got = _parse_images_from_input(
            "@https://example.com/a.png和@http://e.com/b.jpg"
        )
        assert got == ["https://example.com/a.png", "http://e.com/b.jpg"]

    def test_url_comma_separated(self):
        got = _parse_images_from_input(
            "@https://a.com/x.png,@https://b.com/y.png"
        )
        assert got == ["https://a.com/x.png", "https://b.com/y.png"]

    def test_url_no_extension(self):
        """URLs are self-identifying via scheme — no extension required."""
        got = _parse_images_from_input("@https://example.com/photo")
        assert got == ["https://example.com/photo"]

    def test_url_with_query_string(self):
        got = _parse_images_from_input("@https://example.com/photo?v=2&x=1")
        assert got == ["https://example.com/photo?v=2&x=1"]

    def test_url_with_fragment(self):
        got = _parse_images_from_input("@https://example.com/photo#section")
        assert got == ["https://example.com/photo#section"]

    def test_url_with_extension_and_query(self):
        got = _parse_images_from_input("@https://example.com/a.png?v=2")
        assert got == ["https://example.com/a.png?v=2"]
