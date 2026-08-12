"""Tests for the inline ModelPicker widget (Ctrl+G model switching).

Same interaction style as ApprovalPrompt: an inline row in the chat,
``1``-``9`` picks a numbered model, ``Esc`` cancels, and the row
re-renders to a ✓ Switched / ✗ Canceled status line.
"""

import pytest; pytestmark = pytest.mark.unit

import asyncio

from slife.config import ModelConfig
from slife.ui.model_picker import ModelPicker


def _models():
    return [
        ModelConfig(ref="alpha/m1", provider="alpha", api_model="m1", display_name="Alpha One", api_key="k"),
        ModelConfig(ref="beta/m2", provider="beta", api_model="m2", display_name="Beta Two", api_key="k", supports_vision=True),
        ModelConfig(ref="gamma/m3", provider="gamma", api_model="m3", display_name="Gamma & [Three]", api_key="k", thinking_enabled=True),
    ]


class TestModelPicker:
    def _make(self, active_ref="beta/m2"):
        future = asyncio.Future()
        picker = ModelPicker(_models(), active_ref, future)
        return picker, future

    @pytest.mark.asyncio
    async def test_active_model_starred(self):
        picker, _ = self._make()
        text = str(picker.render())
        assert "★ Beta Two" in text  # active model starred
        assert "Alpha One" in text
        assert "Gamma & [Three]" in text  # markup-hazardous name renders literally
        assert "beta/m2" in text

    @pytest.mark.asyncio
    async def test_escape_binding_is_priority(self):
        picker, _ = self._make()
        esc = [b for b in picker.BINDINGS if b.key == "escape"]
        assert len(esc) == 1
        assert esc[0].priority and esc[0].action == "cancel"

    @pytest.mark.asyncio
    async def test_action_cancel_sets_none(self):
        picker, future = self._make()
        picker.action_cancel()
        assert future.result() is None
        assert "Canceled" in str(picker.render())

    @pytest.mark.asyncio
    async def test_resolve_sets_choice_and_status(self):
        picker, future = self._make()
        picker._resolve(_models()[0])
        assert future.result().ref == "alpha/m1"
        assert "Switched" in str(picker.render())
        assert "Alpha One" in str(picker.render())

    @pytest.mark.asyncio
    async def test_second_decision_ignored(self):
        picker, future = self._make()
        picker._resolve(_models()[0])
        picker._resolve(_models()[1])  # repeat press must not override
        assert future.result().ref == "alpha/m1"
        assert "Alpha One" in str(picker.render())

    @pytest.mark.asyncio
    async def test_pilot_click_selects_model(self):
        """Real Textual mouse dispatch: clicking the 2nd model's row selects it."""
        from textual.app import App

        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            future = asyncio.Future()
            picker = ModelPicker(_models(), "beta/m2", future)
            await app.mount(picker)
            await pilot.pause()
            # No CSS in a bare App → content starts at line 0.  Header = 0,
            # m1 = lines 1-2, m2 name = line 3.
            await pilot.click(picker, offset=(10, 3))
            assert future.result().ref == "beta/m2"
            assert "Switched" in str(picker.render())

    @pytest.mark.asyncio
    async def test_pilot_digit_selects_model(self):
        """Real Textual key dispatch: typing 2 resolves the 2nd model."""
        from textual.app import App

        app = App()
        async with app.run_test(size=(80, 24)) as pilot:
            future = asyncio.Future()
            picker = ModelPicker(_models(), "beta/m2", future)
            await app.mount(picker)
            picker.focus()
            await pilot.pause()
            await pilot.press("2")
            assert future.result().ref == "beta/m2"
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
