"""Multimodal utilities — image encoding for vision APIs."""

import base64
import mimetypes
from pathlib import Path


def _ensure_mimetypes() -> None:
    """Lazily initialize mimetypes database (avoids import-time side effect)."""
    if not mimetypes.inited:
        mimetypes.init()


def encode_image(path: str | Path) -> dict:
    """Encode an image file as an OpenAI vision content block.

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


