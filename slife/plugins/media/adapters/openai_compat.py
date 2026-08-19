"""OpenAI-compatible adapter — ``/images/generations`` family.

Covers providers that expose the OpenAI media endpoints (OpenAI itself,
Azure, and most OpenAI-compatible gateways).  v1 implements image
generation only; TTS/ASR endpoints (``/audio/speech``,
``/audio/transcriptions``) are natural follow-ups on the same base URL.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import httpx

from slife.plugins.media.adapters.base import ArtifactSaver, MediaAdapterError
from slife.plugins.media.config import ProviderConfig

logger = logging.getLogger(__name__)


class OpenAICompatAdapter:
    def __init__(self, config: ProviderConfig):
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._saver = ArtifactSaver()

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(180.0, connect=30.0),
                headers={"Authorization": f"Bearer {self._config.api_key}"},
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def generate_image(
        self, *, model: str, prompt: str, size: str = "",
        image_path: Path | None = None, outputs_dir: str = "",
        extra_params: dict | None = None,
    ) -> str:
        if image_path is not None:
            raise NotImplementedError(
                "image-conditioned generation is not supported by this "
                "adapter yet"
            )
        body: dict = {"model": model, "prompt": prompt, "n": 1}
        if size:
            body["size"] = size
        body.update(extra_params or {})
        client = await self._ensure_client()
        try:
            resp = await client.post(
                f"{self._config.base_url}/images/generations", json=body,
            )
        except httpx.HTTPError as e:
            raise MediaAdapterError(f"Image generation request failed: {e}") from e
        if resp.status_code >= 400:
            raise MediaAdapterError(
                f"Image API error ({resp.status_code}): {resp.text[:500]}",
                status_code=resp.status_code,
            )
        data = resp.json()
        items = data.get("data") or []
        if not items:
            raise MediaAdapterError(
                f"No images in response: {str(data)[:300]}"
            )
        item = items[0]
        if item.get("b64_json"):
            path = self._saver.save_bytes(
                base64.b64decode(item["b64_json"]), "image", "png", outputs_dir,
            )
            return str(path)
        url = item.get("url")
        if url:
            path = await self._saver.save_url(
                str(url), "image", outputs_dir=outputs_dir,
            )
            return str(path)
        raise MediaAdapterError(
            f"Image item has neither url nor b64_json: {str(item)[:300]}"
        )

    async def generate_video(self, **kwargs) -> str:
        raise NotImplementedError("video generation")

    async def text_to_speech(self, **kwargs) -> str:
        raise NotImplementedError("speech synthesis")

    async def transcribe_audio(self, **kwargs) -> str:
        raise NotImplementedError("speech recognition")
