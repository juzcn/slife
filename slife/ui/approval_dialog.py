"""Tool approval dialog — Textual ModalScreen for human-in-the-loop confirmation.

Shown when an external MCP server has ``require_approval: true`` and the
LLM requests one of its tools.  The user must approve or deny before the
tool executes.
"""

from __future__ import annotations

import asyncio
import json

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

from slife.agent.loop import ToolCallInfo


class ApprovalDialog(ModalScreen[bool]):
    """Modal dialog asking the user to approve or deny a tool call.

    Returns ``True`` (approved) or ``False`` (denied) via
    ``asyncio.Future`` — the caller creates the future, passes it
    to the constructor, and awaits the result.

    Usage::

        future: asyncio.Future[bool] = asyncio.Future()
        app.push_screen(ApprovalDialog(tool_call, future))
        approved = await future
    """

    DEFAULT_CSS = """
    ApprovalDialog {
        align: center middle;
    }

    ApprovalDialog > Vertical {
        width: 60;
        background: #161b22;
        border: solid #30363d;
        padding: 1 2;
    }

    ApprovalDialog Static#approval-title {
        color: #f0c040;
        text-style: bold;
        width: 100%;
    }

    ApprovalDialog Static#approval-tool-name {
        color: #e6edf3;
        text-style: bold;
        margin: 1 0 0 0;
    }

    ApprovalDialog Static#approval-args {
        color: #8b949e;
        margin: 0 0 1 0;
        max-height: 10;
        overflow-y: auto;
    }

    ApprovalDialog Horizontal {
        width: 100%;
        align: center middle;
        margin: 1 0 0 0;
    }

    ApprovalDialog Button {
        margin: 0 1;
    }

    ApprovalDialog Button#approve-btn {
        background: #1a7f37;
        color: #ffffff;
    }

    ApprovalDialog Button#approve-btn:hover {
        background: #26a641;
    }

    ApprovalDialog Button#deny-btn {
        background: #da3633;
        color: #ffffff;
    }

    ApprovalDialog Button#deny-btn:hover {
        background: #f85149;
    }
    """

    def __init__(
        self,
        tool_call: ToolCallInfo,
        future: asyncio.Future[bool],
        name: str | None = None,
        id: str | None = None,
        classes: str | None = None,
    ) -> None:
        super().__init__(name=name, id=id, classes=classes)
        self._tool_call = tool_call
        self._future = future

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static("⚠ Tool Approval Required", id="approval-title")
            yield Static(
                f"Server tool: [bold]{self._tool_call.name}[/bold]",
                id="approval-tool-name",
            )
            yield Static(
                self._format_args(),
                id="approval-args",
            )
            with Horizontal():
                yield Button("Approve (Enter)", id="approve-btn", variant="success")
                yield Button("Deny (Esc)", id="deny-btn", variant="error")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "approve-btn":
            self._future.set_result(True)
        else:
            self._future.set_result(False)
        self.dismiss()

    def on_key(self, event) -> None:
        """Handle Enter / Escape for quick keyboard confirmation."""
        if event.key == "enter":
            self._future.set_result(True)
            self.dismiss()
        elif event.key == "escape":
            self._future.set_result(False)
            self.dismiss()

    def _format_args(self) -> str:
        """Format tool arguments for display, truncating long values."""
        args = self._tool_call.arguments
        parts: list[str] = []
        for k, v in args.items():
            s = str(v)
            if len(s) > 120:
                s = s[:120] + "…"
            parts.append(f"  {k}: {s}")
        return "\n".join(parts) if parts else "  (no arguments)"
