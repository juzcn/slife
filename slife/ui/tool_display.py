"""Tool call display widget — Claude Code CLI style."""

import json

from textual.containers import Vertical
from textual.widgets import Static


class ToolCallWidget(Vertical):
    """Display a tool call with status indicator and collapsible details.

    Claude Code style: subtle amber header, spinner while running,
    monospace details area.
    """

    SPINNER_FRAMES = ["◌", "◍", "●", "◍"]

    def __init__(
        self,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
    ):
        super().__init__()
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_call_id = tool_call_id
        self._is_collapsed = True
        self._status: str = "running"
        self._result: str = ""
        self._result_is_error: bool = False
        self._header_widget: Static | None = None
        self._detail_widget: Static | None = None
        self.add_class("tool-call")

    def on_mount(self) -> None:
        """Build initial child widgets — called once when mounted into the DOM."""
        self._header_widget = Static(
            self._header_text(),
            id=f"tool-header-{self.tool_call_id}",
            classes="tool-call-header",
        )
        self._detail_widget = Static(
            self._detail_text(),
            id=f"tool-detail-{self.tool_call_id}",
            classes="tool-detail",
        )
        self._detail_widget.display = False
        self.mount(self._header_widget)
        self.mount(self._detail_widget)

    def _refresh(self) -> None:
        """Update existing child widgets in place — never removes/recreates them."""
        if self._header_widget is not None:
            self._header_widget.update(self._header_text())
        if self._detail_widget is not None:
            self._detail_widget.update(self._detail_text())

    def set_running(self) -> None:
        """Indicate the tool is currently executing."""
        self._status = "running"
        self._refresh()

    def set_complete(self, result: str, is_error: bool = False) -> None:
        """Indicate the tool has completed with a result."""
        self._status = "error" if is_error else "done"
        self._result = result[:2000] + "..." if len(result) > 2000 else result
        self._result_is_error = is_error
        self._refresh()

    def toggle(self) -> None:
        """Toggle the detail area visibility."""
        self._is_collapsed = not self._is_collapsed
        if self._detail_widget is not None:
            self._detail_widget.display = False if self._is_collapsed else True
        if self._header_widget is not None:
            self._header_widget.update(self._header_text())

    def on_click(self) -> None:
        """Toggle detail on click."""
        self.toggle()

    def _header_text(self) -> str:
        """Build the header line — Claude Code style."""
        status_icon = {
            "running": "[#d29922]◌[/]",
            "done": "[#3fb950]●[/]",
            "error": "[#f85149]●[/]",
            "pending": "[#484f58]◌[/]",
        }.get(self._status, "[#484f58]◌[/]")

        status_text = {
            "running": "[#d29922]running[/]",
            "done": "[#3fb950]done[/]",
            "error": "[#f85149]error[/]",
            "pending": "[#484f58]pending[/]",
        }.get(self._status, "[#484f58]pending[/]")

        indicator = "▾" if not self._is_collapsed else "▸"

        return (
            f"{indicator} {status_icon} "
            f"[bold #d29922]{self.tool_name}[/bold #d29922] "
            f"[#8b949e]{self._args_preview()}[/#8b949e]  "
            f"{status_text}"
        )

    def _detail_text(self) -> str:
        """Build the detail area text — monospace style."""
        parts = [
            "[bold #8b949e]Arguments:[/bold #8b949e]",
            f"[#c9d1d9]{json.dumps(self.tool_args, indent=2, ensure_ascii=False)}[/#c9d1d9]",
        ]
        if self._result:
            label = "Error" if self._result_is_error else "Result"
            color = "#f85149" if self._result_is_error else "#c9d1d9"
            parts.extend(["", f"[bold #8b949e]{label}:[/bold #8b949e]", f"[{color}]{self._result}[/]"])
        return "\n".join(parts)

    def _args_preview(self) -> str:
        """Short preview of args for the header line."""
        items = list(self.tool_args.items())
        if not items:
            return "no args"
        first_key, first_val = items[0]
        preview = str(first_val)[:50]
        if len(str(first_val)) > 50:
            preview += "..."
        if len(items) > 1:
            preview += f", +{len(items) - 1} more"
        return f"{first_key}={preview}"
