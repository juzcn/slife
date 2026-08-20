"""Multimodal utilities — image content block generation for vision APIs."""

from __future__ import annotations

import base64
import logging
import mimetypes
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _ensure_mimetypes() -> None:
    if not mimetypes.inited:
        mimetypes.init()


def is_image_source(source: str | Path) -> bool:
    """Return True when *source* is attachable via ``include_image``: an
    HTTP(S) URL or an existing local file.  Single source of truth shared
    by the ``@path`` extractor and the vision block builders so the
    acceptance check is never duplicated.
    """
    s = str(source)
    if s.startswith(("http://", "https://")):
        return True
    return Path(s).is_file()


def include_image_url(source: str | Path) -> dict[str, Any] | None:
    """Build a vision content block from a local file path or HTTPS URL.

    - Local path → reads file, base64-encodes, returns ``data:`` URI block.
    - HTTPS URL → returns the URL block directly.

    Returns ``None`` when a local file doesn't exist or can't be read.
    """
    source_str = str(source)
    if not is_image_source(source_str):
        logger.debug("include_image_not_found path=%s", source_str)
        return None
    if source_str.startswith(("http://", "https://")):
        return {"type": "image_url", "image_url": {"url": source_str}}

    p = Path(source)
    _ensure_mimetypes()
    mime_type = mimetypes.guess_type(str(p))[0] or "image/png"
    if not mime_type.startswith("image/"):
        mime_type = "image/png"

    try:
        data = base64.b64encode(p.read_bytes()).decode("ascii")
    except OSError:
        logger.debug("include_image_read_error path=%s", p)
        return None

    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{data}"},
    }
