"""Shared test fixtures and mocks for the slife test suite."""

import os
import json
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from dataclasses import dataclass, field

import pytest

from slife.config import Config, ModelConfig
from slife.agent.llm_client import TokenUsage, StreamChunk
from slife.agent.conversation import Conversation
from slife.agent.loop import ToolCallInfo, AgentResult
from slife.tools.base import Tool
from slife.tools.registry import ToolRegistry


# ── Environment helpers ───────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    """Remove slife-related env vars for isolated tests."""
    for key in list(os.environ.keys()):
        if key.startswith("SLIFE_") or key.startswith("TEST_"):
            monkeypatch.delenv(key, raising=False)
    yield


# ── Model config fixtures ─────────────────────────────────────────────


@pytest.fixture
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


@pytest.fixture
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


@pytest.fixture
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
            {"type": "shell", "timeout": 30},
        ],
        max_iterations=10,
        system_prompt="You are slife, a helpful AI assistant.",
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


@pytest.fixture
def zero_usage():
    """Empty token usage."""
    return TokenUsage()


@pytest.fixture
def sample_usage():
    """Typical token usage values."""
    return TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)


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


@pytest.fixture
def echo_tool():
    """An echo test tool instance."""
    return _EchoTool()


@pytest.fixture
def failing_tool():
    """A failing test tool instance."""
    return _FailingTool()


@pytest.fixture
def tool_registry(echo_tool, failing_tool):
    """Registry with both echo and failing tools registered."""
    registry = ToolRegistry()
    registry.register(echo_tool)
    registry.register(failing_tool)
    return registry


@pytest.fixture
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


class _MockResponse:
    """Mock for a non-streaming API response."""
    def __init__(self, content=None, tool_calls=None, usage=None):
        self.choices = [
            type("Choice", (), {
                "message": type("Message", (), {
                    "content": content,
                    "tool_calls": tool_calls,
                })()
            })()
        ]
        self.usage = usage or _MockUsage()


@pytest.fixture
def mock_openai_client():
    """Create a mock AsyncOpenAI client with configurable responses."""
    mock = MagicMock()
    mock.chat = MagicMock()
    mock.chat.completions = MagicMock()
    mock.chat.completions.create = AsyncMock()
    return mock


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


def build_json5_config(models=None, active_model=None, tools=None, agent=None, system_prompt=None):
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
    if system_prompt is not None:
        cfg.setdefault("agent", {})["system_prompt"] = system_prompt
    return cfg
