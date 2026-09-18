"""Scheduled-task LLM-tool validation (A9) — cron + timezone are rejected at
set time, never stored-enabled-and-silently-never-fired."""

import pytest; pytestmark = pytest.mark.unit

from slife.tools.schedule import ScheduledTaskSetTool


class TestScheduledTaskSetValidation:
    def _tool(self) -> ScheduledTaskSetTool:
        # No memfiles client → _call() returns the offline string, but pure
        # validation runs in-process BEFORE _call, which is what we test.
        return ScheduledTaskSetTool()

    @pytest.mark.asyncio
    async def test_rejects_impossible_cron_strict(self):
        """A9: "0 9 31 2 *" (Feb 31) passes croniter non-strict but can never
        fire — it must be rejected at set time (strict=True, matching the
        scheduler's consuming next_run)."""
        tool = self._tool()
        r = await tool.execute(name="daily", description="d", schedule="0 9 31 2 *")
        assert "invalid cron expression" in r

    @pytest.mark.asyncio
    async def test_rejects_bad_timezone(self):
        """A9: a bad IANA timezone must be rejected up front with a clear
        error, not surface later as a ZoneInfoNotFoundError in the trigger
        loop."""
        tool = self._tool()
        r = await tool.execute(
            name="daily", description="d", schedule="0 9 * * *",
            timezone="Not/AZone",
        )
        assert "invalid timezone" in r

    @pytest.mark.asyncio
    async def test_valid_schedule_passes_validation(self):
        """A valid expression is not rejected; it proceeds to the (offline)
        store call rather than returning a validation error."""
        tool = self._tool()
        r = await tool.execute(name="daily", description="d", schedule="0 9 * * *")
        assert "invalid cron" not in r
        assert "invalid timezone" not in r
        # Not merely "no validation error": it must actually reach the store
        # call, so a passing assert can never be the offline message instead.
        assert r == tool.offline_message

    @pytest.mark.asyncio
    async def test_rejects_empty_description(self):
        """The description IS the worker's instruction, so an empty one is
        refused at set time — a task that reaches a worker with no
        instruction becomes self-invented work with real side effects."""
        tool = self._tool()
        for empty in ("", "   "):
            assert "description is required" in await tool.execute(
                name="daily", description=empty, schedule="0 9 * * *",
            )

    @pytest.mark.asyncio
    async def test_guessed_parameter_still_reports_the_missing_instruction(self):
        """A caller that passes `prompt` instead of `description` reaches the
        tool with an empty description (the unknown key lands in **kwargs), so
        even on this path the failure is the missing instruction — the
        unknown NAME is refused one layer up, by the registry."""
        tool = self._tool()
        r = await tool.execute(
            name="daily", prompt="reply OK", schedule="0 9 * * *",
        )
        assert "description is required" in r
