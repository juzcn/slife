"""Tool call display widget — Claude Code CLI style with human-friendly labels.

Design: single Static widget, no child widgets, no compose/query complexity.
All rendering is done by building a markup string and calling self.update().
"""

from dataclasses import dataclass

from rich.markup import escape as _escape
from textual.widgets import Static

_counter: int = 0


def _unique_suffix() -> str:
    """Return a unique counter-based suffix to prevent widget ID collisions."""
    global _counter
    _counter += 1
    return str(_counter)


# ── Human-friendly tool metadata ────────────────────────────────────


@dataclass(frozen=True)
class _ToolMeta:
    """Human-friendly metadata for a single tool type.

    Groups the action label (present tense, shown while running),
    done label (past tense, shown when complete), and the primary
    argument key used for header previews into one record.
    """

    action_label: str   # "Running command"
    done_label: str     # "Ran command"
    primary_arg: str    # "command"


# Single source of truth for tool display metadata.
# Add new tools here — no need to update multiple dicts.
_TOOL_META: dict[str, _ToolMeta] = {
    "execute_shell": _ToolMeta("Running command", "Ran command", "command"),
    "web_search":    _ToolMeta("Searching web",  "Searched web",  "query"),
    "list_skills":   _ToolMeta("Listing skills",  "Listed skills",  ""),
    "use_skill":     _ToolMeta("Loading skill",   "Loaded skill",   "skill_name"),
    "read_file":     _ToolMeta("Reading file",   "Read file",     "file_path"),
    "write_file":    _ToolMeta("Writing file",   "Wrote file",    "file_path"),
    "grep":          _ToolMeta("Searching code", "Searched code", "pattern"),
    "glob":          _ToolMeta("Finding files",  "Found files",   "pattern"),
    "web_fetch":     _ToolMeta("Fetching URL",   "Fetched URL",   "url"),
}

# Max preview length for the primary argument value in the header.
_PRIMARY_ARG_MAX = 72


def _friendly_label(tool_name: str, status: str) -> str:
    """Return a human-readable action label for the given tool and status."""
    meta = _TOOL_META.get(tool_name)
    if meta is None:
        return tool_name.replace("_", " ").capitalize()
    return meta.action_label if status in ("running", "pending") else meta.done_label


def _primary_arg_value(tool_name: str, tool_args: dict) -> str | None:
    """Extract the most human-relevant argument value for the header preview."""
    meta = _TOOL_META.get(tool_name)
    key = meta.primary_arg if meta else None
    if key and key in tool_args:
        return str(tool_args[key])
    for _k, v in tool_args.items():
        if isinstance(v, str) and v.strip():
            return v
    return None


# ── Status display constants ─────────────────────────────────────────

_STATUS_ICON: dict[str, str] = {
    "running": "[#d29922]◌[/]",
    "done":    "[#3fb950]●[/]",
    "error":   "[#f85149]●[/]",
    "pending": "[#484f58]◌[/]",
}

_STATUS_TEXT: dict[str, str] = {
    "running": "[#d29922]running[/]",
    "done":    "[#3fb950]done[/]",
    "error":   "[#f85149]error[/]",
    "pending": "[#484f58]pending[/]",
}

_STATUS_DEFAULT = "pending"


# ── Widget ───────────────────────────────────────────────────────────


class ToolCallWidget(Static):
    """Display a tool call as a single Static widget — no child widgets.

    Design rationale:
      - Extends Static directly: self.update() renders everything.
      - No compose(), no child widgets, no query_one/get_child_by_id.
      - No on_mount() needed — the widget is self-contained.
      - Collapsed/expanded rendering is just different markup strings.

    Claude Code style: amber header line, expandable detail below.
    """

    def __init__(
        self,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
    ):
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_call_id = tool_call_id
        self._is_collapsed = True
        self._status: str = "running"
        self._result: str = ""
        self._result_is_error: bool = False
        self._suffix = _unique_suffix()
        # Pass initial markup to parent so the widget has content
        # from the moment it's mounted — no self.update() in __init__.
        super().__init__(self._build_markup())
        self.add_class("tool-call")

    # ── Public API ─────────────────────────────────────────────────

    def set_running(self) -> None:
        """Indicate the tool is currently executing."""
        self._status = "running"
        self.update(self._build_markup())

    def set_complete(self, result: str, is_error: bool = False) -> None:
        """Indicate the tool has completed with a result."""
        self._status = "error" if is_error else "done"
        self._result = result[:2000] + "..." if len(result) > 2000 else result
        self._result_is_error = is_error
        self.update(self._build_markup())

    def toggle(self) -> None:
        """Toggle the detail area visibility."""
        self._is_collapsed = not self._is_collapsed
        self.update(self._build_markup())

    def on_click(self) -> None:
        """Toggle detail on click."""
        self.toggle()

    # ── Rendering ──────────────────────────────────────────────────

    def _build_markup(self) -> str:
        """Build the full markup string for the widget."""
        parts = [self._header_line()]

        if not self._is_collapsed:
            parts.append(self._detail_block())

        return "\n".join(parts)

    # ── Markup builders ────────────────────────────────────────────

    def _header_line(self) -> str:
        """Build the one-line header with status icon, label, and arg preview."""
        icon = _STATUS_ICON.get(self._status, _STATUS_ICON[_STATUS_DEFAULT])
        status = _STATUS_TEXT.get(self._status, _STATUS_TEXT[_STATUS_DEFAULT])
        indicator = "▾" if not self._is_collapsed else "▸"
        label = _friendly_label(self.tool_name, self._status)

        parts = [f"{indicator} {icon} [bold #d29922]{label}[/bold #d29922]"]

        primary = _primary_arg_value(self.tool_name, self.tool_args)
        if primary:
            short = _escape(primary[:_PRIMARY_ARG_MAX])
            if len(primary) > _PRIMARY_ARG_MAX:
                short += "…"
            parts.append(f": [#8b949e]{short}[/#8b949e]")

        parts.extend(["  ", status])
        return "".join(parts)

    def _detail_block(self) -> str:
        """Build the expandable detail block with args and result."""
        lines: list[str] = []

        # ── Arguments ────────────────────────────────────────────
        if self.tool_args:
            lines.append("[bold #8b949e]Arguments[/bold #8b949e]")
            meta = _TOOL_META.get(self.tool_name)
            primary_key = meta.primary_arg if meta else None
            for key, value in self.tool_args.items():
                val_str = _escape(str(value))
                if len(val_str) > 500:
                    val_str = val_str[:500] + "…"
                if key == primary_key:
                    lines.append(
                        f"  [#d29922]{key}[/#d29922] = [#e6edf3]{val_str}[/#e6edf3]"
                    )
                else:
                    lines.append(
                        f"  [#8b949e]{key}[/#8b949e] = [#c9d1d9]{val_str}[/#c9d1d9]"
                    )
        else:
            lines.append("[#8b949e](no arguments)[/#8b949e]")

        # ── Result ───────────────────────────────────────────────
        if self._result:
            lines.append("")
            escaped_result = _escape(self._result)
            if self._result_is_error:
                lines.append("[bold #f85149]Error[/bold #f85149]")
                lines.append(f"[#f85149]{escaped_result}[/#f85149]")
            else:
                result_lines = escaped_result.split("\n")
                lines.append("[bold #8b949e]Result[/bold #8b949e]")
                if len(result_lines) > 20:
                    result_display = "\n".join(result_lines[:20])
                    lines.append(f"[#c9d1d9]{result_display}[/#c9d1d9]")
                    lines.append(
                        f"[#484f58]… {len(result_lines) - 20} more lines …[/#484f58]"
                    )
                else:
                    lines.append(f"[#c9d1d9]{escaped_result}[/#c9d1d9]")

        return "\n".join(lines)
