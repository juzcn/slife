"""Tests for slife.ui.tool_display — ToolCallWidget pure logic tests."""

import pytest
from unittest.mock import MagicMock, patch


def _make_widget(**kwargs):
    """Create a ToolCallWidget with mocked Textual internals."""
    with patch("slife.ui.tool_display.Vertical.__init__", return_value=None):
        from slife.ui.tool_display import ToolCallWidget
        w = ToolCallWidget.__new__(ToolCallWidget)
        w.tool_name = kwargs.get("tool_name", "web_search")
        w.tool_args = kwargs.get("tool_args", {"query": "cats"})
        w.tool_call_id = kwargs.get("tool_call_id", "call_abc")
        w._is_collapsed = kwargs.get("_is_collapsed", True)
        w._status = kwargs.get("_status", "pending")
        w._result = kwargs.get("_result", "")
        w._result_is_error = kwargs.get("_result_is_error", False)
        w._header_widget = kwargs.get("_header_widget", None)
        w._detail_widget = kwargs.get("_detail_widget", None)
        w.mount = MagicMock()
        w.query_one = MagicMock()
        w._nodes = {}
        w._dom_children = []
        return w


class TestToolCallWidget:
    """Tests for ToolCallWidget logic (no Textual runtime needed)."""

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
        w._rebuild = MagicMock()
        w.set_running()
        assert w._status == "running"

    def test_set_complete_success(self):
        w = _make_widget()
        w._rebuild = MagicMock()
        w.set_complete("Search results here")
        assert w._status == "done"
        assert w._result == "Search results here"
        assert w._result_is_error is False

    def test_set_complete_error(self):
        w = _make_widget()
        w._rebuild = MagicMock()
        w.set_complete("Something failed", is_error=True)
        assert w._status == "error"
        assert w._result_is_error is True

    def test_set_complete_truncates_long_result(self):
        w = _make_widget()
        w._rebuild = MagicMock()
        long_result = "x" * 3000
        w.set_complete(long_result)
        assert len(w._result) == 2003  # 2000 + "..."
        assert w._result.endswith("...")

    def test_header_text_pending(self):
        w = _make_widget()
        text = w._header_text()
        assert "▸" in text
        assert "web_search" in text
        assert "pending" in text

    def test_header_text_running(self):
        w = _make_widget(_status="running")
        text = w._header_text()
        assert "running" in text

    def test_header_text_done(self):
        w = _make_widget(_status="done")
        text = w._header_text()
        assert "done" in text

    def test_header_text_error(self):
        w = _make_widget(_status="error")
        text = w._header_text()
        assert "error" in text

    def test_header_text_expanded(self):
        w = _make_widget(_is_collapsed=False)
        text = w._header_text()
        assert "▾" in text

    def test_args_preview_single_arg(self):
        w = _make_widget()
        preview = w._args_preview()
        assert "query=cats" in preview

    def test_args_preview_multiple_args(self):
        w = _make_widget(tool_args={"query": "cats", "num": 5})
        preview = w._args_preview()
        assert "query=cats" in preview
        assert "+1 more" in preview

    def test_args_preview_no_args(self):
        w = _make_widget(tool_args={})
        assert w._args_preview() == "no args"

    def test_args_preview_truncates_long_values(self):
        w = _make_widget(tool_args={"text": "a" * 100})
        preview = w._args_preview()
        assert "..." in preview

    def test_detail_text_with_result(self):
        w = _make_widget(_result="Search completed")
        text = w._detail_text()
        assert "Arguments" in text
        assert "cats" in text
        assert "Result" in text
        assert "Search completed" in text

    def test_detail_text_with_error(self):
        w = _make_widget(_result="Failure", _result_is_error=True)
        text = w._detail_text()
        assert "Error" in text

    def test_detail_text_no_result(self):
        w = _make_widget()
        text = w._detail_text()
        assert "Arguments" in text
        assert "Result" not in text
        assert "Error" not in text
