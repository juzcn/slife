"""Shared adapter contract + artifact storage for the media plugin."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx

from slife.paths import get_memfiles_dir

logger = logging.getLogger(__name__)


class MediaAdapterError(Exception):
    """Provider-side failure (HTTP error, error body, failed task, ...).

    Args:
        message: Human-readable detail (surfaced to the LLM verbatim).
        status_code: HTTP status when the failure came from an HTTP response.
    """

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@runtime_checkable
class MediaAdapter(Protocol):
    """Wire-adapter contract.  Every method returns a result string:

    - ``generate_image`` / ``generate_video`` / ``text_to_speech`` return
      the absolute path of the saved artifact.
    - ``transcribe_audio`` returns the transcript text.

    Unsupported capabilities raise :class:`NotImplementedError`.
    """

    async def generate_image(
        self, *, model: str, prompt: str, size: str = "",
        image_path: Path | None = None, extra_params: dict | None = None,
    ) -> str: ...

    async def generate_video(
        self, *, model: str, prompt: str, image_path: Path | None = None,
        extra_params: dict | None = None, deadline_s: float = 1200.0,
    ) -> str: ...

    async def text_to_speech(
        self, *, model: str, text: str, voice: str = "",
        extra_params: dict | None = None,
    ) -> str: ...

    async def transcribe_audio(
        self, *, model: str, audio_path: Path,
        extra_params: dict | None = None,
    ) -> str: ...

    async def close(self) -> None: ...


class ArtifactSaver:
    """Saves generated artifacts under ``{agent}.files/media/{kind}/``."""

    def base_dir(self, kind: str) -> Path:
        base = get_memfiles_dir() / "media" / kind
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _unique_path(self, kind: str, ext: str) -> Path:
        name = (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_{secrets.token_hex(4)}.{ext.lstrip('.')}"
        )
        return self.base_dir(kind) / name

    async def save_url(self, url: str, kind: str, ext: str = "") -> Path:
        """Download *url* and store it; returns the local path."""
        if not ext:
            ext = Path(url.split("?")[0]).suffix.lstrip(".") or "bin"
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=30.0),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
        except httpx.HTTPError as e:
            raise MediaAdapterError(
                f"Failed to download generated {kind}: {e}"
            ) from e
        return self.save_bytes(data, kind, ext)

    def save_bytes(self, data: bytes, kind: str, ext: str) -> Path:
        path = self._unique_path(kind, ext)
        path.write_bytes(data)
        logger.info(
            "media_artifact_saved kind=%s path=%s bytes=%d",
            kind, path, len(data),
        )
        return path
