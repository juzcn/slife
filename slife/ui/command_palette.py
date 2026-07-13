"""Command palette — dropdown overlay for slash-command completion.

Appears above the input when the user types "/", showing available
slash commands. Filters as the user types. Tab completes.

Uses Content.from_text(markup=False) for safe rendering of file paths.
"""

from textual.content import Content
from textual.containers import Vertical
from textual.widgets import Static

from slife.ui.commands import (
    COMMANDS,
    match_commands,
    complete_file_path,
)

# Max suggestions to show before scrolling.
_MAX_VISIBLE = 10


class CommandPalette(Vertical):
    """Dropdown list of slash-command suggestions.

    Mounted as a sibling before the input. Shows command names
    for "/" prefix, file paths for "/file " prefix.

    Not focusable — the input keeps focus. Tab reads the first
    suggestion and completes it via the app.
    """

    DEFAULT_CSS = """
    CommandPalette {
        width: 100%;
        height: auto;
        max-height: 12;
        background: #161b22;
        border: solid #30363d;
        display: none;
        overflow-y: auto;
        padding: 0;
    }
    CommandPalette.-visible {
        display: block;
    }
    """

    def __init__(self):
        super().__init__()
        self._items: list[str] = []

    # ── Public API ──────────────────────────────────────────────────

    def show_suggestions(self, value: str) -> None:
        """Populate and show the palette based on current input value."""
        items = self._get_suggestions(value)
        if not items:
            self.hide()
            return

        self._items = items
        self._rebuild(items)
        self.add_class("-visible")

    def hide(self) -> None:
        """Hide the palette and clear items."""
        self.remove_class("-visible")
        self._items.clear()
        self.remove_children()

    def selected_text(self) -> str:
        """Return the top completion text, or '' if hidden."""
        if not self._items or not self.has_class("-visible"):
            return ""
        return self._items[0] if self._items else ""

    @property
    def visible(self) -> bool:
        return bool(self._items) and self.has_class("-visible")

    # ── Internals ───────────────────────────────────────────────────

    def _get_suggestions(self, value: str) -> list[str]:
        """Compute completion strings from current input value."""
        if not value.startswith("/"):
            return []

        # /file <partial> → file path completion
        if value.startswith("/file ") or value == "/file":
            partial = value[len("/file"):].strip()
            paths = complete_file_path(partial)
            return [f"/file {p}" for p in paths]

        # Command name completion
        commands = match_commands(value)
        return [c.usage or c.name for c in commands]

    def _rebuild(self, items: list[str]) -> None:
        """Replace children with suggestion rows."""
        self.remove_children()
        shown = items[:_MAX_VISIBLE]
        for item in shown:
            name = item.split()[0].lstrip("/")
            cmd = next((c for c in COMMANDS if c.name == f"/{name}"), None)

            content = Content.from_markup(f"[bold #d29922]/{name}[/bold #d29922]")
            if cmd:
                content = content + Content.from_text(
                    f"  {cmd.description}", markup=False
                ).stylize("#8b949e")

            self.mount(Static(content))
