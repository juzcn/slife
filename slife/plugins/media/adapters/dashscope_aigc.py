"""DashScope AIGC adapter — Aliyun Bailian / Token Plan native API.

One envelope ``{"model", "input", "parameters"}`` covers three shapes:

- **Sync generation** — ``POST /services/aigc/multimodal-generation/generation``
  (image generation, HTTP TTS, ASR).  Result lives in
  ``output.choices[0].message.content[*]`` as ``image`` / ``audio`` /
  ``text`` items.
- **Async tasks** — ``POST /services/aigc/video-generation/video-synthesis``
  with header ``X-DashScope-Async: enable`` returns a ``task_id``; poll
  ``GET /tasks/{task_id}`` until ``task_status`` is SUCCEEDED/FAILED.
- **Local-file input** — two-step upload: ``GET /uploads?action=getPolicy``
  for OSS credentials, multipart POST to ``upload_host``, then reference
  the file as ``oss://{upload_dir}/{filename}`` with request header
  ``X-DashScope-OssResourceResolve: enable``.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from slife.plugins.media.adapters.base import ArtifactSaver, MediaAdapterError
from slife.plugins.media.config import ProviderConfig

logger = logging.getLogger(__name__)

_SYNC_PATH = "/services/aigc/multimodal-generation/generation"
_VIDEO_PATH = "/services/aigc/video-generation/video-synthesis"

#: Poll cadence for async tasks (Aliyun's own examples use 15 s).
_POLL_INTERVAL_S = 15.0

#: Model params may carry this key to override the input field name that
#: carries the reference image (dashscope i2v models vary: image_url /
#: img_url).  Consumed by the adapter, never sent to the API.
_IMAGE_FIELD_KEY = "image_field"
_DEFAULT_IMAGE_FIELD = "image_url"


class DashScopeAIGCAdapter:
    def __init__(self, config: ProviderConfig):
        self._config = config
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._saver = ArtifactSaver()

    # ── HTTP plumbing ────────────────────────────────────────────────

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        timeout=httpx.Timeout(180.0, connect=30.0),
                        headers={
                            "Authorization": f"Bearer {self._config.api_key}",
                        },
                    )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _request(
        self, method: str, url: str, *,
        extra_headers: dict | None = None, json_body: dict | None = None,
    ) -> dict:
        client = await self._ensure_client()
        try:
            resp = await client.request(
                method, url, headers=extra_headers, json=json_body,
            )
        except httpx.HTTPError as e:
            raise MediaAdapterError(
                f"Bailian request failed ({method} {url}): {e}"
            ) from e
        if resp.status_code >= 400:
            raise MediaAdapterError(
                f"Bailian API error ({resp.status_code}): {resp.text[:500]}",
                status_code=resp.status_code,
            )
        try:
            data = resp.json()
        except ValueError as e:
            raise MediaAdapterError(
                f"Bailian returned non-JSON response: {resp.text[:200]}"
            ) from e
        if isinstance(data, dict) and data.get("code"):
            raise MediaAdapterError(
                f"Bailian returned error: {data.get('code')} — "
                f"{data.get('message', '')}"
            )
        return data

    # ── Envelope helpers ─────────────────────────────────────────────

    async def _sync_generate(
        self, model: str, content: list[dict], parameters: dict,
        *, extra_headers: dict | None = None,
    ) -> dict:
        body = {
            "model": model,
            "input": {"messages": [{"role": "user", "content": content}]},
            "parameters": parameters,
        }
        data = await self._request(
            "POST", f"{self._config.base_url}{_SYNC_PATH}",
            extra_headers=extra_headers, json_body=body,
        )
        output = data.get("output")
        if not isinstance(output, dict):
            raise MediaAdapterError(
                f"Bailian response has no output: {str(data)[:300]}"
            )
        return output

    @staticmethod
    def _message_content(output: dict) -> list[dict]:
        try:
            content = output["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise MediaAdapterError(
                f"Unexpected Bailian output shape: {str(output)[:300]}"
            ) from e
        if isinstance(content, str):
            return [{"text": content}]
        if not isinstance(content, list):
            raise MediaAdapterError(
                f"Unexpected Bailian content type: {type(content).__name__}"
            )
        return [item for item in content if isinstance(item, dict)]

    async def _async_submit(
        self, model: str, input_data: dict, parameters: dict,
    ) -> str:
        body = {
            "model": model,
            "input": input_data,
            "parameters": parameters,
        }
        data = await self._request(
            "POST", f"{self._config.base_url}{_VIDEO_PATH}",
            extra_headers={"X-DashScope-Async": "enable"}, json_body=body,
        )
        task_id = (data.get("output") or {}).get("task_id")
        if not task_id:
            raise MediaAdapterError(
                f"Bailian async submit returned no task_id: {str(data)[:300]}"
            )
        logger.info("media_task_submitted model=%s task_id=%s", model, task_id)
        return str(task_id)

    async def _async_poll(self, task_id: str, deadline_s: float) -> dict:
        loop = asyncio.get_running_loop()
        start = loop.time()
        url = f"{self._config.base_url}/tasks/{task_id}"
        while True:
            task = asyncio.current_task()
            if task is not None and getattr(task, "cancelling", lambda: 0)():
                raise asyncio.CancelledError()
            if loop.time() - start > deadline_s:
                raise MediaAdapterError(
                    f"Generation timed out after {int(deadline_s)}s. "
                    f"Provider task_id: {task_id} — the render may still "
                    f"complete on the provider side."
                )
            data = await self._request("GET", url)
            output = data.get("output") or {}
            status = output.get("task_status", "")
            if status == "SUCCEEDED":
                return output
            if status in ("FAILED", "UNKNOWN", "CANCELED"):
                raise MediaAdapterError(
                    f"Generation task failed (status={status}): "
                    f"{output.get('message') or output.get('code') or 'no detail'}"
                )
            await asyncio.sleep(_POLL_INTERVAL_S)

    # ── Local-file upload (two-step OSS) ─────────────────────────────

    async def upload_file(self, *, model: str, file_path: Path) -> str:
        """Upload a local file; returns an ``oss://`` URL for model input."""
        if not file_path.is_file():
            raise FileNotFoundError(f"File not found: '{file_path}'")
        policy = await self._get_upload_policy(model)
        data = policy.get("data") or {}
        upload_host = data.get("upload_host")
        upload_dir = data.get("upload_dir")
        if not upload_host or not upload_dir:
            raise MediaAdapterError(
                f"Upload policy missing upload_host/upload_dir: "
                f"{str(policy)[:300]}"
            )
        object_key = f"{upload_dir}/{file_path.name}"
        form = {
            "key": object_key,
            "OSSAccessKeyId": str(data.get("oss_access_key_id", "")),
            "policy": str(data.get("policy", "")),
            "Signature": str(data.get("signature", "")),
            "success_action_status": "200",
        }
        for src, dst in (
            ("x_oss_forbid_overwrite", "x-oss-forbid-overwrite"),
            ("x_oss_object_acl", "x-oss-object-acl"),
        ):
            if data.get(src):
                form[dst] = str(data[src])
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=30.0),
            ) as upload_client:
                with open(file_path, "rb") as f:
                    resp = await upload_client.post(
                        upload_host, data=form,
                        files={"file": (file_path.name, f)},
                    )
                    resp.raise_for_status()
        except httpx.HTTPError as e:
            raise MediaAdapterError(
                f"Failed to upload file '{file_path.name}': {e}"
            ) from e
        oss_url = f"oss://{object_key}"
        logger.info(
            "media_file_uploaded file=%s oss=%s", file_path.name, oss_url,
        )
        return oss_url

    async def _get_upload_policy(self, model: str) -> dict:
        client = await self._ensure_client()
        url = f"{self._config.base_url}/uploads"
        try:
            resp = await client.get(
                url, params={"action": "getPolicy", "model": model},
            )
        except httpx.HTTPError as e:
            raise MediaAdapterError(
                f"Upload policy request failed: {e}"
            ) from e
        if resp.status_code >= 400:
            raise MediaAdapterError(
                f"Upload policy error ({resp.status_code}): {resp.text[:300]}",
                status_code=resp.status_code,
            )
        return resp.json()

    # ── Capabilities ─────────────────────────────────────────────────

    async def generate_image(
        self, *, model: str, prompt: str, size: str = "",
        image_path: Path | None = None, outputs_dir: str = "",
        extra_params: dict | None = None,
    ) -> str:
        headers: dict[str, str] = {}
        content: list[dict] = [{"text": prompt}]
        if image_path is not None:
            oss_url = await self.upload_file(model=model, file_path=image_path)
            content.append({"image": oss_url})
            headers["X-DashScope-OssResourceResolve"] = "enable"
        params = dict(extra_params or {})
        if size:
            params["size"] = size
        output = await self._sync_generate(
            model, content, params, extra_headers=headers or None,
        )
        for item in self._message_content(output):
            url = item.get("image")
            if url:
                path = await self._saver.save_url(
                    str(url), "image", outputs_dir=outputs_dir,
                )
                return str(path)
        raise MediaAdapterError(
            f"No image in Bailian response: {str(output)[:300]}"
        )

    async def generate_video(
        self, *, model: str, prompt: str, image_path: Path | None = None,
        outputs_dir: str = "", extra_params: dict | None = None,
        deadline_s: float = 1200.0,
    ) -> str:
        params = dict(extra_params or {})
        image_field = str(params.pop(_IMAGE_FIELD_KEY, _DEFAULT_IMAGE_FIELD))
        input_data: dict = {"prompt": prompt}
        if image_path is not None:
            input_data[image_field] = await self.upload_file(
                model=model, file_path=image_path,
            )
        task_id = await self._async_submit(model, input_data, params)
        output = await self._async_poll(task_id, deadline_s)
        url = output.get("video_url")
        if not url:
            results = output.get("results") or []
            if results and isinstance(results, list):
                url = (results[0] or {}).get("url")
        if not url:
            raise MediaAdapterError(
                f"No video_url in completed task: {str(output)[:300]}"
            )
        path = await self._saver.save_url(
            str(url), "video", "mp4", outputs_dir=outputs_dir,
        )
        return str(path)

    async def text_to_speech(
        self, *, model: str, text: str, voice: str = "",
        outputs_dir: str = "", extra_params: dict | None = None,
    ) -> str:
        # qwen-tts uses a flat input {text, voice, language_type, ...} —
        # NOT the chat messages envelope.  Non-streaming responses carry
        # the finished audio at output.audio.url (24 h validity).
        input_data: dict = {"text": text}
        if voice:
            input_data["voice"] = voice
        input_data.update(extra_params or {})
        data = await self._request(
            "POST", f"{self._config.base_url}{_SYNC_PATH}",
            json_body={"model": model, "input": input_data},
        )
        output = data.get("output") or {}
        audio = output.get("audio") or {}
        url = audio.get("url")
        if url:
            path = await self._saver.save_url(
                str(url), "audio", outputs_dir=outputs_dir,
            )
            return str(path)
        b64 = audio.get("data")
        if b64:
            import base64

            path = self._saver.save_bytes(
                base64.b64decode(b64), "audio", "wav", outputs_dir,
            )
            return str(path)
        raise MediaAdapterError(
            f"No audio in Bailian TTS response: {str(output)[:300]}"
        )

    async def transcribe_audio(
        self, *, model: str, audio_path: Path,
        extra_params: dict | None = None,
    ) -> str:
        oss_url = await self.upload_file(model=model, file_path=audio_path)
        output = await self._sync_generate(
            model, [{"audio": oss_url}], dict(extra_params or {}),
            extra_headers={"X-DashScope-OssResourceResolve": "enable"},
        )
        texts = [
            str(item["text"]) for item in self._message_content(output)
            if item.get("text")
        ]
        if not texts:
            raise MediaAdapterError(
                f"No transcript in Bailian ASR response: {str(output)[:300]}"
            )
        return "\n".join(texts)
