"""Inline model picker — same interaction style as ApprovalPrompt.

Lets the operator switch the active model from the UI even when the
current LLM is down (no API round-trip needed).  Rendered as a numbered
row in the chat stream; typing ``1``-``9`` picks a model, ``Esc`` cancels.

Same contract as :class:`~slife.ui.approval_prompt.ApprovalPrompt`: the
caller creates an ``asyncio.Future``, mounts the picker, focuses it, and
awaits the result — which is the chosen :class:`ModelConfig` (or ``None``
on cancel).  The caller performs the actual switch.
"""

from __future__ import annotations

import asyncio

from textual.binding import Binding
from textual.content import Content
from textual.events import Click, Key
from textual.widgets import Static

from slife.config import ModelConfig

# Inline picker keys: digits 1-9.  Cap the rendered list accordingly.
_MAX_CHOICES = 9


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
    """Inline numbered model selector — ``1``-``9`` picks, ``Esc`` cancels.

    *future* resolves with the selected :class:`ModelConfig`, or ``None``
    if the operator cancels.  The row re-renders itself to a ✓ Switched /
    ✗ Canceled status line once decided.
    """

    can_focus = True

    # Esc cancels in the priority pass, before the App's non-priority
    # escape -> cancel (REVIEW C7) — same as ApprovalPrompt.
    BINDINGS = [
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
        # Reuse the approval-prompt visual style (unified with approve).
        self.add_class("approval-prompt")
        self.update(self._build_content())

    def action_cancel(self) -> None:
        """Cancel the picker (Esc)."""
        self._resolve(None)

    async def _on_key(self, event: Key) -> None:
        """Type a digit to pick that numbered model — before ChatView's
        printable-key redirect steals it.  Other keys fall through."""
        if (
            event.is_printable
            and event.character
            and event.character.isdigit()
        ):
            idx = int(event.character) - 1
            if 0 <= idx < len(self._models):
                self._resolve(self._models[idx])
                event.stop()
                return
        await super()._on_key(event)

    def on_click(self, event: Click) -> None:
        """Click a model row to pick it (mouse-friendly fallback to digits).

        Maps the click to the content line, accounting for the widget's own
        border/padding via ``content_offset``, then to the model index
        (each model renders on two lines: name, then metadata).
        """
        if self._decided:
            return
        line = event.offset.y - self.content_offset.y
        if line <= 0:
            return  # header line
        idx = (line - 1) // 2
        if 0 <= idx < len(self._models):
            self._resolve(self._models[idx])
            event.stop()

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

        content = (
            _mc("[bold #d29922]⚠ [/]")
            + _mc("[bold]Switch model[/bold] [dim]— click a model or type a number, Esc cancels[/dim]")
        )

        for i, m in enumerate(self._models[:_MAX_CHOICES], 1):
            star = "★ " if m.ref == self._active_ref else "  "
            content = content + _lit(
                f"\n  {i}. {star}{m.display_name}",
                "bold" if m.ref == self._active_ref else "",
            )
            meta = self._meta_line(m)
            if meta:
                content = content + _lit("\n     " + meta, "#8b949e")

        if len(self._models) > _MAX_CHOICES:
            content = content + _lit(
                f"\n  … {len(self._models) - _MAX_CHOICES} more", "#484f58"
            )
        return content

    def _meta_line(self, m: ModelConfig) -> str:
        """One-line model metadata: ref + context window + capability flags."""
        parts = [m.ref]
        if m.context_window:
            parts.append(f"ctx {m.context_window}")
        if m.supports_vision:
            parts.append("👁")
        if m.thinking_enabled:
            parts.append("🧠")
        return "  ".join(parts)

    def _status_line(self) -> Content:
        if self._choice is not None:
            return (
                _mc("[bold #3fb950]✓ Switched[/bold #3fb950]")
                + _mc(" — ")
                + _lit(self._choice.display_name, "bold")
            )
        return _mc("[bold #f85149]✗ Canceled[/bold #f85149]")
