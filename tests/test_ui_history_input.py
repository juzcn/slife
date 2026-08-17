"""Tests for the HistoryInput chat prompt — the multi-line input widget.

Regression: Textual's single-line ``Input`` drops everything after the
first newline on paste, so long multi-line text was silently truncated.
``HistoryInput`` is built on ``TextArea`` which pastes the whole text.
Enter submits, Shift+Enter inserts a literal newline, and up/down walk
command history only when the cursor is on the first / last line —
otherwise they move the cursor.
"""

import pytest; pytestmark = pytest.mark.unit

from textual.app import App, ComposeResult
from textual.events import Paste
from textual.widgets import Static

from slife.ui.app import HistoryInput


class Host(App):
    """Minimal app hosting the real widget with the real submit path."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield HistoryInput(id="prompt")
        yield Static("", id="out")

    def on_history_input_submitted(self, event: HistoryInput.Submitted) -> None:
        self.submitted.append(event.value)
        event.input.add_history(event.value)
        event.input.clear()


@pytest.mark.asyncio
async def test_paste_preserves_multiline_text():
    """The reported bug: long multi-line paste must not be truncated."""
    app = Host()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        await hi._on_paste(Paste("alpha\nbeta\nvery long line " * 2))
        await pilot.pause()
        assert hi.document.line_count == 5
        assert hi.text.startswith("alpha\nbeta\n")
        assert hi.text.count("very long line") == 2


@pytest.mark.asyncio
async def test_enter_submits_full_multiline_text():
    app = Host()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        await hi._on_paste(Paste("one\ntwo\nthree"))
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["one\ntwo\nthree"]
        assert hi.text == ""  # cleared after submit


@pytest.mark.asyncio
async def test_shift_enter_inserts_newline_without_submitting():
    app = Host()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        await pilot.press("a")
        await pilot.press("shift+enter")
        await pilot.press("b")
        await pilot.pause()
        assert hi.text == "a\nb"
        assert app.submitted == []


@pytest.mark.asyncio
async def test_history_navigation_restores_draft():
    app = Host()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        hi.add_history("hello")
        hi.add_history("world")
        hi.text = "in progress"
        hi.move_cursor(hi.document.end)
        await pilot.press("up")
        await pilot.pause()
        assert hi.text == "world"
        await pilot.press("up")
        await pilot.pause()
        assert hi.text == "hello"
        await pilot.press("down")
        await pilot.pause()
        assert hi.text == "world"
        await pilot.press("down")
        await pilot.pause()
        assert hi.text == "in progress"  # draft restored


@pytest.mark.asyncio
async def test_arrow_keys_move_cursor_inside_multiline_buffer():
    """Up/down only touch history at the buffer edges."""
    app = Host()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        hi.add_history("history line")
        hi.text = "lineA\nlineB"
        hi.move_cursor((1, 3))
        await pilot.press("up")
        await pilot.pause()
        assert hi.cursor_location[0] == 0  # moved, did not replace text
        assert hi.text == "lineA\nlineB"


@pytest.mark.asyncio
async def test_enter_on_blank_does_not_submit():
    app = Host()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == []