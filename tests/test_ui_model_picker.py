"""Tests for the inline ModelPicker widget (Ctrl+S model switching).

Same interaction style as ApprovalPrompt: an inline row in the chat,
``↑/↓`` moves a cursor, ``Enter`` picks, ``Esc`` cancels, and the row
re-renders to a ✓ Switched / ✗ Canceled status line.  Pure priority
bindings — no ``_on_key`` / ``on_click`` overrides.
"""

import pytest; pytestmark = pytest.mark.unit

import asyncio

from slife.config import ModelConfig
from slife.ui.model_picker import ModelPicker


def _models():
    return [
        ModelConfig(ref="alpha/m1", provider="alpha", api_model="m1", display_name="Alpha One", api_key="k"),
        ModelConfig(ref="beta/m2", provider="beta", api_model="m2", display_name="Beta Two", api_key="k", supports_vision=True),
        ModelConfig(ref="gamma/m3&[x]", provider="gamma", api_model="m3", display_name="Gamma & [Three]", api_key="k", thinking_enabled=True),
    ]


class TestModelPicker:
    def _make(self, active_ref="beta/m2"):
        future = asyncio.Future()
        picker = ModelPicker(_models(), active_ref, future)
        return picker, future

    @pytest.mark.asyncio
    async def test_lists_all_models(self):
        """Every model is rendered — no 9-slot cap, no ``… N more``."""
        many = [
            ModelConfig(ref=f"p{i}/m{i}", provider=f"p{i}", api_model=f"m{i}",
                        display_name=f"M{i}", api_key="k")
            for i in range(1, 13)
        ]
        future = asyncio.Future()
        picker = ModelPicker(many, "p1/m1", future)
        text = str(picker.render())
        for i in range(1, 13):
            assert f"p{i}/m{i}" in text
        assert "more" not in text

    @pytest.mark.asyncio
    async def test_active_model_starred_and_cursor_on_it(self):
        picker, _ = self._make()
        text = str(picker.render())
        # Cursor opens on the active model; both markers show on that row.
        assert "▸ 2. ★ beta/m2" in text
        assert "alpha/m1" in text
        assert "gamma/m3&[x]" in text  # markup-hazardous ref renders literally
        # Capability flags as text labels, not emojis.
        assert "vision" in text  # Beta Two supports vision
        assert "thinking" in text  # Gamma has thinking enabled
        assert "👁" not in text
        assert "🧠" not in text

    @pytest.mark.asyncio
    async def test_hint_shows_navigation(self):
        picker, _ = self._make()  # 3 models
        text = str(picker.render())
        assert "↑/↓" in text
        assert "Enter" in text
        assert "Esc Cancel" in text

    @pytest.mark.asyncio
    async def test_navigation_bindings_are_priority(self):
        picker, _ = self._make()
        keys = {b.key: b for b in picker.BINDINGS}
        for key, action in (
            ("up", "cursor_up"),
            ("down", "cursor_down"),
            ("enter", "pick"),
            ("escape", "cancel"),
        ):
            b = keys[key]
            assert b.action == action and b.priority

    @pytest.mark.asyncio
    async def test_cursor_moves_and_clamps(self):
        """Cursor starts on the active model and clamps at both ends."""
        picker, future = self._make()
        assert picker._cursor == 1  # beta/m2
        picker.action_cursor_up()    # -> alpha/m1
        picker.action_cursor_up()    # clamp at top
        assert picker._cursor == 0
        picker.action_cursor_down()
        picker.action_cursor_down()  # -> beta, then gamma
        picker.action_cursor_down()  # clamp at bottom
        assert picker._cursor == 2
        picker.action_pick()
        assert future.result().ref == "gamma/m3&[x]"

    @pytest.mark.asyncio
    async def test_action_pick_selects_cursor_model(self):
        picker, future = self._make()
        picker.action_pick()  # cursor opens on active model
        assert future.result().ref == "beta/m2"
        assert "Switched" in str(picker.render())
        assert "beta/m2" in str(picker.render())  # ref in status line

    @pytest.mark.asyncio
    async def test_action_cancel_sets_none(self):
        picker, future = self._make()
        picker.action_cancel()
        assert future.result() is None
        assert "Canceled" in str(picker.render())

    @pytest.mark.asyncio
    async def test_second_decision_ignored(self):
        picker, future = self._make()
        picker.action_pick()  # picks cursor model (beta/m2)
        picker.action_cursor_down()  # ignored after decision
        picker.action_pick()  # ignored
        assert future.result().ref == "beta/m2"
        assert "beta/m2" in str(picker.render())

    @pytest.mark.asyncio
    async def test_pilot_arrows_and_enter_select(self):
        """Real Textual key dispatch: ↓ + Enter picks the model under cursor."""
        from textual.app import App

        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            future = asyncio.Future()
            picker = ModelPicker(_models(), "beta/m2", future)
            await app.mount(picker)
            picker.focus()
            await pilot.pause()
            await pilot.press("down")  # beta -> gamma
            await pilot.press("enter")
            assert future.result().ref == "gamma/m3&[x]"
            assert "Switched" in str(picker.render())

    @pytest.mark.asyncio
    async def test_pilot_escape_cancels(self):
        from textual.app import App

        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            future = asyncio.Future()
            picker = ModelPicker(_models(), "beta/m2", future)
            await app.mount(picker)
            picker.focus()
            await pilot.pause()
            await pilot.press("escape")
            assert future.result() is None
            assert "Canceled" in str(picker.render())

    @pytest.mark.asyncio
    async def test_picker_mount_scrolls_into_view(self):
        """Mounting the picker must scroll the chat so the picker is visible.

        Regression: an immediate scroll_end runs against the pre-mount
        content and the picker's insertion can leave the view pinned at the
        top — the picker ends up below the fold.  Scroll is deferred until
        layout (call_after_refresh), same as ChatView._follow_after_refresh.
        """
        from textual.app import App, ComposeResult
        from textual.widgets import Input

        from slife.ui.chat import ChatView
        from slife.ui.model_picker import ModelPicker

        class T(App):
            def compose(self) -> ComposeResult:
                yield ChatView(id="chat-view")
                yield Input(id="user-input")

        app = T()
        async with app.run_test(size=(80, 30)) as pilot:
            cv = app.query_one("#chat-view", ChatView)
            for i in range(50):
                cv.add_user_message(f"user {i}")
                cv.add_assistant_message().append_text(f"reply {i}")
            await pilot.pause()

            picker = ModelPicker(_models(), "beta/m2", asyncio.Future())
            cv.mount(picker)
            cv.call_after_refresh(cv.scroll_end, animate=False)
            picker.focus()
            for _ in range(3):
                await pilot.pause()

            # Scrolled to the bottom so the picker (mounted at the end) is
            # in view — not pinned at the top by the mount's re-layout.
            assert cv.scroll_offset.y >= cv.max_scroll_y - 1, (
                f"scroll {cv.scroll_offset.y} not at bottom (max {cv.max_scroll_y})"
            )

    @pytest.mark.asyncio
    async def test_pilot_unbound_key_falls_through(self):
        """A non-navigation key must NOT be swallowed — no keyboard hijack."""
        from textual.app import App

        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            future = asyncio.Future()
            picker = ModelPicker(_models(), "beta/m2", future)
            await app.mount(picker)
            picker.focus()
            await pilot.pause()
            await pilot.press("a")
            # The picker ignores it and stays undecided (not hijacked).
            assert future.done() is False
            assert picker._decided is False
