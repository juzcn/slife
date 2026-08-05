"""Memfiles tools — expose local files as publicly accessible HTTPS URLs
and save content to persistent file storage."""


from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from slife.paths import get_memfiles_dir
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

# ── Filename helpers ──────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """Turn arbitrary text into a safe filename slug.

    ``"Project Notes 2026!"`` → ``"project-notes-2026"``
    """
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-")[:120]


def _unique_path(directory: Path, stem: str, suffix: str) -> Path:
    """Return a unique file path: ``directory / stem{suffix}``, adding _N if needed."""
    candidate = directory / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    n = 1
    while True:
        candidate = directory / f"{stem}_{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def _extract_title(content: str) -> str | None:
    """Extract a title from the first ``# Heading`` line in markdown content."""
    match = re.match(r"^#\s+(.+)", content.strip(), re.MULTILINE)
    return match.group(1).strip() if match else None


# ── Index helpers ─────────────────────────────────────────────────────

_INDEX_PATH: Path | None = None


def _index_file() -> Path:
    global _INDEX_PATH
    if _INDEX_PATH is None:
        _INDEX_PATH = get_memfiles_dir() / "index.json"
    return _INDEX_PATH


def _load_index() -> list[dict]:
    idx = _index_file()
    if idx.exists():
        try:
            return json.loads(idx.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_index(entries: list[dict]) -> None:
    idx = _index_file()
    idx.parent.mkdir(parents=True, exist_ok=True)
    idx.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")


def _add_index_entry(
    title: str, filename: str, tags: list[str], source: str,
) -> None:
    entries = _load_index()
    entries.append({
        "title": title,
        "filename": filename,
        "tags": tags or [],
        "source": source,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_index(entries)


class ExposeFileTool(Tool):
    """Expose a local file as a public HTTPS URL.

    Registers the file with a short hex token in the file-backed registry
    and returns a publicly-accessible URL served through the ngrok tunnel.
    The URL is primarily intended so multimodal LLMs can fetch local
    images/files without base64 encoding.

    Requires the memfiles tunnel to be active.  Returns an error if the
    tunnel is offline or the file does not exist.
    """

    name: ClassVar[str] = "expose_file"
    category: ClassVar[str] = "MemFiles"
    description: ClassVar[str] = (
        "Expose a local file as a public HTTPS URL. "
        "Use this to convert a local file path into a URL that multimodal "
        "LLMs can fetch directly — e.g. to pass a local image to a vision "
        "model. Returns an error if the memfiles tunnel is not running."
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
        from slife.memfiles.tunnel import is_active, share_url_for

        if not is_active():
            return (
                "Error: the memfiles tunnel is not running. "
                "Start ngrok with an auth token (NGROK_AUTHTOKEN) first, "
                "or use @path syntax in the chat input which handles this "
                "automatically."
            )

        # ── Register and build the URL ─────────────────────────────
        from slife.memfiles.token import register_file

        file_id = register_file(str(p.resolve()))
        url = share_url_for(file_id)

        if url is None:
            return (
                "Error: memfiles tunnel went offline while building the URL. "
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
    category: ClassVar[str] = "MemFiles"
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


# ═══════════════════════════════════════════════════════════════════════
# save_to_memfiles
# ═══════════════════════════════════════════════════════════════════════


class SaveToMemfilesTool(Tool):
    """Save content, a URL, or a local file to persistent file storage.

    Files are stored as plain files in the ``memfiles/`` folder, always
    accessible via both local path and sharing URL.  The LLM can read
    them directly via their path, and the user can open them via URL.
    """

    name: ClassVar[str] = "save_content_or_files"
    category: ClassVar[str] = "MemFiles"
    _subagent_skip: ClassVar[bool] = True  # subagents must not write to the main agent's file store
    description: ClassVar[str] = (
        "Save content to persistent file storage. Provide ONE of: content (markdown "
        "text), url (to download), or path (to a local file). The file is "
        "stored in the memfiles/ folder and accessible via both local path and "
        "a public sharing URL. Use when the user says 'remember this', "
        "'save this', or 'keep this file'."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "content": {
                "type": "string",
                "description": (
                    "Markdown content to save as a .md file. "
                    "Use for conversation summaries, notes, or any text."
                ),
            },
            "url": {
                "type": "string",
                "description": "URL to download and save. The page or file at the URL is stored.",
            },
            "path": {
                "type": "string",
                "description": "Local file path to copy into memfiles. The file is copied, not moved.",
            },
            "title": {
                "type": "string",
                "description": (
                    "Optional title — used as the filename. Auto-generated "
                    "from the first markdown heading, URL path, or original "
                    "filename if omitted."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional tags for categorisation.",
            },
        },
        "required": [],
    }

    async def execute(self, **kwargs) -> str:
        content: str = kwargs.get("content", "")
        url: str = kwargs.get("url", "")
        path: str = kwargs.get("path", "")
        title: str = kwargs.get("title", "")
        tags: list[str] = kwargs.get("tags", [])

        # ── Exactly one source required ──────────────────────────
        sources = [k for k in ("content", "url", "path") if kwargs.get(k)]
        if len(sources) == 0:
            return "Error: provide one of: content, url, or path."
        if len(sources) > 1:
            return f"Error: provide only one source, got: {', '.join(sources)}."

        source = sources[0]

        # ── Prepare memfiles directory ─────────────────────────────
        mem_dir = get_memfiles_dir()
        mem_dir.mkdir(parents=True, exist_ok=True)

        # ── Dispatch by source type ──────────────────────────────
        if source == "content":
            return await self._save_content(content, title, tags, mem_dir)
        elif source == "url":
            return await self._save_url(url, title, tags, mem_dir)
        else:
            return await self._save_path(path, title, tags, mem_dir)

    # ── Content → .md file ───────────────────────────────────────

    async def _save_content(
        self, content: str, title: str, tags: list[str], mem_dir: Path,
    ) -> str:
        if not content.strip():
            return "Error: content is empty."

        display_title = title or _extract_title(content) or "untitled"
        stem = _slugify(display_title) or "untitled"
        suffix = ".md"

        filepath = _unique_path(mem_dir, stem, suffix)
        filepath.write_text(content.strip(), encoding="utf-8")

        return self._build_result(filepath, display_title, tags, "content")

    # ── URL → downloaded file ────────────────────────────────────

    async def _save_url(
        self, url: str, title: str, tags: list[str], mem_dir: Path,
    ) -> str:
        import aiohttp
        from urllib.parse import urlparse

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
        except Exception as e:
            return f"Error: download failed — {e}"

        # Determine filename
        url_name = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
        if title:
            stem = _slugify(title)
            display_title = title
        elif url_name:
            stem = _slugify(url_name.rsplit(".", 1)[0]) if "." in url_name else _slugify(url_name)
            display_title = url_name
        else:
            stem = "untitled"
            display_title = "untitled"

        # Determine extension
        if url_name and "." in url_name:
            ext = "." + url_name.rsplit(".", 1)[-1].split("?")[0]
            ext = re.sub(r"[^\w.]", "", ext)[:10]
            if not ext.startswith("."):
                ext = ""
        else:
            ext = ""

        filepath = _unique_path(mem_dir, stem, ext or "")
        filepath.write_bytes(raw)

        return self._build_result(filepath, display_title, tags, "url")

    # ── Local file → copy ────────────────────────────────────────

    async def _save_path(
        self, path: str, title: str, tags: list[str], mem_dir: Path,
    ) -> str:
        src = Path(path)
        if not src.exists():
            return f"Error: file not found — {path}"
        if not src.is_file():
            return f"Error: not a file — {path}"

        stem = _slugify(title) if title else src.stem
        display_title = title or src.name

        filepath = _unique_path(mem_dir, stem, src.suffix)
        shutil.copy2(src, filepath)

        return self._build_result(filepath, display_title, tags, "path")

    # ── Shared result builder ────────────────────────────────────

    def _build_result(
        self, filepath: Path, title: str, tags: list[str], source: str,
    ) -> str:
        """Build result string + update index. Common to all source types."""
        from slife.memfiles.token import register_file
        from slife.memfiles.tunnel import share_url_for

        _add_index_entry(title, filepath.name, tags, source)

        file_id = register_file(str(filepath.resolve()))
        url = share_url_for(file_id)

        lines = [
            f"Saved: {filepath}",
        ]
        if url:
            lines.append(f"URL: {url}")
        else:
            lines.append("URL: (sharing offline — file accessible locally)")

        return "\n".join(lines)
