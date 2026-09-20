"""Tests for embeddings native tools — list / set / switch / remove / enable.

Each provider is one OpenAI-compatible endpoint (base_url + api_key + single
model); tools manage providers, not a per-provider model registry.
"""

import pytest; pytestmark = pytest.mark.unit

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from tests.conftest import dump_config, load_config_text
from slife.tools.embeddings import (
    ListEmbeddingsTool,
    SetEmbeddingsTool, SwitchEmbeddingsTool, RemoveEmbeddingsTool,
    EnableEmbeddingsTool,
)


# ── Helpers ───────────────────────────────────────────────────────────


def _write_config(path: Path, data: dict) -> None:
    path.write_text(dump_config(data), encoding="utf-8")


def _read_config(path: Path) -> dict:
    return load_config_text(path.read_text(encoding="utf-8"))


def _make_path(tmp_path: Path) -> Path:
    p = tmp_path / "slife.yaml"
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
                    "model": "text-embedding-3-small",
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
    async def test_lists_providers(self, tmp_path):
        p = _make_path(tmp_path)
        tool = ListEmbeddingsTool(config_path=p)
        result = await tool.execute()
        assert "local-embed" in result
        assert "openai" in result
        assert "http://127.0.0.1:8000/v1" in result
        assert "text-embedding-3-small" in result
        assert "★" in result            # active marker
        assert "local-embed" in result

    @pytest.mark.asyncio
    async def test_no_embeddings(self, tmp_path):
        p = tmp_path / "empty.yaml"
        _write_config(p, {"embeddings": {"providers": {}}})
        tool = ListEmbeddingsTool(config_path=p)
        result = await tool.execute()
        assert "No embeddings" in result


# ── SetEmbeddingsTool ─────────────────────────────────────────────────


class TestSetEmbeddingsTool:
    @pytest.mark.asyncio
    async def test_creates_new_provider(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(
            provider="bedrock", model="cohere.embed",
            base_url="https://bedrock.example/v1", api_key="sk-b",
        )
        assert "Created" in result
        raw = _read_config(p)
        emb = raw["embeddings"]
        assert "bedrock" in emb["providers"]
        assert emb["providers"]["bedrock"]["base_url"] == "https://bedrock.example/v1"
        assert emb["providers"]["bedrock"]["model"] == "cohere.embed"

    @pytest.mark.asyncio
    async def test_updates_existing_provider_model(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(
            provider="openai", model="text-embedding-3-large",
        )
        assert "Updated" in result
        raw = _read_config(p)
        pcfg = raw["embeddings"]["providers"]["openai"]
        assert pcfg["model"] == "text-embedding-3-large"
        assert "dim" not in pcfg

    @pytest.mark.asyncio
    async def test_provider_missing_requires_base_url(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetEmbeddingsTool(config_path=p)
        result = await tool.execute(provider="nope", model="m")
        assert "does not exist" in result

    @pytest.mark.asyncio
    async def test_first_set_becomes_active(self, tmp_path):
        p = tmp_path / "slife.yaml"
        _write_config(p, {"embeddings": {"providers": {}}})
        tool = SetEmbeddingsTool(config_path=p)
        _no_reload(tool)
        await tool.execute(provider="p1", model="m1",
                           base_url="http://x/v1", api_key="k")
        raw = _read_config(p)
        assert raw["embeddings"]["active_model"] == "p1"

    @pytest.mark.asyncio
    async def test_hot_reload_calls_plugins(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SetEmbeddingsTool(config_path=p)
        # AsyncMock — the MCP client's call_tool is async; a sync MagicMock
        # would silently mask the un-awaited-call bug (it returns a value
        # without needing await), exactly how the original defect got through.
        memdb_client = MagicMock()
        memdb_client.call_tool = AsyncMock(return_value='{"status": "ok"}')
        memfiles_client = MagicMock()
        memfiles_client.call_tool = AsyncMock(return_value='{"status": "ok"}')
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
    async def test_switches_provider(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(provider="openai")
        assert "Switched" in result
        raw = _read_config(p)
        assert raw["embeddings"]["active_model"] == "openai"

    @pytest.mark.asyncio
    async def test_provider_not_found(self, tmp_path):
        p = _make_path(tmp_path)
        tool = SwitchEmbeddingsTool(config_path=p)
        result = await tool.execute(provider="nope")
        assert "not found" in result


# ── RemoveEmbeddingsTool ──────────────────────────────────────────────


class TestRemoveEmbeddingsTool:
    @pytest.mark.asyncio
    async def test_removes_provider(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveEmbeddingsTool(config_path=p)
        _no_reload(tool)
        result = await tool.execute(provider="openai")
        assert "Removed" in result
        raw = _read_config(p)
        assert "openai" not in raw["embeddings"]["providers"]

    @pytest.mark.asyncio
    async def test_cannot_remove_active(self, tmp_path):
        p = _make_path(tmp_path)
        tool = RemoveEmbeddingsTool(config_path=p)
        result = await tool.execute(provider="local-embed")
        assert "cannot remove the active" in result

    @pytest.mark.asyncio
    async def test_removing_last_provider_drops_section(self, tmp_path):
        p = tmp_path / "slife.yaml"
        _write_config(p, {
            "embeddings": {
                "providers": {"p1": {"base_url": "http://x/v1"}},
                "active_model": "",
            },
        })
        tool = RemoveEmbeddingsTool(config_path=p)
        _no_reload(tool)
        await tool.execute(provider="p1")
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


# ── Hot reload: which indexes follow the section ──────────────────────


class TestHotReloadTargets:
    """The reload loop is a manifest over the plugin specs, plus the host's own
    index — the tool catalog's drainer, which is not a plugin and used to be
    left out of the hand-written pair, so a provider switch left the tool index
    embedding against the replaced endpoint until restart.
    """

    @pytest.mark.asyncio
    async def test_the_loop_is_driven_by_the_specs_not_a_name_list(
        self, tmp_path, monkeypatch,
    ):
        """A plugin declares its reload tool; the loop needs no edit for it.

        This is the property that failed: the set of semantic indexes lived in
        a hard-written tuple inside the reload helper, so an index anywhere
        else (the host's own) had to be remembered separately — and was not.
        """
        from types import SimpleNamespace

        from slife.plugins import spec as spec_module
        from slife.plugins.spec import PluginSpec
        from slife.tools import embeddings as emb_tools

        client = MagicMock()
        client.call_tool = AsyncMock(return_value='{"status": "rebuilt"}')
        # Patch the registry the helper reads (it imports it at call time).
        monkeypatch.setattr(spec_module, "PLUGIN_SPECS", {
            "third-index": PluginSpec(
                "third-index", "some.module", ctx_field="third_client",
                semantic_reload_tool="__third_reload_semantic",
            ),
        }, raising=False)

        ctx = SimpleNamespace(third_client=client, catalog=None, config=None)
        notes = await emb_tools._hot_reload(ctx, enabled=True)
        client.call_tool.assert_called_once_with(
            "__third_reload_semantic", {"enabled": True},
        )
        assert "third-index: rebuilt" in notes

    @pytest.mark.asyncio
    async def test_a_declared_index_with_no_client_says_restart_to_apply(
        self, monkeypatch,
    ):
        from types import SimpleNamespace

        from slife.plugins import spec as spec_module
        from slife.plugins.spec import PluginSpec
        from slife.tools import embeddings as emb_tools

        monkeypatch.setattr(spec_module, "PLUGIN_SPECS", {
            "memdb": PluginSpec(
                "memdb", "m", ctx_field="memdb_client",
                semantic_reload_tool="__memory_reload_semantic",
            ),
        }, raising=False)
        ctx = SimpleNamespace(memdb_client=None, catalog=None, config=None)
        notes = await emb_tools._hot_reload(ctx, enabled=True)
        assert "memdb: plugin not connected — restart to apply" in notes

    @pytest.mark.asyncio
    async def test_the_tool_index_is_reloaded_with_the_section_it_was_given(
        self,
    ):
        """The host's index is rebuilt against the NEW section, in-process.

        The manager holds the endpoint it was constructed with, so the section
        has to be handed over: re-reading the file inside the reload would also
        resolve a different path than the tool wrote under ``--config``.
        """
        from types import SimpleNamespace

        from slife.tools import embeddings as emb_tools

        manager = MagicMock()
        manager.reload = AsyncMock(return_value={"status": "ok"})
        manager.state = "indexing"
        catalog = SimpleNamespace(semantic_manager=manager)
        config = SimpleNamespace(embeddings_config="OLD")
        ctx = SimpleNamespace(catalog=catalog, config=config)

        section = {"providers": {"p": {"base_url": "http://x/v1"}}, "active_model": "p"}
        notes = await emb_tools._apply_embeddings_change(ctx, section, enabled=True)

        manager.reload.assert_awaited_once()
        assert manager.reload.await_args.args[0] is config.embeddings_config
        assert "tool catalog: indexing" in notes
        # …and the process's config now describes the section that was written,
        # which is what system_health and the NEXT subagent's config see.
        assert config.embeddings_config.active_model == "p"

    @pytest.mark.asyncio
    async def test_switching_semantic_search_off_stops_the_tool_index_too(self):
        """``embeddings_enable(false)`` must stop every index — the tool index
        kept draining new tool rows while the two plugins stopped, which is the
        same asymmetry seen from the other side."""
        from types import SimpleNamespace

        from slife.tools import embeddings as emb_tools

        manager = MagicMock()
        manager.disable = AsyncMock()
        ctx = SimpleNamespace(
            catalog=SimpleNamespace(semantic_manager=manager),
            config=SimpleNamespace(embeddings_config="OLD"),
        )
        notes = await emb_tools._apply_embeddings_change(ctx, {}, enabled=False)
        manager.disable.assert_awaited_once()
        assert "tool catalog: disabled" in notes

    @pytest.mark.asyncio
    async def test_a_worker_owns_no_index_and_says_nothing_about_one(self):
        """A worker queries its parent's index; it has nothing to reload, and
        "restart to apply" would promise something its restart cannot give."""
        from types import SimpleNamespace

        from slife.tools import embeddings as emb_tools

        ctx = SimpleNamespace(
            catalog=SimpleNamespace(semantic_manager=None),
            config=SimpleNamespace(embeddings_config="OLD"),
        )
        notes = await emb_tools._apply_embeddings_change(ctx, {}, enabled=True)
        assert "tool catalog" not in notes
