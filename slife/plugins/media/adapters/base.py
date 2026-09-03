"""Shared adapter contract + artifact storage for the media plugin."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

import httpx2

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
        image: str | Path | None = None, outputs_dir: str = "",
        extra_params: dict | None = None,
    ) -> str: ...

    async def generate_video(
        self, *, model: str, prompt: str, image: str | Path | None = None,
        outputs_dir: str = "", extra_params: dict | None = None,
        deadline_s: float = 1200.0,
    ) -> str: ...

    async def text_to_speech(
        self, *, model: str, text: str, voice: str = "",
        outputs_dir: str = "", extra_params: dict | None = None,
    ) -> str: ...

    async def transcribe_audio(
        self, *, model: str, audio_path: Path,
        extra_params: dict | None = None,
    ) -> str: ...

    async def close(self) -> None: ...


class ArtifactSaver:
    """Saves generated artifacts under the working directory (or the
    directory a caller passes via ``outputs_dir``).

    Generated media are work products — they live in the user's working
    directory, NOT in the memfiles cabinet (which only stores files saved
    explicitly via the save tools).  The ``kind`` argument is retained for
    filename context only; no subdirectory is created.
    """

    def base_dir(self, kind: str, outputs_dir: str = "") -> Path:
        base = Path(outputs_dir).expanduser() if outputs_dir else Path.cwd()
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _unique_path(self, kind: str, ext: str, outputs_dir: str = "") -> Path:
        name = (
            f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_{secrets.token_hex(4)}.{ext.lstrip('.')}"
        )
        return self.base_dir(kind, outputs_dir) / name

    async def save_url(
        self, url: str, kind: str, ext: str = "", outputs_dir: str = "",
    ) -> Path:
        """Download *url* and store it; returns the local path."""
        if not ext:
            ext = Path(url.split("?")[0]).suffix.lstrip(".") or "bin"
        try:
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(300.0, connect=30.0),
                follow_redirects=True,
            ) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.content
        except httpx2.HTTPError as e:
            raise MediaAdapterError(
                f"Failed to download generated {kind}: {e}"
            ) from e
        return self.save_bytes(data, kind, ext, outputs_dir)

    def save_bytes(
        self, data: bytes, kind: str, ext: str, outputs_dir: str = "",
    ) -> Path:
        path = self._unique_path(kind, ext, outputs_dir)
        path.write_bytes(data)
        logger.info(
            "media_artifact_saved kind=%s path=%s bytes=%d",
            kind, path, len(data),
        )
        return path
