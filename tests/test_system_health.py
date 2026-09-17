"""Tests for Slife.tools.system_health — system health check tool."""

import pytest; pytestmark = pytest.mark.integration


import json
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from slife.tools.system import (
    _CHECK_FUNCTIONS,
    check_memdb,
    check_wechat,
    check_memfiles,
    check_sharefile,
    check_local_embed,
    check_mcp_gateway,
    check_a2a,
    check_media,
    check_job_coding,
    check_tool_catalog,
    check_watchdog,
    _group_by_component,
    _component_status,
    _collapse,
    _format_fact,
    _verdict,
    _render_report,
    _dedupe_records,
    SystemHealthTool,
)


@contextmanager
def _patch_all_checks(**overrides):
    """Patch the startup store and EVERY check, so a test asserts on a report
    built from exactly the entries it names.

    Per-check patches listed by hand are how the older execute() tests passed
    by luck: several forgot a check, and the real one leaked into the report.
    ``startup=`` and ``<check_fn_name>=`` are the overrides.
    """
    startup = overrides.pop("startup", [])
    with ExitStack() as stack:
        stack.enter_context(patch("slife.tools.system.get_startup_records",
                                  return_value=startup))
        for name in _CHECK_FUNCTIONS:
            stack.enter_context(patch(f"slife.tools.system.{name}",
                                      return_value=overrides.get(name, [])))
        yield


def _render(entries: list[dict]) -> str:
    """Render a flat entry list the way ``system_health`` does."""
    return _render_report(_group_by_component(entries))


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


class TestDedupeRecords:
    """Startup records vs live check entries must not double-report.

    The rule is generic — a startup record is dropped when a live entry covers
    the same ``(component, key)`` — so a producer only has to name its
    component after the live check that reports it.  (The retired
    component-pair map is what let ``a2a`` list ``status`` twice.)
    """

    @staticmethod
    def _startup(component, key, level="warning"):
        return {"component": component, "level": level, "key": key,
                "value": "startup", "hint": "recorded at startup"}

    @staticmethod
    def _live(component, key, level="ok"):
        return {"component": component, "level": level, "key": key,
                "value": "live", "hint": "probed now"}

    def test_live_ok_supersedes_stale_warning(self):
        merged = _dedupe_records(
            [self._startup("mcp_servers", "fs"), self._startup("mcp_servers", "github")],
            [self._live("mcp_servers", "fs")],
        )
        # kept startup records come first, the live entries after them
        assert sorted(e["key"] for e in merged) == ["fs", "github"]
        assert all(e["level"] == "ok" for e in merged if e["key"] == "fs")
        # github: not covered by live — the startup record is preserved.
        assert any(e["key"] == "github" and e["level"] == "warning" for e in merged)

    def test_live_warning_still_supersedes_startup_ok(self):
        """The live report is authoritative in BOTH directions: a live
        "disconnected" must not be masked by a stale "connected" record."""
        merged = _dedupe_records(
            [self._startup("mcp_servers", "fs", level="ok")],
            [self._live("mcp_servers", "fs", level="warning")],
        )
        entries = [e for e in merged if e["key"] == "fs"]
        assert len(entries) == 1
        assert entries[0]["level"] == "warning"

    def test_a2a_startup_record_does_not_double_report(self):
        """Regression: the startup a2a record and the live check share the
        component AND key, so a healthy mesh reported ``status`` twice."""
        merged = _dedupe_records(
            [self._startup("a2a", "status", level="ok")],
            [self._live("a2a", "status", level="warning")],
        )
        assert [e["key"] for e in merged] == ["status"]
        assert merged[0]["level"] == "warning"

    def test_wechat_startup_record_does_not_double_report(self):
        merged = _dedupe_records(
            [self._startup("wechat", "status", level="ok")],
            [self._live("wechat", "status")],
        )
        assert len([e for e in merged if e["component"] == "wechat"]) == 1

    def test_different_key_is_not_superseded(self):
        """Same component, different key: the live check could not probe that
        fact (e.g. an unloadable config), so the record stays."""
        merged = _dedupe_records(
            [self._startup("wechat", "status", level="ok")],
            [self._live("wechat", "enabled")],
        )
        assert len(merged) == 2

    def test_keeps_startup_records_with_no_live_counterpart(self):
        merged = _dedupe_records([self._startup("config", "path", level="ok")], [])
        assert len(merged) == 1

    def test_keyless_records_are_kept(self):
        """A record with no key cannot be covered by a live entry."""
        merged = _dedupe_records(
            [{"component": "x", "level": "ok"}],
            [{"component": "x", "level": "warning", "key": "status"}],
        )
        assert len(merged) == 2

    def test_no_mutation_of_inputs(self):
        startup = [self._startup("mcp_servers", "fs")]
        live = [self._live("mcp_servers", "fs")]
        _dedupe_records(startup, live)
        assert len(startup) == 1
        assert len(live) == 1


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
        entries = [{"level": "error"}]
        assert _component_status(entries) == "error"

    def test_single_entry(self):
        assert _component_status([{"level": "ok"}]) == "ok"

    def test_empty_entries_defaults_to_ok(self):
        assert _component_status([]) == "ok"

    def test_info_is_not_a_problem(self):
        """A disabled server is intentionally off — it must not raise the
        component's status (or it would land in the problems section)."""
        assert _component_status([{"level": "info"}, {"level": "ok"}]) == "ok"


# ── _collapse ─────────────────────────────────────────────────────────


class TestCollapse:
    """The generic mechanism that keeps 19 identical sentences to one line."""

    def test_identical_entries_merge_into_one_fact(self):
        entries = [
            {"component": "mcp_servers", "level": "warning", "key": f"s{i}",
             "value": "disconnected", "hint": "retry"}
            for i in range(19)
        ]
        items = _collapse(entries)
        assert len(items) == 1
        assert len(items[0]["keys"]) == 19
        assert _format_fact(items[0]) == "disconnected [s0, s1, s10, s11, s12, s13, s14, s15, s16, s17, s18, s2, s3, s4, s5, s6, s7, s8, s9]"

    def test_same_hint_different_value_does_not_merge(self):
        """Two servers that failed differently are two different facts —
        merging on (level, hint) alone would silently drop one."""
        entries = [
            {"component": "mcp_servers", "level": "warning", "key": "a",
             "value": "disconnected (ECONNREFUSED)", "hint": "retry"},
            {"component": "mcp_servers", "level": "warning", "key": "b",
             "value": "disconnected (timeout)", "hint": "retry"},
        ]
        assert len(_collapse(entries)) == 2

    def test_different_level_does_not_merge(self):
        entries = [
            {"component": "x", "level": "warning", "key": "a", "value": "v"},
            {"component": "x", "level": "error", "key": "b", "value": "v"},
        ]
        assert len(_collapse(entries)) == 2

    def test_empty(self):
        assert _collapse([]) == []

    def test_single_entry(self):
        items = _collapse([{"component": "x", "level": "ok", "key": "k", "value": "v"}])
        assert len(items) == 1
        assert items[0]["keys"] == ["k"]

    def test_problems_sort_before_ok_inside_a_component(self):
        entries = [
            {"component": "x", "level": "ok", "key": "a", "value": "fine"},
            {"component": "x", "level": "error", "key": "b", "value": "broken"},
        ]
        assert [i["level"] for i in _collapse(entries)] == ["error", "ok"]

    def test_keyless_entries_collapse_without_a_key_list(self):
        items = _collapse([
            {"component": "x", "level": "ok", "value": "same"},
            {"component": "x", "level": "ok", "value": "same"},
        ])
        assert len(items) == 1
        assert items[0]["keys"] == []
        assert _format_fact(items[0]) == "same"


class TestFormatFact:
    """The three shapes of a fact — mechanical, never paraphrased."""

    def test_single_key_renders_key_value(self):
        item = {"level": "ok", "value": "1.2 MB", "hint": "", "keys": ["db"]}
        assert _format_fact(item) == "db=1.2 MB"

    def test_single_key_without_value_renders_the_key(self):
        item = {"level": "ok", "value": "", "hint": "", "keys": ["db"]}
        assert _format_fact(item) == "db"

    def test_many_keys_render_value_then_the_list(self):
        item = {"level": "ok", "value": "active", "hint": "",
                "keys": ["a", "b"]}
        assert _format_fact(item) == "active [a, b]"

    def test_key_list_is_capped(self):
        keys = [f"s{i:03d}" for i in range(40)]
        item = {"level": "warning", "value": "disconnected", "hint": "", "keys": keys}
        text = _format_fact(item)
        assert text.startswith("disconnected [")
        assert text.endswith("+16 more]")
        assert "s023" in text and "s024" not in text

    def test_no_key_no_value_renders_empty(self):
        item = {"level": "ok", "value": "", "hint": "", "keys": []}
        assert _format_fact(item) == ""


# ── _render_report ────────────────────────────────────────────────────


class TestRenderReport:
    """The report is the LLM's view: problems first, then one line per
    healthy component.  These are table-driven over synthetic entries, which
    is the only way to test the renderer without booting the plugins."""

    def test_empty_report_is_healthy_and_safe(self):
        report = _render([])
        assert report.startswith("system_health: HEALTHY")
        assert "## Problems" not in report
        assert report.strip()

    def test_never_looks_like_an_error_or_a_compacted_result(self):
        report = _render([{"component": "x", "level": "error", "key": "k",
                           "value": "broken", "hint": "fix it"}])
        assert not report.startswith("Error")
        assert not report.startswith((" ", "[", "#", "\n"))
        assert "[compacted at save:" not in report

    def test_problem_leads_and_healthy_follows(self):
        report = _render([
            {"component": "memdb", "level": "ok", "key": "db", "value": "1.2 MB"},
            {"component": "a2a", "level": "warning", "key": "status",
             "value": "unavailable", "hint": "Start mosquitto."},
        ])
        assert report.index("## Problems") < report.index("## Components OK")
        assert report.index("a2a") < report.index("memdb")
        assert "[WARN]  a2a" in report
        assert "— Start mosquitto." in report

    def test_error_ranks_above_warning(self):
        report = _render([
            {"component": "a2a", "level": "warning", "key": "status", "value": "w"},
            {"component": "wechat", "level": "error", "key": "status", "value": "e"},
        ])
        assert report.index("[ERROR] wechat") < report.index("[WARN]  a2a")

    def test_healthy_components_render_one_line_each(self):
        report = _render([
            {"component": "memdb", "level": "ok", "key": "db", "value": "1.2 MB"},
            {"component": "memdb", "level": "ok", "key": "embedding", "value": "ready"},
        ])
        body = report.split("## Components OK", 1)[1].strip().splitlines()
        assert len(body) == 1
        assert body[0] == "memdb: db=1.2 MB; embedding=ready"

    def test_environment_facts_sort_last(self):
        report = _render([
            {"component": "node", "level": "ok", "key": "version", "value": "v25"},
            {"component": "memdb", "level": "ok", "key": "db", "value": "1.2 MB"},
            {"component": "watchdog", "level": "ok", "key": "p", "value": "active"},
        ])
        body = report.split("## Components OK", 1)[1]
        assert body.index("memdb") < body.index("watchdog") < body.index("node")

    def test_info_entries_are_reported_as_ok_never_as_problems(self):
        report = _render([
            {"component": "mcp_servers", "level": "info", "key": "slack",
             "value": "disabled"},
        ])
        assert "## Problems" not in report
        assert "HEALTHY" in report
        assert "slack=disabled" in report

    def test_problem_without_a_hint_has_no_dangling_separator(self):
        report = _render([{"component": "memfiles", "level": "warning",
                           "key": "plugin", "value": "store_error: boom"}])
        line = next(ln for ln in report.splitlines() if ln.startswith("[WARN]"))
        assert line.endswith("store_error: boom")
        assert not line.rstrip().endswith("—")

    def test_every_problem_fact_in_a_component_shares_one_remedy_once(self):
        entries = [
            {"component": "mcp_servers", "level": "warning", "key": f"s{i}",
             "value": "disconnected", "hint": "The wrapper retries."}
            for i in range(19)
        ]
        report = _render(entries)
        assert report.count("The wrapper retries.") == 1
        assert report.count("disconnected [") == 1

    def test_distinct_remedies_pair_with_their_own_fact(self):
        report = _render([
            {"component": "mixed", "level": "warning", "key": "a",
             "value": "one", "hint": "fix one"},
            {"component": "mixed", "level": "error", "key": "b",
             "value": "two", "hint": "fix two"},
        ])
        line = next(ln for ln in report.splitlines() if ln.startswith("[ERROR]"))
        assert "two — fix two" in line and "one — fix one" in line

    def test_machine_only_keys_are_not_rendered(self):
        plain = [{"component": "mcp_servers", "level": "warning", "key": "fs",
                  "value": "disconnected", "hint": "retry"}]
        noisy = [{"component": "mcp_servers", "level": "warning", "key": "fs",
                  "value": "disconnected", "hint": "retry",
                  "enabled": True, "state": "disconnected", "tool_count": 0,
                  "transport": "stdio", "peers": [{"agent_name": "x"}]}]
        assert _render(plain) == _render(noisy)

    def test_unknown_level_does_not_crash(self):
        report = _render([{"component": "x", "level": "banana", "key": "k",
                           "value": "v"}])
        assert "[WARN]" in report

    def test_a_check_that_blew_up_is_reported_with_its_blast_radius(self):
        report = _render([{
            "component": "system_health", "level": "error",
            "key": "check_a2a_failed", "value": "check_a2a failed",
            "hint": "RuntimeError: boom — the rest of this report is unaffected.",
        }])
        assert "[ERROR] system_health" in report
        assert "unaffected" in report

    def test_the_two_server_families_report_separately(self):
        report = _render([
            {"component": "mcp_servers", "level": "warning", "key": "fs",
             "value": "disconnected", "hint": "use mcp_list."},
            {"component": "rest-api", "level": "warning", "key": "github",
             "value": "disconnected", "hint": "use rest_api_list."},
        ])
        lines = report.splitlines()
        mcp_line = "[WARN]  mcp_servers: fs=disconnected — use mcp_list."
        rest_line = "[WARN]  rest-api   : github=disconnected — use rest_api_list."
        assert mcp_line in lines and rest_line in lines
        assert lines.index(mcp_line) < lines.index(rest_line)

    def test_collapsed_watchdog_entries_share_one_line(self):
        report = _render([
            {"component": "watchdog", "level": "ok", "key": p, "value": "active"}
            for p in ("memdb", "wechat")
        ])
        body = report.split("## Components OK", 1)[1].strip().splitlines()
        assert len(body) == 1
        assert body[0] == "watchdog: active [memdb, wechat]"

    def test_oversized_report_folds_only_the_healthy_section(self):
        entries = [{"component": "a2a", "level": "warning", "key": "status",
                    "value": "unavailable", "hint": "fix it"}]
        entries += [{"component": f"c{i}", "level": "ok", "key": "k",
                     "value": "v" * 200} for i in range(60)]
        report = _render(entries)
        assert len(report) <= 7000
        assert "fix it" in report          # problems survive the fold
        assert "detail omitted" in report   # and the fold announces itself

    def test_realistic_report_fits_the_save_budget(self):
        """The previous JSON report was 16 KB and had its middle amputated by
        the save-side compaction (8000).  A baseline exam must not."""
        entries = [
            {"component": "config", "level": "ok", "key": "path",
             "value": r"D:\Dev\Workspace\slife\slife.json5 (16 models, 20 MCP servers, embeddings=enabled)"},
            {"component": "model", "level": "ok", "key": "active",
             "value": "deepseek/deepseek-flash (thinking=on, vision=on, ctx 1000000)"},
        ]
        entries += [{"component": c, "level": "ok", "key": "version", "value": "v1"}
                    for c in ("node", "npm", "bun", "uv")]
        entries += [
            {"component": "memdb", "level": "ok", "key": "db", "value": "7.3 MB (slife.db)"},
            {"component": "memdb", "level": "ok", "key": "embedding",
             "value": "ready (BAAI/bge-m3, dim=1024)"},
            {"component": "memfiles", "level": "ok", "key": "plugin",
             "value": "connected (semantic index ready)"},
            {"component": "watchdog", "level": "ok", "key": "memdb", "value": "active"},
        ]
        entries += [
            {"component": "mcp_servers", "level": "warning", "key": n,
             "value": "disconnected", "hint": "The wrapper retries."}
            for n in [f"server-{i}" for i in range(18)] + ["github", "mcp-registry"]
        ]
        report = _render(entries)
        assert len(report) < 8000


class TestVerdict:
    """The one-line verdict that leads the report."""

    def _problems(self, *specs):
        # (rank, order, component, status, line)
        return [(0 if s == "error" else 1, i, f"c{i}", s, "line")
                for i, s in enumerate(specs)]

    def test_all_healthy(self):
        text = _verdict([], 16)
        assert text == "HEALTHY — 16 components checked, no problems."

    def test_single_component_is_not_pluralised(self):
        assert "1 component checked" in _verdict([], 1)

    def test_degrades_with_counts_and_names(self):
        text = _verdict(self._problems("warning", "warning", "error"), 15)
        assert text.startswith("DEGRADED — 3 problems (1 error, 2 warnings): ")
        assert "15 components OK." in text

    def test_one_problem_reads_singular(self):
        text = _verdict(self._problems("warning"), 16)
        assert "1 problem (0 errors, 1 warning):" in text

    def test_long_name_list_is_capped(self):
        text = _verdict(self._problems(*(["warning"] * 12)), 1)
        assert "+4 more" in text


# ── check_memdb ───────────────────────────────────────────────────────


class TestCheckMemdb:
    """Tests for check_memdb() — async probe of the memdb plugin's __check."""

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        entries = await check_memdb()
        assert entries[0]["component"] == "memdb"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "offline"
        assert "Restart slife" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_plugin_facts_are_interpreted(self):
        payload = {
            "db": {"exists": True, "size_mb": 1.0, "path": "slife.db"},
            "semantic": {"configured": True, "provider": "local", "model": "bge-m3",
                         "dimension": 1024, "available": True, "semantic_ready": True,
                         "state": "ready", "reason": "", "unembedded": 0, "loaded": True},
        }
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        entries = await check_memdb(client=client)
        client.call_tool.assert_called_once_with("__check")
        dbs = [e for e in entries if e["key"] == "db"]
        emb = [e for e in entries if e["key"] == "embedding"]
        assert len(dbs) == 1 and dbs[0]["level"] == "ok"
        # The DB is agent-scoped, so the value names the live file.
        assert dbs[0]["value"] == "1.0 MB (slife.db)"
        assert "hint" not in dbs[0]
        assert len(emb) == 1 and emb[0]["level"] == "ok"
        assert emb[0]["value"] == "ready (bge-m3, dim=1024)"

    @pytest.mark.asyncio
    async def test_plugin_semantic_building_is_warning(self):
        payload = {
            "db": {"exists": False, "path": "slife.db"},
            "semantic": {"configured": True, "provider": "local", "model": "bge-m3",
                         "dimension": 1024, "available": True, "semantic_ready": False,
                         "state": "indexing", "reason": "", "unembedded": 5, "loaded": False},
        }
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        entries = await check_memdb(client=client)
        dbs = [e for e in entries if e["key"] == "db"]
        emb = [e for e in entries if e["key"] == "embedding"]
        assert dbs[0]["level"] == "warning"
        assert dbs[0]["value"] == "not found (slife.db)"
        assert "first memory write" in dbs[0]["hint"]
        assert emb[0]["level"] == "warning"
        # A building index is a fact with no action — the numbers replace the
        # sentence the old report repeated.
        assert emb[0]["value"] == "indexing (5 turns pending; keyword search available)"
        assert "hint" not in emb[0]

    @pytest.mark.asyncio
    async def test_plugin_semantic_stalled_says_stalled(self):
        """A stalled index must say so.

        A stall carries a ``reason`` too, and the generic reason branch used to
        win — so the report read "unavailable (… resumes automatically …)" with
        an empty hint.  The word "stalled" never appeared, and the one thing the
        message promised was the one thing the code could not deliver.
        """
        payload = {
            "db": {"exists": True, "path": "slife.db"},
            "semantic": {
                "configured": True, "provider": "local", "model": "bge-m3",
                "dimension": 1024, "available": True, "semantic_ready": False,
                "state": "stalled", "unembedded": 7, "loaded": True,
                "reason": "semantic index stalled — the embedder failed "
                          "repeatedly and gave up this round.",
            },
        }
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        entries = await check_memdb(client=client)
        emb = [e for e in entries if e["key"] == "embedding"]
        assert emb[0]["level"] == "warning"
        assert emb[0]["value"] == "stalled (7 turns pending; keyword search available)"
        assert "hint" not in emb[0]

    @pytest.mark.asyncio
    async def test_missing_endpoint_points_at_the_config_tool(self):
        payload = {
            "db": {"exists": True, "size_mb": 1.0, "path": "slife.db"},
            "semantic": {"configured": False, "available": False,
                         "semantic_ready": False, "state": "unconfigured",
                         "reason": "", "unembedded": 0},
        }
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        entries = await check_memdb(client=client)
        emb = [e for e in entries if e["key"] == "embedding"][0]
        assert emb["value"] == "unavailable (no embeddings endpoint configured)"
        assert "embeddings_model_set" in emb["hint"]

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
                assert result[0]["value"] == "unknown (config not loaded)"

    @pytest.mark.asyncio
    async def test_disabled_in_config(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = False

        result = await check_wechat(config=mock_config)
        assert len(result) == 1
        assert result[0]["value"] == "disabled (wechat.enabled: false)"

    @pytest.mark.asyncio
    async def test_enabled_but_client_unavailable(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        result = await check_wechat(config=mock_config)
        assert len(result) == 1
        assert result[0]["component"] == "wechat"
        assert result[0]["value"] == "offline"
        assert "Restart slife" in result[0]["hint"]

    @pytest.mark.asyncio
    async def test_enabled_probes_plugin_facts(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        payload = {"logged_in": True, "auth_failed": False, "last_error": "",
                   "session": {"saved": True, "saved_at": 0.0, "age_h": 1.0, "max_age_h": 72.0}}
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_wechat(client=client, config=mock_config)
        client.call_tool.assert_called_once_with("__check")
        assert len(result) == 1
        assert result[0]["component"] == "wechat"
        assert result[0]["level"] == "ok"
        assert result[0]["value"] == "logged_in (session 1.0h of 72h, 71.0h left)"
        assert "hint" not in result[0]

    @pytest.mark.asyncio
    async def test_expired_session_facts_report_warning(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        payload = {"logged_in": False, "auth_failed": False, "last_error": "",
                   "session": {"saved": True, "saved_at": 0.0, "age_h": 73.0, "max_age_h": 72.0}}
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_wechat(client=client, config=mock_config)
        assert result[0]["level"] == "warning"
        assert result[0]["value"] == "session_expired (73.0h old, max 72h)"
        assert "wechat_login" in result[0]["hint"]

    @pytest.mark.asyncio
    async def test_rejected_session_is_an_error_with_the_remedy(self):
        mock_config = MagicMock()
        mock_config.wechat_config = MagicMock()
        mock_config.wechat_config.enabled = True

        payload = {"logged_in": True, "auth_failed": True, "last_error": "401",
                   "session": {"saved": True, "saved_at": 0.0, "age_h": 1.0, "max_age_h": 72.0}}
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_wechat(client=client, config=mock_config)
        assert result[0]["level"] == "error"
        assert result[0]["value"] == "session_rejected (last error: 401)"

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
                assert result[0]["value"] == "unknown (config not loaded)"


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
    """Tests for SystemHealthTool.execute() — the rendered report."""

    @pytest.mark.asyncio
    async def test_execute_returns_a_healthy_report(self):
        tool = SystemHealthTool()
        with _patch_all_checks():
            result = await tool.execute()
        assert result.startswith("system_health: HEALTHY")
        assert "## Problems" not in result

    @pytest.mark.asyncio
    async def test_execute_includes_startup_records(self):
        tool = SystemHealthTool()
        startup = [{"component": "startup", "level": "ok", "key": "bootstrap",
                    "value": "done"}]
        with _patch_all_checks(startup=startup):
            result = await tool.execute()
        assert "startup" in result
        assert "bootstrap=done" in result

    @pytest.mark.asyncio
    async def test_execute_with_warnings_is_not_healthy(self):
        tool = SystemHealthTool()
        startup = [{"component": "db", "level": "warning", "key": "schema",
                    "value": "migrated", "hint": "check logs"}]
        with _patch_all_checks(startup=startup):
            result = await tool.execute()
        assert result.startswith("system_health: DEGRADED")
        assert "## Problems" in result
        assert "check logs" in result

    @pytest.mark.asyncio
    async def test_execute_all_healthy(self):
        tool = SystemHealthTool()
        startup = [
            {"component": "a", "level": "ok", "key": "k", "value": "v"},
            {"component": "b", "level": "ok", "key": "k", "value": "v"},
        ]
        with _patch_all_checks(startup=startup):
            result = await tool.execute()
        assert "HEALTHY" in result
        assert "2 components checked" in result

    @pytest.mark.asyncio
    async def test_recovered_mcp_server_is_not_contradictory(self):
        """A server that was slow to cold-start (startup warning recorded) but
        is now connected (live check ok) must not keep the report unhealthy —
        the stale startup record is superseded by the live result."""
        tool = SystemHealthTool()
        startup = [
            {"component": "mcp_servers", "level": "warning", "key": "fs",
             "value": "connect_pending",
             "hint": "enabled but not yet connected; retrying in background."},
        ]
        live = [
            {"component": "mcp_servers", "level": "ok", "key": "fs",
             "value": "connected (5 tools, stdio)"},
        ]
        with _patch_all_checks(startup=startup,
                               check_mcp_gateway=live):
            result = await tool.execute()
        assert "HEALTHY" in result
        assert "## Problems" not in result
        assert "connect_pending" not in result
        assert "fs=connected (5 tools, stdio)" in result

    @pytest.mark.asyncio
    async def test_execute_no_duplicate_watchdog_entries(self):
        """Regression (BUGS.md #6): startup watchdog records are re-reported
        by check_watchdog — the watchdog component must list each plugin once,
        not duplicated."""
        tool = SystemHealthTool()
        records = [
            {"component": "watchdog", "level": "ok", "key": name, "value": "active"}
            for name in ("local-embed", "mcp")
        ]
        with _patch_all_checks(startup=records, check_watchdog=records):
            result = await tool.execute()
        watchdog_lines = [ln for ln in result.splitlines() if ln.startswith("watchdog")]
        assert len(watchdog_lines) == 1
        assert watchdog_lines[0] == "watchdog: active [local-embed, mcp]"


# ── check_watchdog ─────────────────────────────────────────────────────


class TestCheckWatchdogFunction:
    """Tests for check_watchdog() — dedup + source of records."""

    def test_no_records_reports_none(self):
        with patch("slife.tools.system.get_startup_records", return_value=[]):
            entries = check_watchdog()
        assert entries == [{"component": "watchdog", "level": "ok",
                            "key": "status",
                            "value": "none (subagent, or plugins not started)"}]

    def test_keeps_latest_per_plugin(self):
        records = [
            {"component": "watchdog", "key": "plugin_a", "value": "warning", "level": "warning"},
            {"component": "watchdog", "key": "plugin_a", "value": "ok", "level": "ok"},
        ]
        with patch("slife.tools.system.get_startup_records", return_value=records):
            entries = check_watchdog()
        assert len(entries) == 1
        assert entries[0]["value"] == "ok"  # later record overwrites

    def test_exhausted_watchdog_keeps_its_remedy(self):
        records = [{"component": "watchdog", "key": "memdb", "value": "exhausted",
                    "level": "error", "hint": "Restart slife to recover."}]
        with patch("slife.tools.system.get_startup_records", return_value=records):
            entries = check_watchdog()
        assert entries[0]["hint"] == "Restart slife to recover."


class _FakeMcpClient:
    """Minimal stand-in for the slife-mcp wrapper client."""

    def __init__(self, payload):
        self._payload = payload

    async def call_tool(self, name, arguments=None):
        assert name == "__check"
        return json.dumps(self._payload)


class TestCheckMcpFunction:
    """Tests for check_mcp_gateway() server filtering."""

    @staticmethod
    def _client(payload):
        # the wrapper's __check now returns {"servers": [...]} only — semantic
        # moved host-side with the retired in-memory store
        if isinstance(payload, dict):
            return _FakeMcpClient(payload)
        return _FakeMcpClient({"servers": payload})

    @staticmethod
    def _server(name, state="running", **extra):
        server = {
            "name": name,
            "state": state,
            "status": "connected" if state == "running" else "failed",
            "enabled": True,
            "tool_count": 2 if state == "running" else 0,
            "error": "" if state == "running" else "boom",
            "transport": "stdio",
        }
        server.update(extra)
        return server

    @pytest.mark.asyncio
    async def test_checks_all_by_default(self):
        payload = [self._server("fs"), self._server("github", state="stopped")]
        entries = await check_mcp_gateway(client=self._client(payload))
        assert [e["key"] for e in entries] == ["fs", "github"]

    @pytest.mark.asyncio
    async def test_checks_single_server(self):
        payload = [self._server("fs"), self._server("github", state="stopped")]
        entries = await check_mcp_gateway(server="github", client=self._client(payload))
        assert len(entries) == 1
        assert entries[0]["key"] == "github"
        assert entries[0]["level"] == "warning"

    @pytest.mark.asyncio
    async def test_connected_server_carries_its_facts_in_the_value(self):
        entries = await check_mcp_gateway(client=self._client([self._server("fs")]))
        entry = entries[0]
        assert entry["level"] == "ok"
        assert entry["value"] == "connected (2 tools, stdio)"
        assert "hint" not in entry

    @pytest.mark.asyncio
    async def test_stopped_server_is_disconnected(self):
        """A stopped-but-enabled server reports 'disconnected' with a
        retry hint — there is no build-owned healthy verdict anymore."""
        payload = [self._server("broken", state="stopped")]
        entries = await check_mcp_gateway(client=self._client(payload))
        entry = entries[0]
        assert entry["level"] == "warning"
        assert entry["value"].startswith("disconnected")
        assert "retries in the background" in entry["hint"]
        assert "healthy" not in entry

    @pytest.mark.asyncio
    async def test_disconnected_hint_never_names_a_retired_tool(self):
        """The old hint said "use check_mcp_gateway" — a tool that does not
        exist (the standalone checks were deleted as dead code)."""
        payload = [self._server("broken", state="stopped")]
        entries = await check_mcp_gateway(client=self._client(payload))
        assert "check_mcp_gateway" not in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_rest_api_servers_are_a_separate_component(self):
        """github/mcp-registry live in the rest-api section and are managed by
        rest_api_* tools, so they are reported apart from the mcp.servers —
        a REST-API server *is* an MCP server, but the operator's next move
        differs and the remedy has to name the right tool set."""
        payload = [
            self._server("fs", state="stopped", error=""),
            self._server("github", state="stopped", error="",
                         source={"type": "rest_api"}),
        ]
        entries = await check_mcp_gateway(client=self._client(payload))
        by_key = {e["key"]: e for e in entries}
        assert by_key["fs"]["component"] == "mcp_servers"
        assert by_key["github"]["component"] == "rest-api"
        assert by_key["fs"]["value"] == by_key["github"]["value"] == "disconnected"
        assert "mcp_list" in by_key["fs"]["hint"]
        assert "rest_api_list" in by_key["github"]["hint"]
        assert "mcp_list" not in by_key["github"]["hint"]

    @pytest.mark.asyncio
    async def test_connected_rest_api_server_keeps_its_family_component(self):
        payload = [self._server("github", source={"type": "rest_api"})]
        entries = await check_mcp_gateway(client=self._client(payload))
        assert entries[0]["component"] == "rest-api"
        assert entries[0]["value"] == "connected (2 tools, stdio)"

    @pytest.mark.asyncio
    async def test_disabled_server_is_info_without_a_hint(self):
        payload = [self._server("slack", enabled=False, state="stopped")]
        entries = await check_mcp_gateway(client=self._client(payload))
        assert entries[0]["level"] == "info"
        assert entries[0]["value"] == "disabled"
        assert "hint" not in entries[0]

    @pytest.mark.asyncio
    async def test_server_not_found(self):
        payload = [self._server("fs")]
        entries = await check_mcp_gateway(server="nope", client=self._client(payload))
        assert len(entries) == 1
        assert entries[0]["key"] == "nope"
        assert entries[0]["value"] == "not_found"
        assert entries[0]["level"] == "warning"

    @pytest.mark.asyncio
    async def test_server_not_found_when_no_servers(self):
        entries = await check_mcp_gateway(server="nope", client=self._client([]))
        assert entries[0]["value"] == "not_found"

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        entries = await check_mcp_gateway()
        assert entries[0]["value"] == "unavailable (client not connected)"
        assert entries[0]["level"] == "warning"

    @pytest.mark.asyncio
    async def test_servers_only_payload(self):
        """The wrapper's __check returns servers only — semantic moved to the
        host-as-plugin __check (the host owns the retired catalog's index)."""
        payload = {"servers": [self._server("fs")]}
        entries = await check_mcp_gateway(client=self._client(payload))
        assert [e["key"] for e in entries] == ["fs"]


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
        assert "mosquitto" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_plugin_disconnected(self):
        """Mosquitto down → no active MQTT port → a2a unavailable."""
        client = _FakeA2aClient(self._status(connected=False))
        entries = await check_a2a(client=client)
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "unavailable (broker localhost:1883)"
        assert "No active MQTT port" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_connected_no_peers(self):
        client = _FakeA2aClient(self._status())
        entries = await check_a2a(client=client)
        assert len(entries) == 1
        assert entries[0]["level"] == "ok"
        assert entries[0]["value"] == "connected (broker localhost:1883, no peers)"
        assert "hint" not in entries[0]

    @pytest.mark.asyncio
    async def test_connected_with_peers(self):
        client = _FakeA2aClient(self._status(peers=[
            {"agent_name": "peer-1", "status": "idle"},
        ]))
        entries = await check_a2a(client=client)
        assert entries[0]["level"] == "ok"
        assert entries[0]["peers"][0]["agent_name"] == "peer-1"
        assert entries[0]["value"] == "connected (broker localhost:1883, 1 peer: peer-1)"

    @pytest.mark.asyncio
    async def test_connected_multiple_peers_plural(self):
        """Two peers pluralise and list both names."""
        client = _FakeA2aClient(self._status(peers=[
            {"agent_name": "peer-1", "status": "idle"},
            {"agent_name": "peer-2", "status": "idle"},
        ]))
        entries = await check_a2a(client=client)
        assert "2 peers: peer-1, peer-2" in entries[0]["value"]

    @pytest.mark.asyncio
    async def test_queued_backlog_surfaces_in_the_value(self):
        """Undelivered messages are a degradation signal the exam used to
        probe and throw away."""
        client = _FakeA2aClient(self._status(
            queued={"tasks": 2, "presence": 1, "cancellations": 0},
        ))
        entries = await check_a2a(client=client)
        assert entries[0]["value"].endswith(", 3 queued)")

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
        }
        data.update(overrides)
        return data

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        entries = await check_memfiles()
        assert entries[0]["component"] == "memfiles"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "offline"
        assert "Restart slife" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_connected_semantic_ready(self):
        client = _FakeMemfilesClient(self._status())
        entries = await check_memfiles(client=client)
        assert len(entries) == 1
        assert entries[0]["level"] == "ok"
        assert entries[0]["value"] == "connected (semantic index ready)"
        assert "hint" not in entries[0]

    @pytest.mark.asyncio
    async def test_connected_semantic_indexing(self):
        """A building index is a fact on an ok entry — no remedy to give."""
        client = _FakeMemfilesClient(self._status(
            state="indexing", semantic_ready=False, unembedded=5,
        ))
        entries = await check_memfiles(client=client)
        assert len(entries) == 1
        assert entries[0]["level"] == "ok"
        assert entries[0]["value"] == "connected (semantic index indexing, 5 pending)"

    @pytest.mark.asyncio
    async def test_store_error_carries_the_reason_as_the_fact(self):
        client = _FakeMemfilesClient(self._status(
            ok=False, state="store_error", semantic_ready=False,
            reason="store init failed",
        ))
        entries = await check_memfiles(client=client)
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "store_error: store init failed"
        assert "hint" not in entries[0]

    @pytest.mark.asyncio
    async def test_check_failure_reports_warning(self):
        client = MagicMock()
        client.call_tool = AsyncMock(side_effect=RuntimeError("boom"))
        entries = await check_memfiles(client=client)
        assert entries[0]["level"] == "warning"
        assert "boom" in entries[0]["hint"]


class _FakeSharefileClient:
    """Minimal stand-in for the sharefile plugin MCP client."""

    def __init__(self, payload):
        self._payload = payload

    async def call_tool(self, name, arguments=None):
        assert name == "__check"
        return json.dumps(self._payload)


class TestCheckSharefileFunction:
    """Tests for check_sharefile() — the tunnel facts the harness composes."""

    @pytest.mark.asyncio
    async def test_client_unavailable(self):
        """No client at all — distinct from "the plugin answered, tunnel down"."""
        entries = await check_sharefile()
        assert entries[0]["component"] == "sharefile"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "plugin_offline"

    @pytest.mark.asyncio
    async def test_active_tunnel_reports_its_url(self):
        client = _FakeSharefileClient({
            "active": True, "state": "active",
            "url": "https://x.lhr.life", "reason": "", "provider": "localhost.run",
        })
        entries = await check_sharefile(client=client)
        assert entries[0]["level"] == "ok"
        assert entries[0]["value"] == "https://x.lhr.life"
        assert "hint" not in entries[0]

    @pytest.mark.asyncio
    async def test_down_tunnel_names_the_provider_and_carries_its_reason(self):
        """The hint must not paste one provider's remediation onto another's
        failure: the provider is a fact (in the value) and the reason is
        quoted verbatim from that provider."""
        client = _FakeSharefileClient({
            "active": False, "state": "failed", "url": "",
            "provider": "cloudflare",
            "reason": "cloudflared not found ('cloudflared').",
        })
        entries = await check_sharefile(client=client)
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "offline (cloudflare)"
        assert "cloudflared not found" in entries[0]["hint"]
        # The ngrok-specific remediation must not leak into another provider.
        assert "NGROK_AUTHTOKEN" not in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_down_tunnel_without_a_reason_still_names_the_provider(self):
        client = _FakeSharefileClient({
            "active": False, "state": "failed", "url": "",
            "provider": "ngrok", "reason": "",
        })
        entries = await check_sharefile(client=client)
        assert entries[0]["value"] == "offline (ngrok)"
        assert "sharefile.json5" in entries[0]["hint"]


class TestCheckLocalEmbed:
    """check_local_embed probes the ACTIVE embedding endpoint uniformly.

    Every provider — local-embed or a cloud API like SiliconFlow — is one
    ordinary OpenAI-compatible endpoint: the api_key resolves like the model
    section's and rides as a Bearer header, then ``/models`` is probed.
    Only the ACTIVE provider is probed; nothing inactive is ever checked.
    """

    @staticmethod
    def _endpoint(**overrides):
        ep = {
            "provider": "siliconflow",
            "base_url": "https://api.siliconflow.cn/v1",
            "api_key": "${SILICONFLOW_API_KEY}",
            "model": "BAAI/bge-m3",
        }
        ep.update(overrides)
        return ep

    def _http(self, seen, models=None, error=None):
        """Fake httpx2.AsyncClient recording the request headers."""
        class _FakeResp:
            def __init__(self, models):
                self._models = models
            def raise_for_status(self):
                pass
            def json(self):
                return {"data": self._models}

        class _FakeHttp:
            def __init__(self, *a, **k):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *a):
                return False
            async def get(self, url, headers=None):
                if error is not None:
                    raise error
                seen["url"] = url
                seen["authorization"] = (headers or {}).get("Authorization")
                return _FakeResp(models or [])

        return _FakeHttp

    @pytest.mark.asyncio
    async def test_no_base_url_offline(self):
        """No configured endpoint → offline/not configured."""
        with patch("slife.plugins.memdb.embedding_config.get_active_endpoint",
                   return_value={"base_url": "", "api_key": "", "model": ""}):
            entries = await check_local_embed()
        assert entries[0]["component"] == "local_embed"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "offline (no base_url)"
        assert "embeddings_model_set" in entries[0]["hint"]

    @pytest.mark.asyncio
    async def test_active_cloud_provider_probed_with_auth(self):
        """Active = siliconflow: probed with the RESOLVED Bearer key — the old
        401 (probe without a key) is the regression being guarded."""
        seen = {}
        with patch("slife.plugins.memdb.embedding_config.get_active_endpoint",
                   return_value=self._endpoint()), \
             patch("slife.tools.system.httpx2.AsyncClient", self._http(
                 seen, models=[{"id": "BAAI/bge-m3"}])) , \
             patch("slife.config._resolve_secret", return_value="sk-real"):
            entries = await check_local_embed()
        assert entries[0]["level"] == "ok"
        assert seen["url"] == "https://api.siliconflow.cn/v1/models"
        assert seen["authorization"] == "Bearer sk-real"
        assert entries[0]["value"] == \
            "BAAI/bge-m3 (1 models at https://api.siliconflow.cn/v1)"

    @pytest.mark.asyncio
    async def test_unresolvable_placeholder_key_not_sent(self):
        """A key credstore can't resolve is never sent as a literal token —
        the probe still runs (header just absent)."""
        seen = {}
        with patch("slife.plugins.memdb.embedding_config.get_active_endpoint",
                   return_value=self._endpoint()), \
             patch("slife.tools.system.httpx2.AsyncClient", self._http(
                 seen, models=[{"id": "BAAI/bge-m3"}])), \
             patch("slife.config._resolve_secret",
                   return_value="${SILICONFLOW_API_KEY}"):
            entries = await check_local_embed()
        assert entries[0]["level"] == "ok"
        assert seen["authorization"] is None

    @pytest.mark.asyncio
    async def test_local_embed_is_an_ordinary_endpoint(self):
        """local-embed is handled exactly like a cloud endpoint — a model
        list without the old loaded/available metadata is healthy, not a
        "model not loaded" warning."""
        seen = {}
        with patch("slife.plugins.memdb.embedding_config.get_active_endpoint",
                   return_value={
                       "provider": "local_embed",
                       "base_url": "http://127.0.0.1:17347/v1",
                       "api_key": "local",
                       "model": "bge-m3",
                   }), \
             patch("slife.tools.system.httpx2.AsyncClient", self._http(
                 seen, models=[{"id": "bge-m3"}])), \
             patch("slife.config._resolve_secret", return_value="local"):
            entries = await check_local_embed()
        assert entries[0]["level"] == "ok"
        assert entries[0]["value"] == "bge-m3 (1 models at http://127.0.0.1:17347/v1)"
        assert "17347" in seen["url"]
        # The ordinary-path key "local" still rides as the Bearer header.
        assert seen["authorization"] == "Bearer local"

    @pytest.mark.asyncio
    async def test_probe_error_reports_unreachable(self):
        seen = {}
        with patch("slife.plugins.memdb.embedding_config.get_active_endpoint",
                   return_value=self._endpoint()), \
             patch("slife.tools.system.httpx2.AsyncClient", self._http(
                 seen, error=RuntimeError("Connection error."))), \
             patch("slife.config._resolve_secret", return_value="sk-real"):
            entries = await check_local_embed()
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "unavailable"
        assert "unreachable" in entries[0]["hint"]


class TestCheckToolsInternal:
    """Per-plugin check tools are not Tool classes at all — ``system_health``
    is the ONE registered health tool and aggregates the ``check_*``
    functions.  (E8: the nine unregistered ``Check*Tool`` wrapper classes
    were dead code — the module header used to list them as live LLM tools;
    they are gone, and the aggregated tool is the only LLM-facing surface.)
    """

    def test_system_health_stays_auto_registered(self):
        assert SystemHealthTool.__dict__.get("_skip_auto_register") is not True

    def test_no_check_wrapper_classes_remain(self):
        import slife.tools.system as system_mod

        wrappers = {
            name for name in vars(system_mod)
            if name.startswith("Check") and name.endswith("Tool")
        }
        # CheckAsyncTool/CancelAsyncTool are real registered tools; the
        # per-subsystem check wrappers are the deleted dead set.
        assert not (wrappers - {"CheckAsyncTool", "CancelAsyncTool"})


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
            assert result[0]["value"].startswith("not_configured")

    @pytest.mark.asyncio
    async def test_configured_but_client_unavailable(self):
        with patch(
            "slife.plugins.media.config.load_media_config",
            return_value=MagicMock(is_empty=MagicMock(return_value=False)),
        ):
            result = await check_media()
            assert result[0]["component"] == "media"
            assert result[0]["value"] == "offline"
            assert "Restart slife" in result[0]["hint"]

    @pytest.mark.asyncio
    async def test_configured_probes_plugin_facts(self):
        with patch(
            "slife.plugins.media.config.load_media_config",
            return_value=MagicMock(is_empty=MagicMock(return_value=False)),
        ):
            payload = {
                "configured": True, "error": "",
                "providers": [{"id": "p1", "api": "openai-images",
                               "kinds": ["image"], "has_api_key": True}],
            }
            client = MagicMock()
            client.call_tool = AsyncMock(return_value=json.dumps(payload))
            result = await check_media(client=client)
            client.call_tool.assert_called_once_with("__check")
            keys = {e["key"] for e in result}
            assert "enabled" in keys and "p1" in keys
            p1 = next(e for e in result if e["key"] == "p1")
            assert p1["level"] == "ok"
            assert p1["value"] == "image (openai-images)"

    @pytest.mark.asyncio
    async def test_provider_without_a_key_warns_with_the_remedy(self):
        with patch(
            "slife.plugins.media.config.load_media_config",
            return_value=MagicMock(is_empty=MagicMock(return_value=False)),
        ):
            payload = {
                "configured": True, "error": "",
                "providers": [{"id": "p1", "api": "openai-images",
                               "kinds": ["image"], "has_api_key": False}],
            }
            client = MagicMock()
            client.call_tool = AsyncMock(return_value=json.dumps(payload))
            result = await check_media(client=client)
            p1 = next(e for e in result if e["key"] == "p1")
            assert p1["level"] == "warning"
            assert "api_key" in p1["hint"]


class TestCheckJobCoding:
    """Tests for check_job_coding() — built-in jobs plugin, __check probe."""

    @pytest.mark.asyncio
    async def test_offline_warns(self):
        result = await check_job_coding()
        assert result[0]["component"] == "job-coding"
        assert result[0]["value"] == "offline"
        assert "Restart slife" in result[0]["hint"]

    @pytest.mark.asyncio
    async def test_probes_plugin_facts(self):
        payload = {
            "jobs_dir": r"C:\jobs", "jobs": 2,
            "job_names": ["summarize", "translate"],
            "llm_model": "scnet/DeepSeek-V4-Flash-0731", "error": "",
            "mcp_gateway": {"port": 1234, "source": "env", "connected": True},
        }
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_job_coding(client=client)
        client.call_tool.assert_called_once_with("__check")
        keys = {e["key"] for e in result}
        assert keys == {"jobs", "llm_model", "mcp_gateway"}
        gw = next(e for e in result if e["key"] == "mcp_gateway")
        assert gw["level"] == "ok"
        assert gw["value"] == "connected (port 1234)"
        jobs = next(e for e in result if e["key"] == "jobs")
        assert jobs["level"] == "ok"
        assert jobs["value"] == "2 (summarize, translate)"
        model = next(e for e in result if e["key"] == "llm_model")
        assert model["level"] == "ok" and model["value"] == "scnet/DeepSeek-V4-Flash-0731"
        assert "hint" not in model

    @pytest.mark.asyncio
    async def test_unresolved_llm_warns(self):
        payload = {"jobs_dir": r"C:\jobs", "jobs": 0, "job_names": [],
                   "llm_model": "unconfigured", "error": ""}
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_job_coding(client=client)
        model = next(e for e in result if e["key"] == "llm_model")
        assert model["level"] == "warning" and model["value"] == "unconfigured"
        assert "job_coding_model" in model["hint"]

    @pytest.mark.asyncio
    async def test_known_port_without_a_live_client_is_not_a_problem(self):
        """Jobs reach MCP tools through the plugin's gateway, and the
        connection opens on the job's first ``mcp.call`` — a client that has
        never been built is the design.  The check used to warn here on every
        fresh session and prescribe "re-run system_health shortly", which no
        probe could ever clear (the probe never connects)."""
        payload = {"jobs_dir": r"C:\jobs", "jobs": 1, "job_names": ["x"],
                   "llm_model": "m", "error": "",
                   "mcp_gateway": {"port": 1, "source": "env", "connected": False}}
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_job_coding(client=client)
        gw = next(e for e in result if e["key"] == "mcp_gateway")
        assert gw["level"] == "ok"
        assert gw["value"] == "connects on demand (port 1)"
        assert "hint" not in gw

    @pytest.mark.asyncio
    async def test_no_gateway_port_is_the_failure(self):
        """Without a port every ``mcp.call`` fails — the one state worth a
        warning, and the fact the probe can actually distinguish."""
        payload = {"jobs_dir": r"C:\jobs", "jobs": 1, "job_names": ["x"],
                   "llm_model": "m", "error": "",
                   "mcp_gateway": {"port": None, "source": "", "connected": False}}
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_job_coding(client=client)
        gw = next(e for e in result if e["key"] == "mcp_gateway")
        assert gw["level"] == "warning"
        assert gw["value"] == "unavailable (no gateway port)"
        assert "mcp.call" in gw["hint"]

    @pytest.mark.asyncio
    async def test_jobs_dir_is_named_when_there_are_no_jobs(self):
        payload = {"jobs_dir": r"C:\jobs", "jobs": 0, "job_names": [],
                   "llm_model": "m", "error": ""}
        client = MagicMock()
        client.call_tool = AsyncMock(return_value=json.dumps(payload))
        result = await check_job_coding(client=client)
        jobs = next(e for e in result if e["key"] == "jobs")
        assert jobs["level"] == "ok"
        assert jobs["value"] == r"none (add a .py file to C:\jobs or use job-write)"


class TestCheckToolCatalog:
    """Tests for check_tool_catalog() — the tools.db exam (a gap: the catalog
    is the single source of truth for tools and was never checked)."""

    _FACTS = {
        "tools": 1538, "servers": 19, "loaded": 12,
        "semantic": {"configured": True, "available": True,
                     "semantic_ready": True, "state": "ready", "reason": "",
                     "model": "BAAI/bge-m3", "dimension": 1024, "unembedded": 0},
    }

    @pytest.mark.asyncio
    async def test_no_context_reports_nothing(self):
        """Called outside a service (a unit test, a bare tool) — nothing to
        say rather than a false alarm."""
        assert await check_tool_catalog() == []

    @pytest.mark.asyncio
    async def test_catalog_service_missing_is_a_warning(self):
        ctx = MagicMock(catalog=None)
        entries = await check_tool_catalog(ctx=ctx)
        assert entries[0]["component"] == "tool_catalog"
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "unavailable"

    @pytest.mark.asyncio
    async def test_facts_are_interpreted(self):
        ctx = MagicMock(catalog=MagicMock())
        with patch("slife.mcp.host_server._host_catalog_facts",
                   AsyncMock(return_value=self._FACTS)):
            entries = await check_tool_catalog(ctx=ctx)
        keys = {e["key"] for e in entries}
        assert keys == {"db", "semantic"}
        db = next(e for e in entries if e["key"] == "db")
        assert db["level"] == "ok"
        assert db["value"] == "1538 tools, 19 servers, 12 loaded"
        sem = next(e for e in entries if e["key"] == "semantic")
        assert sem["level"] == "ok"
        assert sem["value"] == "ready (BAAI/bge-m3, dim=1024)"

    @pytest.mark.asyncio
    async def test_broken_catalog_reports_the_reason(self):
        ctx = MagicMock(catalog=MagicMock())
        with patch("slife.mcp.host_server._host_catalog_facts",
                   AsyncMock(return_value={"error": "catalog probe failed: disk"})):
            entries = await check_tool_catalog(ctx=ctx)
        assert entries[0]["level"] == "warning"
        assert entries[0]["value"] == "probe failed"
        assert "disk" in entries[0]["hint"]


class TestOkEntriesCarryNoHint:
    """The mechanical enforcement of the value/hint rule.

    ``value`` is the fact (what the healthy section prints); ``hint`` is the
    remedy and is rendered only for warning/error entries.  A fact parked in
    the hint of an ``ok`` entry is invisible to the report, so this test fails
    the moment one is added — pointing the author at ``value`` instead.
    """

    @pytest.mark.asyncio
    async def test_every_check_keeps_its_healthy_entries_hintless(self):
        import slife.tools.system as system_mod

        cases = [
            ("check_memdb", {
                "db": {"exists": True, "size_mb": 1.0, "path": "slife.db"},
                "semantic": {"configured": True, "available": True,
                             "semantic_ready": True, "state": "ready",
                             "reason": "", "model": "m", "dimension": 3,
                             "unembedded": 0},
            }),
            ("check_memfiles", {"ok": True, "connected": True, "state": "ready",
                                "semantic_ready": True, "unembedded": 0,
                                "reason": ""}),
            ("check_sharefile", {"active": True, "state": "active",
                                 "url": "https://x", "reason": "",
                                 "provider": "ngrok"}),
            ("check_a2a", {"enabled": True, "connected": True, "agent_name": "a",
                           "status": "idle", "broker": "b", "peers": [],
                           "queued": {}}),
            ("check_media", {"configured": True, "error": "",
                             "providers": [{"id": "p", "api": "x",
                                            "kinds": ["image"],
                                            "has_api_key": True}]}),
            ("check_job_coding", {"jobs_dir": "d", "jobs": 1, "job_names": ["j"],
                                  "llm_model": "m", "error": "",
                                  "mcp_gateway": {"port": 1, "connected": True}}),
        ]
        for name, payload in cases:
            client = MagicMock()
            client.call_tool = AsyncMock(return_value=json.dumps(payload))
            entries = await getattr(system_mod, name)(client=client)
            for entry in entries:
                if entry["level"] in ("ok", "info"):
                    assert not entry.get("hint"), (
                        f"{name} puts a fact in the hint of a healthy "
                        f"{entry['key']!r} entry — move it into value: {entry}"
                    )

    @pytest.mark.asyncio
    async def test_media_and_watchdog_placeholders_are_hintless(self):
        import slife.tools.system as system_mod

        with patch("slife.plugins.media.config.load_media_config",
                   return_value=MagicMock(is_empty=MagicMock(return_value=True))):
            entries = await system_mod.check_media()
        assert entries[0]["level"] == "ok" and not entries[0].get("hint")

        with patch("slife.tools.system.get_startup_records", return_value=[]):
            entries = system_mod.check_watchdog()
        assert entries[0]["level"] == "ok" and not entries[0].get("hint")
