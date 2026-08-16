"""Terminal image display via textual-image.

Auto-detection selects Sixel, Kitty TGP, Halfcell, or Unicode based on
terminal capability.  However, ``textual-image``'s detection only checks
the terminal protocol layer — it doesn't know whether Textual's compositor
can actually render the escape sequences (VS Code's xterm.js claims Sixel
support but Textual can't render it there).  We add a second layer of
detection to force Halfcell in known-broken environments.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from textual.content import Content
from textual.widgets import Static

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Eager import — MUST run before Textual App.run()
# ═══════════════════════════════════════════════════════════════════════

_Image: type | None = None
_HalfcellImage: type | None = None
_use_sixel: bool = False

# ── Detect whether Sixel actually works with Textual's compositor ─
# textual-image's auto-detection only checks the terminal protocol layer.
# Many terminals claim Sixel support but Textual can't render it there
# (VS Code, PyCharm, Warp, Alacritty, etc.).  We whitelist terminals
# where Sixel + Textual is proven to work.
_sixel_safe_terminals = frozenset({
    "Windows Terminal",   # WT_SESSION env var
    "WezTerm",            # TERM_PROGRAM
    "iTerm.app",          # TERM_PROGRAM
    "kitty",              # KITTY_WINDOW_ID
})
_term_program = os.environ.get("TERM_PROGRAM", "")
_wt_session = os.environ.get("WT_SESSION", "")
_kitty_id = os.environ.get("KITTY_WINDOW_ID", "")

if _wt_session or _kitty_id or _term_program in _sixel_safe_terminals:
    _use_sixel = True

try:
    from textual_image.widget import Image as _Image  # pyright: ignore[reportMissingImports]
    from textual_image.widget import HalfcellImage as _HalfcellImage  # pyright: ignore[reportMissingImports]
    logger.debug("textual-image loaded, sixel_usable=%s", _use_sixel)
except Exception:
    logger.debug("textual-image not available")


# ═══════════════════════════════════════════════════════════════════════
# Fallback
# ═══════════════════════════════════════════════════════════════════════

def _fallback_widget(file_path: str, *, broken: bool = False) -> Static:
    from rich.markup import escape

    path = Path(file_path)
    # The filename can be a user/agent-supplied path — escape Rich markup so
    # a `[` (e.g. "photo[1].png") can't raise MarkupError or inject styling,
    # which used to kill the whole turn via safe_image_widget's "never raises"
    # contract.
    name = escape(path.name)
    prefix = "⚠" if broken else "\U0001f5bc"
    try:
        size_bytes = path.stat().st_size
        size_str = f"{size_bytes / 1024:.0f}KB" if size_bytes >= 1024 else f"{size_bytes}B"
    except OSError:
        size_str = "?KB"
    content = Content.from_markup(
        f"[dim #6e7681]{prefix} {name} ({size_str})[/dim #6e7681]"
    )
    widget = Static(content)
    widget.add_class("chat-image-fallback")
    return widget


# ═══════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════

def safe_image_widget(file_path: str, css_class: str = "chat-image"):
    path = Path(file_path)
    if not path.exists():
        return _fallback_widget(file_path, broken=True)
    if not path.is_file():
        return _fallback_widget(file_path, broken=True)

    # Sixel only in whitelisted terminals where Textual can render it.
    # Everything else gets Halfcell (coloured Unicode — works everywhere).
    if _use_sixel and _Image is not None:
        try:
            return _Image(str(path.resolve()), classes=css_class)  # type: ignore[return-value]
        except Exception:
            logger.debug("sixel_create_failed", exc_info=True)

    # Halfcell (coloured Unicode blocks) works in any true-colour terminal —
    # VS Code, PyCharm, Warp, Alacritty, etc.  CSS constraints prevent
    # overflow into docked widgets.
    if _HalfcellImage is not None:
        try:
            return _HalfcellImage(str(path.resolve()), classes=css_class)  # type: ignore[return-value]
        except Exception:
            logger.debug("halfcell_create_failed", exc_info=True)

    return _fallback_widget(file_path)


def is_image_file(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg",
    }
