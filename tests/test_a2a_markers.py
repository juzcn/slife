"""Tests for slife.a2a.markers — unified `[Kind:{json}]` identity markers."""

import pytest; pytestmark = pytest.mark.unit

from slife.a2a.markers import (
    HEARTBEAT, HUMAN, REMOTE, SCHEDULE, SUBAGENT, UNKNOWN, WECHAT,
    parse_marker, render_marker,
)


# ── render_marker ───────────────────────────────────────────────────────


class TestRenderMarker:
    """Building the `[Kind:{json}]` marker text."""

    def test_with_payload(self):
        m = render_marker(SUBAGENT, subagent_name="health-check-0735", task_id="abc")
        assert m == '[Subagent:{"subagent_name": "health-check-0735", "task_id": "abc"}]'

    def test_empty_payload_is_empty_object(self):
        # Never a bare `[Kind]` — always parseable.
        assert render_marker(HEARTBEAT) == "[Heartbeat:{}]"

    def test_unicode_payload(self):
        m = render_marker(REMOTE, peer_id="主-01")
        assert parse_marker(m)[0]["peer_id"] == "主-01"


# ── parse_marker ────────────────────────────────────────────────────────


class TestParseMarker:
    """Splitting `[Kind:{json}]  remainder` into identity + text."""

    def test_human_no_marker(self):
        identity, rest = parse_marker("测试一下定时任务")
        assert identity is None
        assert rest == "测试一下定时任务"

    def test_empty_string(self):
        assert parse_marker("") == (None, "")

    def test_round_trip(self):
        m = render_marker(SUBAGENT, subagent_name="health-check-0735", task_id="bd58713c85be")
        identity, rest = parse_marker(m + " 定时任务已完成")
        assert identity == {
            "kind": "Subagent",
            "subagent_name": "health-check-0735",
            "task_id": "bd58713c85be",
        }
        assert rest == "定时任务已完成"

    def test_empty_payload(self):
        identity, rest = parse_marker("[Heartbeat:{}] click")
        assert identity == {"kind": "Heartbeat"}
        assert rest == "click"

    def test_nested_brace_in_payload(self):
        m = render_marker(REMOTE, peer_id="a", task_id="t}")
        identity, rest = parse_marker(m + " hi")
        assert identity == {"kind": "Remote", "peer_id": "a", "task_id": "t}"}
        assert rest == "hi"

    def test_no_marker_prefix_is_not_a_marker(self):
        # A bare `[Heartbeat]` (no `{json}`) is not a valid marker — the
        # grammar requires a payload, so it parses as plain human text.
        identity, rest = parse_marker("[Heartbeat] click")
        assert identity is None
        assert rest == "[Heartbeat] click"

    def test_unknown_kind_normalized(self):
        identity, rest = parse_marker("[Foo:{}] x")
        assert identity == {"kind": UNKNOWN}
        assert rest == "x"

    def test_unknown_kind_preserves_payload(self):
        identity, _ = parse_marker('[Foo:{"bar": 1}] x')
        assert identity == {"kind": UNKNOWN, "bar": 1}


# ── Kind constants ──────────────────────────────────────────────────────


class TestKindConstants:
    """The marker kinds align with the channel/source vocabulary."""

    def test_values(self):
        assert HUMAN == "Human"
        assert WECHAT == "Wechat"
        assert SUBAGENT == "Subagent"
        assert HEARTBEAT == "Heartbeat"
        assert SCHEDULE == "Schedule"
        assert REMOTE == "Remote"
        assert UNKNOWN == "Unknown"
