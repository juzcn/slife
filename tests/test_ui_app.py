"""Tests for slife.ui.app — AgentService, event handler, StatusBar logic."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from slife.config import Config, ModelConfig
from slife.agent.llm_client import TokenUsage
from slife.agent.loop import ToolCallInfo, AgentResult, MaxIterationsExceeded
from slife.agent.service import AgentService
from slife.ui.app import StatusBar
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
        assert service._mcp_client is None
        assert service._mcp_process is None

    def test_model_display_name(self, sample_config):
        service = AgentService(sample_config)
        assert service.model_display_name == "DeepSeek V4 Flash"

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

    @pytest.mark.asyncio
    async def test_process_message(self, sample_config):
        """process_message delegates to agent_loop.run."""
        service = AgentService(sample_config)
        handler = AsyncMock()

        mock_result = AgentResult(text="Hi!", usage=TokenUsage(1, 1, 2))
        service.agent_loop.run = AsyncMock(return_value=mock_result)

        result = await service.process_message("hello", None, handler)

        service.agent_loop.run.assert_awaited_once_with(
            user_input="hello",
            conversation=service.conversation,
            images=None,
            handler=handler,
        )
        assert result.text == "Hi!"

    @pytest.mark.asyncio
    async def test_process_message_with_images(self, sample_config):
        service = AgentService(sample_config)
        handler = AsyncMock()

        mock_result = AgentResult(text="Nice pic!", usage=TokenUsage(1, 2, 3))
        service.agent_loop.run = AsyncMock(return_value=mock_result)

        result = await service.process_message("describe", ["img.png"], handler)

        call_kwargs = service.agent_loop.run.call_args[1]
        assert call_kwargs["images"] == ["img.png"]


# ── AgentService MCP ───────────────────────────────────────────────────


class TestAgentServiceMCP:
    """Tests for AgentService MCP start/stop methods."""

    @pytest.mark.asyncio
    async def test_start_mcp_disabled_config(self, sample_config):
        """start_mcp returns early when MCP is disabled."""
        sample_config.mcp_config.enabled = False
        service = AgentService(sample_config)

        await service.start_mcp()

        assert service._mcp_client is None

    @pytest.mark.asyncio
    async def test_stop_mcp_nothing_running(self, sample_config):
        """stop_mcp is safe when nothing is connected."""
        service = AgentService(sample_config)

        await service.stop_mcp()

        assert service._mcp_client is None
        assert service._mcp_process is None

    @pytest.mark.asyncio
    async def test_stop_mcp_with_client(self, sample_config):
        """stop_mcp disconnects client and stops process cleanly."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock()
        mock_process = AsyncMock()
        mock_process.stop = AsyncMock()

        service._mcp_client = mock_client
        service._mcp_process = mock_process

        await service.stop_mcp()

        mock_client.disconnect.assert_awaited_once()
        mock_process.stop.assert_awaited_once()
        assert service._mcp_client is None
        assert service._mcp_process is None

    @pytest.mark.asyncio
    async def test_stop_mcp_handles_errors(self, sample_config):
        """stop_mcp handles disconnect/stop errors gracefully."""
        service = AgentService(sample_config)
        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock(side_effect=RuntimeError("oops"))
        mock_process = AsyncMock()
        mock_process.stop = AsyncMock(side_effect=OSError("fail"))

        service._mcp_client = mock_client
        service._mcp_process = mock_process

        # Should not raise
        await service.stop_mcp()

        assert service._mcp_client is None
        assert service._mcp_process is None


# ── TUIHandler ───────────────────────────────────────────────────────


class TestTUIHandler:
    """Tests for TUIHandler — uses fully mocked app."""

    def _make_app_mock(self):
        app = MagicMock()
        app._active_assistant = MagicMock()
        app._tool_widgets = {}
        return app

    @pytest.mark.asyncio
    async def test_on_thinking_chunk(self):
        app = self._make_app_mock()
        handler = TUIHandler(app)
        await handler.on_thinking_chunk("Hmm...")
        app._active_assistant.append_thinking.assert_called_once_with("Hmm...")

    @pytest.mark.asyncio
    async def test_on_thinking_chunk_no_active_assistant(self):
        app = self._make_app_mock()
        app._active_assistant = None
        handler = TUIHandler(app)
        await handler.on_thinking_chunk("Hmm...")

    @pytest.mark.asyncio
    async def test_on_text_chunk(self):
        app = self._make_app_mock()
        handler = TUIHandler(app)
        await handler.on_text_chunk("Hello")
        app._active_assistant.append_text.assert_called_once_with("Hello")

    @pytest.mark.asyncio
    async def test_on_text_chunk_no_active_assistant(self):
        app = self._make_app_mock()
        app._active_assistant = None
        handler = TUIHandler(app)
        await handler.on_text_chunk("text")

    @pytest.mark.asyncio
    async def test_on_tool_call(self):
        app = self._make_app_mock()
        app._tool_widgets = {}
        mock_chat_view = MagicMock()
        app.query_one.return_value = mock_chat_view

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

    @pytest.mark.asyncio
    async def test_on_tool_result_error(self):
        app = self._make_app_mock()
        mock_widget = MagicMock()
        app._tool_widgets = {"c1": mock_widget}
        handler = TUIHandler(app)
        await handler.on_tool_result("c1", "Error: failed", is_error=True)
        mock_widget.set_complete.assert_called_once_with("Error: failed", True)

    @pytest.mark.asyncio
    async def test_on_tool_result_missing_widget(self):
        app = self._make_app_mock()
        app._tool_widgets = {}
        handler = TUIHandler(app)
        await handler.on_tool_result("unknown", "result", False)

    @pytest.mark.asyncio
    async def test_on_token_usage(self):
        app = self._make_app_mock()
        app.service = MagicMock()
        handler = TUIHandler(app)
        usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)
        await handler.on_token_usage(usage)
        assert app.service.session_usage == usage
        app._active_assistant.set_token_usage.assert_called_once_with(usage)
        app._update_status.assert_called_once()


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
        bar.update_info(model="DeepSeek V4", tokens=1500, thinking=True)
        text = bar.update.call_args[0][0]
        assert "DeepSeek V4" in text
        assert "1,500 tokens" in text
        assert "thinking" in text

    def test_update_info_no_model(self):
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info()
        text = bar.update.call_args[0][0]
        assert "Ctrl+C" in text

    def test_update_info_no_tokens_hides_count(self):
        with patch("slife.ui.app.Static.__init__", return_value=None):
            bar = StatusBar()
        bar.update = MagicMock()
        bar.update_info(model="Test", tokens=0)
        text = bar.update.call_args[0][0]
        assert "tokens" not in text
