"""slife-memfiles — standard plugin: file cabinet + public file sharing.

A self-contained, replaceable plugin exactly like memdb / mqtt.  Slife
(the main process) is a thin MCP client: it spawns this plugin over
Streamable HTTP, registers the ``memfiles__*`` tools, and never touches
file-serving state directly.

The plugin owns everything:
  - the token registry (token → local path, in-process),
  - the ngrok tunnel (exposed so multimodal LLM APIs can fetch local
    files by public HTTPS URL),
  - serving the file bytes on the same port via a custom HTTP route
    (``GET /share/{file_id}``) — one port, two protocols: ``/mcp``
    (Streamable HTTP) and ``/share/...`` (plain HTTP).

LLM-visible tools: ``expose_file``, ``save_content_or_files``.
Harness-only tools (``__`` prefix, never LLM-visible): ``__tunnel_status``,
``__register_file``.

Usage::
    uv run python -m slife.plugins.memfiles.server
"""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import shutil
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path

from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from slife.paths import get_memfiles_dir
from slife.plugins.memfiles.tunnel import (
    is_active,
    public_url,
    share_url_for,
    start_monitor,
    start_tunnel,
    stop_monitor,
    stop_tunnel,
)
from slife.server_utils import (
    bind_free_port,
    create_plugin_server,
    run_plugin_server,
    shutdown_server_logging,
    signal_port,
)

# ── Own port — bound by main() so the tunnel can forward to it ────────
_PLUGIN_PORT: int = 0


# ═══════════════════════════════════════════════════════════════════════
# Tunnel lifecycle — eager start on plugin startup, graceful failure
# ═══════════════════════════════════════════════════════════════════════


@asynccontextmanager
async def _memfiles_lifespan(_app):
    """Eagerly start the ngrok tunnel on plugin startup (like the mqtt
    plugin connects the mesh eagerly).  A failed start is tolerated — the
    tunnel retries with its own bounded backoff, and the share tools fall
    back to an on-demand start.  On shutdown the tunnel is disconnected.
    """
    if _PLUGIN_PORT:
        try:
            from slife.threads import run_daemon
            await run_daemon(start_tunnel, _PLUGIN_PORT, name="ngrok-tunnel")
        except Exception as e:
            logger.warning("memfiles_tunnel_eager_failed err=%s", e)
        # Background monitor — one-shot retry if the eager start failed.
        start_monitor(_PLUGIN_PORT)
    try:
        yield
    finally:
        stop_monitor()
        stop_tunnel()


mcp, _log_path, logger = create_plugin_server(
    "slife-memfiles",
    instructions=(
        "slife-memfiles — file cabinet + public file sharing. "
        "expose_file makes a local file reachable by public HTTPS URL "
        "(prefer it when passing local images/files to a multimodal model); "
        "save_content_or_files persists content/URLs/files into the agent's "
        "files folder and returns both the local path and a share URL."
    ),
    lifespan=_memfiles_lifespan,
)


async def _ensure_tunnel() -> bool:
    """Start the tunnel if it isn't active (lazy on-demand fallback)."""
    if is_active():
        return True
    if not _PLUGIN_PORT:
        return False
    try:
        from slife.threads import run_daemon
        await run_daemon(start_tunnel, _PLUGIN_PORT, name="ngrok-tunnel-on-demand")
        return is_active()
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# Token registry — in-process (server + tunnel live in one process)
# ═══════════════════════════════════════════════════════════════════════

_registry: dict[str, str] = {}       # token → absolute local path
_path_to_token: dict[str, str] = {}  # absolute path → token (dedup)


def _register_file(file_path: str) -> str:
    """Return a short hex token for *file_path*, reusing an existing one.

    30 chars (``secrets.token_hex(15)``) — deliberately below 32 to avoid
    the generic ``[A-Za-z0-9]{32,}`` secret-sanitization pattern in
    ``logfmt.py``.  Hex (not base64url) so no underscores break the
    Textual/Rich markdown URL detection.
    """
    existing = _path_to_token.get(file_path)
    if existing:
        return existing
    tok = secrets.token_hex(15)
    _registry[tok] = file_path
    _path_to_token[file_path] = tok
    logger.debug("register_file token=%s path=%s", tok, file_path)
    return tok


def _lookup_file(token: str) -> str | None:
    """Return the local path for *token*, or ``None`` if unknown."""
    return _registry.get(token)


def _reset_registry() -> None:
    """Clear all registered tokens (used by tests)."""
    _registry.clear()
    _path_to_token.clear()


# ═══════════════════════════════════════════════════════════════════════
# Filename helpers
# ═══════════════════════════════════════════════════════════════════════


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


# ── Index helpers (user-facing file index under <agent>.files/index.json) ──

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


def _add_index_entry(title: str, filename: str, tags: list[str], source: str) -> None:
    entries = _load_index()
    entries.append({
        "title": title,
        "filename": filename,
        "tags": tags or [],
        "source": source,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_index(entries)


# ═══════════════════════════════════════════════════════════════════════
# HTTP route — serve file bytes on the same port as /mcp
# ═══════════════════════════════════════════════════════════════════════


@mcp.custom_route("/share/{file_id}", methods=["GET"])
async def handle_share(request: Request) -> Response:
    """Serve a registered local file by its share token.

    Returns 403 for an unknown/expired token, 404 if the file no longer
    exists.  Streams in 64 KB chunks.
    """
    file_id = request.path_params["file_id"]

    file_path_str = _lookup_file(file_id)
    if file_path_str is None:
        return Response("Unknown share link or session expired", status_code=403)

    file_path = Path(file_path_str)
    if not file_path.is_file():
        return Response("File no longer exists", status_code=404)

    if not mimetypes.inited:
        mimetypes.init()
    mime_type, _ = mimetypes.guess_type(str(file_path))
    content_type = mime_type or "application/octet-stream"
    file_size = file_path.stat().st_size

    logger.info("share_served path=%s mime=%s size=%s", file_path, content_type, file_size)

    def _iter_chunks():
        with open(file_path, "rb") as f:
            while chunk := f.read(65536):
                yield chunk

    return StreamingResponse(
        _iter_chunks(),
        status_code=200,
        headers={
            "Content-Type": content_type,
            "Content-Length": str(file_size),
            "Content-Disposition": f'inline; filename="{file_path.name}"',
            # Suppress the ngrok free-tier interstitial browser-warning page.
            "ngrok-skip-browser-warning": "true",
        },
    )


# ═══════════════════════════════════════════════════════════════════════
# Harness-only tools (``__`` prefix — callable by the harness, never the LLM)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(name="__tunnel_status", description="File-sharing tunnel status as JSON.")
async def __tunnel_status() -> str:
    """Return ``{active, url, hint}`` for the harness health check."""
    if is_active():
        return json.dumps(
            {"active": True, "url": public_url() or ""}, ensure_ascii=False,
        )
    return json.dumps(
        {
            "active": False,
            "url": "",
            "hint": (
                "File sharing tunnel unavailable. "
                "Check NGROK_AUTHTOKEN credential or ngrok account limits "
                "(free tier: 1 online agent — one tunnel per token)."
            ),
        },
        ensure_ascii=False,
    )


@mcp.tool(name="__register_file", description="Register a file and return its share URL.")
async def __register_file(path: str) -> str:
    """Register *path* and return ``{file_id, url}`` for the harness."""
    await _ensure_tunnel()
    file_id = _register_file(str(Path(path).resolve()))
    url = share_url_for(file_id) or ""
    return json.dumps({"file_id": file_id, "url": url}, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════════════
# LLM-visible tools (registered as memfiles__<name> by the harness)
# ═══════════════════════════════════════════════════════════════════════


@mcp.tool(
    name="expose_file",
    description=(
        "Expose a local file as a public HTTPS URL for multimodal LLMs to "
        "fetch directly.  Requires the file-sharing tunnel to be active."
    ),
)
async def expose_file(path: str) -> str:
    """Expose a local file as a public HTTPS URL."""
    p = Path(path)
    if not p.exists():
        return f"Error: file not found — {path}"
    if not p.is_file():
        return f"Error: not a file — {path}"

    if not await _ensure_tunnel():
        return (
            "Error: file sharing service is not available. "
            "Run system_health to check service status."
        )

    file_id = _register_file(str(p.resolve()))
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


@mcp.tool(
    name="save_content_or_files",
    description=(
        "Save content, a URL, or a local file to persistent storage "
        "(the agent's files folder).  Provide exactly one of "
        "content/url/path.  Sharing URL included when the tunnel is up.  "
        "Use when the user says 'remember this' / 'save this'."
    ),
)
async def save_content_or_files(
    content: str = "",
    url: str = "",
    path: str = "",
    title: str = "",
    tags: list[str] | None = None,
) -> str:
    """Save content, a URL, or a local file to persistent file storage."""
    tags = tags or []
    sources = [k for k in ("content", "url", "path") if locals()[k]]
    if len(sources) == 0:
        return "Error: provide one of: content, url, or path."
    if len(sources) > 1:
        return f"Error: provide only one source, got: {', '.join(sources)}."

    mem_dir = get_memfiles_dir()
    mem_dir.mkdir(parents=True, exist_ok=True)

    if content:
        return _save_content(content, title, tags, mem_dir)
    if url:
        return await _save_url(url, title, tags, mem_dir)
    return await _save_path(path, title, tags, mem_dir)


# ── Save helpers ─────────────────────────────────────────────────────


def _build_result(filepath: Path, title: str, tags: list[str], source: str) -> str:
    """Build result string + update index.  Common to all source types."""
    _add_index_entry(title, filepath.name, tags, source)
    file_id = _register_file(str(filepath.resolve()))
    url = share_url_for(file_id)

    lines = [f"Saved: {filepath}"]
    if url:
        lines.append(f"URL: {url}")
    else:
        lines.append("URL: (sharing offline — file accessible locally)")
    return "\n".join(lines)


def _save_content(content: str, title: str, tags: list[str], mem_dir: Path) -> str:
    if not content.strip():
        return "Error: content is empty."
    display_title = title or _extract_title(content) or "untitled"
    stem = _slugify(display_title) or "untitled"
    filepath = _unique_path(mem_dir, stem, ".md")
    filepath.write_text(content.strip(), encoding="utf-8")
    return _build_result(filepath, display_title, tags, "content")


async def _save_url(url: str, title: str, tags: list[str], mem_dir: Path) -> str:
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

    if url_name and "." in url_name:
        ext = "." + url_name.rsplit(".", 1)[-1].split("?")[0]
        ext = re.sub(r"[^\w.]", "", ext)[:10]
        if not ext.startswith("."):
            ext = ""
    else:
        ext = ""

    filepath = _unique_path(mem_dir, stem, ext or "")
    filepath.write_bytes(raw)
    return _build_result(filepath, display_title, tags, "url")


async def _save_path(path: str, title: str, tags: list[str], mem_dir: Path) -> str:
    src = Path(path)
    if not src.exists():
        return f"Error: file not found — {path}"
    if not src.is_file():
        return f"Error: not a file — {path}"

    stem = _slugify(title) if title else src.stem
    display_title = title or src.name

    filepath = _unique_path(mem_dir, stem, src.suffix)
    shutil.copy2(src, filepath)
    return _build_result(filepath, display_title, tags, "path")


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    """Run the memfiles plugin on Streamable HTTP.

    Binds its own port first (the plugin owns the ngrok tunnel and must
    know its port to forward to), signals it to the parent, then serves
    MCP on ``/mcp`` and file bytes on ``/share/{file_id}`` — same port.
    """
    import os

    global _PLUGIN_PORT
    sock, port = bind_free_port()
    _PLUGIN_PORT = port
    os.environ["SLIFE_MEMFILES_PORT"] = str(port)
    signal_port(port)

    logger.info("memfiles_start log=%s port=%s", _log_path, port)

    try:
        run_plugin_server(mcp, sockets=[sock])
    finally:
        logger.info("memfiles_shutdown port=%s", port)
        shutdown_server_logging()


if __name__ == "__main__":
    main()
