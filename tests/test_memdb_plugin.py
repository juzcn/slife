"""Tests for the memdb plugin server — save-turn orphan guarantee.

Exercises ``_repair_orphan_tool_results`` (the persistence-boundary guard
that ensures no turn is ever saved with an orphaned tool_call).
"""

import pytest; pytestmark = pytest.mark.unit


import slife.plugins.memdb.server as plugin


def _assistant(tool_call_id: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": tool_call_id, "type": "function", "function": {"name": "t", "arguments": "{}"}},
        ],
    }


def _tool_result(tool_call_id: str, content: str = "ok") -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class TestRepairOrphanToolResults:
    def test_no_orphans_no_change(self):
        msgs = [
            _assistant("c1"),
            _tool_result("c1"),
            {"role": "user", "content": "hi"},
        ]
        repaired = plugin._repair_orphan_tool_results(msgs)
        assert repaired == 0
        assert len(msgs) == 3

    def test_inserts_synthetic_result_after_orphaned_call(self):
        msgs = [_assistant("orphan_1")]
        repaired = plugin._repair_orphan_tool_results(msgs)
        assert repaired == 1
        assert len(msgs) == 2
        assert msgs[1] == {
            "role": "tool",
            "tool_call_id": "orphan_1",
            "content": "Error: request cancelled by user",
        }

    def test_parallel_calls_only_missing_ones_repaired(self):
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "a", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                    {"id": "b", "type": "function", "function": {"name": "t", "arguments": "{}"}},
                ],
            },
            _tool_result("a"),
        ]
        repaired = plugin._repair_orphan_tool_results(msgs)
        assert repaired == 1
        # synthetic result for "b" inserted right after the assistant msg
        assert msgs[1]["role"] == "tool"
        assert msgs[1]["tool_call_id"] == "b"
        # the real "a" result stays after
        assert msgs[2]["tool_call_id"] == "a"

    def test_interleaved_orphan_only_affects_its_own_call(self):
        msgs = [
            _assistant("c1"),
            _tool_result("c1"),
            _assistant("c2"),          # orphaned
            _assistant("c3"),
            _tool_result("c3"),
        ]
        repaired = plugin._repair_orphan_tool_results(msgs)
        assert repaired == 1
        assert msgs[3]["role"] == "tool"
        assert msgs[3]["tool_call_id"] == "c2"

    def test_empty_and_none(self):
        assert plugin._repair_orphan_tool_results([]) == 0
        assert plugin._repair_orphan_tool_results(None) == 0

    def test_idempotent(self):
        msgs = [_assistant("x")]
        plugin._repair_orphan_tool_results(msgs)
        second = plugin._repair_orphan_tool_results(msgs)
        assert second == 0
