"""Tool call display widget — Claude Code CLI style with human-friendly labels.

Design: single Static widget, no child widgets, no compose/query complexity.
All rendering is done by building a Content tree and calling self.update().

Safety: user data (args, results) is placed in Content.from_text(markup=False),
so special characters like &, [, ] are never interpreted as markup —
eliminating MarkupError crashes from search results containing URLs, JSON, etc.
"""

import subprocess
import sys
from dataclasses import dataclass

from textual.content import Content
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
    "web_fetch":         _ToolMeta("Fetching URL",            "Fetched URL",            "url"),
    "run_python_script": _ToolMeta("Building python command", "Built python command",  "script"),
    "get_os_info":       _ToolMeta("Checking OS",             "Checked OS",             ""),
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
    "running": "◌",
    "done":    "●",
    "error":   "●",
    "pending": "◌",
}

_STATUS_COLOR: dict[str, str] = {
    "running": "#d29922",
    "done":    "#3fb950",
    "error":   "#f85149",
    "pending": "#484f58",
}

_STATUS_LABEL: dict[str, str] = {
    "running": "running",
    "done":    "done",
    "error":   "error",
    "pending": "pending",
}

_STATUS_DEFAULT = "pending"


# ── Safe Content builders ────────────────────────────────────────────

def _mc(text: str) -> Content:
    """Build Content from a **controlled** markup string.

    Only use for strings we construct ourselves (labels, section headers).
    Never pass user data or tool output through this function.
    """
    return Content.from_markup(text)


def _lit(text: str, style: str = "") -> Content:
    """Build Content from arbitrary text — NEVER parsed as markup.

    This is the safe path for all user data: command output, search results,
    file contents, etc. Characters like &, [, ] are rendered literally.
    """
    c = Content.from_text(text, markup=False)
    if style:
        c = c.stylize(style)
    return c


# ── Widget ───────────────────────────────────────────────────────────


class ToolCallWidget(Static):
    """Display a tool call as a single Static widget — no child widgets.

    Design rationale:
      - Extends Static directly: self.update() renders everything.
      - No compose(), no child widgets, no query_one/get_child_by_id.
      - No on_mount() needed — the widget is self-contained.
      - Collapsed/expanded rendering is just different Content objects.
      - User data goes through _lit() (Content.from_text(markup=False))
        so special characters never cause MarkupError.

    Keyboard:
      - Ctrl+Y — copy result (when widget is focused and expanded)
      - Enter / Space — toggle expand/collapse

    Claude Code style: amber header line, expandable detail below.
    """

    can_focus = True

    BINDINGS = [
        ("ctrl+y", "copy_result", "Copy result"),
    ]

    def __init__(
        self,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
        iteration: int = 0,
        max_iterations: int = 10,
    ):
        self.tool_name = tool_name
        self.tool_args = tool_args
        self.tool_call_id = tool_call_id
        self._iteration = iteration
        self._max_iterations = max_iterations
        self._is_collapsed = True
        self._status: str = "running"
        self._result: str = ""
        self._result_is_error: bool = False
        self._suffix = _unique_suffix()
        # Pass initial Content to parent so the widget has content
        # from the moment it's mounted — no self.update() in __init__.
        super().__init__(self._build_content())
        self.add_class("tool-call")

    # ── Public API ─────────────────────────────────────────────────

    def set_running(self) -> None:
        """Indicate the tool is currently executing."""
        self._status = "running"
        self.update(self._build_content())

    def set_complete(self, result: str, is_error: bool = False) -> None:
        """Indicate the tool has completed with a result."""
        self._status = "error" if is_error else "done"
        self._result = result[:2000] + "..." if len(result) > 2000 else result
        self._result_is_error = is_error
        self.update(self._build_content())

    def toggle(self) -> None:
        """Toggle the detail area visibility."""
        self._is_collapsed = not self._is_collapsed
        self.update(self._build_content())

    def on_click(self) -> None:
        """Toggle detail on click."""
        self.toggle()

    def action_copy_result(self) -> None:
        """Copy the result (or arguments if no result yet) to clipboard."""
        text = self._result if self._result else str(self.tool_args)
        if not text:
            return
        _copy_to_clipboard(text)

    def action_toggle(self) -> None:
        """Toggle expand/collapse via keyboard."""
        self.toggle()

    # ── Rendering ──────────────────────────────────────────────────

    def _build_content(self) -> Content:
        """Build the full Content tree for the widget."""
        content = self._header_line()

        if not self._is_collapsed:
            content = content + _mc("\n") + self._detail_block()

        return content

    # ── Content builders ────────────────────────────────────────────

    def _header_line(self) -> Content:
        """Build the one-line header with status icon, label, and arg preview."""
        status = self._status
        color = _STATUS_COLOR.get(status, _STATUS_COLOR[_STATUS_DEFAULT])
        icon = _STATUS_ICON.get(status, _STATUS_ICON[_STATUS_DEFAULT])
        label_text = _STATUS_LABEL.get(status, _STATUS_LABEL[_STATUS_DEFAULT])
        indicator = "▾" if not self._is_collapsed else "▸"
        label = _friendly_label(self.tool_name, status)

        # Indicator
        content = _lit(indicator + " ")
        # Status icon (colored)
        content = content + _lit(icon + " ", style=color)
        # Label (bold amber)
        content = content + _mc(f"[bold #d29922]{label}[/bold #d29922]")

        # Primary arg preview (user data — safe path)
        primary = _primary_arg_value(self.tool_name, self.tool_args)
        if primary:
            short = primary[:_PRIMARY_ARG_MAX]
            if len(primary) > _PRIMARY_ARG_MAX:
                short += "…"
            content = content + _mc(": ") + _lit(short, style="#8b949e")

        # Status text
        content = content + _lit("  ") + _lit(label_text, style=color)

        # Iteration counter (e.g. "1/10")
        if self._iteration > 0:
            content = content + _lit(
                f"  ({self._iteration}/{self._max_iterations})",
                style="#484f58",
            )

        return content

    def _detail_block(self) -> Content:
        """Build the expandable detail block with args and result.

        All user data (arg values, result text) goes through _lit()
        which uses Content.from_text(markup=False) — completely safe
        against MarkupError from &, [, ] in command output.
        """
        content = Content()

        # ── Arguments ────────────────────────────────────────────
        if self.tool_args:
            content = content + _mc("[bold #8b949e]Arguments[/bold #8b949e]\n")
            meta = _TOOL_META.get(self.tool_name)
            primary_key = meta.primary_arg if meta else None
            for key, value in self.tool_args.items():
                val_str = str(value)
                if len(val_str) > 500:
                    val_str = val_str[:500] + "…"
                key_style = "#d29922" if key == primary_key else "#8b949e"
                val_style = "#e6edf3" if key == primary_key else "#c9d1d9"
                content = content + _mc(f"  [{key_style}]{key}[/{key_style}] = ")
                content = content + _lit(val_str, style=val_style)
                content = content + _mc("\n")
        else:
            content = content + _mc("[#8b949e](no arguments)[/#8b949e]")

        # ── Result ───────────────────────────────────────────────
        if self._result:
            content = content + _mc("\n")
            if self._result_is_error:
                content = content + _mc("[bold #f85149]Error[/bold #f85149]\n")
                content = content + _lit(self._result, style="#f85149")
            else:
                result_lines = self._result.split("\n")
                content = content + _mc("[bold #8b949e]Result[/bold #8b949e]\n")
                if len(result_lines) > 20:
                    result_display = "\n".join(result_lines[:20])
                    content = content + _lit(result_display, style="#c9d1d9")
                    content = content + _mc("\n")
                    content = content + _mc(
                        f"[#484f58]… {len(result_lines) - 20} more lines …[/#484f58]"
                    )
                else:
                    content = content + _lit(self._result, style="#c9d1d9")

        return content


# ── Clipboard helper ─────────────────────────────────────────────────


def _copy_to_clipboard(text: str) -> None:
    """Copy text to the system clipboard (cross-platform).

    Uses platform-specific commands via subprocess so we don't
    add an external dependency like pyperclip.
    """
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["clip"],
                input=text.encode("utf-8"),
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
        else:
            # Linux — try wl-copy (Wayland) then xclip (X11)
            for cmd in (["wl-copy"], ["xclip", "-selection", "clipboard"]):
                try:
                    subprocess.run(cmd, input=text.encode("utf-8"), check=False)
                    break
                except FileNotFoundError:
                    continue
    except Exception:
        pass
