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
        Binding("1", "pick(0)", "1", priority=True),
        Binding("2", "pick(1)", "2", priority=True),
        Binding("3", "pick(2)", "3", priority=True),
        Binding("4", "pick(3)", "4", priority=True),
        Binding("5", "pick(4)", "5", priority=True),
        Binding("6", "pick(5)", "6", priority=True),
        Binding("7", "pick(6)", "7", priority=True),
        Binding("8", "pick(7)", "8", priority=True),
        Binding("9", "pick(8)", "9", priority=True),
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

    def action_pick(self, index: int) -> None:
        """Pick the numbered model (1-9 key).  Out-of-range → no-op."""
        if not self._decided and 0 <= index < len(self._models):
            self._resolve(self._models[index])

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

        content = _mc("[bold #d29922]⚠ [/]") + _mc("[bold]Switch model[/bold]")

        for i, m in enumerate(self._models[:_MAX_CHOICES], 1):
            star = "★ " if m.ref == self._active_ref else "  "
            content = content + _lit(
                f"\n  {i}. {star}{m.display_name}",
                "bold" if m.ref == self._active_ref else "",
            ) + _lit(f"  {m.provider}", "#8b949e")
            meta = self._meta_line(m)
            if meta:
                content = content + _lit("\n     " + meta, "#8b949e")

        if len(self._models) > _MAX_CHOICES:
            content = content + _lit(
                f"\n  … {len(self._models) - _MAX_CHOICES} more", "#484f58"
            )

        # Key hints on the last line, same as ApprovalPrompt.  Reflect the
        # actual selectable range (1-N, capped at the bound digit keys).
        max_key = min(len(self._models), _MAX_CHOICES)
        key_hint = "1" if max_key <= 1 else f"1-{max_key}"
        content = content + _mc(
            f"\n[#6e7681]{key_hint} [/][bold #3fb950]Select[/]  "
            "[#6e7681]Esc [/][bold #f85149]Cancel[/]"
        )
        return content

    def _meta_line(self, m: ModelConfig) -> str:
        """One-line model metadata: ref + context window + capability flags."""
        parts = [m.ref]
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
                _mc("[bold #3fb950]✓ Switched[/bold #3fb950]")
                + _mc(" — ")
                + _lit(self._choice.display_name, "bold")
                + _lit(f"  {self._choice.provider}", "#8b949e")
            )
        return _mc("[bold #f85149]✗ Canceled[/bold #f85149]")
