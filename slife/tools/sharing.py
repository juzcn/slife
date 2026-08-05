"""Sharing tools — expose local files as publicly accessible HTTPS URLs."""

from __future__ import annotations

from typing import ClassVar

from slife.tools.base import Tool


class ExposeFileTool(Tool):
    """Expose a local file as a public HTTPS URL.

    Signs the file path with HMAC and returns a publicly-accessible URL
    served through the ngrok tunnel.  The URL is primarily intended so
    multimodal LLMs can fetch local images/files without base64 encoding.

    Requires the sharing tunnel to be active.  Returns an error if the
    tunnel is offline or the file does not exist.
    """

    name: ClassVar[str] = "expose_file"
    category: ClassVar[str] = "Sharing"
    description: ClassVar[str] = (
        "Expose a local file as a public HTTPS URL. "
        "Use this to convert a local file path into a URL that multimodal "
        "LLMs can fetch directly — e.g. to pass a local image to a vision "
        "model. Returns an error if the sharing tunnel is not running."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Local file path to expose, e.g. 'D:\\Downloads\\photo.png'. "
                    "The file must exist on disk."
                ),
            },
        },
        "required": ["path"],
    }

    async def execute(self, **kwargs) -> str:
        path: str = kwargs["path"]

        # ── Validate the file exists ──────────────────────────────────
        from pathlib import Path

        p = Path(path)
        if not p.exists():
            return f"Error: file not found — {path}"
        if not p.is_file():
            return f"Error: not a file — {path}"

        # ── Check the tunnel is running ───────────────────────────────
        from slife.sharing.tunnel import is_active, share_url_for

        if not is_active():
            return (
                "Error: the sharing tunnel is not running. "
                "Start ngrok with an auth token (NGROK_AUTHTOKEN) first, "
                "or use @path syntax in the chat input which handles this "
                "automatically."
            )

        # ── Register and build the URL ─────────────────────────────
        from slife.sharing.token import register_file

        file_id = register_file(str(p.resolve()))
        url = share_url_for(file_id)

        if url is None:
            return (
                "Error: sharing tunnel went offline while building the URL. "
                "Please retry."
            )

        return (
            f"Public URL for {p.name}:\n"
            f"{url}\n\n"
            f"Use this URL in multimodal API calls to let the LLM fetch "
            f"the file directly."
        )


class IncludeImageTool(Tool):
    """Wrap an image reference in OpenAI vision content-block format.

    A pure passthrough — accepts the two image formats the OpenAI vision
    API natively supports and returns them structured for multimodal use.
    Local file paths are **not** accepted; use ``expose_file`` first to
    convert a local path to an HTTPS URL, then pass that URL here.

    OpenAI vision API accepts two image formats:

      - **url**  — ``https://...`` (remote URL)
      - **base64** — ``data:image/jpeg;base64,/9j/4AAQ...``

    At least one of *url* or *base64* must be provided.
    """

    name: ClassVar[str] = "include_image"
    category: ClassVar[str] = "Sharing"
    description: ClassVar[str] = (
        "Include an image for multimodal processing. "
        "Provide either url (https:// URL) or base64 (data: URI). "
        "Local file paths are NOT accepted — use expose_file first to "
        "convert a local path to a public HTTPS URL, then call this tool."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": (
                    "Image as an HTTPS URL (https://...). "
                    "NOT a local file path — use expose_file for that."
                ),
            },
            "base64": {
                "type": "string",
                "description": (
                    "Image as a data URI (data:image/jpeg;base64,/9j/4AAQ...)."
                ),
            },
        },
    }

    async def execute(self, **kwargs) -> str:
        url: str = kwargs.get("url", "")
        base64: str = kwargs.get("base64", "")

        if not url and not base64:
            return "Error: provide at least one of 'url' or 'base64'."

        # ── Reject local file paths ───────────────────────────────────
        if url and not url.startswith(("http://", "https://")):
            if len(url) >= 2 and url[1] == ":":
                return (
                    "Error: local file paths are not accepted. "
                    "Use expose_file first to get a public HTTPS URL, "
                    "then pass that URL here."
                )
            return (
                "Error: 'url' must be an HTTPS URL. "
                "For local files, call expose_file first."
            )

        # ── base64 data URI ───────────────────────────────────────────
        if base64:
            return self._format_result(base64, source="data URI")

        # ── HTTPS URL ─────────────────────────────────────────────────
        return self._format_result(url, source="URL")

    @staticmethod
    def _format_result(image_url: str, source: str) -> str:
        """Return a consistent result format."""
        return (
            f"Image ready for multimodal use (source: {source}):\n"
            f"{image_url}\n\n"
            f"Use this URL in multimodal API calls to let the LLM process "
            f"the image."
        )
