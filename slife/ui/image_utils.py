"""Terminal image display via textual-image.

Uses the auto-detected ``Image`` widget which selects Sixel, Kitty TGP,
Halfcell, or Unicode based on terminal capability.  CSS constraints
prevent overflow into docked widgets.
"""

from __future__ import annotations

import logging
from pathlib import Path

from textual.content import Content
from textual.widgets import Static

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# Eager import — MUST run before Textual App.run()
# ═══════════════════════════════════════════════════════════════════════

_Image: type | None = None

try:
    from textual_image.widget import Image as _Image  # pyright: ignore[reportMissingImports]
    logger.debug("textual-image loaded")
except Exception:
    logger.debug("textual-image not available")


# ═══════════════════════════════════════════════════════════════════════
# Fallback
# ═══════════════════════════════════════════════════════════════════════

def _fallback_widget(file_path: str, *, broken: bool = False) -> Static:
    path = Path(file_path)
    name = path.name
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
    if _Image is not None:
        try:
            return _Image(str(path.resolve()), classes=css_class)  # type: ignore[return-value]
        except Exception:
            logger.debug("image_create_failed", exc_info=True)
    return _fallback_widget(file_path)


def is_image_file(path: str) -> bool:
    suffix = Path(path).suffix.lower()
    return suffix in {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".tif", ".svg",
    }
