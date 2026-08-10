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
    _requires_tunnel: ClassVar[bool] = True  # skipped at registration when tunnel is offline
    description: ClassVar[str] = (
        "Expose a local file as a public HTTPS URL via the file-sharing tunnel, "
        "for multimodal LLMs to fetch directly. Only available when the tunnel is online."
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
                "Error: file sharing service is not available. "
                "Run system_health to check service status."
            )

        # ── Register and build the URL ─────────────────────────────
        from slife.memfiles.token import register_file

        file_id = register_file(str(p.resolve()))
        url = share_url_for(file_id)

        if url is None:
            return (
                "Error: file sharing service became unavailable. "
                "Please retry or run system_health for details."
            )

        return (
            f"Public URL for {p.name}:\n"
            f"{url}\n\n"
            f"Use this URL in multimodal API calls to let the LLM fetch "
            f"the file directly."
        )


class IncludeImageTool(Tool):
    """Include an image for the LLM to process with vision.

    Takes a local file path or HTTPS URL and makes the image visible
    to the vision model.  Works exactly like the ``@`` syntax in chat.
    """

    name: ClassVar[str] = "include_image"
    category: ClassVar[str] = "MemFiles"
    _requires_vision: ClassVar[bool] = True
    description: ClassVar[str] = (
        "Include an image for vision processing. "
        "Pass a local file path or HTTPS URL. Works like @ syntax."
    )
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "source": {
                "type": "string",
                "description": (
                    "Local file path (e.g. 'D:\\Downloads\\photo.jpg') "
                    "or HTTPS URL (e.g. 'https://example.com/photo.jpg')."
                ),
            },
        },
        "required": ["source"],
    }

    async def execute(self, **kwargs) -> str:
        from slife.agent.multimodal import include_image_url
        source: str = kwargs["source"]
        block = include_image_url(source)
        if block is None:
            return f"Error: cannot read image — {source}"

        ctx = getattr(self, "_ctx", None)
        conv = ctx.conversation if ctx is not None else None
        if conv is not None:
            conv.inject_images_to_last_user([block])

        return f"Image included: {source}"


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
    description: ClassVar[str] = (
        "Save content, a URL, or a local file to persistent storage (memfiles/). "
        "Provide exactly one of content/url/path. Sharing URL included when the "
        "tunnel is up. Use when the user says 'remember this' / 'save this'."
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
