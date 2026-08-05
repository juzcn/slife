"""Meta tools — agent self-management.

list_tools      — inventory with category filter
check_async     — poll background task result
cancel_async    — cancel a running background task
clear_context   — reset conversation history
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from typing import ClassVar, TYPE_CHECKING

if TYPE_CHECKING:
    from slife.config import Config

from slife.tools.base import Tool

logger = logging.getLogger(__name__)

# ═══════════════════════════════════════════════════════════════════════
# list_tools
# ═══════════════════════════════════════════════════════════════════════

_PLUGIN_LABELS: dict[str, str] = {
    "memory": "Memory (built-in plugin)",
    "wechat": "WeChat (built-in plugin)",
}


def _classify(name: str) -> str:
    if name.startswith("a2a_"):
        return "Agent Communication (A2A)"
    if name.startswith("cli_"):
        return "CLI"
    if name.startswith("rest_api_"):
        return "REST API"
    if name.startswith("config_env") or name == "native_tool_set":
        return "Config"
    if name.startswith("skill_") or name in ("list_skills", "use_skill", "add_skill", "remove_skill", "check_skills_dir"):
        return "Skills"
    if name.startswith("check_") or name == "system_health":
        return "System"
    if name.startswith("execute_") or name.startswith("install_") or name.startswith("run_"):
        return "Execution"
    if name.startswith("credential_") or name.startswith("inject_") or name.startswith("uninject_"):
        return "Credentials"
    if name in ("list_tools", "check_async", "cancel_async", "clear_context"):
        return "Meta"
    return "Other"


class ListToolsTool(Tool):
    name: ClassVar[str] = "list_tools"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = "List available tools. category: all (default), native, or mcp."
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "category": {
                "type": "string",
                "enum": ["all", "native", "mcp"],
                "description": "all (default) / native / mcp.",
            },
        },
        "required": [],
    }

    async def execute(self, category: str = "all", **kwargs) -> str:
        from slife.tools.registry import get_registry
        from slife.mcp.tool_adapter import MCPProxyTool

        registry = get_registry()
        if registry is None:
            return "Tool registry is not available (called before initialization)."

        all_tools = registry.list_tools()
        if not all_tools:
            return "No tools are currently registered."

        natives: list[Tool] = []
        mcp_proxies: dict[str, list[Tool]] = defaultdict(list)
        for t in all_tools:
            if isinstance(t, MCPProxyTool):
                mcp_proxies[t._server].append(t)
            else:
                natives.append(t)

        show_native = category in ("all", "native")
        show_mcp = category in ("all", "mcp")
        lines: list[str] = []

        if show_native:
            lines.append(f"## Native Tools ({len(natives)} total)\n")
            native_groups: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for t in sorted(natives, key=lambda t: t.name):
                cat = getattr(t, "category", "") or _classify(t.name)
                desc = t.description.split(".")[0].strip() + "."
                native_groups[cat].append((t.name, desc))

            for cat in sorted(native_groups):
                items = native_groups[cat]
                lines.append(f"### {cat} ({len(items)})")
                for name, desc in items:
                    lines.append(f"- **`{name}`** — {desc}")
                lines.append("")

        if show_mcp and mcp_proxies:
            lines.append(f"## MCP-Connected Servers ({len(mcp_proxies)} servers)\n")
            for server in sorted(mcp_proxies):
                tools = mcp_proxies[server]
                label = _PLUGIN_LABELS.get(server, f"MCP: {server}")
                tool_names = sorted(t.name for t in tools)
                lines.append(f"- **{label}** ({len(tools)} tools): "
                             + ", ".join(f"`{n}`" for n in tool_names))
            lines.append("")
        elif show_mcp and not mcp_proxies:
            lines.append("## MCP-Connected Servers\n\nNo MCP servers connected.\n")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════
# Async tasks
# ═══════════════════════════════════════════════════════════════════════

_tasks: dict[str, asyncio.Task] = {}


def schedule(coro) -> str:
    task_id = uuid.uuid4().hex[:8]
    task = asyncio.create_task(_runner(coro, task_id))
    _tasks[task_id] = task
    logger.info("async_task_scheduled id=%s", task_id)
    return task_id


async def _runner(coro, task_id: str) -> str:
    try:
        result = await coro
    except Exception as e:
        result = f"Error: {type(e).__name__}: {e}"
    logger.info("async_task_done id=%s len=%d", task_id, len(result))
    return result


def _get_task(task_id: str) -> asyncio.Task | None:
    return _tasks.get(task_id)


def _pop_task(task_id: str) -> asyncio.Task | None:
    return _tasks.pop(task_id, None)


class CheckAsyncTool(Tool):
    name: ClassVar[str] = "check_async"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = "查询异步任务结果。运行中则返回状态，已完成则返回结果。"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "异步任务返回的 task_id。"},
        },
        "required": ["task_id"],
    }

    async def execute(self, **kwargs) -> str:
        task_id: str = kwargs["task_id"]
        task = _get_task(task_id)
        if task is None:
            return f"Error: 未找到 task_id='{task_id}'。任务可能已完成并被清理，或 task_id 不正确。"
        if not task.done():
            return f"⏳ 任务仍在运行中…\n  task_id: {task_id}\n  稍后再调用 check_async 查询。"
        _pop_task(task_id)
        try:
            result = task.result()
        except Exception as e:
            result = f"Error: 异步任务执行失败：{type(e).__name__}: {e}"
        return f"✓ 任务完成（task_id: {task_id}）\n\n{result}"


class CancelAsyncTool(Tool):
    name: ClassVar[str] = "cancel_async"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = "取消正在运行的异步任务。已完成的任务无法取消。"
    parameters: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "要取消的任务的 task_id。"},
        },
        "required": ["task_id"],
    }

    async def execute(self, **kwargs) -> str:
        task_id: str = kwargs["task_id"]
        task = _get_task(task_id)
        if task is None:
            return f"Error: 未找到 task_id='{task_id}'。任务可能已完成并被清理，或 task_id 不正确。"
        if task.done():
            _pop_task(task_id)
            return f"任务 '{task_id}' 已经完成，无需取消。"
        task.cancel()
        _pop_task(task_id)
        logger.info("async_task_cancelled id=%s", task_id)
        return f"✓ 任务 '{task_id}' 已取消。"


# ═══════════════════════════════════════════════════════════════════════
# clear_context
# ═══════════════════════════════════════════════════════════════════════

class ClearContextTool(Tool):
    name = "clear_context"
    category: ClassVar[str] = "Meta"
    description = "Clear conversation history, keeping only the system prompt. Use when context is polluted."
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        from slife.agent.conversation import get_conversation

        conv = get_conversation()
        if conv is None:
            return "Conversation is not yet initialised. This tool must be called after the agent service has started."
        removed = conv.clear_history()
        if removed == 0:
            return "Context is already clean — no old turns to remove."
        remaining = len(conv.messages)
        logger.info("clear_context removed=%d remaining=%d", removed, remaining)
        return f"[OK] Cleared {removed} old message(s); {remaining} remaining (system prompt + current turn)."


# ═══════════════════════════════════════════════════════════════════════
# prepare_image


import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import ClassVar

from slife.paths import get_memory_dir
from slife.tools.base import Tool

logger = logging.getLogger(__name__)

# ── Filename helpers ──────────────────────────────────────────────────


def _slugify(text: str) -> str:
    """Turn arbitrary text into a safe filename slug.

    ``"Project Notes 2026!"`` → ``"project-notes-2026"``
    """
    # Lowercase, replace non-alphanum with hyphens
    slug = re.sub(r"[^\w\s-]", "", text.lower())
    slug = re.sub(r"[-\s]+", "-", slug)
    return slug.strip("-")[:120]  # reasonable max length


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
        _INDEX_PATH = get_memory_dir() / "index.json"
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


# ── Tool ──────────────────────────────────────────────────────────────


class SaveToMemoryTool(Tool):
    """Save content, a URL, or a local file to persistent memory.

    Files are stored as plain files in the ``memory/`` folder, always
    accessible via both local path and sharing URL.  The LLM can read
    them directly via their path, and the user can open them via URL.
    """

    name: ClassVar[str] = "save_to_memory"
    category: ClassVar[str] = "Meta"
    description: ClassVar[str] = (
        "Save content to persistent memory. Provide ONE of: content (markdown "
        "text), url (to download), or path (to a local file). The file is "
        "stored in the memory/ folder and accessible via both local path and "
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
                "description": "Local file path to copy into memory. The file is copied, not moved.",
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

        # ── Prepare memory directory ─────────────────────────────
        mem_dir = get_memory_dir()
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

        # Determine title
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

        # Fetch
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
            # Sanitise: keep only safe chars
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
        from slife.sharing.token import sign_path
        from slife.sharing.tunnel import share_url_for

        # Index
        _add_index_entry(title, filepath.name, tags, source)

        # Sharing URL
        token = sign_path(str(filepath.resolve()))
        url = share_url_for(token, filepath.name)

        lines = [
            f"Saved: {filepath}",
        ]
        if url:
            lines.append(f"URL: {url}")
        else:
            lines.append("URL: (sharing offline — file accessible locally)")

        return "\n".join(lines)
