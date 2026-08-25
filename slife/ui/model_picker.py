"""Inline model picker — same interaction style as ApprovalPrompt.

Lets the operator switch the active model from the UI even when the
current LLM is down (no API round-trip needed).  Rendered as a numbered
row in the chat stream; ``↑/↓`` move a cursor, ``Enter`` picks, ``Esc``
cancels.  Every configured model is listed — no cap.

Same contract as :class:`~slife.ui.approval_prompt.ApprovalPrompt`: the
caller creates an ``asyncio.Future``, mounts the picker, focuses it, and
awaits the result — which is the chosen :class:`ModelConfig` (or ``None``
on cancel).  The caller performs the actual switch.
"""

from __future__ import annotations

import asyncio

from textual.binding import Binding
from textual.content import Content
from textual.widgets import Static

from slife.config import ModelConfig
from slife.ui.i18n import t

# Inline picker navigation: ↑/↓ move a cursor, Enter picks, Esc cancels.
# Every configured model is listed — no cap.


def _mc(text: str) -> Content:
    """Build Content from a **controlled** markup string (labels/sections only)."""
    return Content.from_markup(text)


def _lit(text: str, style: str = "") -> Content:
    """Build Content from arbitrary text — NEVER parsed as markup.

    Safe path for model names / refs, which may contain ``[``, ``&`` etc.
    """
    c = Content.from_text(text, markup=False)
    if style:
        c = c.stylize(style)
    return c


class ModelPicker(Static):
    """Inline model selector — ``↑/↓`` move a cursor, ``Enter`` picks, ``Esc`` cancels.

    Pure priority bindings, no ``_on_key`` / ``on_click`` overrides —
    exactly the ApprovalPrompt interaction model, so focus and key
    routing stay with Textual (no swallowed keys, no dead input).  The
    row re-renders itself to a ✓ Switched / ✗ Canceled status line once
    decided.
    """

    can_focus = True

    # Priority bindings fire in Textual's priority pass (App → … → this
    # widget) BEFORE ChatView's printable-key redirect and the App's
    # non-priority escape -> cancel — same as ApprovalPrompt.
    BINDINGS = [
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("enter", "pick", "Pick", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    def __init__(
        self,
        models: list[ModelConfig],
        active_ref: str,
        future: asyncio.Future[ModelConfig | None],
    ) -> None:
        super().__init__("")
        self._models = models
        self._active_ref = active_ref
        self._future = future
        self._decided: bool = False
        self._choice: ModelConfig | None = None
        # Cursor opens on the active model, so a bare Enter switches to the
        # current selection without any navigation.
        self._cursor = next(
            (i for i, m in enumerate(models) if m.ref == active_ref), 0
        )
        # Reuse the approval-prompt visual style (unified with approve).
        self.add_class("approval-prompt")
        self.update(self._build_content())

    def action_cursor_up(self) -> None:
        """Move the selection cursor up (clamped at the top)."""
        if not self._decided and self._cursor > 0:
            self._cursor -= 1
            self.update(self._build_content())

    def action_cursor_down(self) -> None:
        """Move the selection cursor down (clamped at the bottom)."""
        if not self._decided and self._cursor < len(self._models) - 1:
            self._cursor += 1
            self.update(self._build_content())

    def action_pick(self) -> None:
        """Pick the model under the cursor (Enter)."""
        if not self._decided and 0 <= self._cursor < len(self._models):
            self._resolve(self._models[self._cursor])

    def action_cancel(self) -> None:
        """Cancel the picker (Esc)."""
        self._resolve(None)

    def _resolve(self, model: ModelConfig | None) -> None:
        """Resolve the future once; ignore any repeat keypress."""
        if self._decided:
            return
        self._decided = True
        self._choice = model
        self._future.set_result(model)
        self.update(self._build_content())

    # ── Rendering ──────────────────────────────────────────────────

    def _build_content(self) -> Content:
        if self._decided:
            return self._status_line()

        content = _mc("[bold #d29922]⚠ [/]") + _mc(f"[bold]{t('picker_title')}[/bold]")

        for i, m in enumerate(self._models, 1):
            row_idx = i - 1
            cursor = "▸ " if row_idx == self._cursor else "  "
            star = "★ " if m.ref == self._active_ref else "  "
            content = content + _lit(
                f"\n{cursor}{i}. {star}{m.ref}",
                "bold" if m.ref == self._active_ref else "",
            )
            meta = self._meta_line(m)
            if meta:
                content = content + _lit("  " + meta, "#8b949e")

        # Key hints on the last line, same as ApprovalPrompt.
        content = content + _mc(
            f"\n[#6e7681]↑/↓ [/][bold #3fb950]{t('picker_select')}[/]  "
            f"[#6e7681]Enter [/][bold #3fb950]{t('picker_pick')}[/]  "
            f"[#6e7681]Esc [/][bold #f85149]{t('picker_cancel')}[/]"
        )
        return content

    def _meta_line(self, m: ModelConfig) -> str:
        """One-line model metadata: context window + capability flags."""
        parts = []
        if m.context_window:
            parts.append(f"ctx {m.context_window}")
        if m.supports_vision:
            parts.append("vision")
        if m.thinking_enabled:
            parts.append("thinking")
        return "  ".join(parts)

    def _status_line(self) -> Content:
        if self._choice is not None:
            return (
                _mc(f"[bold #3fb950]{t('picker_switched')}[/bold #3fb950]")
                + _mc(" — ")
                + _lit(self._choice.ref, "bold")
            )
        return _mc(f"[bold #f85149]{t('picker_canceled')}[/bold #f85149]")
