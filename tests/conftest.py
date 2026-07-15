"""Shared test fixtures and mocks for the Slife test suite."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from slife.config import Config, ModelConfig
from slife.agent.llm_client import TokenUsage
from slife.agent.conversation import Conversation
from slife.tools.base import Tool
from slife.tools.registry import ToolRegistry


# ── Model config fixtures ─────────────────────────────────────────────


@pytest.fixture(scope="session")
def sample_model_config():
    """A typical ModelConfig for testing."""
    return ModelConfig(
        ref="deepseek/deepseek-v4-flash",
        provider="deepseek",
        api_model="deepseek-v4-flash",
        display_name="DeepSeek V4 Flash",
        api_key="sk-test-key",
        base_url="https://api.deepseek.com",
        api="openai-completions",
        supports_vision=False,
        max_tokens=4096,
        context_window=131072,
        temperature=0.7,
        top_p=1.0,
        thinking_enabled=False,
        reasoning_effort=None,
    )


@pytest.fixture(scope="session")
def thinking_model_config():
    """Model config with thinking enabled."""
    return ModelConfig(
        ref="deepseek/deepseek-v4-pro",
        provider="deepseek",
        api_model="deepseek-v4-pro",
        display_name="DeepSeek V4 Pro",
        api_key="sk-pro-key",
        base_url="https://api.deepseek.com",
        api="openai-completions",
        supports_vision=True,
        max_tokens=8192,
        context_window=131072,
        temperature=0.6,
        top_p=0.9,
        thinking_enabled=True,
        reasoning_effort="high",
    )


@pytest.fixture(scope="session")
def openai_model_config():
    """Model config for a non-DeepSeek provider (OpenAI)."""
    return ModelConfig(
        ref="openai/gpt-4o",
        provider="openai",
        api_model="gpt-4o",
        display_name="GPT-4o",
        api_key="sk-openai-key",
        base_url="https://api.openai.com/v1",
        api="openai-completions",
        supports_vision=True,
        max_tokens=4096,
        context_window=128000,
        temperature=0.7,
        top_p=1.0,
        thinking_enabled=False,
        reasoning_effort=None,
    )


# ── Config fixtures ───────────────────────────────────────────────────


@pytest.fixture
def sample_config(sample_model_config):
    """A typical Config with one model and shell tool."""
    return Config(
        models=[sample_model_config],
        active_model_ref="deepseek/deepseek-v4-flash",
        tools=[
            {"name": "execute_shell", "timeout": 30},
        ],
        max_iterations=10,
    )


# ── Conversation fixture ──────────────────────────────────────────────


@pytest.fixture
def conversation():
    """Fresh conversation with a system prompt."""
    return Conversation(system_prompt="You are a helpful assistant.")


@pytest.fixture
def empty_conversation():
    """Fresh conversation without system prompt."""
    return Conversation()


# ── Token usage fixtures ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def zero_usage():
    """Empty token usage."""
    return TokenUsage()


# ── Tool registry fixtures ────────────────────────────────────────────


class _EchoTool(Tool):
    """Test tool that echoes its arguments."""
    name = "echo"
    description = "Echoes back the input."
    parameters = {
        "type": "object",
        "properties": {
            "message": {"type": "string", "description": "Message to echo."}
        },
        "required": ["message"],
    }

    async def execute(self, message: str = "") -> str:
        return f"Echo: {message}"


class _FailingTool(Tool):
    """Test tool that always raises."""
    name = "failer"
    description = "Always fails."
    parameters = {
        "type": "object",
        "properties": {
            "reason": {"type": "string", "description": "Reason for failure."}
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        reason = kwargs.get("reason", "unknown")
        raise RuntimeError(f"Intentional failure: {reason}")


@pytest.fixture(scope="session")
def echo_tool():
    """An echo test tool instance."""
    return _EchoTool()


@pytest.fixture(scope="session")
def failing_tool():
    """A failing test tool instance."""
    return _FailingTool()


@pytest.fixture(scope="session")
def tool_registry(echo_tool, failing_tool):
    """Registry with both echo and failing tools registered (session-scoped, read-only)."""
    registry = ToolRegistry()
    registry.register(echo_tool)
    registry.register(failing_tool)
    return registry


@pytest.fixture(scope="session")
def empty_registry():
    """An empty tool registry."""
    return ToolRegistry()


# ── LLM response mocks ────────────────────────────────────────────────


class _MockChoice:
    """Mock for openai choice object."""
    def __init__(self, delta):
        self.delta = delta


class _MockStreamEvent:
    """Mock for a streaming API event."""
    def __init__(self, delta=None, usage=None):
        self.choices = [_MockChoice(delta)] if delta else []
        self.usage = usage


class _MockDelta:
    """Mock delta with optional content, reasoning, tool_calls."""
    def __init__(self, content=None, reasoning_content=None, tool_calls=None):
        self.content = content
        self.reasoning_content = reasoning_content
        self.tool_calls = tool_calls


class _MockToolCallDelta:
    """Mock for a single tool call delta."""
    def __init__(self, index=0, id=None, function=None):
        self.index = index
        self.id = id
        self.function = function


class _MockFunctionDelta:
    """Mock for function delta in tool call."""
    def __init__(self, name=None, arguments=""):
        self.name = name
        self.arguments = arguments


class _MockUsage:
    """Mock for API usage response."""
    def __init__(self, prompt_tokens=100, completion_tokens=50, total_tokens=150):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


# ── Async helpers ─────────────────────────────────────────────────────


def async_return(value):
    """Create a coroutine that returns the given value."""
    async def _inner():
        return value
    return _inner()


def make_async_iter(items):
    """Create an async iterator from a list of items."""
    async def _gen():
        for item in items:
            yield item
    return _gen()


# ── JSON5 config builders ─────────────────────────────────────────────


def build_json5_config(models=None, active_model=None, tools=None, agent=None):
    """Build a minimal JSON5-serializable config dict for testing."""
    cfg = {
        "models": models or {
            "providers": {
                "deepseek": {
                    "base_url": "https://api.deepseek.com",
                    "api_key": "sk-test",
                    "models": [
                        {
                            "model": "deepseek-v4-flash",
                            "name": "DeepSeek V4 Flash",
                        }
                    ],
                }
            }
        },
        "active_model": active_model or "deepseek/deepseek-v4-flash",
        "tools": tools or [],
    }
    if agent is not None:
        cfg["agent"] = agent
    return cfg
