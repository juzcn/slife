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
# ═══════════════════════════════════════════════════════════════════════


class PrepareImageTool(Tool):
    """Validate, cache, and convert an image to a public HTTPS URL.

    Sources
      - **local path** → cached, BLOB'd, served via ngrok media URL.
      - **remote URL** → downloaded, cached, BLOB'd, then returned
        as-is (already public — no tunnel needed).

    The image BLOB is always written to ``diary_images`` for permanent
    memory.  Injection uses lightweight HTTPS URLs — base64 is **never**
    sent to the LLM.
    """

    name: ClassVar[str] = "prepare_image"
    category: ClassVar[str] = "Display"
    description: ClassVar[str] = (
        "Load and prepare an image for vision analysis: local path or http(s) URL. "
        "Returns a public HTTPS URL that any vision tool can consume. "
        "Use this BEFORE calling vision/OCR tools so they have a URL. "
        "The image is permanently stored and recoverable across sessions."
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

    def __init__(self, config=None):
        self._config = config

    @classmethod
    def from_config(cls, cfg: dict, config: "Config | None"):
        return cls(config=config)

    async def execute(self, **kwargs) -> str:
        path: str = kwargs["path"]

        # ── URL input: inject directly, no caching needed ───────
        if path.startswith(("http://", "https://")):
            return await self._show_url(path)

        # ── Local file input ────────────────────────────────────
        return await self._show_local(path)

    async def _show_url(self, url: str) -> str:
        """Download an image URL, cache it, write BLOB, and inject."""
        import uuid
        from urllib.parse import urlparse

        import aiohttp

        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return f"Error: 无效的 URL — {url}"

        # ── Download ───────────────────────────────────────────
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    if resp.status != 200:
                        return f"Error: HTTP {resp.status} — {url}"
                    raw = await resp.read()
                    content_type = resp.content_type or ""
        except Exception as e:
            return f"Error: 下载图片失败 — {e}"

        mime_type = content_type if content_type.startswith("image/") else "image/png"
        file_name = (parsed.path.rsplit("/", 1)[-1] if parsed.path else "") or "image"

        return await self._ingest(raw, mime_type, file_name)

    async def _show_local(self, path: str) -> str:
        """Read a local image file, write its BLOB, and inject for the LLM."""
        import mimetypes
        from pathlib import Path
        from slife.ui.image_utils import is_image_file

        p = Path(path)
        if not p.exists():
            return f"Error: 文件不存在 — {path}"
        if not p.is_file():
            return f"Error: 不是文件 — {path}"
        if not is_image_file(path):
            return f"Error: 不支持的图片格式 — {p.suffix}（支持 png/jpg/gif/webp/bmp）"

        raw = p.read_bytes()

        if not mimetypes.inited:
            mimetypes.init()
        mime_type = mimetypes.guess_type(str(p))[0] or "image/png"
        if not mime_type.startswith("image/"):
            mime_type = "image/png"

        return await self._ingest(raw, mime_type, p.name)

    async def _ingest(self, raw: bytes, mime_type: str, file_name: str) -> str:
        """Common pipeline: cache → BLOB → inject (shared by URL and local)."""
        import uuid
        from pathlib import Path
        from slife.paths import get_images_dir

        # ── Determine extension ────────────────────────────────
        import mimetypes
        if not mimetypes.inited:
            mimetypes.init()
        ext = mimetypes.guess_extension(mime_type) or ".png"

        # ── Cache to logs/images/ ──────────────────────────────
        images_dir = get_images_dir()
        images_dir.mkdir(parents=True, exist_ok=True)
        img_id = str(uuid.uuid4())
        cache = images_dir / f"{img_id}{ext}"
        cache.write_bytes(raw)

        # ── Write BLOB to SQLite (for media server + permanent memory) ─
        from slife.agent.multimodal import _write_image_blob as write_blob
        await write_blob(raw, mime_type, file_name, image_id=img_id)

        # ── Stage for LLM analysis: URL only, no base64 ─────────
        if self._config is not None and self._config.active_model.supports_vision:
            from slife.agent.conversation import get_conversation
            from slife.agent.multimodal import image_url_block
            conv = get_conversation()
            if conv is not None:
                block = image_url_block(img_id)
                if block is not None:
                    conv.inject_image(block)

        # ── Build result: compact, scannable ──────────────────
        from slife.media.tunnel import media_url_for
        size_kb = len(raw) / 1024
        size_str = f"{size_kb:.0f} KB" if size_kb < 1024 else f"{size_kb / 1024:.1f} MB"
        url = media_url_for(img_id)
        lines = [
            f"[image: {cache.resolve()}]",
            f"url: {url}" if url else "url: (tunnel offline — no vision)",
            f"file: {file_name}  {size_str}  {mime_type}",
        ]
        return "\n".join(lines)


# Image BLOB write and URL block helpers live in
# ``slife.agent.multimodal`` — shared by prepare_image and
# Conversation.add_user_message.  No base64 ever.
