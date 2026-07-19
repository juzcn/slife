"""Tests for Slife.tools.system_health — system health check tool."""

import json
from unittest.mock import MagicMock, patch

import pytest

from slife.tools.system_health import (
    _check_runtime_imports,
    _check_embedding_config,
    _check_wechat_status,
    _group_by_component,
    _component_status,
    _build_summary,
    _overall_healthy,
    SystemHealthTool,
)


# ── _group_by_component ───────────────────────────────────────────────


class TestGroupByComponent:
    """Tests for _group_by_component()."""

    def test_empty_list(self):
        assert _group_by_component([]) == {}

    def test_single_entry(self):
        entries = [{"component": "test", "level": "ok"}]
        result = _group_by_component(entries)
        assert "test" in result
        assert len(result["test"]) == 1

    def test_multiple_components(self):
        entries = [
            {"component": "a", "level": "ok"},
            {"component": "b", "level": "warning"},
            {"component": "a", "level": "error"},
        ]
        result = _group_by_component(entries)
        assert len(result) == 2
        assert len(result["a"]) == 2
        assert len(result["b"]) == 1

    def test_entry_without_component_defaults_to_unknown(self):
        entries = [{"level": "ok"}]
        result = _group_by_component(entries)
        assert "unknown" in result


# ── _component_status ─────────────────────────────────────────────────


class TestComponentStatus:
    """Tests for _component_status()."""

    def test_all_ok(self):
        entries = [{"level": "ok"}, {"level": "ok"}]
        assert _component_status(entries) == "ok"

    def test_mixed_ok_and_warning(self):
        entries = [{"level": "ok"}, {"level": "warning"}]
        assert _component_status(entries) == "warning"

    def test_error_wins(self):
        entries = [{"level": "ok"}, {"level": "warning"}, {"level": "error"}]
        assert _component_status(entries) == "error"

    def test_warning_only(self):
        entries = [{"level": "warning"}, {"level": "warning"}]
        assert _component_status(entries) == "warning"

    def test_error_only(self):
        entries = [{"level": "error"}, {"level": "error"}]
        assert _component_status(entries) == "error"

    def test_single_entry(self):
        assert _component_status([{"level": "ok"}]) == "ok"

    def test_empty_entries_defaults_to_ok(self):
        assert _component_status([]) == "ok"


# ── _overall_healthy ──────────────────────────────────────────────────


class TestOverallHealthy:
    """Tests for _overall_healthy()."""

    def test_empty_groups_is_healthy(self):
        assert _overall_healthy({}) is True

    def test_all_ok_is_healthy(self):
        groups = {
            "a": [{"level": "ok"}],
            "b": [{"level": "ok"}, {"level": "ok"}],
        }
        assert _overall_healthy(groups) is True

    def test_one_warning_is_unhealthy(self):
        groups = {
            "a": [{"level": "ok"}],
            "b": [{"level": "warning"}],
        }
        assert _overall_healthy(groups) is False

    def test_one_error_is_unhealthy(self):
        groups = {
            "a": [{"level": "error"}],
        }
        assert _overall_healthy(groups) is False


# ── _build_summary ────────────────────────────────────────────────────


class TestBuildSummary:
    """Tests for _build_summary()."""

    def test_all_ok(self):
        groups = {
            "a": [{"level": "ok"}],
            "b": [{"level": "ok"}],
        }
        summary = _build_summary(groups)
        assert "2 ok" in summary
        assert "warning" not in summary.lower()

    def test_with_warnings(self):
        groups = {
            "a": [{"level": "ok"}],
            "b": [{"level": "warning"}],
            "c": [{"level": "warning"}],
        }
        summary = _build_summary(groups)
        assert "1 ok" in summary
        assert "2 warning(s): b, c" in summary

    def test_with_errors(self):
        groups = {
            "a": [{"level": "error"}],
            "z": [{"level": "error"}],
        }
        summary = _build_summary(groups)
        assert "0 ok" in summary
        assert "2 error(s): a, z" in summary

    def test_mixed_all_levels(self):
        groups = {
            "a": [{"level": "ok"}],
            "b": [{"level": "warning"}, {"level": "ok"}],
            "c": [{"level": "error"}],
        }
        summary = _build_summary(groups)
        assert "1 ok" in summary
        assert "1 warning(s): b" in summary
        assert "1 error(s): c" in summary


# ── _check_runtime_imports ────────────────────────────────────────────


class TestCheckRuntimeImports:
    """Tests for _check_runtime_imports()."""

    def test_returns_list_of_entries(self):
        result = _check_runtime_imports()
        assert isinstance(result, list)
        assert len(result) > 0
        for entry in result:
            assert "component" in entry
            assert entry["component"] == "runtime"
            assert "level" in entry
            assert entry["level"] in ("ok", "warning")
            assert "key" in entry
            assert "value" in entry
            assert "hint" in entry

    def test_ok_when_packages_available(self):
        """Standard Python packages like 'os' or 'json' are always importable."""
        result = _check_runtime_imports()
        ok_entries = [e for e in result if e["level"] == "ok"]
        assert len(ok_entries) > 0


# ── _check_embedding_config ───────────────────────────────────────────


class TestCheckEmbeddingConfig:
    """Tests for _check_embedding_config()."""

    def test_no_config_returns_warning(self):
        # These are imported inside _check_embedding_config via:
        #   from slife.plugins.memory.embedding_config import read_embedding_config
        #   from slife.plugins.memory.embeddings import EmbeddingClient
        with patch(
            "slife.plugins.memory.embedding_config.read_embedding_config",
            return_value=None,
        ):
            with patch("slife.plugins.memory.embeddings.EmbeddingClient"):
                result = _check_embedding_config()
                assert len(result) == 1
                assert result[0]["component"] == "embeddings"
                assert result[0]["level"] == "warning"
                assert result[0]["key"] == "backend"
                assert result[0]["value"] == "none"

    def test_gguf_available(self):
        mock_client = MagicMock()
        mock_client.backend = "gguf"
        mock_client.available = True
        mock_client.dimension = 384
        cfg = {"gguf_path": "/tmp/model.gguf", "model": "bge-small"}

        with patch(
            "slife.plugins.memory.embeddings.EmbeddingClient"
        ) as MockClient:
            MockClient.from_config.return_value = mock_client
            with patch(
                "slife.plugins.memory.embedding_config.read_embedding_config",
                return_value=cfg,
            ):
                result = _check_embedding_config()
                assert len(result) == 1
                assert result[0]["level"] == "ok"
                assert result[0]["value"] == "gguf"
                assert "dim=384" in result[0]["hint"]

    def test_api_available(self):
        mock_client = MagicMock()
        mock_client.backend = "api"
        mock_client.available = True
        mock_client.dimension = 1536
        cfg = {"model": "text-embedding-3-small"}

        with patch(
            "slife.plugins.memory.embeddings.EmbeddingClient"
        ) as MockClient:
            MockClient.from_config.return_value = mock_client
            with patch(
                "slife.plugins.memory.embedding_config.read_embedding_config",
                return_value=cfg,
            ):
                result = _check_embedding_config()
                assert len(result) == 1
                assert result[0]["level"] == "ok"
                assert result[0]["value"] == "api"
                assert "API embeddings ready" in result[0]["hint"]

    def test_gguf_unavailable(self):
        mock_client = MagicMock()
        mock_client.backend = "gguf"
        mock_client.available = False
        cfg = {"gguf_path": "/tmp/model.gguf", "model": "bge-small"}

        with patch(
            "slife.plugins.memory.embeddings.EmbeddingClient"
        ) as MockClient:
            MockClient.from_config.return_value = mock_client
            with patch(
                "slife.plugins.memory.embedding_config.read_embedding_config",
                return_value=cfg,
            ):
                result = _check_embedding_config()
                assert len(result) == 1
                assert result[0]["level"] == "warning"
                assert result[0]["value"] == "gguf"
                assert "NOT installed" in result[0]["hint"]

    def test_api_unavailable(self):
        mock_client = MagicMock()
        mock_client.backend = "api"
        mock_client.available = False
        cfg = {"model": "text-embedding-3-small"}

        with patch(
            "slife.plugins.memory.embeddings.EmbeddingClient"
        ) as MockClient:
            MockClient.from_config.return_value = mock_client
            with patch(
                "slife.plugins.memory.embedding_config.read_embedding_config",
                return_value=cfg,
            ):
                result = _check_embedding_config()
                assert len(result) == 1
                assert result[0]["level"] == "warning"
                assert result[0]["value"] == "api"
                assert "NOT installed" in result[0]["hint"]

    def test_unknown_backend_unavailable(self):
        mock_client = MagicMock()
        mock_client.backend = "unknown_backend"
        mock_client.available = False

        with patch(
            "slife.plugins.memory.embeddings.EmbeddingClient"
        ) as MockClient:
            MockClient.from_config.return_value = mock_client
            with patch(
                "slife.plugins.memory.embedding_config.read_embedding_config",
                return_value={"model": "x"},
            ):
                result = _check_embedding_config()
                assert len(result) == 1
                assert result[0]["level"] == "warning"
                assert result[0]["value"] == "unknown"


# ── _check_wechat_status ──────────────────────────────────────────────


class TestCheckWechatStatus:
    """Tests for _check_wechat_status()."""

    def test_config_none_returns_unknown(self):
        """When config is None and slife.json5 doesn't exist, returns unknown."""
        # Config is imported inside _check_wechat_status via:
        #   from slife.config import Config, parse_cli_agent
        with patch("slife.config.Config") as MockConfig:
            MockConfig.from_json5.side_effect = Exception("no config")
            with patch("pathlib.Path.exists", return_value=False):
                result = _check_wechat_status(config=None)
                assert len(result) == 1
                assert result[0]["component"] == "wechat"
                assert result[0]["key"] == "enabled"
                assert result[0]["value"] == "unknown"

    def test_disabled_in_config(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = False

        result = _check_wechat_status(config=mock_config)
        assert len(result) == 1
        assert result[0]["value"] == "disabled"

    def test_not_logged_in(self):
        mock_config = MagicMock()
        mock_config.agent_id = "testuser"
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        # load_wechat_config is imported inside _check_wechat_status via:
        #   from slife.plugins.wechat.config import load_wechat_config
        with patch(
            "slife.plugins.wechat.config.load_wechat_config",
            return_value={},
        ):
            result = _check_wechat_status(config=mock_config)
            assert len(result) == 1
            assert result[0]["key"] == "status"
            assert result[0]["value"] == "not_logged_in"

    def test_session_expired(self):
        import time
        mock_config = MagicMock()
        mock_config.agent_id = "testuser"
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        # Session saved 24 hours ago — expired
        old_time = time.time() - (24 * 3600)
        with patch(
            "slife.plugins.wechat.config.load_wechat_config",
            return_value={"bot_token": "tok", "saved_at": old_time},
        ):
            result = _check_wechat_status(config=mock_config)
            assert len(result) == 1
            assert result[0]["value"] == "session_expired"

    def test_logged_in(self):
        import time
        mock_config = MagicMock()
        mock_config.agent_id = "testuser"
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        # Session saved now — valid
        now = time.time()
        with patch(
            "slife.plugins.wechat.config.load_wechat_config",
            return_value={"bot_token": "tok", "saved_at": now},
        ):
            result = _check_wechat_status(config=mock_config)
            assert len(result) == 1
            assert result[0]["value"] == "logged_in"

    def test_config_load_exception_falls_back_to_default(self):
        """When config loading fails, _check_wechat_status falls back
        to trying to load config from disk itself."""
        with patch(
            "slife.config.Config"
        ) as MockConfig:
            MockConfig.from_json5.side_effect = Exception("parse error")
            with patch("pathlib.Path.exists", return_value=True):
                result = _check_wechat_status(config=None)
                # If loading throws, config stays None, so we get "unknown"
                assert len(result) == 1
                assert result[0]["value"] == "unknown"


# ── SystemHealthTool ──────────────────────────────────────────────────


class TestSystemHealthToolMetadata:
    """Tests for SystemHealthTool metadata."""

    def test_name(self):
        tool = SystemHealthTool()
        assert tool.name == "system_health"

    def test_description(self):
        tool = SystemHealthTool()
        assert "system health" in tool.description.lower()

    def test_parameters_empty(self):
        tool = SystemHealthTool()
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == []


class TestSystemHealthToolExecute:
    """Tests for SystemHealthTool.execute()."""

    @pytest.mark.asyncio
    async def test_execute_returns_json(self):
        tool = SystemHealthTool()
        with patch(
            "slife.tools.system_health.get_startup_records",
            return_value=[],
        ):
            with patch(
                "slife.tools.system_health._check_wechat_status",
                return_value=[],
            ):
                result = await tool.execute()
                parsed = json.loads(result)
                assert "healthy" in parsed
                assert "summary" in parsed
                assert "components" in parsed

    @pytest.mark.asyncio
    async def test_execute_includes_startup_records(self):
        tool = SystemHealthTool()
        startup_entries = [
            {
                "component": "startup",
                "level": "ok",
                "key": "bootstrap",
                "value": "done",
                "hint": "all good",
            }
        ]
        with patch(
            "slife.tools.system_health.get_startup_records",
            return_value=startup_entries,
        ):
            with patch(
                "slife.tools.system_health._check_wechat_status",
                return_value=[],
            ):
                result = await tool.execute()
                parsed = json.loads(result)
                assert "startup" in parsed["components"]

    @pytest.mark.asyncio
    async def test_execute_with_warnings_is_not_healthy(self):
        tool = SystemHealthTool()
        startup_entries = [
            {"component": "db", "level": "warning", "key": "schema",
             "value": "migrated", "hint": "check logs"},
        ]
        with patch(
            "slife.tools.system_health.get_startup_records",
            return_value=startup_entries,
        ):
            with patch(
                "slife.tools.system_health._check_wechat_status",
                return_value=[],
            ):
                result = await tool.execute()
                parsed = json.loads(result)
                assert parsed["healthy"] is False
                assert "warning" in parsed["summary"].lower()

    @pytest.mark.asyncio
    async def test_execute_all_healthy(self):
        tool = SystemHealthTool()
        startup_entries = [
            {"component": "a", "level": "ok"},
            {"component": "b", "level": "ok"},
        ]
        with patch(
            "slife.tools.system_health.get_startup_records",
            return_value=startup_entries,
        ):
            with patch(
                "slife.tools.system_health._check_wechat_status",
                return_value=[],
            ):
                with patch(
                    "slife.tools.system_health._check_runtime_imports",
                    return_value=[],
                ):
                    with patch(
                        "slife.tools.system_health._check_embedding_config",
                        return_value=[],
                    ):
                        result = await tool.execute()
                        parsed = json.loads(result)
                        assert parsed["healthy"] is True
                        # Summary may include runtime import checks (which are ok)
                        assert "ok" in parsed["summary"].lower()
