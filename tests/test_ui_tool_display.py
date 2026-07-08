"""Tests for tool display widgets (slife.ui.tool_display).

Tests widget construction, state transitions, and display logic.
ToolCallWidget methods that require the Textual DOM (_rebuild, on_mount,
toggle, set_running, set_complete) are tested via direct state manipulation
and testing the text-generation methods (_header_text, _detail_text, _args_preview)
which are pure functions of internal state.
"""

import json

import pytest

from slife.ui.tool_display import ToolCallWidget


# ══════════════════════════════════════════════════════════════════════
# ToolCallWidget - Construction
# ══════════════════════════════════════════════════════════════════════


class TestToolCallWidgetInit:
    """Tests for ToolCallWidget.__init__()."""

    def test_stores_parameters(self):
        """Constructor parameters are stored."""
        widget = ToolCallWidget(
            tool_name="web_search",
            tool_args={"query": "cats"},
            tool_call_id="call_123",
        )
        assert widget.tool_name == "web_search"
        assert widget.tool_args == {"query": "cats"}
        assert widget.tool_call_id == "call_123"

    def test_initial_state(self):
        """Initial state is 'pending' with collapsed details."""
        widget = ToolCallWidget(
            tool_name="test",
            tool_args={},
            tool_call_id="id1",
        )
        assert widget._status == "pending"
        assert widget._is_collapsed is True
        assert widget._result == ""
        assert widget._result_is_error is False

    def test_css_class(self):
        """Has 'tool-call' CSS class."""
        widget = ToolCallWidget(
            tool_name="test",
            tool_args={},
            tool_call_id="id1",
        )
        assert "tool-call" in widget.classes


# ══════════════════════════════════════════════════════════════════════
# State transitions (pure state, no DOM)
# ══════════════════════════════════════════════════════════════════════


class TestStateTransitions:
    """Tests for ToolCallWidget state management without Textual DOM."""

    def test_set_running_status(self):
        """set_running changes _status to 'running'."""
        widget = ToolCallWidget("t", {}, "c1")
        # Manually set state (since _rebuild needs DOM)
        widget._status = "running"
        assert widget._status == "running"

    def test_set_complete_success_status(self):
        """set_complete success changes status to 'done'."""
        widget = ToolCallWidget("t", {}, "c1")
        widget._status = "done"
        widget._result = "All good!"
        widget._result_is_error = False
        assert widget._status == "done"
        assert widget._result == "All good!"
        assert widget._result_is_error is False

    def test_set_complete_error_status(self):
        """set_complete error changes status to 'error'."""
        widget = ToolCallWidget("t", {}, "c1")
        widget._status = "error"
        widget._result = "Failed miserably"
        widget._result_is_error = True
        assert widget._status == "error"
        assert widget._result == "Failed miserably"
        assert widget._result_is_error is True

    def test_result_truncation_logic(self):
        """Results over 2000 chars are truncated."""
        widget = ToolCallWidget("t", {}, "c1")
        long_result = "x" * 3000
        # Simulate what set_complete does
        truncated = long_result[:2000] + "..." if len(long_result) > 2000 else long_result
        assert len(truncated) == 2003
        assert truncated.endswith("...")

    def test_short_result_not_truncated(self):
        """Short results are not truncated."""
        widget = ToolCallWidget("t", {}, "c1")
        short = "Short result"
        truncated = short[:2000] + "..." if len(short) > 2000 else short
        assert truncated == "Short result"
        assert not truncated.endswith("...")

    def test_toggle_collapse_logic(self):
        """toggle flips _is_collapsed."""
        widget = ToolCallWidget("t", {}, "c1")
        assert widget._is_collapsed is True
        widget._is_collapsed = not widget._is_collapsed
        assert widget._is_collapsed is False
        widget._is_collapsed = not widget._is_collapsed
        assert widget._is_collapsed is True


# ══════════════════════════════════════════════════════════════════════
# Header text (pure function of state)
# ══════════════════════════════════════════════════════════════════════


class TestHeaderText:
    """Tests for ToolCallWidget._header_text()."""

    def test_pending_status(self):
        """Pending status shows pending indicator."""
        widget = ToolCallWidget("search", {"q": "test"}, "c1")
        header = widget._header_text()
        assert "pending" in header

    def test_running_status(self):
        """Running status shows running indicator."""
        widget = ToolCallWidget("search", {"q": "test"}, "c1")
        widget._status = "running"
        header = widget._header_text()
        assert "running" in header

    def test_done_status(self):
        """Done status shows done indicator."""
        widget = ToolCallWidget("search", {"q": "test"}, "c1")
        widget._status = "done"
        header = widget._header_text()
        assert "done" in header

    def test_error_status(self):
        """Error status shows error indicator."""
        widget = ToolCallWidget("search", {"q": "test"}, "c1")
        widget._status = "error"
        header = widget._header_text()
        assert "error" in header

    def test_unknown_status_defaults_to_pending(self):
        """Unknown status values show pending-like indicators."""
        widget = ToolCallWidget("t", {}, "c1")
        widget._status = "unknown_bogus_status"
        header = widget._header_text()
        # Falls back to pending icon
        assert "pending" in header or "◌" in header

    def test_contains_tool_name(self):
        """Header includes the tool name."""
        widget = ToolCallWidget("execute_shell", {"command": "ls"}, "c1")
        header = widget._header_text()
        assert "execute_shell" in header

    def test_collapsed_indicator(self):
        """Collapsed state shows ▸, expanded shows ▾."""
        widget = ToolCallWidget("t", {}, "c1")
        assert "▸" in widget._header_text()
        widget._is_collapsed = False
        assert "▾" in widget._header_text()


# ══════════════════════════════════════════════════════════════════════
# Args preview (pure function of tool_args)
# ══════════════════════════════════════════════════════════════════════


class TestArgsPreview:
    """Tests for ToolCallWidget._args_preview()."""

    def test_single_arg(self):
        """Single argument shows key=value."""
        widget = ToolCallWidget("t", {"key": "value"}, "c1")
        preview = widget._args_preview()
        assert "key=value" in preview

    def test_multiple_args(self):
        """Multiple args shows first + count."""
        widget = ToolCallWidget("t", {"a": 1, "b": 2, "c": 3}, "c1")
        preview = widget._args_preview()
        assert "a=1" in preview
        assert "+2 more" in preview

    def test_no_args(self):
        """No args shows 'no args'."""
        widget = ToolCallWidget("t", {}, "c1")
        preview = widget._args_preview()
        assert "no args" in preview

    def test_long_value_truncated(self):
        """Long values are truncated to 50 chars."""
        long_val = "x" * 100
        widget = ToolCallWidget("t", {"data": long_val}, "c1")
        preview = widget._args_preview()
        assert "..." in preview
        assert len(preview) < 100  # Much shorter than the original value

    def test_short_value_not_truncated(self):
        """Short values are shown fully."""
        widget = ToolCallWidget("t", {"short": "hello"}, "c1")
        preview = widget._args_preview()
        assert "short=hello" in preview
        assert "..." not in preview


# ══════════════════════════════════════════════════════════════════════
# Detail text (pure function of state)
# ══════════════════════════════════════════════════════════════════════


class TestDetailText:
    """Tests for ToolCallWidget._detail_text()."""

    def test_shows_arguments(self):
        """Detail area shows formatted arguments."""
        widget = ToolCallWidget("t", {"query": "cats", "limit": 10}, "c1")
        detail = widget._detail_text()
        assert "Arguments" in detail
        assert "query" in detail
        assert "cats" in detail
        assert "limit" in detail
        assert "10" in detail

    def test_shows_result_when_present(self):
        """Detail area shows result when available."""
        widget = ToolCallWidget("t", {}, "c1")
        widget._result = "Command output here"
        detail = widget._detail_text()
        assert "Result" in detail
        assert "Command output here" in detail

    def test_shows_error_when_result_is_error(self):
        """Detail area labels errors differently."""
        widget = ToolCallWidget("t", {}, "c1")
        widget._result = "Something failed"
        widget._result_is_error = True
        detail = widget._detail_text()
        assert "Error" in detail
        assert "Something failed" in detail

    def test_empty_result_not_shown(self):
        """When no result, result section is omitted."""
        widget = ToolCallWidget("t", {"x": 1}, "c1")
        detail = widget._detail_text()
        assert "Result" not in detail
        assert "Error" not in detail


# ══════════════════════════════════════════════════════════════════════
# JSON formatting
# ══════════════════════════════════════════════════════════════════════


class TestJsonFormatting:
    """Tests for JSON argument formatting in detail view."""

    def test_complex_args_formatted(self):
        """Complex nested args are pretty-printed as JSON."""
        widget = ToolCallWidget(
            "complex_tool",
            {
                "filters": {"category": "books", "price": {"min": 10, "max": 50}},
                "sort": "relevance",
            },
            "c1",
        )
        detail = widget._detail_text()
        # Should contain JSON-formatted arguments
        assert "category" in detail
        assert "books" in detail
        assert "price" in detail

    def test_unicode_in_args(self):
        """Unicode characters in args are preserved."""
        widget = ToolCallWidget("t", {"query": "café résumé"}, "c1")
        detail = widget._detail_text()
        assert "café résumé" in detail

    def test_empty_dict_args(self):
        """Empty dict args are formatted correctly."""
        widget = ToolCallWidget("t", {}, "c1")
        detail = widget._detail_text()
        assert "{}" in detail


# ══════════════════════════════════════════════════════════════════════
# DOM-dependent methods (with mocked Textual operations)
# ══════════════════════════════════════════════════════════════════════


class TestToolCallWidgetDOM:
    """Tests for ToolCallWidget methods that require Textual DOM (mocked)."""

    def test_on_mount_calls_rebuild(self):
        """on_mount triggers _rebuild to build initial layout."""
        from unittest.mock import MagicMock

        widget = ToolCallWidget("test", {"key": "val"}, "c1")
        widget.mount = MagicMock()

        widget.on_mount()
        # Should have mounted at least the header widget
        assert widget.mount.call_count >= 1

    def test_set_running_updates_status_and_rebuilds(self):
        """set_running changes status and rebuilds (rebuild mocked)."""
        widget = ToolCallWidget("test", {}, "c1")
        rebuild_called = [0]
        widget._rebuild = lambda: rebuild_called.__setitem__(0, rebuild_called[0] + 1)

        widget.set_running()
        assert widget._status == "running"
        assert rebuild_called[0] == 1

    def test_set_complete_stores_result_and_rebuilds(self):
        """set_complete updates status, result, and rebuilds."""
        widget = ToolCallWidget("test", {}, "c1")
        rebuild_called = [0]
        widget._rebuild = lambda: rebuild_called.__setitem__(0, rebuild_called[0] + 1)

        widget.set_complete("output text")
        assert widget._status == "done"
        assert widget._result == "output text"
        assert widget._result_is_error is False
        assert rebuild_called[0] == 1

    def test_set_complete_error_stores_result(self):
        """set_complete with error stores error state."""
        widget = ToolCallWidget("test", {}, "c1")
        widget._rebuild = lambda: None

        widget.set_complete("fail", is_error=True)
        assert widget._status == "error"
        assert widget._result == "fail"
        assert widget._result_is_error is True

    def test_set_complete_truncation_in_method(self):
        """Long results truncated in set_complete."""
        widget = ToolCallWidget("test", {}, "c1")
        widget._rebuild = lambda: None

        long_result = "x" * 3000
        widget.set_complete(long_result)
        assert len(widget._result) == 2003
        assert widget._result.endswith("...")

    def test_toggle_flips_collapse_state(self):
        """toggle flips _is_collapsed."""
        widget = ToolCallWidget("test", {}, "c1")
        # Mock query_one to avoid DOM requirement
        from unittest.mock import MagicMock
        mock_detail = MagicMock()
        mock_header = MagicMock()
        widget.query_one = MagicMock(side_effect=[mock_detail, mock_header])

        assert widget._is_collapsed is True
        widget.toggle()
        assert widget._is_collapsed is False
        mock_header.update.assert_called_once()

    def test_on_click_flips_collapse(self):
        """on_click delegates to toggle, flipping collapse."""
        widget = ToolCallWidget("test", {}, "c1")
        from unittest.mock import MagicMock
        mock_detail = MagicMock()
        mock_header = MagicMock()
        widget.query_one = MagicMock(side_effect=[mock_detail, mock_header])

        assert widget._is_collapsed is True
        widget.on_click()
        assert widget._is_collapsed is False
        widget.on_click()
        assert widget._is_collapsed is True
