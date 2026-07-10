"""Tests for slife.ui.tool_display — ToolCallWidget and helper logic."""

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
        # Verify markup was passed to update()
        markup = w.update.call_args[0][0]
        assert "Running" in markup or "Searching" in markup

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

    # ── Build markup ──────────────────────────────────────────────

    def test_build_markup_collapsed_only_header(self):
        w = _make_widget()
        text = w._build_markup()
        assert "▸" in text
        assert "Searching web" in text
        assert "Arguments" not in text
        assert "Result" not in text

    def test_build_markup_expanded_includes_detail(self):
        w = _make_widget(_is_collapsed=False, _result="done")
        text = w._build_markup()
        assert "▾" in text
        assert "Arguments" in text
        assert "Result" in text

    def test_build_markup_valid_rich_tags(self):
        """Markup should have balanced Rich markup tags."""
        w = _make_widget(_status="done", _result="hello", _is_collapsed=False)
        text = w._build_markup()
        assert text.count("[") == text.count("]")

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
        text = w._header_line()
        assert "▸" in text
        assert "Searching web" in text
        assert "pending" in text

    def test_header_line_running_shows_friendly_label(self):
        w = _make_widget(_status="running")
        text = w._header_line()
        assert "Searching web" in text
        assert "running" in text

    def test_header_line_done_shows_past_tense_label(self):
        w = _make_widget(_status="done")
        text = w._header_line()
        assert "Searched web" in text
        assert "done" in text

    def test_header_line_error(self):
        w = _make_widget(_status="error")
        text = w._header_line()
        assert "error" in text

    def test_header_line_expanded(self):
        w = _make_widget(_is_collapsed=False)
        text = w._header_line()
        assert "▾" in text

    def test_header_line_includes_primary_arg_preview(self):
        w = _make_widget(tool_name="web_search", tool_args={"query": "cats"})
        text = w._header_line()
        assert "cats" in text

    def test_header_line_truncates_long_primary_arg(self):
        w = _make_widget(tool_args={"query": "x" * 100})
        text = w._header_line()
        assert "…" in text

    def test_header_line_execute_shell_shows_command(self):
        w = _make_widget(
            tool_name="execute_shell",
            tool_args={"command": "npm test"},
            _status="running",
        )
        text = w._header_line()
        assert "Running command" in text
        assert "npm test" in text

    def test_header_line_unknown_tool_falls_back_to_name(self):
        w = _make_widget(tool_name="custom_tool", tool_args={"x": "1"})
        text = w._header_line()
        assert "Custom tool" in text

    # ── Detail block ──────────────────────────────────────────────

    def test_detail_block_with_result(self):
        w = _make_widget(_result="Search completed")
        text = w._detail_block()
        assert "Arguments" in text
        assert "cats" in text
        assert "Result" in text
        assert "Search completed" in text

    def test_detail_block_with_error(self):
        w = _make_widget(_result="Failure", _result_is_error=True)
        text = w._detail_block()
        assert "Error" in text

    def test_detail_block_no_result(self):
        w = _make_widget()
        text = w._detail_block()
        assert "Arguments" in text
        assert "Result" not in text
        assert "Error" not in text

    def test_detail_block_no_args(self):
        w = _make_widget(tool_args={})
        text = w._detail_block()
        assert "no arguments" in text

    def test_detail_block_highlights_primary_arg(self):
        w = _make_widget(
            tool_name="web_search",
            tool_args={"query": "cats", "num": 5},
        )
        text = w._detail_block()
        assert "d29922" in text  # amber highlight for primary arg key
        assert "8b949e" in text  # dim for secondary arg key

    def test_detail_block_truncates_long_arg_values(self):
        w = _make_widget(tool_args={"query": "y" * 600})
        text = w._detail_block()
        assert "…" in text

    def test_detail_block_multiline_result_shows_truncation_hint(self):
        w = _make_widget(_result="\n".join([f"line {i}" for i in range(30)]))
        text = w._detail_block()
        assert "more lines" in text


class TestHelperFunctions:
    """Tests for the module-level helpers."""

    def test_friendly_label_running_known_tool(self):
        from slife.ui.tool_display import _friendly_label
        assert _friendly_label("execute_shell", "running") == "Running command"
        assert _friendly_label("web_search", "running") == "Searching web"

    def test_friendly_label_done_known_tool(self):
        from slife.ui.tool_display import _friendly_label
        assert _friendly_label("execute_shell", "done") == "Ran command"
        assert _friendly_label("web_search", "done") == "Searched web"

    def test_friendly_label_unknown_tool(self):
        from slife.ui.tool_display import _friendly_label
        label = _friendly_label("my_custom_tool", "running")
        assert "My custom tool" in label

    def test_primary_arg_value_known_tool(self):
        from slife.ui.tool_display import _primary_arg_value
        val = _primary_arg_value("web_search", {"query": "cats", "num": 5})
        assert val == "cats"

    def test_primary_arg_value_fallback_to_first_string(self):
        from slife.ui.tool_display import _primary_arg_value
        val = _primary_arg_value("unknown_tool", {"x": 1, "y": "hello"})
        assert val == "hello"

    def test_primary_arg_value_no_string_returns_none(self):
        from slife.ui.tool_display import _primary_arg_value
        val = _primary_arg_value("unknown_tool", {"x": 1, "y": 2})
        assert val is None

    def test_unique_suffix_increments(self):
        from slife.ui.tool_display import _unique_suffix
        a = _unique_suffix()
        b = _unique_suffix()
        assert int(b) == int(a) + 1
