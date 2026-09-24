"""Tests for the HistoryInput chat prompt — the multi-line input widget.

Regression: Textual's single-line ``Input`` drops everything after the
first newline on paste, so long multi-line text was silently truncated.
``HistoryInput`` is built on ``TextArea`` which pastes the whole text.
Enter submits and never breaks a line — a multi-line draft is composed
with Shift+Enter — and up/down walk command history only when the cursor
is on the first / last line, otherwise they move the cursor.
"""

import pytest; pytestmark = pytest.mark.unit

from textual.app import App, ComposeResult
from textual.events import Paste
from textual.widgets import Static

from slife.ui.app import HistoryInput


class _PromptHost(App):
    """The real widget, composed as the app composes it.

    The submit handler lives in the concrete hosts below, never here: Textual
    dispatches ``on_*`` handlers found on **every** class in the MRO, so a
    handler defined in a shared base runs once per class that has one.
    """

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str] = []

    def compose(self) -> ComposeResult:
        yield HistoryInput(id="prompt")
        yield Static("", id="out")


class Host(_PromptHost):
    """Minimal app hosting the real widget with the real submit path."""

    def on_history_input_submitted(self, event: HistoryInput.Submitted) -> None:
        self.submitted.append(event.value)
        event.input.add_history(event.value)
        event.input.clear()


class RecordingHost(_PromptHost):
    """Same path, but the box is left alone — what Enter *left* stays visible.

    The real handler clears the input, which wipes any stray character along
    with the message.  Reading the buffer after a submit is how a stray one
    gets caught at all.
    """

    def on_history_input_submitted(self, event: HistoryInput.Submitted) -> None:
        self.submitted.append(event.value)


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


@pytest.mark.asyncio
async def test_enter_on_blank_does_not_type_a_newline():
    """Enter is send, never a line break — an empty one leaves no trace.

    TextArea handles Enter by inserting ``"\\n"``, and Textual dispatches
    ``_on_key`` to every class in the MRO, so it is a second call inside the
    widget rather than something ``event.stop()`` calls off.  The prompt has
    to ``prevent_default()``: without it each blank Enter grew the box by a
    line — invisible while a submit cleared the text, plain on an empty one.
    """
    app = Host()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        await pilot.press("enter")
        await pilot.press("enter")
        await pilot.pause()
        assert hi.text == ""
        assert hi.document.line_count == 1


@pytest.mark.asyncio
async def test_enter_submits_rather_than_breaking_the_line():
    """A sent draft keeps its own text and gains no trailing newline."""
    app = RecordingHost()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        await pilot.press(*"hello")
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["hello"]
        assert hi.text == "hello"  # no "\n" left behind by TextArea


@pytest.mark.asyncio
async def test_enter_submits_with_the_cursor_inside_a_multiline_draft():
    """Enter means send wherever the cursor sits — mid-draft included.

    A multi-line draft is composed with Shift+Enter, so Enter must not fall
    back to TextArea's line break just because the cursor is not on the last
    line.
    """
    app = RecordingHost()
    async with app.run_test() as pilot:
        hi = app.query_one(HistoryInput)
        hi.focus()
        await pilot.press(*"one")
        await pilot.press("shift+enter")
        await pilot.press(*"two")
        await pilot.press("up")  # cursor to the first line, mid-draft
        await pilot.press("enter")
        await pilot.pause()
        assert app.submitted == ["one\ntwo"]
        assert hi.text == "one\ntwo"