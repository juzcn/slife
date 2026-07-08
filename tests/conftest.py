"""Shared fixtures for slife test suite."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from slife.config import Config, ModelConfig
from slife.agent.llm_client import TokenUsage


# ── Environment isolation ────────────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch):
    """Remove all slife-related env vars for test isolation."""
    for key in list(os.environ.keys()):
        if key.startswith("SLIFE") or key.startswith("TEST_"):
            monkeypatch.delenv(key, raising=False)
    yield


# ── TokenUsage fixtures ──────────────────────────────────────────────


@pytest.fixture
def zero_usage():
    """Empty token usage."""
    return TokenUsage()


@pytest.fixture
def small_usage():
    """Small token usage for additive tests."""
    return TokenUsage(prompt_tokens=100, completion_tokens=50, total_tokens=150)


@pytest.fixture
def large_usage():
    """Large token usage for additive tests."""
    return TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500)


# ── ModelConfig fixtures ─────────────────────────────────────────────


@pytest.fixture
def basic_model_dict():
    """Minimal valid model dictionary."""
    return {
        "model": "deepseek-v4-flash",
        "api_key": "sk-test123",
    }


@pytest.fixture
def full_model_dict():
    """Full model dictionary with all options."""
    return {
        "model": "deepseek/deepseek-v4-pro",
        "name": "DeepSeek V4 Pro",
        "api_key": "sk-test456",
        "base_url": "https://api.deepseek.com",
        "api": "openai-completions",
        "reasoning": True,
        "reasoning_effort": "high",
        "input": ["text", "image"],
        "context_window": 131072,
        "max_tokens": 8192,
        "temperature": 0.7,
        "top_p": 1.0,
    }


@pytest.fixture
def basic_model_config(basic_model_dict):
    """Basic ModelConfig instance."""
    return ModelConfig.from_dict(basic_model_dict)


# ── Config fixtures ──────────────────────────────────────────────────


@pytest.fixture
def minimal_json5_config():
    """Minimal valid JSON5 config content."""
    return """
{
    models: {
        providers: {
            deepseek: {
                base_url: "https://api.deepseek.com",
                api_key: "sk-test",
                models: [
                    { model: "deepseek-v4-flash", name: "DeepSeek V4 Flash" }
                ]
            }
        }
    },
    tools: [
        { type: "shell", timeout: 30 }
    ]
}
"""


@pytest.fixture
def multi_model_json5_config():
    """JSON5 config with multiple models and providers."""
    return """
{
    models: {
        providers: {
            deepseek: {
                base_url: "https://api.deepseek.com",
                api_key: "sk-test-ds",
                models: [
                    {
                        model: "deepseek-v4-flash",
                        name: "DeepSeek V4 Flash",
                        reasoning: false,
                        input: ["text", "image"],
                        context_window: 131072,
                        max_tokens: 8192,
                    },
                    {
                        model: "deepseek-v4-pro",
                        name: "DeepSeek V4 Pro",
                        reasoning: true,
                        reasoning_effort: "high",
                        input: ["text", "image"],
                        context_window: 131072,
                        max_tokens: 8192,
                    },
                ]
            },
            openai: {
                base_url: "https://api.openai.com/v1",
                api_key: "sk-test-oai",
                models: [
                    { model: "gpt-5", name: "GPT-5" }
                ]
            }
        }
    },
    active_model: "deepseek/deepseek-v4-pro",
    agent: {
        max_iterations: 20,
        system_prompt: "Custom system prompt for testing.",
    },
    tools: [
        { type: "serper", api_key: "test-serper-key" },
        { type: "shell", timeout: 60 },
    ]
}
"""


@pytest.fixture
def temp_config_file(minimal_json5_config):
    """Create a temporary JSON5 config file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json5", delete=False, encoding="utf-8"
    ) as f:
        f.write(minimal_json5_config)
        f.flush()
        yield Path(f.name)
    # Cleanup
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def temp_multi_config_file(multi_model_json5_config):
    """Create a temporary multi-model JSON5 config file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json5", delete=False, encoding="utf-8"
    ) as f:
        f.write(multi_model_json5_config)
        f.flush()
        yield Path(f.name)
    # Cleanup
    Path(f.name).unlink(missing_ok=True)


# ── Mock fixtures ────────────────────────────────────────────────────


@pytest.fixture
def mock_openai_client():
    """Create a mock AsyncOpenAI client."""
    mock = MagicMock()
    mock.chat = MagicMock()
    mock.chat.completions = MagicMock()
    mock.chat.completions.create = AsyncMock()
    return mock


@pytest.fixture
def mock_tool_registry():
    """Create a mock tool registry."""
    registry = MagicMock()
    registry.to_openai_functions.return_value = []
    registry.execute = AsyncMock(return_value="mock result")
    return registry


@pytest.fixture
def mock_event_handler():
    """Create a mock AgentEventHandler."""
    handler = MagicMock()
    handler.on_thinking_chunk = AsyncMock()
    handler.on_text_chunk = AsyncMock()
    handler.on_tool_call = AsyncMock()
    handler.on_tool_result = AsyncMock()
    handler.on_token_usage = AsyncMock()
    return handler


# ── Temp files ───────────────────────────────────────────────────────


@pytest.fixture
def temp_image_file():
    """Create a temporary PNG-like file (valid PNG header)."""
    import struct
    import zlib

    # Minimal valid PNG file
    def create_png(width=1, height=1):
        def chunk(chunk_type, data):
            c = chunk_type + data
            crc = struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
            return struct.pack(">I", len(data)) + c + crc

        header = b"\x89PNG\r\n\x1a\n"
        ihdr = chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        raw = zlib.compress(b"\x00\xff\x00\xff\x00\xff\x00")
        idat = chunk(b"IDAT", raw)
        iend = chunk(b"IEND", b"")
        return header + ihdr + idat + iend

    with tempfile.NamedTemporaryFile(
        mode="wb", suffix=".png", delete=False
    ) as f:
        png_data = create_png()
        f.write(png_data)
        f.flush()
        yield Path(f.name)
    Path(f.name).unlink(missing_ok=True)


@pytest.fixture
def temp_text_file():
    """Create a temporary text file."""
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as f:
        f.write("hello world\n")
        f.flush()
        yield Path(f.name)
    Path(f.name).unlink(missing_ok=True)
