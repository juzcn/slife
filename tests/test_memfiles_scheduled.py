"""Memfiles scheduled-task registry + reports store methods (real temp DB).

Covers: reports write/mirror/backfill, the scheduled_tasks / scheduled_runs
state machine, the report kind joining the unified search/drainer view, and
the scheduled-task MCP server tools.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = pytest.mark.unit

import slife.plugins.memfiles.server as plugin
from slife.plugins.memfiles.store import MemfilesStore


async def _real_store(tmp_path) -> MemfilesStore:
    store = MemfilesStore(tmp_path / ".index.db")
    await store.setup(embedding_dim=0, embedding_model="")
    return store


# ── scheduled_tasks ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_scheduled_task_create_and_update(tmp_path):
    store = await _real_store(tmp_path)
    try:
        r = await store.upsert_scheduled_task(
            "daily_diary", "write the diary", "0 0 * * *", "Asia/Shanghai",
        )
        assert r["name"] == "daily_diary"

        task = await store.get_scheduled_task("daily_diary")
        assert task is not None
        assert task["schedule"] == "0 0 * * *"
        assert task["timezone"] == "Asia/Shanghai"
        assert task["enabled"] == 1

        # update (same name → same id)
        r2 = await store.upsert_scheduled_task(
            "daily_diary", "new description", "0 9 * * *",
        )
        task2 = await store.get_scheduled_task("daily_diary")
        assert task2["description"] == "new description"
        assert task2["schedule"] == "0 9 * * *"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_scheduled_tasks_enabled_filter(tmp_path):
    store = await _real_store(tmp_path)
    try:
        await store.upsert_scheduled_task("a", schedule="0 9 * * *")
        await store.upsert_scheduled_task("b", schedule="0 9 * * *", enabled=False)
        all_tasks = await store.list_scheduled_tasks()
        assert {t["name"] for t in all_tasks} == {"a", "b"}
        enabled = await store.list_scheduled_tasks(enabled_only=True)
        assert [t["name"] for t in enabled] == ["a"]
    finally:
        await store.close()


# ── task removal ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_remove_scheduled_task_cleans_runs(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        await store.record_scheduled_run(task["task_id"], "2026-08-25T00:00:00")

        assert await store.remove_scheduled_task("daily") is True
        assert await store.get_scheduled_task("daily") is None
        assert await store.list_scheduled_runs(task_id=task["task_id"]) == []
        # removing again is a no-op
        assert await store.remove_scheduled_task("daily") is False
    finally:
        await store.close()

@pytest.mark.asyncio
async def test_record_and_mark_missed(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")

        r = await store.record_scheduled_run(task["task_id"], "2026-08-25T00:00:00")
        assert r["run_id"] > 0

        runs = await store.list_scheduled_runs(task_id=task["task_id"])
        assert len(runs) == 1
        assert runs[0]["status"] == "pending"  # success unconfirmed until a report

        # missed for a different due_at
        await store.mark_run_missed(task["task_id"], "2026-08-26T00:00:00")
        runs = await store.list_scheduled_runs(status="missed")
        assert len(runs) == 1
        assert runs[0]["due_at"] == "2026-08-26T00:00:00"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mark_run_skipped_closes_missed_and_failed(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        await store.record_scheduled_run(task["task_id"], "2026-08-25T00:00:00")
        await store.mark_run_failed(task["task_id"], "2026-08-25T00:00:00", "boom")
        await store.mark_run_missed(task["task_id"], "2026-08-26T00:00:00")
        await store.record_scheduled_run(task["task_id"], "2026-08-27T00:00:00")

        await store.mark_run_skipped(task["task_id"], "2026-08-26T00:00:00")
        await store.mark_run_skipped(task["task_id"], "2026-08-25T00:00:00")
        runs = await store.list_scheduled_runs(status="skipped")
        assert {r["due_at"] for r in runs} == {
            "2026-08-25T00:00:00", "2026-08-26T00:00:00",
        }

        # a pending (unconfirmed) run is not closed by skip — only missed/failed
        await store.mark_run_skipped(task["task_id"], "2026-08-27T00:00:00")
        runs = await store.list_scheduled_runs(status="pending")
        assert len(runs) == 1
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_mark_run_failed_only_pending(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        await store.record_scheduled_run(task["task_id"], "2026-08-25T00:00:00")
        await store.mark_run_failed(task["task_id"], "2026-08-25T00:00:00",
                                    "interrupted")
        runs = await store.list_scheduled_runs(task_id=task["task_id"])
        assert runs[0]["status"] == "failed"
        assert runs[0]["error"] == "interrupted"

        # a ran (report-backed) run is not downgraded by a late cancel
        await store.record_scheduled_run(task["task_id"], "2026-08-26T00:00:00")
        await store.upsert_report(task["task_id"], "Ok", "fine")
        await store.mark_run_failed(task["task_id"], "2026-08-26T00:00:00",
                                    "late cancel")
        runs = await store.list_scheduled_runs(task_id=task["task_id"])
        by_due = {r["due_at"]: r for r in runs}
        assert by_due["2026-08-26T00:00:00"]["status"] == "ran"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_fail_unconfirmed_runs_sweep(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")

        # pending without a report → swept to failed
        await store.record_scheduled_run(task["task_id"], "2026-08-25T00:00:00")
        await store.record_scheduled_run(task["task_id"], "2026-08-26T00:00:00",
                                         status="ran")

        # 'ran' rows are always report-backed = confirmed success → untouched
        await store.record_scheduled_run(task["task_id"], "2026-08-27T00:00:00",
                                         status="ran")
        await store.upsert_report(task["task_id"], "Done", "ok")

        stale = await store.fail_unconfirmed_runs()
        assert {r["due_at"] for r in stale} == {
            "2026-08-25T00:00:00",
        }
        assert stale[0]["name"] == "daily"

        runs = await store.list_scheduled_runs(task_id=task["task_id"])
        by_due = {r["due_at"]: r for r in runs}
        assert by_due["2026-08-25T00:00:00"]["status"] == "failed"
        assert by_due["2026-08-26T00:00:00"]["status"] == "ran"
        assert by_due["2026-08-25T00:00:00"]["error"] == \
            "slife restarted before completion"
        assert by_due["2026-08-27T00:00:00"]["status"] == "ran"

        # idempotent — a second sweep can't repeat the run
        assert await store.fail_unconfirmed_runs() == []
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_record_run_idempotent_on_due_at(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        await store.record_scheduled_run(task["task_id"], "2026-08-25T00:00:00")
        await store.record_scheduled_run(task["task_id"], "2026-08-25T00:00:00")
        runs = await store.list_scheduled_runs(task_id=task["task_id"])
        assert len(runs) == 1
    finally:
        await store.close()


# ── reports + backfill ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upsert_report_mirrors_md_and_backfills_run(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        due = "2026-08-25T00:00:00"
        await store.record_scheduled_run(task["task_id"], due)

        rep = await store.upsert_report(
            task_id=task["task_id"], title="Daily Report",
            content="Today went well.",
        )
        assert rep["kind"] == "report"
        md = (tmp_path / "reports" / "daily-report.md").read_text(encoding="utf-8")
        assert "Today went well." in md

        # report_id backfilled onto the run, and the report arrival confirms
        # it (pending → ran) — the one success writeback
        runs = await store.list_scheduled_runs(task_id=task["task_id"])
        assert runs[0]["report_id"] == rep["doc_id"]
        assert runs[0]["status"] == "ran"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_upsert_report_append_same_title(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        await store.record_scheduled_run(task["task_id"], "2026-08-25T00:00:00")
        await store.record_scheduled_run(task["task_id"], "2026-08-26T00:00:00")
        a = await store.upsert_report(task["task_id"], "Summary", "first")
        b = await store.upsert_report(task["task_id"], "Summary", "second")
        assert a["doc_id"] == b["doc_id"]  # same title → same report, appended
        md = (tmp_path / "reports" / "summary.md").read_text(encoding="utf-8")
        assert "first" in md and "second" in md
        # both runs got linked to the (single) report and confirmed ran
        runs = await store.list_scheduled_runs(task_id=task["task_id"])
        assert all(r["report_id"] == a["doc_id"] for r in runs)
        assert all(r["status"] == "ran" for r in runs)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_upsert_report_with_due_at_confirms_exact_run(tmp_path):
    """A backfill report with due_at confirms the targeted run even when a
    newer (staler) missed run exists — never grabs the wrong one."""
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        newer, old = "2026-08-26T00:00:00", "2026-08-25T00:00:00"
        for due in (newer, old):
            await store.mark_run_missed(task["task_id"], due)
        # Backfill transitions the OLD run to pending in place (ON CONFLICT).
        await store.record_scheduled_run(task["task_id"], old)
        rep = await store.upsert_report(
            task_id=task["task_id"], title="Backfill", content="done",
            due_at=old,
        )
        runs = {r["due_at"]: r for r in
                await store.list_scheduled_runs(task_id=task["task_id"])}
        assert runs[old]["status"] == "ran"           # the backfilled run
        assert runs[old]["report_id"] == rep["doc_id"]
        assert runs[newer]["status"] == "missed"      # newer stale run untouched
        assert runs[newer]["report_id"] is None
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_list_and_get_report(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        await store.upsert_report(task["task_id"], "Alpha", "content a",
                                  period_start="2026-08-01", period_end="2026-08-07")
        await store.upsert_report(task["task_id"], "Beta", "content b")

        listed = await store.list_reports(task_id=task["task_id"])
        assert listed["total"] == 2
        titles = {e["title"] for e in listed["entries"]}
        assert titles == {"Alpha", "Beta"}

        # Pick Alpha by title, not by position: listing is newest-first and
        # both reports were inserted within the same second, so the tie on
        # the second-precision created_at makes entries[0] ambiguous.
        alpha = next(e for e in listed["entries"] if e["title"] == "Alpha")
        one = await store.get_report(alpha["id"])
        assert one is not None
        assert one["period_start"] == "2026-08-01"
        assert one["period_end"] == "2026-08-07"
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_report_kind_in_unified_search(tmp_path):
    store = await _real_store(tmp_path)
    try:
        task = await store.upsert_scheduled_task("daily", schedule="0 0 * * *")
        await store.upsert_report(task["task_id"], "Quarterly", "revenue up 20%", tags="finance")
        await store.upsert_note("Python", "asyncio notes", "py")

        # report is a searchable kind
        hits = await store.search("revenue", kind="report", limit=10)
        assert any("Quarterly" in h.get("text", "") or "revenue" in h.get("text", "") for h in hits)
    finally:
        await store.close()


@pytest.mark.asyncio
async def test_report_in_drainer_view(tmp_path):
    store = await _real_store(tmp_path)
    try:
        # embedding dim 0 → no vec tables; drainer skips reports (count 0)
        assert await store.count_unembedded() == 0
        await store.close()

        # with embedding enabled (dim>0 but vec may be unavailable), a report
        # joins the unified unembedded view if vec tables exist.
        store2 = MemfilesStore(tmp_path / ".index2.db")
        await store2.setup(embedding_dim=4, embedding_model="test")
        try:
            if store2._embedding_dim <= 0:
                pytest.skip("sqlite-vec unavailable")
            task = await store2.upsert_scheduled_task("daily", schedule="0 0 * * *")
            await store2.upsert_report(task["task_id"], "Quarterly", "revenue up", tags="")
            kinds = {d["kind"] for d in await store2.get_unembedded_docs(10)}
            assert "report" in kinds
        finally:
            await store2.close()
    finally:
        if store._conn is not None:
            await store.close()


# ═══════════════════════════════════════════════════════════════════════
# Scheduled-task MCP server tools (real store via patched _ensure_store)
# ═══════════════════════════════════════════════════════════════════════


class TestScheduledServerTools:
    @pytest.mark.asyncio
    async def test_task_set_validates_cron(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                bad = await plugin.scheduled_task_set(name="x", schedule="61 * * * *")
                assert "invalid cron" in bad.lower()
                ok = await plugin.scheduled_task_set(
                    name="daily", description="d", schedule="0 9 * * *",
                )
                info = json.loads(ok)
                assert info["name"] == "daily" and info["task_id"] >= 1
                # 'manual' bypasses cron validation
                manual = await plugin.scheduled_task_set(name="m", schedule="manual")
                assert "task_id" in json.loads(manual)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_task_set_rejects_invalid_name(self, tmp_path):
        """Task names double as the subagent worker name, so they must be safe
        identifiers — Chinese/space/over-long names are rejected."""
        store = await _real_store(tmp_path)
        try:
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                for bad in ("每日日报", "Daily Report", "-lead", ".dot", "x" * 65):
                    err = await plugin.scheduled_task_set(
                        name=bad, schedule="0 9 * * *",
                    )
                    assert "not a valid task/worker name" in err, bad
                ok = await plugin.scheduled_task_set(
                    name="daily_report", schedule="0 9 * * *",
                )
                assert "task_id" in json.loads(ok)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_task_remove_and_list(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                await plugin.scheduled_task_set(name="a", schedule="0 9 * * *")
                await plugin.scheduled_task_set(name="b", schedule="0 9 * * *", enabled=False)
                listed = json.loads(await plugin.scheduled_task_list())
                assert listed["total"] == 2
                enabled = json.loads(await plugin.scheduled_task_list(enabled_only=True))
                assert enabled["total"] == 1

                assert "removed" in await plugin.scheduled_task_remove(name="a")
                assert "not found" in await plugin.scheduled_task_remove(name="a")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_run_list_and_skip(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                await plugin.scheduled_task_set(name="daily", schedule="0 0 * * *")
                task = await store.get_scheduled_task("daily")
                await store.record_scheduled_run(task["id"], "2026-08-25T00:00:00")
                await store.mark_run_missed(task["id"], "2026-08-26T00:00:00")

                runs = json.loads(await plugin.scheduled_run_list(name="daily"))
                assert runs["total"] == 2
                missed = json.loads(await plugin.scheduled_run_list(name="daily", status="missed"))
                assert missed["total"] == 1

                skip = await plugin.scheduled_run_skip("daily", "2026-08-26T00:00:00")
                assert "skipped" in skip
                skipped = json.loads(await plugin.scheduled_run_list(name="daily", status="skipped"))
                assert skipped["total"] == 1
                # unknown task
                assert "not found" in await plugin.scheduled_run_list(name="nope")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_fail_unconfirmed_server_tool(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                await plugin.scheduled_task_set(name="daily", schedule="0 0 * * *")
                task = await store.get_scheduled_task("daily")
                await store.record_scheduled_run(task["id"], "2026-08-25T00:00:00")
                fail_unconfirmed = getattr(plugin, "__scheduled_fail_unconfirmed")
                resp = json.loads(await fail_unconfirmed())
                assert resp["failed"] == 1
                assert resp["runs"][0]["name"] == "daily"
                # idempotent
                assert json.loads(await fail_unconfirmed())["failed"] == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_save_cron_report_links_run_and_reads_back(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                await plugin.scheduled_task_set(name="daily", schedule="0 0 * * *")
                task = await store.get_scheduled_task("daily")
                await store.record_scheduled_run(task["id"], "2026-08-25T00:00:00")

                saved = await plugin.save_cron_report(
                    name="daily", title="Daily Report", content="All good.",
                )
                assert saved.startswith("Saved: ")
                # run got linked
                runs = await store.list_scheduled_runs(task_id=task["id"])
                assert runs[0]["report_id"] is not None

                reports = json.loads(await plugin.report_list(name="daily"))
                assert reports["total"] == 1
                rid = reports["entries"][0]["id"]
                content = await plugin.report_read(rid)
                assert "All good." in content

                # unknown task errors cleanly
                err = await plugin.save_cron_report(name="ghost", title="t", content="c")
                assert "not found" in err
        finally:
            await store.close()
