"""Multimodal utilities — image encoding for vision APIs.

All image injection now goes through :func:`prepare_image_url` which
produces lightweight HTTPS URL blocks served by the media server.
Base64 data URIs are **never** sent to the LLM — if no ngrok tunnel
is active, image injection is silently skipped.
"""

import base64
import logging
import mimetypes
import os
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _ensure_mimetypes() -> None:
    """Lazily initialize mimetypes database (avoids import-time side effect)."""
    if not mimetypes.inited:
        mimetypes.init()


# ── Preparation (write BLOB → URL) ────────────────────────────────


async def prepare_image_url(path: str | Path) -> dict[str, Any] | None:
    """Prepare an image for the LLM: read → SQLite BLOB → return URL block.

    Returns an OpenAI vision content block when the ngrok tunnel is
    active, or ``None`` when no tunnel is running (image injection
    is silently skipped — no base64 fallback).
    """
    from slife.media.tunnel import media_url_for

    p = Path(path)
    if not p.is_file():
        logger.debug("prepare_image_not_found path=%s", p)
        return None

    raw = p.read_bytes()
    _ensure_mimetypes()
    mime_type = mimetypes.guess_type(str(p))[0] or "image/png"
    if not mime_type.startswith("image/"):
        mime_type = "image/png"

    image_id = await _write_image_blob(raw, mime_type, p.name)
    if image_id is None:
        return None

    url = media_url_for(image_id)
    if url is None:
        logger.debug("prepare_image_no_tunnel id=%s", image_id)
        return None

    return {"type": "image_url", "image_url": {"url": url}}


# ── Image blocks from existing BLOBs ──────────────────────────────


def image_url_block(image_id: str) -> dict[str, Any] | None:
    """Build a URL content block for an image already in the BLOB table.

    Callers that have already written the BLOB (e.g. ``show_image``
    via :func:`_ingest`) use this to avoid a duplicate write.
    """
    from slife.media.tunnel import media_url_for

    url = media_url_for(image_id)
    if url is None:
        return None
    return {"type": "image_url", "image_url": {"url": url}}


# ── BLOB write helper ─────────────────────────────────────────────


async def _write_image_blob(
    data: bytes, mime_type: str, file_name: str, *, image_id: str | None = None,
) -> str | None:
    """Write raw image bytes to ``diary_images`` and return the image_id.

    If *image_id* is not given a random UUID is generated.  Callers
    that already have an image_id (e.g. from the cache filename)
    should pass it explicitly so both match.

    Returns ``None`` if the write fails (logged).
    """
    import aiosqlite

    from slife.paths import get_db_path

    try:
        db_path = get_db_path(os.environ.get("SLIFE_AGENT_ID", "slife"))
        if image_id is None:
            image_id = str(uuid.uuid4())
        db = await aiosqlite.connect(str(db_path))
        try:
            # Ensure the table exists — plugins start in parallel so
            # the memory server may not have created it yet.
            await db.execute(
                """CREATE TABLE IF NOT EXISTS diary_images (
                       image_id  TEXT PRIMARY KEY,
                       data      BLOB NOT NULL,
                       mime_type TEXT NOT NULL DEFAULT 'image/png',
                       file_name TEXT NOT NULL DEFAULT '',
                       file_size INTEGER NOT NULL DEFAULT 0
                   )"""
            )
            await db.execute(
                """INSERT OR REPLACE INTO diary_images
                   (image_id, data, mime_type, file_name, file_size)
                   VALUES (?, ?, ?, ?, ?)""",
                (image_id, data, mime_type, file_name, len(data)),
            )
            await db.commit()
            logger.debug("blob_written id=%s size=%d", image_id, len(data))
            return image_id
        finally:
            await db.close()
    except Exception as e:
        logger.warning("blob_write_failed err=%s", e)
        return None


# ── Legacy: base64 encode (for tests only, NOT used in LLM context) ─


def encode_image(path: str | Path) -> dict:
    """Encode an image file as an OpenAI vision content block.

    **This function is deprecated for LLM context.**  Use
    :func:`prepare_image_url` instead, which produces a lightweight
    HTTPS URL served by the media server.  This function only exists
    for test code and is never called from production paths.

    Returns a dict suitable for use in a user message's content array:
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}

    Supported formats: PNG, JPEG, GIF, WebP (depends on model).
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {path}")
    if not path.is_file():
        raise ValueError(f"Not a file: {path}")

    data = path.read_bytes()

    # Guess MIME type, default to PNG
    _ensure_mimetypes()
    mime_type = mimetypes.guess_type(str(path))[0] or "image/png"
    if not mime_type.startswith("image/"):
        mime_type = "image/png"

    b64 = base64.b64encode(data).decode("ascii")

    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime_type};base64,{b64}"},
    }
