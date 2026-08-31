"""Tool call display widget — Claude Code CLI style with human-friendly labels.

Design: a scrollable panel (VerticalScroll) holding one Static child that
carries the whole Content tree (header line plus, when expanded, args and
result).  Long results scroll inside the panel instead of being clipped.

Safety: user data (args, results) is placed in Content.from_text(markup=False),
so special characters like &, [, ] are never interpreted as markup —
eliminating MarkupError crashes from search results containing URLs, JSON, etc.
"""

import os
import subprocess
import sys
from textual.content import Content
from textual.containers import VerticalScroll
from textual.widgets import Static

from slife.platform import IS_WINDOWS
from slife.ui.content import lit as _lit
from slife.ui.content import mc as _mc
from slife.ui.i18n import t

# WSL: Linux kernel with Windows interop — clip.exe is the native clipboard.
_IS_WSL = sys.platform == "linux" and os.path.exists("/proc/sys/fs/binfmt_misc/WSLInterop")

_counter: int = 0


def _unique_suffix() -> str:
    """Return a unique counter-based suffix to prevent widget ID collisions."""
    global _counter
    _counter += 1
    return str(_counter)


# ── Tool display helpers ─────────────────────────────────────────────

_PRIMARY_ARG_MAX = 72
# Line budget for an expanded result body.  The panel is scrollable and
# capped by max-height (see slife.tcss `.tool-call`), so this is only a
# content-size guard against pathological tool output (a huge shell dump):
# beyond it the rest is cut with an explicit note.  Tool output is already
# bounded upstream by the 20%-of-context rule (~200k tokens worst case,
# extremely rare), so realistic outputs are never touched; 2000 lines keeps
# the Content build bounded while comfortably covering even long git diffs.
# The QR login block (~25 lines) fits trivially inside it.
_MAX_RESULT_LINES = 2000


def _friendly_label(tool_name: str, status: str) -> str:
    """Return a human-readable label: present tense when running, past when done."""
    label = tool_name.replace("_", " ").capitalize()
    if status in ("running", "pending"):
        return label  # "Run command"
    return label  # same — simple is fine, the status icon already signals done


def _primary_arg_value(tool_args: dict) -> str | None:
    """Pick the first non-empty string argument for the header preview."""
    for v in tool_args.values():
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

# Status → i18n key.  Resolved through t() at render time so the label
# follows the active language (en/zh).  Kept as a key map (not a pre-built
# string dict) so a language switch via set_language() takes effect on the
# next render without rebuilding the table.
_STATUS_LABEL_KEY: dict[str, str] = {
    "running": "td_running",
    "done":    "td_done",
    "error":   "td_error_label",
    "pending": "td_pending",
}

_STATUS_DEFAULT = "pending"


# ── Widget ───────────────────────────────────────────────────────────


class ToolCallWidget(VerticalScroll):
    """Scrollable tool call display: a header row + expandable detail body.

    The widget is a real scroll container (Textual's ``Static`` has a
    fixed ``virtual_size`` in this version and cannot scroll), so long
    results — command output, QR login blocks — grow up to
    ``max-height`` and scroll inside the panel with a visible scrollbar
    instead of being clipped silently.

    Design rationale:
      - One Static child carries the whole Content tree (header line
        plus, when expanded, args + result) — the single-renderable
        structure is preserved, only the render target changed.
      - User data goes through _lit() (Content.from_text(markup=False))
        so special characters never cause MarkupError.

    Keyboard:
      - Ctrl+Y — copy result (when widget is focused and expanded)
      - Enter / Space — toggle expand/collapse
      - scroll as usual (wheel / PageUp / PageDown) inside the panel

    Claude Code style: amber header line, expandable detail below.
    """

    can_focus = True

    BINDINGS = [
        ("enter,space", "toggle", "Toggle detail"),
        ("ctrl+y", "copy_result", "Copy result"),
    ]

    def __init__(
        self,
        tool_name: str,
        tool_args: dict,
        tool_call_id: str,
        iteration: int = 0,
        max_iterations: int = 30,
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
        # Single renderable child holds every state of the panel.
        self._content: Static | None = None
        super().__init__()
        self.add_class("tool-call")

    # ── Composition ────────────────────────────────────────────────

    def compose(self) -> list[Static]:
        self._content = Static("", classes="tool-content")
        return [self._content]

    def on_mount(self) -> None:
        self._refresh()
        # Re-render once the container is laid out so the "more lines
        # below" hint reflects the real scroll-overflow state.
        self.call_after_refresh(self._refresh)

    def _refresh(self) -> None:
        if self._content is not None:
            self._content.update(self._build_content())

    def _scroll_more_hint(self) -> Content | None:
        """A one-line "more lines below" hint when the expanded panel
        overflows (content clipped by max-height).  ``max_scroll_y`` only
        exists after layout, so bare/unit-test instances are guarded.
        """
        try:
            n = self.max_scroll_y
        except Exception:
            n = 0
        if n <= 0:
            return None
        return _mc(f"[#484f58]{t('td_scroll_more', n=n)}[/#484f58]\n")

    # ── Public API ─────────────────────────────────────────────────

    def set_running(self) -> None:
        """Indicate the tool is currently executing."""
        self._status = "running"
        self._refresh()

    def set_complete(self, result: str, is_error: bool = False) -> None:
        """Indicate the tool has completed with a result."""
        self._status = "error" if is_error else "done"
        self._result = result
        self._result_is_error = is_error
        self._refresh()

    def toggle(self) -> None:
        """Toggle the detail area visibility."""
        self._is_collapsed = not self._is_collapsed
        self._refresh()

    def on_click(self) -> None:
        """Expand detail on click (never collapse — avoids destroying text selection).

        When collapsed, any click expands so the user can read args/results.
        When expanded, clicks do nothing — the user may be selecting text
        to copy. Use Enter/Space (or the toggle binding) to collapse instead.
        """
        if self._is_collapsed:
            self.toggle()

    def action_copy_result(self) -> None:
        """Copy the result (or arguments if no result yet) to clipboard."""
        text = self._result if self._result else str(self.tool_args)
        if not text:
            return
        _copy_to_clipboard(text)

    async def action_toggle(self, attribute_name: str = "") -> None:
        """Toggle expand/collapse via keyboard."""
        self.toggle()

    # ── Rendering ──────────────────────────────────────────────────

    def _build_content(self) -> Content:
        """Build the full Content tree for the widget."""
        content = self._header_line()

        if not self._is_collapsed:
            body = self._detail_block()
            # Visible scroll affordance at the top of the expanded panel:
            # a pending scroll-offset means content is clipped below — show
            # it unconditionally (independent of Textual's scrollbar draw).
            hint = self._scroll_more_hint()
            if hint is not None:
                body = hint + body
            content = content + _mc("\n") + body

        return content

    # ── Content builders ────────────────────────────────────────────

    def _header_line(self) -> Content:
        """Build the one-line header with status icon, label, and arg preview."""
        status = self._status
        color = _STATUS_COLOR.get(status, _STATUS_COLOR[_STATUS_DEFAULT])
        icon = _STATUS_ICON.get(status, _STATUS_ICON[_STATUS_DEFAULT])
        label_key = _STATUS_LABEL_KEY.get(status, _STATUS_LABEL_KEY[_STATUS_DEFAULT])
        label_text = t(label_key)
        indicator = "▾" if not self._is_collapsed else "▸"
        label = _friendly_label(self.tool_name, status)

        # Indicator
        content = _lit(indicator + " ")
        # Status icon (colored)
        content = content + _lit(icon + " ", style=color)
        # Label (bold amber) — _lit, not _mc: the tool name is LLM/MCP
        # controlled and may contain markup characters like `[`.
        content = content + _lit(label, style="bold #d29922")

        # Primary arg preview (user data — safe path).  The value is raw
        # LLM/tool data shown in the ALWAYS-VISIBLE collapsed header, so run it
        # through the same sanitizer the loop applies to tool results — a
        # secret passed as an argument must not sit in the header.
        primary = _primary_arg_value(self.tool_args)
        if primary:
            from slife.logfmt import sanitize_secrets
            primary = sanitize_secrets(primary)
            short = primary[:_PRIMARY_ARG_MAX]
            if len(primary) > _PRIMARY_ARG_MAX:
                short += "…"
            content = content + _mc(": ") + _lit(short, style="#8b949e")

        # Status text
        content = content + _lit("  ") + _lit(label_text, style=color)

        # Iteration counter (e.g. "1/10") — hidden when iterations are
        # uncapped (max_iterations = 0 means no limit).
        if self._iteration > 0 and self._max_iterations > 0:
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
            content = content + _mc(f"[bold #8b949e]{t('td_arguments')}[/bold #8b949e]\n")
            for key, value in self.tool_args.items():
                val_str = str(value)
                if len(val_str) > 500:
                    val_str = val_str[:500] + "…"
                content = content + _lit(f"  {key} = ", style="#8b949e")
                content = content + _lit(val_str, style="#c9d1d9")
                content = content + _mc("\n")
        else:
            content = content + _mc(f"[#8b949e]{t('td_no_args')}[/#8b949e]")

        # ── Result ───────────────────────────────────────────────
        if self._result:
            content = content + _mc("\n")
            if self._result_is_error:
                content = content + _mc(f"[bold #f85149]{t('td_error')}[/bold #f85149]\n")
                content = content + _lit(self._result, style="#f85149")
            else:
                result_lines = self._result.split("\n")
                content = content + _mc(f"[bold #8b949e]{t('td_result')}[/bold #8b949e]\n")
                if len(result_lines) > _MAX_RESULT_LINES:
                    result_display = "\n".join(result_lines[:_MAX_RESULT_LINES])
                    content = content + _lit(result_display, style="#c9d1d9")
                    content = content + _mc("\n")
                    content = content + _mc(
                        f"[#484f58]{t('td_more_lines', n=len(result_lines) - _MAX_RESULT_LINES)}[/#484f58]"
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
        if IS_WINDOWS:
            subprocess.run(
                ["clip"],
                input=text.encode("utf-8"),
                creationflags=subprocess.CREATE_NO_WINDOW,
                check=False,
            )
        elif sys.platform == "darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=False)
        elif _IS_WSL:
            # WSL: use Windows clip.exe (no X11/Wayland clipboard on most WSL setups)
            subprocess.run(
                ["clip.exe"],
                input=text.encode("utf-8"),
                check=False,
            )
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
