"""Shared adapter contract + artifact storage for the media plugin."""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
import slife.timeouts as _timeouts  # module ref — call-time lookup, reload/patch-safe

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


class _HttpClientMixin:
    """Lazy, lock-protected httpx2 client for media HTTP adapters.

    The openai-compat and dashscope adapters carried verbatim copies of this
    (dashscope added the double-checked lock the mixin always uses); call
    :meth:`_init_http_client` from ``__init__`` and the adapter inherits the
    shared client plumbing.
    """

    #: Set by the concrete adapter's ``__init__`` (a ``ProviderConfig``); the
    #: mixin reads ``api_key`` from it.
    _config: Any

    def _init_http_client(self) -> None:
        self._client: httpx2.AsyncClient | None = None
        self._client_lock = asyncio.Lock()

    async def _ensure_client(self) -> httpx2.AsyncClient:
        """Return the adapter's shared client, creating it on first use."""
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx2.AsyncClient(
                        timeout=httpx2.Timeout(
                            _timeouts.timeouts.transport.media_request,
                            connect=_timeouts.timeouts.transport.media_connect,
                        ),
                        headers={"Authorization": f"Bearer {self._config.api_key}"},
                    )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


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
        deadline_s: float | None = None,  # None = registry transport.media_deadline
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
    explicitly via the save tools).  ``kind`` names the artifact in the log
    line and error messages only; no subdirectory is created.
    """

    def base_dir(self, outputs_dir: str = "") -> Path:
        base = Path(outputs_dir).expanduser() if outputs_dir else Path.cwd()
        base.mkdir(parents=True, exist_ok=True)
        return base

    def _unique_path(self, ext: str, outputs_dir: str = "") -> Path:
        from slife.logfmt import log_stamp

        name = f"{log_stamp()}_{secrets.token_hex(4)}.{ext.lstrip('.')}"
        return self.base_dir(outputs_dir) / name

    async def save_url(
        self, url: str, kind: str, ext: str = "", outputs_dir: str = "",
    ) -> Path:
        """Download *url* and store it; returns the local path."""
        if not ext:
            ext = Path(url.split("?")[0]).suffix.lstrip(".") or "bin"
        try:
            async with httpx2.AsyncClient(
                timeout=httpx2.Timeout(
                        _timeouts.timeouts.transport.media_download,
                        connect=_timeouts.timeouts.transport.media_connect,
                    ),
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
        path = self._unique_path(ext, outputs_dir)
        path.write_bytes(data)
        logger.info(
            "media_artifact_saved kind=%s path=%s bytes=%d",
            kind, path, len(data),
        )
        return path
