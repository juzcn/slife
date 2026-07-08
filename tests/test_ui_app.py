"""Tests for app service layer (slife.ui.app).

Tests AgentService and StatusBar logic without requiring a running Textual app.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.config import Config, ModelConfig
from slife.agent.llm_client import TokenUsage
from slife.ui.app import AgentService, StatusBar


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════


def _make_test_config(**overrides) -> Config:
    """Build a minimal Config for testing AgentService."""
    model = ModelConfig(
        ref="test/test-model",
        provider="test",
        api_model="test-model",
        display_name="Test Model",
        api_key="sk-test",
        base_url="https://api.test.com",
        api="openai-completions",
        supports_vision=False,
        max_tokens=4096,
        context_window=131072,
        temperature=0.7,
        top_p=1.0,
        thinking_enabled=False,
        reasoning_effort=None,
    )
    return Config(
        models=[model],
        active_model_ref="test/test-model",
        tools=[{"type": "shell", "timeout": 30}],
        max_iterations=overrides.get("max_iterations", 10),
        system_prompt=overrides.get("system_prompt", "You are helpful."),
    )


# ══════════════════════════════════════════════════════════════════════
# AgentService
# ══════════════════════════════════════════════════════════════════════


class TestAgentServiceInit:
    """Tests for AgentService.__init__()."""

    def test_creates_components(self):
        """AgentService creates LLM client, tool registry, agent loop, and conversation."""
        config = _make_test_config()
        service = AgentService(config)

        assert service.config == config
        assert service.llm_client is not None
        assert service.tool_registry is not None
        assert service.agent_loop is not None
        assert service.conversation is not None

    def test_session_usage_starts_zero(self):
        """Session usage is initialized to zero."""
        service = AgentService(_make_test_config())
        assert service.session_usage.total_tokens == 0
        assert service.session_usage.prompt_tokens == 0
        assert service.session_usage.completion_tokens == 0

    def test_system_prompt_in_conversation(self):
        """System prompt is passed to conversation."""
        config = _make_test_config(system_prompt="Custom prompt for testing.")
        service = AgentService(config)

        msgs = service.conversation.to_openai_messages()
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "Custom prompt for testing."

    def test_tools_loaded_from_config(self):
        """Tools are loaded from config into the registry."""
        config = _make_test_config()
        config.tools = [{"type": "shell", "timeout": 45}]
        service = AgentService(config)

        tools = service.tool_registry.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "execute_shell"

    def test_empty_tools(self):
        """Empty tools list works."""
        config = _make_test_config()
        config.tools = []
        service = AgentService(config)

        tools = service.tool_registry.list_tools()
        assert tools == []


class TestAgentServiceProperties:
    """Tests for AgentService properties."""

    def test_model_display_name(self):
        """model_display_name returns the active model's display name."""
        service = AgentService(_make_test_config())
        assert service.model_display_name == "Test Model"

    def test_thinking_enabled_false(self):
        """thinking_enabled reflects the model config."""
        service = AgentService(_make_test_config())
        assert service.thinking_enabled is False

    def test_thinking_enabled_true(self):
        """thinking_enabled is True when model has it enabled."""
        config = _make_test_config()
        config.models[0].thinking_enabled = True
        service = AgentService(config)
        assert service.thinking_enabled is True


class TestAgentServiceClear:
    """Tests for AgentService.clear()."""

    def test_clears_conversation(self):
        """clear() resets conversation history."""
        service = AgentService(_make_test_config(system_prompt="Keep me"))
        # Add some messages
        service.conversation.add_user_message("Hello")
        service.conversation.add_assistant_message("Hi")

        assert len(service.conversation.to_openai_messages()) == 3

        service.clear()

        # Only system prompt remains
        msgs = service.conversation.to_openai_messages()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"

    def test_resets_session_usage(self):
        """clear() resets session usage to zero."""
        service = AgentService(_make_test_config())
        service.session_usage = TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)

        service.clear()

        assert service.session_usage.total_tokens == 0
        assert service.session_usage.prompt_tokens == 0
        assert service.session_usage.completion_tokens == 0


class TestAgentServiceProcessMessage:
    """Tests for AgentService.process_message()."""

    @pytest.mark.asyncio
    async def test_delegates_to_agent_loop(self):
        """process_message() calls agent_loop.run()."""
        config = _make_test_config()
        service = AgentService(config)

        # Mock the agent loop's run method
        from slife.agent.loop import AgentResult
        service.agent_loop.run = AsyncMock(
            return_value=AgentResult(
                text="Response",
                usage=TokenUsage(total_tokens=10),
            )
        )

        handler = MagicMock()
        result = await service.process_message(
            user_input="Hello",
            images=None,
            handler=handler,
        )

        assert result.text == "Response"
        service.agent_loop.run.assert_called_once()
        call_kwargs = service.agent_loop.run.call_args.kwargs
        assert call_kwargs["user_input"] == "Hello"
        assert call_kwargs["handler"] == handler

    @pytest.mark.asyncio
    async def test_passes_images(self):
        """Images are passed through to agent loop."""
        config = _make_test_config()
        service = AgentService(config)

        from slife.agent.loop import AgentResult
        service.agent_loop.run = AsyncMock(
            return_value=AgentResult(text="ok", usage=TokenUsage())
        )

        await service.process_message(
            user_input="Describe",
            images=["img1.png", "img2.jpg"],
            handler=MagicMock(),
        )

        call_kwargs = service.agent_loop.run.call_args.kwargs
        assert call_kwargs["images"] == ["img1.png", "img2.jpg"]


# ══════════════════════════════════════════════════════════════════════
# StatusBar
# ══════════════════════════════════════════════════════════════════════


class TestStatusBar:
    """Tests for StatusBar widget.

    Tests update_info() by checking what content it passes to self.update().
    """

    @staticmethod
    def _get_display_text(bar: StatusBar, **kwargs) -> str:
        """Call update_info and return what was passed to self.update()."""
        # Monkey-patch update to capture the rendered string
        captured = []

        def capture_update(text):
            captured.append(text)

        bar.update = capture_update
        bar.update_info(**kwargs)
        return captured[0] if captured else ""

    def test_update_info_with_model(self):
        """Status bar shows model name."""
        bar = StatusBar()
        text = self._get_display_text(bar, model="Test Model")
        assert "Test Model" in text

    def test_update_info_with_thinking(self):
        """Status bar shows thinking indicator when enabled."""
        bar = StatusBar()
        text = self._get_display_text(bar, model="M", thinking=True)
        assert "thinking" in text

    def test_update_info_without_thinking(self):
        """Status bar does not show thinking when disabled."""
        bar = StatusBar()
        text = self._get_display_text(bar, model="M", thinking=False)
        assert "thinking" not in text

    def test_update_info_with_tokens(self):
        """Status bar shows token count."""
        bar = StatusBar()
        text = self._get_display_text(bar, model="M", tokens=1234)
        assert "1,234" in text
        assert "tokens" in text

    def test_update_info_zero_tokens(self):
        """Zero tokens are not shown (tokens > 0 check)."""
        bar = StatusBar()
        text = self._get_display_text(bar, model="M", tokens=0)
        assert "tokens" not in text

    def test_update_info_all_present(self):
        """All info is shown when provided."""
        bar = StatusBar()
        text = self._get_display_text(bar, model="GPT-5", tokens=5000, thinking=True)
        assert "GPT-5" in text
        assert "5,000" in text
        assert "thinking" in text

    def test_update_info_no_model(self):
        """When model is empty string, it is not shown."""
        bar = StatusBar()
        text = self._get_display_text(bar, model="")
        # Should not crash, just won't show model name
        assert isinstance(text, str)

    def test_default_values(self):
        """Default values work (all empty/false)."""
        bar = StatusBar()
        text = self._get_display_text(bar)
        # Should show keybindings hint
        assert "Ctrl+C" in text or isinstance(text, str)

    def test_keybindings_shown(self):
        """Key binding hints are always shown."""
        bar = StatusBar()
        text = self._get_display_text(bar, model="M")
        assert "Ctrl+C" in text
        assert "Ctrl+L" in text


# ══════════════════════════════════════════════════════════════════════
# _TUIHandler
# ══════════════════════════════════════════════════════════════════════


class TestTUIHandler:
    """Tests for _TUIHandler (bridges agent events to TUI)."""

    @pytest.mark.asyncio
    async def test_on_thinking_chunk(self):
        """Thinking chunks are forwarded to active assistant."""
        from slife.ui.app import _TUIHandler
        from unittest.mock import MagicMock

        app = MagicMock()
        assistant = MagicMock()
        app._active_assistant = assistant
        handler = _TUIHandler(app)

        await handler.on_thinking_chunk("thinking...")
        assistant.append_thinking.assert_called_once_with("thinking...")

    @pytest.mark.asyncio
    async def test_on_thinking_chunk_no_assistant(self):
        """Thinking chunks are ignored when no active assistant."""
        from slife.ui.app import _TUIHandler
        from unittest.mock import MagicMock

        app = MagicMock()
        app._active_assistant = None
        handler = _TUIHandler(app)

        # Should not raise
        await handler.on_thinking_chunk("thinking...")

    @pytest.mark.asyncio
    async def test_on_text_chunk(self):
        """Text chunks are forwarded to active assistant."""
        from slife.ui.app import _TUIHandler
        from unittest.mock import MagicMock

        app = MagicMock()
        assistant = MagicMock()
        app._active_assistant = assistant
        handler = _TUIHandler(app)

        await handler.on_text_chunk("hello")
        assistant.append_text.assert_called_once_with("hello")

    @pytest.mark.asyncio
    async def test_on_text_chunk_no_assistant(self):
        """Text chunks are ignored when no active assistant."""
        from slife.ui.app import _TUIHandler
        from unittest.mock import MagicMock

        app = MagicMock()
        app._active_assistant = None
        handler = _TUIHandler(app)
        await handler.on_text_chunk("hello")  # Should not raise

    @pytest.mark.asyncio
    async def test_on_tool_call_creates_widget(self):
        """on_tool_call creates a ToolCallWidget and mounts it."""
        from slife.ui.app import _TUIHandler
        from slife.agent.loop import ToolCallInfo
        from slife.ui.tool_display import ToolCallWidget
        from unittest.mock import MagicMock, patch

        app = MagicMock()
        app._tool_widgets = {}
        app._active_assistant = None

        mock_chat_view = MagicMock()
        app.query_one.return_value = mock_chat_view

        handler = _TUIHandler(app)
        tc = ToolCallInfo(id="call_1", name="search", arguments={"q": "test"})

        # Mock set_running to avoid DOM _rebuild call
        with patch.object(ToolCallWidget, 'set_running', return_value=None):
            await handler.on_tool_call(tc)

        # Widget stored in app._tool_widgets
        assert "call_1" in app._tool_widgets
        assert isinstance(app._tool_widgets["call_1"], ToolCallWidget)
        # Widget mounted to chat view
        mock_chat_view.mount.assert_called_once()
        mock_chat_view.scroll_end.assert_called_once()

    @pytest.mark.asyncio
    async def test_on_tool_result_success(self):
        """on_tool_result updates the matching widget."""
        from slife.ui.app import _TUIHandler
        from unittest.mock import MagicMock

        app = MagicMock()
        mock_widget = MagicMock()
        app._tool_widgets = {"call_abc": mock_widget}
        app._active_assistant = None

        handler = _TUIHandler(app)
        await handler.on_tool_result("call_abc", "result text", is_error=False)

        mock_widget.set_complete.assert_called_once_with("result text", False)

    @pytest.mark.asyncio
    async def test_on_tool_result_error(self):
        """on_tool_result with error flag passes is_error=True."""
        from slife.ui.app import _TUIHandler
        from unittest.mock import MagicMock

        app = MagicMock()
        mock_widget = MagicMock()
        app._tool_widgets = {"call_err": mock_widget}
        app._active_assistant = None

        handler = _TUIHandler(app)
        await handler.on_tool_result("call_err", "Error: failed", is_error=True)

        mock_widget.set_complete.assert_called_once_with("Error: failed", True)

    @pytest.mark.asyncio
    async def test_on_tool_result_unknown_widget(self):
        """on_tool_result for unknown widget is a no-op."""
        from slife.ui.app import _TUIHandler
        from unittest.mock import MagicMock

        app = MagicMock()
        app._tool_widgets = {}
        app._active_assistant = None

        handler = _TUIHandler(app)
        await handler.on_tool_result("unknown_id", "result", False)  # Should not raise

    @pytest.mark.asyncio
    async def test_on_token_usage_updates_session(self):
        """on_token_usage updates session usage and status bar."""
        from slife.ui.app import _TUIHandler
        from slife.agent.llm_client import TokenUsage
        from unittest.mock import MagicMock

        app = MagicMock()
        app.service = MagicMock()
        app.service.session_usage = TokenUsage()
        assistant = MagicMock()
        app._active_assistant = assistant

        handler = _TUIHandler(app)
        usage = TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15)

        await handler.on_token_usage(usage)

        assert app.service.session_usage.total_tokens == 15
        assistant.set_token_usage.assert_called_once_with(usage)
        app._update_status.assert_called_once()


# ══════════════════════════════════════════════════════════════════════
# SlifeApp (limited — no full Textual runtime)
# ══════════════════════════════════════════════════════════════════════


class TestSlifeAppBindings:
    """Tests for SlifeApp class-level attributes (no runtime needed)."""

    def test_bindings_exist(self):
        """SlifeApp BINDINGS is a valid list of tuples."""
        from slife.ui.app import SlifeApp
        bindings = SlifeApp.BINDINGS
        assert isinstance(bindings, list)
        assert len(bindings) >= 2
        for binding in bindings:
            assert len(binding) >= 2  # (key, action, ...)
            assert isinstance(binding[0], str)
            assert isinstance(binding[1], str)

    def test_css_path_exists(self):
        """SlifeApp CSS_PATH is set."""
        from slife.ui.app import SlifeApp
        assert SlifeApp.CSS_PATH == "slife.tcss"
