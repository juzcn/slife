"""Inline tool-approval prompt — Claude Code style, no modal overlay.

Rendered as a row in the chat stream when the model requests approval
(``_approve: true`` on any tool, native or MCP-proxied).  The row itself
carries the "waiting for approval" state and takes focus; the user answers
with ``Y`` (approve) / ``N`` (deny) / ``Esc`` (deny).  Once approved, the
agent loop mounts the regular ``ToolCallWidget`` and the tool runs.  A
denied call never mounts a tool widget — this row shows the rejection
instead.
"""

from __future__ import annotations

import asyncio

from textual.binding import Binding
from textual.content import Content
from textual.widgets import Static

from slife.agent.loop import ToolCallInfo

# Arg display caps — keep the waiting prompt compact.
_ARG_MAX = 120
_ARG_LINES_MAX = 6


def _mc(text: str) -> Content:
    """Build Content from a **controlled** markup string.

    Only use for strings we construct ourselves (labels, section headers).
    Never pass user data or tool output through this function.
    """
    return Content.from_markup(text)


def _lit(text: str, style: str = "") -> Content:
    """Build Content from arbitrary text — NEVER parsed as markup.

    This is the safe path for all user data: tool names, arg values, etc.
    Characters like &, [, ] are rendered literally.
    """
    c = Content.from_text(text, markup=False)
    if style:
        c = c.stylize(style)
    return c


class ApprovalPrompt(Static):
    """Inline approval row — ``Y`` approve / ``N`` deny / ``Esc`` deny.

    *future* is resolved with the user's boolean decision; the caller
    (``TUIHandler.on_tool_approval``) creates the future, passes it to the
    constructor, and awaits the result.
    """

    can_focus = True

    # Priority bindings: while focused they fire in Textual's priority pass
    # (App → … → this widget) BEFORE ChatView's printable-key redirect and
    # the App's non-priority ``escape -> cancel`` (REVIEW C7), so Y/N/Esc
    # really decide here instead of typing into the input bar or cancelling
    # the whole agent loop.
    BINDINGS = [
        Binding("y", "approve", "Approve", priority=True),
        Binding("n", "deny", "Deny", priority=True),
        Binding("escape", "deny", "Deny", priority=True),
    ]

    def __init__(
        self,
        tool_call: ToolCallInfo,
        future: asyncio.Future[bool],
    ) -> None:
        super().__init__("")
        self._tool_call = tool_call
        self._future = future
        self._decided: str | None = None  # "approved" | "denied"
        self.add_class("approval-prompt")
        self.update(self._build_content())

    def action_approve(self) -> None:
        """Approve the tool call (Y)."""
        self._decide(approved=True)

    def action_deny(self) -> None:
        """Deny the tool call (N or Esc)."""
        self._decide(approved=False)

    def _decide(self, approved: bool) -> None:
        """Resolve the future once; ignore any repeat keypress."""
        if self._decided is not None:
            return
        self._decided = "approved" if approved else "denied"
        self._future.set_result(approved)
        self.update(self._build_content())

    # ── Rendering ──────────────────────────────────────────────────

    def _build_content(self) -> Content:
        if self._decided is not None:
            return self._status_line()

        content = (
            _mc("[bold #d29922]⚠ [/]")
            + _lit(self._tool_call.name, "bold")
            + _mc(" [dim]requests approval[/dim]")
        )

        args = self._tool_call.arguments
        if args:
            arg_lines: list[str] = []
            for k, v in args.items():
                s = str(v)
                if len(s) > _ARG_MAX:
                    s = s[: _ARG_MAX] + "…"
                arg_lines.append(f"  {k}: {s}")
            display = "\n".join(arg_lines[: _ARG_LINES_MAX])
            if len(arg_lines) > _ARG_LINES_MAX:
                display += f"\n  … {len(arg_lines) - _ARG_LINES_MAX} more"
            content = content + _mc("\n") + _lit(display, "#8b949e")

        content = content + _mc(
            "\n[#6e7681]Y [/][bold #3fb950]Approve[/]  "
            "[#6e7681]N / Esc [/][bold #f85149]Deny[/]"
        )
        return content

    def _status_line(self) -> Content:
        """Post-decision one-liner: ✓ Approved / ✗ Denied + tool name."""
        if self._decided == "approved":
            return (
                _mc("[bold #3fb950]✓ Approved[/bold #3fb950]")
                + _mc(" — ")
                + _lit(self._tool_call.name, "bold")
            )
        return (
            _mc("[bold #f85149]✗ Denied[/bold #f85149]")
            + _mc(" — ")
            + _lit(self._tool_call.name, "bold")
        )
