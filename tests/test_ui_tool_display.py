"""Tests for Slife.ui.tool_display — ToolCallWidget and helper logic."""

# pyright: reportAttributeAccessIssue=false

import pytest
from unittest.mock import MagicMock, patch


def _make_widget(**kwargs):
    """Create a ToolCallWidget with mocked Textual internals.

    ToolCallWidget extends Static — we patch Static.__init__ to skip
    Textual's real constructor, then set up test state manually.
    """
    with patch("slife.ui.tool_display.Static.__init__", return_value=None):
        from slife.ui.tool_display import ToolCallWidget
        w = ToolCallWidget.__new__(ToolCallWidget)
        w.tool_name = kwargs.get("tool_name", "web_search")
        w.tool_args = kwargs.get("tool_args", {"query": "cats"})
        w.tool_call_id = kwargs.get("tool_call_id", "call_abc")
        w._iteration = kwargs.get("_iteration", 1)
        w._max_iterations = kwargs.get("_max_iterations", 10)
        w._is_collapsed = kwargs.get("_is_collapsed", True)
        w._status = kwargs.get("_status", "pending")
        w._result = kwargs.get("_result", "")
        w._result_is_error = kwargs.get("_result_is_error", False)
        w._suffix = kwargs.get("_suffix", "42")
        # Mock update() — the real one needs DOM attachment
        w.update = MagicMock()
        return w


class TestToolCallWidget:
    """Tests for ToolCallWidget — pure logic, no Textual runtime needed."""

    def test_construction_defaults(self):
        w = _make_widget()
        assert w.tool_name == "web_search"
        assert w.tool_args == {"query": "cats"}
        assert w.tool_call_id == "call_abc"
        assert w._is_collapsed is True
        assert w._status == "pending"
        assert w._result == ""
        assert w._result_is_error is False

    def test_set_running(self):
        w = _make_widget()
        w.set_running()
        assert w._status == "running"
        w.update.assert_called_once()
        # Verify Content was passed to update() — check plain text
        content = w.update.call_args[0][0]
        text = content.plain
        assert "Web search" in text

    def test_set_complete_success(self):
        w = _make_widget()
        w.set_complete("Search results here")
        assert w._status == "done"
        assert w._result == "Search results here"
        assert w._result_is_error is False
        w.update.assert_called_once()

    def test_set_complete_error(self):
        w = _make_widget()
        w.set_complete("Something failed", is_error=True)
        assert w._status == "error"
        assert w._result_is_error is True
        w.update.assert_called_once()

    def test_set_complete_truncates_long_result(self):
        w = _make_widget()
        long_result = "x" * 3000
        w.set_complete(long_result)
        assert len(w._result) == 2003  # 2000 + "..."
        assert w._result.endswith("...")

    # ── Build content ────────────────────────────────────────────

    def test_build_content_collapsed_only_header(self):
        w = _make_widget()
        content = w._build_content()
        text = content.plain
        assert "▸" in text
        assert "Web search" in text
        assert "Arguments" not in text
        assert "Result" not in text

    def test_build_content_expanded_includes_detail(self):
        w = _make_widget(_is_collapsed=False, _result="done")
        content = w._build_content()
        text = content.plain
        assert "▾" in text
        assert "Arguments" in text
        assert "Result" in text

    def test_build_content_valid_markup_tags(self):
        """Markup should have balanced Textual markup tags."""
        w = _make_widget(_status="done", _result="hello", _is_collapsed=False)
        content = w._build_content()
        markup = content.markup
        assert markup.count("[") == markup.count("]")

    def test_toggle_flips_and_updates(self):
        w = _make_widget(_is_collapsed=True)
        w.toggle()
        assert w._is_collapsed is False
        w.update.assert_called_once()

    def test_toggle_collapse_back(self):
        w = _make_widget(_is_collapsed=False)
        w.toggle()
        assert w._is_collapsed is True
        w.update.assert_called_once()

    # ── Header line ───────────────────────────────────────────────

    def test_header_line_pending_shows_friendly_label(self):
        w = _make_widget()
        content = w._header_line()
        text = content.plain
        assert "▸" in text
        assert "Web search" in text

    def test_header_line_running_shows_friendly_label(self):
        w = _make_widget(_status="running")
        content = w._header_line()
        text = content.plain
        assert "Web search" in text
        assert "running" in text

    def test_header_line_done_shows_label(self):
        w = _make_widget(_status="done")
        content = w._header_line()
        text = content.plain
        assert "Web search" in text

    def test_header_line_error(self):
        w = _make_widget(_status="error")
        content = w._header_line()
        text = content.plain
        assert "error" in text

    def test_header_line_expanded(self):
        w = _make_widget(_is_collapsed=False)
        content = w._header_line()
        text = content.plain
        assert "▾" in text

    def test_header_line_includes_primary_arg_preview(self):
        w = _make_widget(tool_name="web_search", tool_args={"query": "cats"})
        content = w._header_line()
        text = content.plain
        assert "cats" in text

    def test_header_line_truncates_long_primary_arg(self):
        w = _make_widget(tool_args={"query": "x" * 100})
        content = w._header_line()
        text = content.plain
        assert "…" in text

    def test_header_line_run_command_shows_command(self):
        w = _make_widget(
            tool_name="run_command",
            tool_args={"command": "npm test"},
            _status="running",
        )
        content = w._header_line()
        text = content.plain
        assert "Run command" in text
        assert "npm test" in text

    def test_header_line_unknown_tool_falls_back_to_name(self):
        w = _make_widget(tool_name="custom_tool", tool_args={"x": "1"})
        content = w._header_line()
        text = content.plain
        assert "Custom tool" in text

    # ── Iteration counter ─────────────────────────────────────────

    def test_header_line_includes_iteration(self):
        w = _make_widget(_iteration=3, _max_iterations=10)
        content = w._header_line()
        text = content.plain
        assert "3/10" in text

    def test_header_line_iteration_zero_hidden(self):
        """When iteration is 0 (default/not set), no counter shown."""
        w = _make_widget(_iteration=0, _max_iterations=10)
        content = w._header_line()
        text = content.plain
        assert "0/10" not in text
        assert "/" not in text or "0/" not in text

    def test_header_line_iteration_first_of_ten(self):
        w = _make_widget(_iteration=1, _max_iterations=10)
        content = w._header_line()
        text = content.plain
        assert "1/10" in text

    # ── Detail block ──────────────────────────────────────────────

    def test_detail_block_with_result(self):
        w = _make_widget(_result="Search completed")
        content = w._detail_block()
        text = content.plain
        assert "Arguments" in text
        assert "cats" in text
        assert "Result" in text
        assert "Search completed" in text

    def test_detail_block_with_error(self):
        w = _make_widget(_result="Failure", _result_is_error=True)
        content = w._detail_block()
        text = content.plain
        assert "Error" in text

    def test_detail_block_no_result(self):
        w = _make_widget()
        content = w._detail_block()
        text = content.plain
        assert "Arguments" in text
        assert "Result" not in text
        assert "Error" not in text

    def test_detail_block_no_args(self):
        w = _make_widget(tool_args={})
        content = w._detail_block()
        text = content.plain
        assert "no arguments" in text

    def test_detail_block_shows_all_args(self):
        w = _make_widget(
            tool_name="web_search",
            tool_args={"query": "cats", "num": 5},
        )
        content = w._detail_block()
        text = content.plain
        assert "query" in text
        assert "cats" in text
        assert "num" in text

    def test_detail_block_truncates_long_arg_values(self):
        w = _make_widget(tool_args={"query": "y" * 600})
        content = w._detail_block()
        text = content.plain
        assert "…" in text

    def test_detail_block_multiline_result_shows_truncation_hint(self):
        w = _make_widget(_result="\n".join([f"line {i}" for i in range(30)]))
        content = w._detail_block()
        text = content.plain
        assert "more lines" in text


class TestHelperFunctions:
    """Tests for the module-level helpers."""

    def test_friendly_label_from_tool_name(self):
        from slife.ui.tool_display import _friendly_label
        assert _friendly_label("run_command", "running") == "Run command"
        assert _friendly_label("web_search", "done") == "Web search"

    def test_friendly_label_unknown_tool(self):
        from slife.ui.tool_display import _friendly_label
        label = _friendly_label("my_custom_tool", "running")
        assert "My custom tool" in label

    def test_primary_arg_value_returns_first_string(self):
        from slife.ui.tool_display import _primary_arg_value
        val = _primary_arg_value({"query": "cats", "num": 5})
        assert val == "cats"

    def test_primary_arg_value_fallback_to_first_string(self):
        from slife.ui.tool_display import _primary_arg_value
        val = _primary_arg_value({"x": 1, "y": "hello"})
        assert val == "hello"

    def test_primary_arg_value_no_string_returns_none(self):
        from slife.ui.tool_display import _primary_arg_value
        val = _primary_arg_value({"x": 1, "y": 2})
        assert val is None

    def test_unique_suffix_increments(self):
        from slife.ui.tool_display import _unique_suffix
        a = _unique_suffix()
        b = _unique_suffix()
        assert int(b) == int(a) + 1
