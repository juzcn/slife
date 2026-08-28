"""Tests for embeddings native tools — list / set / switch / remove / enable."""

import pytest; pytestmark = pytest.mark.unit

import json5
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from slife.tools.embeddings import (
    ListEmbeddingsTool, ProviderModelsListTool,
    SetEmbeddingsTool, SwitchEmbeddingsTool, RemoveEmbeddingsTool,
    EnableEmbeddingsTool,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json5.dumps(data, indent=2), encoding="utf-8")


def _read_config(path: Path) -> dict:
    return json5.loads(path.read_text(encoding="utf-8"))


def _make_path(tmp_path: Path) -> Path:
    p = tmp_path / "slife.json5"
    _write_config(p, {
        "embeddings": {
            "providers": {
                "local-embed": {
                    "base_url": "http://127.0.0.1:8000/v1",
                    "api_key": "local",
                },
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "${OPENAI_API_KEY}",
                    "models": [
                        {"model": "text-embedding-3-small", "dim": 1536},
                    ],
                },
            },
            "active_model": "local-embed",
            "enabled": True,
        },
    })
    return p


def _no_reload(tool) -> None:
    """Give a tool an empty ctx so _hot_reload is a no-op."""
    ctx = MagicMock()
    ctx.memdb_client = None
    ctx.memfiles_client = None
    object.__setattr__(tool, "_ctx", ctx)


# ── ListEmbeddingsTool ────────────────────────────────────────────────


class TestListEmbeddingsTool:
    @pytest.mark.asyncio
    async def test_lists_providers_and_models(self, tmp_path):
        p = _make_path(tmp_path)
        tool = ListEmbeddingsTool(config_path=p)
        result = await tool.execute()
        assert "local-embed" in result
        assert "openai" in result
        assert "http://127.0.0.1:8000/v1" in result
        assert "★" in result            # active marker
        assert "local-embed" in result

    @pytest.mark.asyncio
    async def test_no_embeddings(self, tmp_path):
        p = tmp_path / "empty.json5"
        _write_config(p, {"embeddings": {"providers": {}}})
        tool = ListEmbeddingsTool(config_path=p)
        result = await tool.execute()
        assert "No embeddings" in result


# ── ProviderModelsListTool ────────────────────────────────────────────


class TestProviderModelsListTool:
    @pytest.mark.asyncio
    async def test_provider_not_found(self, tmp_path):
        p = _make_path(tmp_path)
        tool = ProviderModelsListTool(config_path=p)
        result = await tool.execute(provider="nope")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_offline_falls_back_to_configured(self, tmp_path):
        p = _make_path(tmp_path)
        tool = ProviderModelsListTool(config_path=p)
        # Discovery fails (endpoint unreachable) — falls back to configured models.
        with patch(
            "openai.AsyncOpenAI",
            side_effect=RuntimeError("connect refused"),
        ):
            result = await tool.execute(provider="openai")
        assert "text-embedding-3-small" in result
        assert "offline" in result

    @pytest.mark.asyncio
    async def test_online_shows_discovered_models(self, tmp_path):
        p = _make_path(tmp_path)
        tool = ProviderModelsListTool(config_path=p)
        fake_client = MagicMock()
        fake_client.models.list = AsyncMock(return_value=MagicMock(
            data=[
                MagicMock(id="m1", dimension=768, active=True),
                MagicMock(id="m2", dimension=1024, active=False),
            ]
        ))
        with patch("openai.AsyncOpenAI", return_value=fake_client):
            result = await tool.execute(provider="openai")
        assert "m1" in result
        assert "m2" in result


# ── SetEmbeddingsTool ─────────────────────────────────────────────────


class TestSetEmbeddingsTool:
    @pytest.mark.asyncio
    async def test_adds_new_provider(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(
            provider="bedrock", model="cohere.embed",
            base_url="https://bedrock.example/v1", api_key="sk-b",
        )
        assert "Added" in result
        raw = _read_config(p)
        emb = raw["embeddings"]
        assert "bedrock" in emb["providers"]
        assert emb["providers"]["bedrock"]["base_url"] == "https://bedrock.example/v1"
        assert emb["providers"]["bedrock"]["models"][0]["model"] == "cohere.embed"

    @pytest.mark.asyncio
    async def test_updates_existing_model(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(
            provider="openai", model="text-embedding-3-small", dim=2048,
        )
        assert "Updated" in result
        raw = _read_config(p)
        models = raw["embeddings"]["providers"]["openai"]["models"]
        assert any(m["model"] == "text-embedding-3-small" and m["dim"] == 2048
                   for m in models)

    @pytest.mark.asyncio
    async def test_provider_missing_requires_base_url(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetEmbeddingsTool(config_path=p)
        result = await tool.execute(provider="nope", model="m")
        assert "does not exist" in result

    @pytest.mark.asyncio
    async def test_first_set_becomes_active(self, tmp_path):
        p = tmp_path / "slife.json5"
        _write_config(p, {"embeddings": {"providers": {}}})
        tool = SetEmbeddingsTool(config_path=p)
        _no_reload(tool)
        await tool.execute(provider="p1", model="m1",
                           base_url="http://x/v1", api_key="k")
        raw = _read_config(p)
        assert raw["embeddings"]["active_model"] == "p1/m1"

    @pytest.mark.asyncio
    async def test_hot_reload_calls_plugins(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetEmbeddingsTool(config_path=p)
        memdb_client = MagicMock()
        memdb_client.call_tool.return_value = '{"status": "ok"}'
        memfiles_client = MagicMock()
        memfiles_client.call_tool.return_value = '{"status": "ok"}'
        ctx = MagicMock()
        ctx.memdb_client = memdb_client
        ctx.memfiles_client = memfiles_client
        object.__setattr__(tool, "_ctx", ctx)

        result = await tool.execute(
            provider="openai", model="text-embedding-3-small",
        )
        memdb_client.call_tool.assert_called_once_with(
            "__memory_reload_semantic", {"enabled": True},
        )
        memfiles_client.call_tool.assert_called_once_with(
            "__memfiles_reload_semantic", {"enabled": True},
        )
        assert "memdb" in result


# ── SwitchEmbeddingsTool ──────────────────────────────────────────────


class TestSwitchEmbeddingsTool:
    @pytest.mark.asyncio
    async def test_switches_to_provider(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(ref="openai")
        assert "Switched" in result
        raw = _read_config(p)
        assert raw["embeddings"]["active_model"] == "openai"

    @pytest.mark.asyncio
    async def test_switches_to_model(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(ref="openai/text-embedding-3-small")
        assert "Switched" in result
        raw = _read_config(p)
        assert raw["embeddings"]["active_model"] == "openai/text-embedding-3-small"

    @pytest.mark.asyncio
    async def test_provider_not_found(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchEmbeddingsTool(config_path=p)
        result = await tool.execute(ref="nope")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_model_not_in_provider(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchEmbeddingsTool(config_path=p)
        result = await tool.execute(ref="openai/not-a-model")
        assert "not found" in result


# ── RemoveEmbeddingsTool ──────────────────────────────────────────────


class TestRemoveEmbeddingsTool:
    @pytest.mark.asyncio
    async def test_removes_model(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(ref="openai/text-embedding-3-small")
        assert "Removed" in result
        raw = _read_config(p)
        assert "text-embedding-3-small" not in str(raw["embeddings"])

    @pytest.mark.asyncio
    async def test_cannot_remove_active(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveEmbeddingsTool(config_path=p)
        result = await tool.execute(ref="local-embed")
        assert "cannot remove the active" in result

    @pytest.mark.asyncio
    async def test_removing_last_provider_drops_section(self, tmp_path):
        p = tmp_path / "slife.json5"
        _write_config(p, {
            "embeddings": {
                "providers": {"p1": {"base_url": "http://x/v1"}},
                "active_model": "",
            },
        })
        tool = RemoveEmbeddingsTool(config_path=p)
        _no_reload(tool)
        await tool.execute(ref="p1")
        raw = _read_config(p)
        assert "embeddings" not in raw


# ── EnableEmbeddingsTool ──────────────────────────────────────────────


class TestEnableEmbeddingsTool:
    @pytest.mark.asyncio
    async def test_enable(self, tmp_path):
        p = _make_path(tmp_path)
        tool = EnableEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(enabled=False)
        assert "disabled" in result
        raw = _read_config(p)
        assert raw["embeddings"]["enabled"] is False

    @pytest.mark.asyncio
    async def test_disable_hot_reloads_disabled(self, tmp_path):
        p = _make_path(tmp_path)
        tool = EnableEmbeddingsTool(config_path=p)
        memdb_client = MagicMock()
        memdb_client.call_tool.return_value = '{"status": "ok"}'
        memfiles_client = MagicMock()
        memfiles_client.call_tool.return_value = '{"status": "ok"}'
        ctx = MagicMock()
        ctx.memdb_client = memdb_client
        ctx.memfiles_client = memfiles_client
        object.__setattr__(tool, "_ctx", ctx)

        await tool.execute(enabled=False)
        memdb_client.call_tool.assert_called_once_with(
            "__memory_reload_semantic", {"enabled": False},
        )
        memfiles_client.call_tool.assert_called_once_with(
            "__memfiles_reload_semantic", {"enabled": False},
        )
