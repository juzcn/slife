"""Tests for model management tools — model_list, model_set, model_remove, model_switch."""

import pytest; pytestmark = pytest.mark.unit

import json5
import pytest
from pathlib import Path

from slife.tools.models import (
    ListModelsTool, SetModelTool, RemoveModelTool, SwitchModelTool,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _write_config(path: Path, data: dict) -> None:
    path.write_text(json5.dumps(data, indent=2), encoding="utf-8")


def _read_config(path: Path) -> dict:
    return json5.loads(path.read_text(encoding="utf-8"))


def _make_path(tmp_path: Path) -> Path:
    """Create a minimal config file and return its path."""
    p = tmp_path / "slife.json5"
    _write_config(p, {
        "models": {
            "mode": "merge",
            "providers": {
                "test": {
                    "base_url": "https://api.test.com",
                    "api_key": "${TEST_KEY}",
                    "api": "openai-completions",
                    "models": [
                        {"model": "test-model", "name": "Test Model", "reasoning": False, "input": ["text"], "context_window": 100000, "max_tokens": 4096},
                        {"model": "test-model-2", "name": "Test Model 2", "reasoning": True, "input": ["text", "image"], "context_window": 200000, "max_tokens": 8192},
                    ],
                },
            },
        },
        "active_model": "test/test-model",
    })
    return p


# ── ListModelsTool ────────────────────────────────────────────────────


class TestListModelsTool:
    @pytest.mark.asyncio
    async def test_lists_all_models(self, tmp_path):
        p = _make_path(tmp_path)
        tool = ListModelsTool(config_path=p)
        result = await tool.execute()
        assert "test/test-model" in result
        assert "test/test-model-2" in result
        assert "Test Model" in result
        assert "★" in result  # active model marker

    @pytest.mark.asyncio
    async def test_no_models(self, tmp_path):
        p = tmp_path / "empty.json5"
        _write_config(p, {"models": {"providers": {}}})
        tool = ListModelsTool(config_path=p)
        result = await tool.execute()
        assert "No models" in result


# ── SetModelTool ──────────────────────────────────────────────────────


class TestSetModelTool:
    @pytest.mark.asyncio
    async def test_add_to_existing_provider(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetModelTool(config_path=p)
        result = await tool.execute(
            provider="test", model="new-model", name="New Model",
            reasoning=True, input=["text", "image"], context_window=50000, max_tokens=2048,
        )
        assert "[OK]" in result
        assert "test/new-model" in result

        raw = _read_config(p)
        models = raw["models"]["providers"]["test"]["models"]
        assert any(m["model"] == "new-model" for m in models)
        new = next(m for m in models if m["model"] == "new-model")
        assert new["reasoning"] is True
        assert new["input"] == ["text", "image"]
        assert new["context_window"] == 50000
        assert new["max_tokens"] == 2048

    @pytest.mark.asyncio
    async def test_create_new_provider(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetModelTool(config_path=p)
        result = await tool.execute(
            provider="bailian", model="qwen-max", name="Qwen Max",
            base_url="https://bailian.api/v1", api_key="${BAILIAN_KEY}",
            api="anthropic-messages",
        )
        assert "[OK]" in result

        raw = _read_config(p)
        pcfg = raw["models"]["providers"]["bailian"]
        assert pcfg["base_url"] == "https://bailian.api/v1"
        assert pcfg["api"] == "anthropic-messages"

    @pytest.mark.asyncio
    async def test_new_provider_requires_base_url(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetModelTool(config_path=p)
        result = await tool.execute(provider="newp", model="m", name="M")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_upsert_replaces_existing(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetModelTool(config_path=p)
        result = await tool.execute(provider="test", model="test-model", name="Updated Test Model")
        assert "Updated" in result

        raw = _read_config(p)
        models = raw["models"]["providers"]["test"]["models"]
        matches = [m for m in models if m["model"] == "test-model"]
        assert len(matches) == 1
        assert matches[0]["name"] == "Updated Test Model"

    @pytest.mark.asyncio
    async def test_no_config_path(self, tmp_path):
        tool = SetModelTool(config_path=None)
        result = await tool.execute(provider="t", model="m", name="N")
        assert "Error" in result


# ── RemoveModelTool ───────────────────────────────────────────────────


class TestRemoveModelTool:
    @pytest.mark.asyncio
    async def test_remove_existing_model(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveModelTool(config_path=p)
        result = await tool.execute(ref="test/test-model-2")
        assert "[OK]" in result
        assert "test/test-model-2" in result

        raw = _read_config(p)
        models = raw["models"]["providers"]["test"]["models"]
        assert not any(m["model"] == "test-model-2" for m in models)
        assert any(m["model"] == "test-model" for m in models)

    @pytest.mark.asyncio
    async def test_remove_active_model_auto_switches(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveModelTool(config_path=p)
        result = await tool.execute(ref="test/test-model")
        assert "Switched" in result
        assert "test/test-model-2" in result

        raw = _read_config(p)
        assert raw["active_model"] == "test/test-model-2"

    @pytest.mark.asyncio
    async def test_remove_last_model_clears_provider(self, tmp_path):
        p = tmp_path / "single.json5"
        _write_config(p, {
            "models": {"providers": {"only": {"api_key": "k", "models": [{"model": "one", "name": "One"}]}}},
            "active_model": "only/one",
        })
        tool = RemoveModelTool(config_path=p)
        result = await tool.execute(ref="only/one")
        assert "No models remaining" in result

    @pytest.mark.asyncio
    async def test_invalid_ref(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveModelTool(config_path=p)
        result = await tool.execute(ref="invalid-ref")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_not_found(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveModelTool(config_path=p)
        result = await tool.execute(ref="test/nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_provider_not_found(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveModelTool(config_path=p)
        result = await tool.execute(ref="nope/model")
        assert "not found" in result


# ── SwitchModelTool ───────────────────────────────────────────────────


class TestSwitchModelTool:
    @pytest.mark.asyncio
    async def test_switch_to_existing_model(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchModelTool(config_path=p)
        result = await tool.execute(ref="test/test-model-2")
        assert "[OK]" in result
        assert "test/test-model-2" in result

        raw = _read_config(p)
        assert raw["active_model"] == "test/test-model-2"

    @pytest.mark.asyncio
    async def test_switch_to_same_model(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchModelTool(config_path=p)
        result = await tool.execute(ref="test/test-model")
        assert "[OK]" in result

    @pytest.mark.asyncio
    async def test_switch_to_nonexistent(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchModelTool(config_path=p)
        result = await tool.execute(ref="test/nope")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_invalid_ref_no_slash(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchModelTool(config_path=p)
        result = await tool.execute(ref="badref")
        assert "Error" in result
