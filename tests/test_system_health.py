"""Tests for Slife.tools.system_health — system health check tool."""

import pytest; pytestmark = pytest.mark.integration


import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.tools.system import (
    check_memdb,
    check_wechat,
    check_memfiles,
    check_local_embed,
    check_mcp,
    check_a2a,
    check_media,
    CheckMemdbTool,
    CheckWechatTool,
    CheckSharefileTool,
    CheckMemfilesTool,
    CheckLocalEmbedTool,
    CheckMediaTool,
    _group_by_component,
    _component_status,
    _build_summary,
    _overall_healthy,
    _dedupe_records,
    SystemHealthTool,
    CheckWatchdogTool,
    CheckMcpTool,
    CheckA2aTool,
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


# ── _dedupe_records ───────────────────────────────────────────────────


class TestDedupeMcpRecords:
    """MCP startup records vs live check_mcp entries must not double-report.

    The contradiction scenario: a slow cold start records
    ``(mcp_server, warning)`` in the startup store; the background reconnect
    later succeeds and the live ``check_mcp`` says the server is connected.
    system_health merges both, so without dedup the stale warning survives
    and the report contradicts itself.
    """

    @staticmethod
    def _startup(name, level="warning", component="mcp_server"):
        return {"component": component, "level": level, "key": name,
                "value": "connect_pending", "hint": "retrying"}

    @staticmethod
    def _live(name, level="ok", component="mcp_servers"):
        return {"component": component, "level": level, "key": name,
                "value": "connected (2 tools loaded)", "hint": "all good"}

    def test_live_ok_supersedes_stale_warning(self):
        startup = [self._startup("fs"), self._startup("github")]
        live = [self._live("fs")]
        merged = _dedupe_records(startup, live)

        # fs: startup warning dropped, live ok kept.
        assert [e["key"] for e in merged if e["component"] == "mcp_servers"] == ["fs"]
        assert all(e["level"] == "ok" for e in merged if e["key"] == "fs")
        # github: not covered by live — startup record preserved.
        assert any(e["key"] == "github" and e["level"] == "warning" for e in merged)

    def test_live_warning_still_supersedes_startup_ok(self):
        """The live report is authoritative in BOTH directions: a server the
        live check says is disconnected must not be masked by a stale
        "connected" startup record."""
        merged = _dedupe_records(
            [self._startup("fs", level="ok")],
            [self._live("fs", level="warning")],
        )
        entries = [e for e in merged if e["key"] == "fs"]
        assert len(entries) == 1
        assert entries[0]["level"] == "warning"

    def test_dedupe_matches_on_name_not_on_components(self):
        """Component prefixes must not fool the name-based match: a startup
        ``mcp_server`` record for ``fs`` is covered by a live ``mcp_servers``
        record for ``fs``, and vice versa."""
        merged = _dedupe_records(
            [self._startup("fs")],
            [self._live("fs")],
        )
        assert [e["key"] for e in merged] == ["fs"]

    def test_keeps_startup_records_with_no_live_counterpart(self):
        startup = [self._startup("only_startup")]
        merged = _dedupe_records(startup, [])
        assert len(merged) == 1
        assert merged[0]["key"] == "only_startup"

    def test_no_mutation_of_inputs(self):
        startup = [self._startup("fs")]
        live = [self._live("fs")]
        _dedupe_records(startup, live)
        assert len(startup) == 1
        assert len(live) == 1


class TestDedupeWatchdogRecords:
    """Startup watchdog records are re-reported (deduplicated, latest-per-key)
    by check_watchdog — merging must not double-report local-embed / mcp."""

    @staticmethod
    def _entry(name, level="ok"):
        return {"component": "watchdog", "level": level, "key": name,
                "value": "running", "hint": "auto-restart active"}

    def test_startup_and_live_do_not_double_report(self):
        startup = [self._entry("local-embed"), self._entry("mcp")]
        live = [self._entry("local-embed"), self._entry("mcp")]
        merged = _dedupe_records(startup, live)
        watchdog = [e for e in merged if e["component"] == "watchdog"]
        assert [e["key"] for e in watchdog] == ["local-embed", "mcp"]
        assert len(watchdog) == 2  # no duplicates

    def test_multiple_startup_records_collapse_to_latest_live(self):
        """Several historical watchdog records for one plugin collapse to the
        single (latest) live entry instead of being reported per-record."""
        startup = [self._entry("mcp", level="ok"), self._entry("mcp", level="warning")]
        live = [self._entry("mcp", level="warning")]
        merged = _dedupe_records(startup, live)
        watchdog = [e for e in merged if e["component"] == "watchdog"]
        assert [e["key"] for e in watchdog] == ["mcp"]
        assert len(watchdog) == 1


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


# ── check_memdb ────────────────────────────────────────────────


class TestCheckMemdb:
    """Tests for check_memdb() — async probe of the memdb plugin's __check."""

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        entries = await check_memdb()
        assert entries[0]["component"] == "memdb"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "offline"
        assert "not connected" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_plugin_passthrough(self):
        payload = [
            {"component": "memdb", "level": "ok", "key": "db",
             "value": "1.0 MB", "hint": "Database ready"},
            {"component": "memdb", "level": "ok", "key": "embedding",
             "value": "ready", "hint": "Semantic search ready"},
        ]
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        entries = await check_memdb(client=client)
        client.call_tool.assert_called_once_with("__check")
        assert entries == payload

    @pytest.mark.asyncio
    async def test_plugin_error_reports_warning(self):
        client = MagicMock()
        client.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        entries = await check_memdb(client=client)
        assert entries[0]["level"] == "warning"
        assert "boom" in entries[0]["hint"]


# ── check_wechat ──────────────────────────────────────────────


class TestCheckWechatStatus:
    """Tests for check_wechat() — config enabled gate + async __check probe."""

    @pytest.mark.asyncio
    async def test_config_none_returns_unknown(self):
        """When config is None and slife.json5 doesn't exist, returns unknown."""
        with patch("slife.config.Config") as MockConfig:
            MockConfig.from_json5.side_effect = Exception("no config")
            with patch("pathlib.Path.exists", return_value=False):
                result = await check_wechat(config=None)
                assert len(result) == 1
                assert result[0]["component"] == "wechat"
                assert result[0]["key"] == "enabled"
                assert result[0]["value"] == "unknown"

    @pytest.mark.asyncio
    async def test_disabled_in_config(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = False

        result = await check_wechat(config=mock_config)
        assert len(result) == 1
        assert result[0]["value"] == "disabled"

    @pytest.mark.asyncio
    async def test_enabled_but_client_unavailable(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        result = await check_wechat(config=mock_config)
        assert len(result) == 1
        assert result[0]["component"] == "wechat"
        assert result[0]["value"] == "offline"
        assert "not connected" in result[0]["hint"]

    @pytest.mark.asyncio
    async def test_enabled_probes_plugin(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        payload = [{"component": "wechat", "level": "ok", "key": "status",
                    "value": "logged_in", "hint": "WeChat logged in"}]
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_wechat(client=client, config=mock_config)
        client.call_tool.assert_called_once_with("__check")
        assert result == payload

    @pytest.mark.asyncio
    async def test_plugin_error_reports_warning(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        client = MagicMock()
        client.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        result = await check_wechat(client=client, config=mock_config)
        assert result[0]["level"] == "warning"
        assert "boom" in result[0]["hint"]

    @pytest.mark.asyncio
    async def test_config_load_exception_falls_back_to_default(self):
        """When config loading fails, check_wechat falls back
        to trying to load config from disk itself."""
        with patch(
            "slife.config.Config"
        ) as MockConfig:
            MockConfig.from_json5.side_effect = Exception("parse error")
            with patch("pathlib.Path.exists", return_value=True):
                result = await check_wechat(config=None)
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
        assert "health report" in tool.description.lower()

    def test_parameters_empty(self):
        tool = SystemHealthTool()
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == []


class TestSystemHealthToolExecute:
    """Tests for SystemHealthTool.execute()."""

    @pytest.mark.asyncio
    async def test_execute_returns_json(self):
        tool = SystemHealthTool()
        with patch("slife.tools.system.get_startup_records", return_value=[]), \
             patch("slife.tools.system.check_memdb", return_value=[]), \
             patch("slife.tools.system.check_wechat", return_value=[]), \
             patch("slife.tools.system.check_memfiles", return_value=[]), \
             patch("slife.tools.system.check_local_embed", return_value=[]), \
             patch("slife.tools.system.check_sharefile", return_value=[]), \
             patch("slife.tools.system.check_mcp", return_value=[]), \
             patch("slife.tools.system.check_a2a", return_value=[]), \
             patch("slife.tools.system.check_media", return_value=[]):
            result = await tool.execute()
            parsed = json.loads(result)
            assert "healthy" in parsed
            assert "summary" in parsed
            assert "components" in parsed

    @pytest.mark.asyncio
    async def test_execute_includes_startup_records(self):
        tool = SystemHealthTool()
        startup_entries = [
            {"component": "startup", "level": "ok", "key": "bootstrap",
             "value": "done", "hint": "all good"},
        ]
        with patch("slife.tools.system.get_startup_records", return_value=startup_entries), \
             patch("slife.tools.system.check_memdb", return_value=[]), \
             patch("slife.tools.system.check_wechat", return_value=[]), \
             patch("slife.tools.system.check_memfiles", return_value=[]), \
             patch("slife.tools.system.check_local_embed", return_value=[]), \
             patch("slife.tools.system.check_sharefile", return_value=[]), \
             patch("slife.tools.system.check_mcp", return_value=[]), \
             patch("slife.tools.system.check_a2a", return_value=[]), \
             patch("slife.tools.system.check_media", return_value=[]):
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
        with patch("slife.tools.system.get_startup_records", return_value=startup_entries), \
             patch("slife.tools.system.check_memdb", return_value=[]), \
             patch("slife.tools.system.check_wechat", return_value=[]), \
             patch("slife.tools.system.check_memfiles", return_value=[]), \
             patch("slife.tools.system.check_local_embed", return_value=[]), \
             patch("slife.tools.system.check_sharefile", return_value=[]), \
             patch("slife.tools.system.check_mcp", return_value=[]), \
             patch("slife.tools.system.check_a2a", return_value=[]), \
             patch("slife.tools.system.check_media", return_value=[]):
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
            "slife.tools.system.get_startup_records",
            return_value=startup_entries,
        ), patch(
            "slife.tools.system.check_memdb", return_value=[],
        ), patch(
            "slife.tools.system.check_wechat", return_value=[],
        ), patch(
            "slife.tools.system.check_memfiles", return_value=[],
        ), patch(
            "slife.tools.system.check_local_embed", return_value=[],
        ), patch(
            "slife.tools.system.check_sharefile", return_value=[],
        ), patch(
            "slife.tools.system.check_mcp", return_value=[],
        ), patch(
            "slife.tools.system.check_a2a", return_value=[],
        ), patch(
            "slife.tools.system.check_media", return_value=[],
        ):
            result = await tool.execute()
            parsed = json.loads(result)
            assert parsed["healthy"] is True
            assert "ok" in parsed["summary"].lower()

    @pytest.mark.asyncio
    async def test_recovered_mcp_server_is_not_contradictory(self):
        """A server that was slow to cold-start (startup warning recorded) but
        is now connected (live check ok) must not keep the report unhealthy —
        the stale startup record is superseded by the live result."""
        tool = SystemHealthTool()
        startup_entries = [
            {"component": "mcp_server", "level": "warning", "key": "fs",
             "value": "connect_pending",
             "hint": "enabled but not yet connected; retrying in background."},
        ]
        live_entries = [
            {"component": "mcp_servers", "level": "ok", "key": "fs",
             "value": "connected (5 tools loaded)",
             "hint": "MCP server 'fs': connected via stdio, 5 tools loaded."},
        ]
        with patch(
            "slife.tools.system.get_startup_records",
            return_value=startup_entries,
        ), patch(
            "slife.tools.system.check_memdb", return_value=[],
        ), patch(
            "slife.tools.system.check_wechat", return_value=[],
        ), patch(
            "slife.tools.system.check_memfiles", return_value=[],
        ), patch(
            "slife.tools.system.check_local_embed", return_value=[],
        ), patch(
            "slife.tools.system.check_sharefile", return_value=[],
        ), patch(
            "slife.tools.system.check_mcp", return_value=live_entries,
        ), patch(
            "slife.tools.system.check_a2a", return_value=[],
        ), patch(
            "slife.tools.system.check_media", return_value=[],
        ):
            result = await tool.execute()
            parsed = json.loads(result)
            # One mcp_servers entry (live), no stale mcp_server warning.
            assert parsed["healthy"] is True
            mcp = parsed["components"].get("mcp_servers", {})
            assert mcp.get("status") == "ok"
            assert "mcp_server" not in parsed["components"]

    @pytest.mark.asyncio
    async def test_execute_no_duplicate_watchdog_entries(self):
        """Regression (BUGS.md #6): startup watchdog records are re-reported
        by check_watchdog — the watchdog group must show each plugin once,
        not local-embed/mcp duplicated."""
        tool = SystemHealthTool()
        startup_entries = [
            {"component": "watchdog", "level": "ok", "key": "local-embed",
             "value": "running", "hint": "auto-restart active"},
            {"component": "watchdog", "level": "ok", "key": "mcp",
             "value": "running", "hint": "auto-restart active"},
        ]
        live_watchdog = [
            {"component": "watchdog", "level": "ok", "key": "local-embed",
             "value": "running", "hint": "auto-restart active"},
            {"component": "watchdog", "level": "ok", "key": "mcp",
             "value": "running", "hint": "auto-restart active"},
        ]
        with patch("slife.tools.system.get_startup_records", return_value=startup_entries), \
             patch("slife.tools.system.check_memdb", return_value=[]), \
             patch("slife.tools.system.check_wechat", return_value=[]), \
             patch("slife.tools.system.check_memfiles", return_value=[]), \
             patch("slife.tools.system.check_local_embed", return_value=[]), \
             patch("slife.tools.system.check_sharefile", return_value=[]), \
             patch("slife.tools.system.check_mcp", return_value=[]), \
             patch("slife.tools.system.check_a2a", return_value=[]), \
             patch("slife.tools.system.check_media", return_value=[]), \
             patch("slife.tools.system.check_watchdog", return_value=live_watchdog):
            result = await tool.execute()
        parsed = json.loads(result)
        wd = parsed["components"]["watchdog"]["entries"]
        assert [e["key"] for e in wd] == ["local-embed", "mcp"]
        assert len(wd) == 2  # no duplicates


# ── CheckWatchdogTool / CheckMcpTool ───────────────────────────────────


class TestCheckWatchdogTool:
    """Tests for CheckWatchdogTool metadata and execute."""

    def test_metadata(self):
        tool = CheckWatchdogTool()
        assert tool.name == "check_watchdog"
        assert "watchdog" in tool.description.lower()
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == []

    @pytest.mark.asyncio
    async def test_execute_returns_json(self):
        tool = CheckWatchdogTool()
        with patch("slife.tools.system.check_watchdog", return_value=[
            {"component": "watchdog", "level": "ok", "key": "plugin_a",
             "value": "running", "hint": "auto-restart active"},
        ]):
            result = await tool.execute()
            parsed = json.loads(result)
            assert parsed[0]["component"] == "watchdog"
            assert parsed[0]["value"] == "running"


class TestCheckMcpTool:
    """Tests for CheckMcpTool metadata and execute."""

    def test_metadata(self):
        tool = CheckMcpTool()
        assert tool.name == "check_mcp"
        assert "mcp" in tool.description.lower()
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == []
        assert "server" in tool.parameters["properties"]
        assert tool.parameters["properties"]["server"]["default"] == ""

    @pytest.mark.asyncio
    async def test_execute_returns_json(self):
        tool = CheckMcpTool()
        with patch("slife.tools.system.check_mcp",
                   new=AsyncMock(return_value=[
                       {"component": "mcp_servers", "level": "ok",
                        "key": "server_a", "value": "connected (5 tools loaded)",
                        "hint": "all good"},
                   ])):
            result = await tool.execute()
            parsed = json.loads(result)
            assert parsed[0]["component"] == "mcp_servers"
            assert parsed[0]["value"] == "connected (5 tools loaded)"

    @pytest.mark.asyncio
    async def test_execute_forwards_server_param(self):
        tool = CheckMcpTool()
        mock = AsyncMock(return_value=[])
        with patch("slife.tools.system.check_mcp", new=mock):
            await tool.execute(server="github")
        mock.assert_awaited_once_with(server="github", client=None)


class _FakeMcpClient:
    """Minimal stand-in for the slife-mcp wrapper client."""

    def __init__(self, payload):
        self._payload = payload

    async def call_tool(self, name, arguments=None):
        assert name == "__check"
        return json.dumps(self._payload)


class TestCheckMcpFunction:
    """Tests for check_mcp() server filtering."""

    @staticmethod
    def _client(payload):
        return _FakeMcpClient(payload)

    @staticmethod
    def _server(name, state="running"):
        return {
            "name": name,
            "state": state,
            "status": "connected" if state == "running" else "failed",
            "enabled": True,
            "tool_count": 2 if state == "running" else 0,
            "error": "" if state == "running" else "boom",
            "transport": "stdio",
        }

    @pytest.mark.asyncio
    async def test_checks_all_by_default(self):
        payload = [self._server("fs"), self._server("github", state="stopped")]
        entries = await check_mcp(client=self._client(payload))
        assert [e["key"] for e in entries] == ["fs", "github"]

    @pytest.mark.asyncio
    async def test_checks_single_server(self):
        payload = [self._server("fs"), self._server("github", state="stopped")]
        entries = await check_mcp(server="github", client=self._client(payload))
        assert len(entries) == 1
        assert entries[0]["key"] == "github"
        assert entries[0]["level"] == "warning"

    @pytest.mark.asyncio
    async def test_server_not_found(self):
        payload = [self._server("fs")]
        entries = await check_mcp(server="nope", client=self._client(payload))
        assert len(entries) == 1
        assert entries[0]["key"] == "nope"
        assert entries[0]["value"] == "not_found"
        assert entries[0]["level"] == "warning"

    @pytest.mark.asyncio
    async def test_server_not_found_when_no_servers(self):
        entries = await check_mcp(server="nope", client=self._client([]))
        assert len(entries) == 1
        assert entries[0]["value"] == "not_found"

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        entries = await check_mcp()
        assert entries[0]["value"] == "client_unavailable"
        assert entries[0]["level"] == "warning"


class _FakeA2aClient:
    """Minimal stand-in for the a2a plugin MCP client."""

    def __init__(self, payload):
        self._payload = payload

    async def call_tool(self, name, arguments=None):
        assert name == "__check"
        return json.dumps(self._payload)


class TestCheckA2aFunction:
    """Tests for check_a2a()."""

    @staticmethod
    def _status(**overrides):
        data = {
            "enabled": True, "connected": True, "agent_name": "slife",
            "status": "idle", "broker": "localhost:1883",
            "peers": [], "queued": {"tasks": 0, "presence": 0, "cancellations": 0},
        }
        data.update(overrides)
        return data

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        entries = await check_a2a()
        assert entries[0]["component"] == "a2a"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "unavailable"
        assert "No active MQTT port" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_plugin_disconnected(self):
        """Mosquitto down → no active MQTT port → a2a unavailable."""
        client = _FakeA2aClient(self._status(connected=False))
        entries = await check_a2a(client=client)
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "unavailable"
        assert "No active MQTT port" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_connected_no_peers(self):
        client = _FakeA2aClient(self._status())
        entries = await check_a2a(client=client)
        assert len(entries) == 1
        assert entries[0]["level"] == "ok"
        assert entries[0]["value"] == "connected"
        assert "localhost:1883" in entries[0]["hint"]
        assert "You have no peers online." in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_connected_with_peers(self):
        client = _FakeA2aClient(self._status(peers=[
            {"agent_name": "peer-1", "status": "idle"},
        ]))
        entries = await check_a2a(client=client)
        assert entries[0]["level"] == "ok"
        assert entries[0]["peers"][0]["agent_name"] == "peer-1"
        assert "You have 1 peer: peer-1." in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_connected_multiple_peers_plural(self):
        """Two peers pluralise the hint and list both names."""
        client = _FakeA2aClient(self._status(peers=[
            {"agent_name": "peer-1", "status": "idle"},
            {"agent_name": "peer-2", "status": "idle"},
        ]))
        entries = await check_a2a(client=client)
        assert "You have 2 peers: peer-1, peer-2." in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_check_failure_reports_warning(self):
        client = MagicMock()
        client.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        entries = await check_a2a(client=client)
        assert entries[0]["level"] == "warning"
        assert "boom" in entries[0]["hint"]


class _FakeMemfilesClient:
    """Minimal stand-in for the memfiles plugin MCP client."""

    def __init__(self, payload):
        self._payload = payload

    async def call_tool(self, name, arguments=None):
        assert name == "__check"
        return json.dumps(self._payload)


class TestCheckMemfilesFunction:
    """Tests for check_memfiles()."""

    @staticmethod
    def _status(**overrides):
        data = {
            "ok": True, "connected": True, "state": "ready",
            "semantic_ready": True, "unembedded": 0, "reason": "",
            "hint": "Cabinet connected; semantic index ready.",
        }
        data.update(overrides)
        return data

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        entries = await check_memfiles()
        assert entries[0]["component"] == "memfiles"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "offline"
        assert "not connected" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_connected_semantic_ready(self):
        client = _FakeMemfilesClient(self._status())
        entries = await check_memfiles(client=client)
        assert len(entries) == 1
        assert entries[0]["level"] == "ok"
        assert entries[0]["value"] == "connected"
        assert "ready" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_connected_semantic_indexing(self):
        client = _FakeMemfilesClient(self._status(
            state="indexing", semantic_ready=False, unembedded=5,
        ))
        entries = await check_memfiles(client=client)
        assert len(entries) == 1
        assert entries[0]["level"] == "ok"
        assert "indexing" in entries[0]["hint"]
        assert "5" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_store_error(self):
        client = _FakeMemfilesClient(self._status(
            ok=False, state="store_error", semantic_ready=False,
            hint="store init failed",
        ))
        entries = await check_memfiles(client=client)
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "store_error"
        assert "store init failed" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_check_failure_reports_warning(self):
        client = MagicMock()
        client.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        entries = await check_memfiles(client=client)
        assert entries[0]["level"] == "warning"
        assert "boom" in entries[0]["hint"]


class TestCheckMemfilesTool:
    """Tests for CheckMemfilesTool metadata and execute."""

    def test_metadata(self):
        tool = CheckMemfilesTool()
        assert tool.name == "check_memfiles"
        assert "cabinet" in tool.description.lower()
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == []

    @pytest.mark.asyncio
    async def test_execute_returns_json(self):
        tool = CheckMemfilesTool()
        with patch("slife.tools.system.check_memfiles",
                   new=AsyncMock(return_value=[
                       {"component": "memfiles", "level": "ok", "key": "plugin",
                        "value": "connected", "hint": "all good"},
                   ])):
            result = await tool.execute()
            parsed = json.loads(result)
            assert parsed[0]["component"] == "memfiles"
            assert parsed[0]["value"] == "connected"

    @pytest.mark.asyncio
    async def test_execute_uses_ctx_memfiles_client(self):
        """execute() reaches the plugin through ToolContext.memfiles_client."""
        tool = CheckMemfilesTool()
        fake_client = _FakeMemfilesClient({
            "ok": True, "connected": True, "state": "ready",
            "semantic_ready": True, "unembedded": 0, "reason": "",
            "hint": "Cabinet connected; semantic index ready.",
        })
        tool._ctx = MagicMock(memfiles_client=fake_client)
        result = await tool.execute()
        parsed = json.loads(result)
        assert parsed[0]["component"] == "memfiles"
        assert parsed[0]["value"] == "connected"


class TestCheckA2aTool:
    """Tests for CheckA2aTool metadata and execute."""

    def test_metadata(self):
        tool = CheckA2aTool()
        assert tool.name == "check_a2a"
        assert "a2a" in tool.description.lower()
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == []

    @pytest.mark.asyncio
    async def test_execute_returns_json(self):
        tool = CheckA2aTool()
        with patch("slife.tools.system.check_a2a",
                   new=AsyncMock(return_value=[
                       {"component": "a2a", "level": "ok", "key": "status",
                        "value": "connected", "hint": "all good"},
                   ])):
            result = await tool.execute()
            parsed = json.loads(result)
            assert parsed[0]["component"] == "a2a"
            assert parsed[0]["value"] == "connected"


class _FakeLocalEmbedClient:
    """Minimal stand-in for the local-embed plugin MCP client."""

    def __init__(self, payload):
        self._payload = payload

    async def call_tool(self, name, arguments=None):
        assert name == "__check"
        return json.dumps(self._payload)


class TestCheckLocalEmbedFunction:
    """Tests for check_local_embed()."""

    @staticmethod
    def _status(**overrides):
        data = {
            "active_model": "bge-m3",
            "models": [
                {"name": "bge-m3", "backend": "gguf", "model": "bge-m3",
                 "dimension": 1024, "dimension_known": True, "loaded": True,
                 "available": True, "max_tokens": 8192},
            ],
        }
        data.update(overrides)
        return data

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        entries = await check_local_embed()
        assert entries[0]["component"] == "local_embed"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "offline"
        assert "not connected" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_active_model_loaded(self):
        client = _FakeLocalEmbedClient(self._status())
        entries = await check_local_embed(client=client)
        assert len(entries) == 1
        assert entries[0]["level"] == "ok"
        assert entries[0]["value"] == "bge-m3"
        assert "bge-m3 loaded" in entries[0]["hint"]
        assert "1/1 model(s) loaded" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_active_model_not_loaded(self):
        client = _FakeLocalEmbedClient(self._status(
            models=[
                {"name": "bge-m3", "backend": "gguf", "model": "bge-m3",
                 "dimension": 1024, "dimension_known": True, "loaded": False,
                 "available": True, "max_tokens": 8192},
            ],
        ))
        entries = await check_local_embed(client=client)
        assert len(entries) == 1
        assert entries[0]["level"] == "warning"
        assert "NOT loaded" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_no_models_reports_warning(self):
        client = _FakeLocalEmbedClient(self._status(models=[]))
        entries = await check_local_embed(client=client)
        assert entries[0]["level"] == "warning"
        assert "NOT loaded" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_check_failure_reports_warning(self):
        client = MagicMock()
        client.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        entries = await check_local_embed(client=client)
        assert entries[0]["level"] == "warning"
        assert "boom" in entries[0]["hint"]


class TestCheckLocalEmbedTool:
    """Tests for CheckLocalEmbedTool metadata and execute."""

    def test_metadata(self):
        tool = CheckLocalEmbedTool()
        assert tool.name == "check_local_embed"
        assert "local-embed" in tool.description.lower()
        assert tool.parameters["type"] == "object"
        assert tool.parameters["required"] == []

    @pytest.mark.asyncio
    async def test_execute_returns_json(self):
        tool = CheckLocalEmbedTool()
        with patch("slife.tools.system.check_local_embed",
                   new=AsyncMock(return_value=[
                       {"component": "local_embed", "level": "ok", "key": "status",
                        "value": "bge-m3", "hint": "all good"},
                   ])):
            result = await tool.execute()
            parsed = json.loads(result)
            assert parsed[0]["component"] == "local_embed"
            assert parsed[0]["value"] == "bge-m3"

    @pytest.mark.asyncio
    async def test_execute_uses_ctx_local_embed_client(self):
        """execute() reaches the plugin through ToolContext.local_embed_client."""
        tool = CheckLocalEmbedTool()
        fake_client = _FakeLocalEmbedClient({
            "active_model": "bge-m3",
            "models": [
                {"name": "bge-m3", "backend": "gguf", "model": "bge-m3",
                 "dimension": 1024, "dimension_known": True, "loaded": True,
                 "available": True, "max_tokens": 8192},
            ],
        })
        tool._ctx = MagicMock(local_embed_client=fake_client)
        result = await tool.execute()
        parsed = json.loads(result)
        assert parsed[0]["component"] == "local_embed"
        assert parsed[0]["value"] == "bge-m3"


class TestCheckToolsInternal:
    """Per-plugin check tools are internal — never auto-registered for the LLM.

    The agent only sees the aggregated ``system_health``; the individual
    ``check_*`` tools exist for the harness (probed by ``system_health``),
    so every one must carry ``_skip_auto_register`` to stay out of the
    factory's auto-discovery.
    """

    _CHECK_TOOL_CLASSES = (
        CheckMemdbTool,
        CheckWechatTool,
        CheckMemfilesTool,
        CheckLocalEmbedTool,
        CheckSharefileTool,
        CheckMediaTool,
        CheckMcpTool,
        CheckA2aTool,
        CheckWatchdogTool,
    )

    def test_all_check_tools_skip_auto_register(self):
        for cls in self._CHECK_TOOL_CLASSES:
            assert cls.__dict__.get("_skip_auto_register") is True, cls.__name__

    def test_system_health_stays_auto_registered(self):
        assert SystemHealthTool.__dict__.get("_skip_auto_register") is not True


class TestCheckMedia:
    """Tests for check_media() — optional plugin, config gate + __check probe."""

    @pytest.mark.asyncio
    async def test_not_configured_is_ok(self):
        with patch(
            "slife.plugins.media.config.load_media_config",
            return_value=MagicMock(is_empty=MagicMock(return_value=True)),
        ):
            result = await check_media()
            assert len(result) == 1
            assert result[0]["component"] == "media"
            assert result[0]["level"] == "ok"
            assert result[0]["value"] == "not_configured"

    @pytest.mark.asyncio
    async def test_configured_but_client_unavailable(self):
        with patch(
            "slife.plugins.media.config.load_media_config",
            return_value=MagicMock(is_empty=MagicMock(return_value=False)),
        ):
            result = await check_media()
            assert result[0]["component"] == "media"
            assert result[0]["value"] == "offline"
            assert "not connected" in result[0]["hint"]

    @pytest.mark.asyncio
    async def test_configured_probes_plugin(self):
        with patch(
            "slife.plugins.media.config.load_media_config",
            return_value=MagicMock(is_empty=MagicMock(return_value=False)),
        ):
            payload = [{"component": "media", "level": "ok", "key": "enabled",
                        "value": "1 provider(s)", "hint": "Media configured"}]
            client = MagicMock()
            client.call_tool = AsyncMock(return_value=json.dumps(payload))
            result = await check_media(client=client)
            client.call_tool.assert_called_once_with("__check")
            assert result == payload
