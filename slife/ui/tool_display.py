"""Tool call display widget (collapsible)."""

import json

from textual.containers import Vertical
from textual.widgets import Static


class ToolCallWidget(Vertical):
    """Display a tool call with status indicator and collapsible details.

    Shows:
      - Header line: status icon, tool name, args preview, status text
      - Collapsible details: full arguments + result
    """

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
        self.add_class("tool-call")

    def on_mount(self) -> None:
        """Build initial layout."""
        self._rebuild()

    def _rebuild(self) -> None:
        """Rebuild child widgets with current state."""
        for child in list(self.children):
            child.remove()

        self.mount(
            Static(
                self._header_text(),
                id=f"tool-header-{self.tool_call_id}",
            )
        )

        # Detail area (collapsed by default)
        detail_style = "display: none;" if self._is_collapsed else ""
        self.mount(
            Static(
                self._detail_text(),
                id=f"tool-detail-{self.tool_call_id}",
                classes="tool-detail",
            )
        )
        if self._is_collapsed:
            detail = self.query_one(f"#tool-detail-{self.tool_call_id}")
            detail.display = False

    def set_running(self) -> None:
        """Indicate the tool is currently executing."""
        self._status = "running"
        self._rebuild()

    def set_complete(self, result: str, is_error: bool = False) -> None:
        """Indicate the tool has completed with a result."""
        self._status = "error" if is_error else "done"
        self._result = result[:2000] + "..." if len(result) > 2000 else result
        self._result_is_error = is_error
        self._rebuild()

    def toggle(self) -> None:
        """Toggle the detail area visibility."""
        self._is_collapsed = not self._is_collapsed
        detail = self.query_one(f"#tool-detail-{self.tool_call_id}")
        detail.display = False if self._is_collapsed else True

    def on_click(self) -> None:
        """Toggle detail on click."""
        self.toggle()

    def _header_text(self) -> str:
        """Build the header line text."""
        status = getattr(self, "_status", "pending")
        icons = {"running": "[yellow]⚙[/yellow]", "done": "[green]✓[/green]", "error": "[red]✗[/red]", "pending": "[dim]…[/dim]"}
        labels = {"running": "[yellow]Running...[/yellow]", "done": "[green]Done[/green]", "error": "[red]Error[/red]", "pending": "[dim]Pending[/dim]"}
        icon = icons.get(status, icons["pending"])
        label = labels.get(status, labels["pending"])

        indicator = "▸" if self._is_collapsed else "▾"

        return (
            f"{indicator} {icon} [bold yellow]{self.tool_name}[/bold yellow] "
            f"[dim]({self._args_preview()})[/dim] {label}"
        )

    def _detail_text(self) -> str:
        """Build the detail area text."""
        parts = [
            "[bold]Arguments:[/bold]",
            json.dumps(self.tool_args, indent=2, ensure_ascii=False),
        ]
        if hasattr(self, "_result"):
            status = "Error" if getattr(self, "_result_is_error", False) else "Result"
            color = "[red]" if getattr(self, "_result_is_error", False) else ""
            parts.extend(["", f"[bold]{status}:[/bold]", f"{color}{self._result}[/]"])
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
