"""Multimodal utilities — image URL generation for vision APIs.

User-attached images (via ``@path`` syntax) are shared as lightweight
HTTPS URLs through the sharing server.  No BLOBs, no base64 — the file
is served directly from disk via a signed token.
"""

from __future__ import annotations

import logging
import mimetypes
from pathlib import Path
from typing import Any

from slife.sharing.token import sign_path
from slife.sharing.tunnel import share_url_for

logger = logging.getLogger(__name__)


def _ensure_mimetypes() -> None:
    if not mimetypes.inited:
        mimetypes.init()


def prepare_image_url(path: str | Path) -> dict[str, Any] | None:
    """Build an OpenAI vision content block for a local image file.

    Generates a signed sharing URL that the LLM can fetch.  Returns
    ``None`` when the file doesn't exist or the tunnel is offline.
    """
    p = Path(path)
    if not p.is_file():
        logger.debug("prepare_image_not_found path=%s", p)
        return None

    _ensure_mimetypes()
    mime_type = mimetypes.guess_type(str(p))[0] or "image/png"
    if not mime_type.startswith("image/"):
        mime_type = "image/png"

    token = sign_path(str(p.resolve()))
    url = share_url_for(token, p.name)
    if url is None:
        logger.debug("prepare_image_no_tunnel path=%s", p)
        return None

    return {"type": "image_url", "image_url": {"url": url}}
