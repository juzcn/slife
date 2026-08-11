"""Display tools — pure UI surfaces for the human operator.

``show_image`` renders an image in the terminal; ``notify_user`` raises a
desktop notification.  Both are general UI tools: the LLM never sees the
rendered output, it just triggers the display.
"""

from __future__ import annotations

from typing import ClassVar

from slife.tools.base import Tool


class ShowImageTool(Tool):
    """Display an image in the terminal.

    Sources
      - **local path** → displayed directly.
      - **remote URL** → downloaded to a temp cache, then displayed.

    No BLOB storage, no URL injection, no base64.  Just display.
    """

    name: ClassVar[str] = "show_image"
    category: ClassVar[str] = "Display"
    # Pure UI tool: renders the image in the terminal via the `[image: <path>]`
    # marker — the LLM never sees the pixels.  No vision required.
    description: ClassVar[str] = (
        "Display an image in the terminal. Accepts a local file path or "
        "an http(s) URL."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": (
                    "Image path or URL. "
                    "Local: 'D:\\Downloads\\photo.png'. "
                    "URL: 'https://example.com/photo.jpg'."
                ),
            },
        },
        "required": ["path"],
    }

    async def execute(self, **kwargs) -> str:
        path: str = kwargs["path"]

        if path.startswith(("http://", "https://")):
            return await self._show_url(path)

        return self._show_local(path)

    async def _show_url(self, url: str) -> str:
        """Download image URL → cache → display."""
        import uuid
        from urllib.parse import urlparse

        import aiohttp
        import mimetypes

        from slife.paths import get_images_dir

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return f"Error: invalid URL — {url}"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30),
                ) as resp:
                    if resp.status != 200:
                        return f"Error: HTTP {resp.status} — {url}"
                    raw = await resp.read()
                    content_type = resp.content_type or ""
        except Exception as e:
            return f"Error: download failed — {e}"

        # Determine extension
        url_name = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
        if content_type.startswith("image/"):
            if not mimetypes.inited:
                mimetypes.init()
            ext = mimetypes.guess_extension(content_type) or ".png"
        elif url_name and "." in url_name:
            ext = "." + url_name.rsplit(".", 1)[-1].split("?")[0]
        else:
            ext = ".png"

        # Cache to logs/images/ for display
        images_dir = get_images_dir()
        images_dir.mkdir(parents=True, exist_ok=True)
        cache = images_dir / f"{uuid.uuid4()}{ext}"
        cache.write_bytes(raw)

        resolved = str(cache.resolve())
        return (
            f"[image: {resolved}]\n"
            f"{url}"
        )

    def _show_local(self, path: str) -> str:
        """Validate local image file → display."""
        from pathlib import Path

        from slife.ui.image_utils import is_image_file

        p = Path(path)
        if not p.exists():
            return f"Error: file not found — {path}"
        if not p.is_file():
            return f"Error: not a file — {path}"
        if not is_image_file(path):
            return f"Error: unsupported image format — {p.suffix} (png/jpg/gif/webp/bmp)"

        resolved = str(p.resolve())
        return (
            f"[image: {resolved}]\n"
            f"{resolved}"
        )


class NotifyUserTool(Tool):
    """Push a desktop notification to the human operator.

    A pure UI tool — like :class:`ShowImageTool`, it only triggers the
    display; the LLM never sees the notification itself.
    """

    name: ClassVar[str] = "notify_user"
    category: ClassVar[str] = "Display"
    description: ClassVar[str] = (
        "Send a desktop notification to the human user (e.g. task done, "
        "attention needed)."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Short notification title (e.g. 'Task Complete', 'Alert').",
            },
            "message": {
                "type": "string",
                "description": "The notification body — be concise (one sentence).",
            },
        },
        "required": ["message"],
    }

    async def execute(self, title: str = "slife", message: str = "", **kwargs) -> str:
        if not message:
            return "Error: message is required."

        # Log prominently
        import logging
        logging.getLogger(__name__).warning(
            "USER_NOTIFICATION title=%s message=%s", title, message,
        )

        # Fire desktop notification (best-effort, non-blocking).
        # Daemon thread: a hung notify backend must never block shutdown.
        from slife.platform import desktop_notify
        from slife.threads import run_daemon
        run_daemon(desktop_notify, title, message, name="desktop-notify")

        return f"Notification sent: [{title}] {message}"
