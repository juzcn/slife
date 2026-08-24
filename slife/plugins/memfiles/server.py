"""slife-memfiles — standard plugin: notes / diary / files cabinet.

A self-contained, replaceable plugin exactly like memdb / media.  Slife
(the main process) is a thin MCP client: it spawns this plugin over
Streamable HTTP, registers the memfiles tools, and never touches
file-serving state directly.  Public sharing lives in a separate plugin
(``sharefile``) — memfiles is the private cabinet only.

Three typed knowledge stores (each md-mirrored on disk + SQLite-indexed):
  - ``note_save(subject, …)`` — a private note keyed by subject, appended
    to ``notes/<subject>.md``
  - ``diary_write(date, …)``   — a private day's diary, appended to
    ``diary/<date>.md``
  - ``file_save`` / ``url_save`` — saved attachments (bytes on disk,
    metadata + optional LLM ``summary`` in the SQLite index)
All save tools return the saved **local path** (clickable) — they never
auto-publish.  ``cabinet_search`` hybrid-searches them (FTS5 + vec0, reusing
memdb's SemanticManager and RRF merge); ``cabinet_read`` re-opens a saved file.

LLM-visible tools: ``note_save``, ``diary_write``, ``file_save``, ``url_save``,
``note_list``, ``diary_list``, ``note_read``, ``diary_read``, ``list_files``,
``cabinet_search``, ``cabinet_read``, ``cabinet_embedding_check``.

Usage::
    uv run python -m slife.plugins.memfiles.server
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
import shutil
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from urllib.parse import urljoin, urlparse

from slife.paths import get_memfiles_dir
from slife.plugins.memdb.embeddings import EmbeddingClient
from slife.plugins.memdb.semantic import SemanticManager
from slife.plugins.memfiles.store import MemfilesStore, _slugify, _unique_path
from slife.server_utils import create_plugin_server, run_plugin_server

# Hard cap on url_save downloads — a multi-GB public URL must not OOM the
# plugin process by buffering the whole body.
_MAX_SAVE_BYTES = 50 * 1024 * 1024  # 50 MB


@asynccontextmanager
async def _memfiles_lifespan(_app):
    """Ensure the store is ready, then serve.  Public sharing lives in the
    separate ``sharefile`` plugin, so no tunnel lifecycle here."""
    try:
        try:
            await _ensure_store()
        except Exception as e:
            logger.warning("memfiles_store_init_error err=%s", e)
        yield
    finally:
        global _store, _manager
        # Stop the semantic drainer BEFORE closing the store connection.
        if _manager is not None:
            await _manager.close()
            _manager = None
        if _store is not None:
            await _store.close()
            _store = None


mcp, _log_path, logger = create_plugin_server(
    "slife-memfiles",
    instructions=(
        "slife-memfiles — notes / diary / files cabinet (private). "
        "note_save / diary_write / file_save / url_save all return the saved "
        "local path (clickable) — they never auto-publish. file_save / "
        "url_save store one or more files with an optional LLM summary "
        "(given at save time) for semantic search. cabinet_search finds them "
        "by hybrid (keyword + semantic) search; cabinet_read re-opens a "
        "saved file. To publish a local file as a public HTTPS URL, call the "
        "separate sharefile plugin's share_file explicitly."
    ),
    lifespan=_memfiles_lifespan,
)


_store: MemfilesStore | None = None
_manager: SemanticManager | None = None
_db_path: Path | None = None
_init_lock: asyncio.Lock | None = None


def _get_db_path() -> Path:
    """Index DB lives next to the files: ``{agent}.files/.index.db``."""
    return get_memfiles_dir() / ".index.db"


def _get_init_lock() -> asyncio.Lock:
    global _init_lock
    if _init_lock is None:
        _init_lock = asyncio.Lock()
    return _init_lock


async def _ensure_store() -> MemfilesStore:
    """Lazy-init the store + SemanticManager inside the event loop.

    Mirrors memdb: the store is sized from the shared embedding config up
    front (vec0 dimension), while the model itself loads in the background
    via ``SemanticManager.start()`` — saves never wait on it.
    """
    if _store is not None:
        return _store
    async with _get_init_lock():
        return await _ensure_store_locked()


async def _ensure_store_locked() -> MemfilesStore:
    global _store, _manager, _db_path
    if _store is not None:
        return _store

    _db_path = _get_db_path()
    logger.info("memfiles_init db=%s", _db_path)

    probe = EmbeddingClient.from_config()
    defer_vec0 = bool(probe.available and probe.backend == "transformer")
    dim = 0 if defer_vec0 else (probe.dimension if probe.available else 0)
    model_id = (
        f"{probe.backend}:{probe._model}"
        if probe.available and not defer_vec0 else ""
    )

    _store = MemfilesStore(_db_path)
    await _store.setup(embedding_dim=dim, embedding_model=model_id)

    _manager = SemanticManager(_store)
    asyncio.create_task(_manager.start())
    return _store


# ═══════════════════════════════════════════════════════════════════════
# LLM-visible tools (registered by the harness)
# ═══════════════════════════════════════════════════════════════════════


def _saved_result(filepath: Path) -> str:
    """Format a save confirmation.

    Memfiles save tools never auto-publish — sharing lives in the separate
    ``sharefile`` plugin — so a save confirms the local file path only.
    """
    return f"Saved: {filepath}"


#: Extension → category subfolder under ``{agent}.files/files/``.
_FILE_CATEGORIES: dict[str, set[str]] = {
    "images":     {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico"},
    "documents":  {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                   ".txt", ".md", ".rtf", ".odt", ".ods", ".odp"},
    "archives":   {".zip", ".tar", ".gz", ".tgz", ".7z", ".rar", ".bz2", ".xz"},
    "code":       {".py", ".js", ".ts", ".java", ".go", ".rs", ".c", ".cpp", ".h",
                   ".json", ".yaml", ".yml", ".toml", ".sh", ".sql",
                   ".html", ".css", ".xml", ".ini", ".cfg"},
    "audio":      {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".aac"},
    "video":      {".mp4", ".mov", ".avi", ".mkv", ".webm"},
    "data":       {".csv", ".tsv", ".parquet", ".db", ".sqlite", ".jsonl", ".ndjson"},
}
_DEFAULT_CATEGORY = "other"


def _detect_category(filename: str, override: str = "") -> str:
    """Pick a ``files/<category>/`` subfolder for a saved file.

    ``override`` (an explicit LLM ``category`` param) wins; otherwise the
    extension decides.  Unknown extensions land in ``other``.
    """
    if override.strip():
        return _slugify(override) or _DEFAULT_CATEGORY
    ext = Path(filename).suffix.lower()
    for cat, exts in _FILE_CATEGORIES.items():
        if ext in exts:
            return cat
    return _DEFAULT_CATEGORY


@mcp.tool(
    name="note_save",
    description=(
        "Write or update a private note for a subject.  Appends a "
        "timestamped section to the local file notes/<subject>.md and "
        "re-indexes it for search.  The note is private — returns the "
        "local file path, never a public share URL."
    ),
)
async def note_save(
    subject: str, content: str, tags: str | None = None,
) -> str:
    """Write or update a private note for a subject.

    Args:
        subject: The note's subject — also its filename (notes/<subject>.md).
        content: The note body (Markdown).
        tags: Optional comma-separated tags for search.
    """
    store = await _ensure_store()
    try:
        info = await store.upsert_note(subject, content, tags or "")
    except ValueError as e:
        return f"Error: {e}"
    if _manager is not None:
        _manager.on_saved()
    return _saved_result(store.mem_dir / info["file_path"])


@mcp.tool(
    name="diary_write",
    description=(
        "Write today's (or a given date's) diary entry.  Appends a "
        "timestamped section to the local file diary/<date>.md and "
        "re-indexes it for search.  date defaults to today (YYYY-MM-DD). "
        "The diary is private — returns the local file path, never a "
        "public share URL."
    ),
)
async def diary_write(
    date: str | None = None, content: str = "", tags: str | None = None,
) -> str:
    """Write today's (or a given date's) diary entry.

    Args:
        date: The diary date, YYYY-MM-DD (default: today).
        content: The diary entry body (Markdown).
        tags: Optional comma-separated tags for search.
    """
    store = await _ensure_store()
    day = date or datetime.now().strftime("%Y-%m-%d")
    try:
        info = await store.upsert_diary(day, content, tags or "")
    except ValueError as e:
        return f"Error: {e}"
    if _manager is not None:
        _manager.on_saved()
    return _saved_result(store.mem_dir / info["file_path"])


@mcp.tool(
    name="file_save",
    description=(
        "Copy one or more local files into the agent's files folder and "
        "record them.  Auto-filed under files/<category>/ by extension "
        "(images / documents / archives / code / audio / video / data / other); "
        "pass category to override.  Optionally give a title and an LLM "
        "summary (the summary makes the file findable by semantic search). "
        "Returns the saved local paths (clickable); to publish a file as a "
        "public URL call share_file on the returned path."
    ),
)
async def file_save(
    paths: list[str], title: str = "", tags: str | None = None,
    summary: str = "", category: str = "",
) -> str:
    """Copy local files into the cabinet and record them.

    Args:
        paths: Absolute paths of the local files to copy into the cabinet.
        title: Display title (default: the source filename).
        tags: Optional comma-separated tags for search.
        summary: Optional LLM summary — makes the file findable by semantic search.
        category: Optional subfolder override (files/<category>/); auto-detected by extension.
    """
    store = await _ensure_store()
    mem_dir = store.mem_dir
    files_dir = mem_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    results = []
    embedded = False
    for p in paths:
        src = Path(p)
        if not src.exists():
            results.append(f"Error: file not found — {p}")
            continue
        if not src.is_file():
            results.append(f"Error: not a file — {p}")
            continue
        stem = _slugify(title) if title else src.stem
        display_title = title or src.name
        cat_dir = files_dir / _detect_category(src.name, category)
        cat_dir.mkdir(parents=True, exist_ok=True)
        saved = _unique_path(cat_dir, stem, src.suffix)
        shutil.copy2(src, saved)
        rel = saved.relative_to(mem_dir).as_posix()
        mime = mimetypes.guess_type(str(src))[0] or ""
        await store.add_file(
            title=display_title, original_path=str(src), saved_path=rel,
            mime=mime, size=src.stat().st_size, tags=tags or "",
            summary=summary,
        )
        if summary:
            embedded = True
        results.append(_saved_result(saved))
    if _manager is not None and embedded:
        _manager.on_saved()
    return "\n".join(results)


@mcp.tool(
    name="url_save",
    description=(
        "Download a public http(s) URL into the agent's files folder and "
        "record it.  Auto-filed under files/<category>/ by extension; pass "
        "category to override.  Optionally give a title and an LLM summary "
        "(for semantic search).  Only publicly reachable URLs are accepted. "
        "Returns the saved local path (clickable); to publish the file as a "
        "public URL call share_file on the returned path."
    ),
)
async def url_save(
    url: str, title: str = "", tags: str | None = None, summary: str = "",
    category: str = "",
) -> str:
    """Download a public http(s) URL into the cabinet and record it.

    Args:
        url: The public http(s) URL to download.
        title: Display title (default: derived from the URL).
        tags: Optional comma-separated tags for search.
        summary: Optional LLM summary — makes the file findable by semantic search.
        category: Optional subfolder override (files/<category>/); auto-detected by extension.
    """
    import aiohttp

    store = await _ensure_store()
    mem_dir = store.mem_dir
    files_dir = mem_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)

    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return f"Error: invalid URL — {url}"

    # SSRF guard — only publicly reachable http(s) URLs may be fetched.
    from slife.threads import run_daemon

    err = await run_daemon(_reject_non_public_url, url)
    if err:
        return f"Error: refusing URL — {err}"

    raw: bytes | None = None
    try:
        async with aiohttp.ClientSession() as session:
            current = url
            # Follow redirects manually, re-running the SSRF guard on every
            # hop — a public URL that 302s to 169.254.169.254 (cloud metadata)
            # must not be fetched and published.  Bounded chain, no loop.
            for _ in range(5):
                err = await run_daemon(_reject_non_public_url, current)
                if err:
                    return f"Error: refusing URL — {err}"
                async with session.get(
                    current,
                    timeout=aiohttp.ClientTimeout(total=30),
                    allow_redirects=False,
                ) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location")
                        if not location:
                            return f"Error: redirect without Location — {url}"
                        current = urljoin(current, location)
                        continue
                    if resp.status != 200:
                        return f"Error: HTTP {resp.status} — {url}"
                    # Stream with a hard size cap — a multi-GB public URL must
                    # not OOM the plugin by buffering the whole body in RAM.
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in resp.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > _MAX_SAVE_BYTES:
                            return (
                                f"Error: file too large "
                                f"({size // (1024 * 1024)}MB > 50MB)"
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    break
    except Exception as e:
        return f"Error: download failed — {e}"
    if raw is None:
        return f"Error: too many redirects — {url}"

    url_name = parsed.path.rsplit("/", 1)[-1] if parsed.path else ""
    display_title = title or url_name or "untitled"
    stem = _slugify(display_title) or "untitled"
    if url_name and "." in url_name:
        ext = "." + url_name.rsplit(".", 1)[-1].split("?")[0]
        ext = re.sub(r"[^\w.]", "", ext)[:10]
        if not ext.startswith("."):
            ext = ""
    else:
        ext = ""
    cat_dir = files_dir / _detect_category(url_name or display_title, category)
    cat_dir.mkdir(parents=True, exist_ok=True)
    saved = _unique_path(cat_dir, stem, ext or "")
    saved.write_bytes(raw)
    rel = saved.relative_to(mem_dir).as_posix()
    mime = mimetypes.guess_type(url_name)[0] or ""
    await store.add_file(
        title=display_title, original_path=url, saved_path=rel,
        mime=mime, size=len(raw), tags=tags or "", summary=summary,
    )
    if _manager is not None and summary:
        _manager.on_saved()
    return _saved_result(saved)


@mcp.tool(
    name="cabinet_search",
    description=(
        "Search the file cabinet (notes, diary and saved files).  kind: "
        "note | diary | file | all (default).  mode: hybrid (keyword + "
        "semantic) or fts5.  Returns matches with their relative path, "
        "snippet and kind."
    ),
)
async def cabinet_search(
    query: str, kind: str = "all", mode: str = "hybrid", limit: int = 20,
) -> str:
    """Search the file cabinet (notes, diary and saved files).

    Args:
        query: The search text.
        kind: note | diary | file | all (default).
        mode: hybrid (keyword + semantic, default) | fts5 (keyword only).
        limit: Maximum results.
    """
    store = await _ensure_store()
    manager = _manager
    mode = mode.lower()
    if mode not in ("hybrid", "fts5"):
        mode = "hybrid"
    if kind not in ("all", "note", "diary", "file"):
        kind = "all"

    emb: list[float] | None = None
    semantic_available = False
    if mode == "hybrid" and manager is not None and manager.semantic_ready:
        e = manager.embedder
        if e is not None and e.available:
            emb = await e.embed_one(query)
            if emb:
                semantic_available = True

    try:
        hits = await store.search(
            query, kind=kind, limit=limit, mode=mode, embed_query=emb,
        )
    except Exception as e:
        logger.exception("memfiles_search_failed query=%s kind=%s", query, kind)
        return json.dumps({"error": str(e)}, ensure_ascii=False)

    hint = ""
    if mode == "hybrid" and not semantic_available:
        hint = manager.reason if manager else (
            "hybrid degraded to fts5 — embedding backend unavailable"
        )
        if not hits:
            hint += " — no keyword matches either"
    elif not hits:
        hint = "no matching memories found"

    return json.dumps(
        {
            "mode": "hybrid" if semantic_available else "fts5",
            "query": query, "kind": kind, "results": hits, "hint": hint,
        },
        ensure_ascii=False, indent=2,
    )


@mcp.tool(
    name="cabinet_embedding_check",
    description=(
        "File-cabinet semantic-search status: shared embedding config plus "
        "the memfiles index's own gate (semantic_ready, state, unembedded). "
        "Independent from memdb's semantic_index_status — the two plugins "
        "reindex their own DBs, so one can be semantically ready while the "
        "other is still building."
    ),
)
async def cabinet_embedding_check() -> str:
    from slife.plugins.memdb.embedding_config import make_check_report

    await _ensure_store()
    manager = _manager
    try:
        report = make_check_report()
        if manager is not None:
            report["semantic_ready"] = manager.semantic_ready
            report["state"] = manager.state
            report["reason"] = manager.reason
            report["unembedded"] = await manager.unembedded()
            e = manager.embedder
            if e is not None:
                # live embedder facts override the config probe
                report["backend"] = e.backend
                report["model"] = e._model
                report["dimension"] = e.dimension
                report["available"] = e.available
                report["loaded"] = e.loaded
            # hint by state
            state = manager.state
            if state == "ready":
                report["hint"] = (
                    f"{report.get('backend', '')} embedding model ready: "
                    f"{report.get('model', '')} (dim={report.get('dimension')})"
                )
            elif state in ("loading", "indexing"):
                report["hint"] = (
                    f"Memfiles index building — {report.get('unembedded', 0)} "
                    "notes/diary/files pending embedding. Keyword search remains available."
                )
            elif state == "stalled":
                report["hint"] = report.get("reason", "Memfiles semantic index stalled.")
            elif state == "disabled" and report.get("configured"):
                report["hint"] = (
                    "Memfiles semantic search disabled. Re-enable via memdb's "
                    "semantic_search_enable true (shared embedding config)."
                )
        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("memfiles_check_embedding_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="cabinet_read",
    description=(
        "Read a saved file's content by its relative path under the agent's "
        "files folder (as returned by file_save / cabinet_search), e.g. "
        "notes/python.md or diary/2026-08-15.md."
    ),
)
async def cabinet_read(path: str) -> str:
    """Read a saved file's content.

    Args:
        path: Relative path under the cabinet, as returned by file_save / cabinet_search.
    """
    store = await _ensure_store()
    try:
        target = store.resolve_safe_path(path)
    except ValueError as e:
        return f"Error: {e}"
    if not target.is_file():
        return f"Error: not a file — {path}"
    try:
        return target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"Error: cannot read {path} — {e}"


# ── Notes & diary browsing ────────────────────────────────────────


@mcp.tool(
    name="note_list",
    description=(
        "List notes (newest-updated first): subject, tags, file path, "
        "timestamps.  Returns {total, limit, offset, entries} — if "
        "offset + len(entries) < total, pass a higher offset to page. "
        "Use note_read(subject) for full content."
    ),
)
async def note_list(limit: int = 50, offset: int = 0) -> str:
    """List notes, newest-updated first.

    Args:
        limit: Maximum entries to return.
        offset: Skip this many entries (for paging).
    """
    store = await _ensure_store()
    data = await store.list_notes(limit=limit, offset=offset)
    return json.dumps(
        {"total": data["total"], "limit": len(data["entries"]),
         "offset": max(0, offset), "entries": data["entries"]},
        ensure_ascii=False, indent=2,
    )


@mcp.tool(
    name="diary_list",
    description=(
        "List diary entries (newest first), optionally within a date range "
        "since/until (YYYY-MM-DD).  Returns {total, limit, offset, entries} — "
        "if offset + len(entries) < total, pass a higher offset to page. "
        "Use diary_read(date) for full content."
    ),
)
async def diary_list(
    since: str | None = None, until: str | None = None,
    limit: int = 50, offset: int = 0,
) -> str:
    """List diary entries, newest first.

    Args:
        since: Lower bound, YYYY-MM-DD (omit for no lower bound).
        until: Upper bound, YYYY-MM-DD (omit for no upper bound).
        limit: Maximum entries to return.
        offset: Skip this many entries (for paging).
    """
    store = await _ensure_store()
    data = await store.list_diary(since=since, until=until, limit=limit, offset=offset)
    return json.dumps(
        {"total": data["total"], "limit": len(data["entries"]),
         "offset": max(0, offset), "entries": data["entries"]},
        ensure_ascii=False, indent=2,
    )


@mcp.tool(
    name="note_read",
    description="Read a note's full content by its subject.",
)
async def note_read(subject: str) -> str:
    """Read a note's full content.

    Args:
        subject: The note's subject (from note_list / note_save).
    """
    store = await _ensure_store()
    note = await store.get_note(subject)
    if note is None:
        return f"Error: note not found — {subject}"
    return note["content"]


@mcp.tool(
    name="diary_read",
    description="Read a day's diary full content by date (YYYY-MM-DD).",
)
async def diary_read(date: str) -> str:
    """Read a day's diary full content.

    Args:
        date: The diary date, YYYY-MM-DD (from diary_list / diary_write).
    """
    store = await _ensure_store()
    entry = await store.get_diary(date)
    if entry is None:
        return f"Error: diary not found — {date}"
    return entry["content"]


@mcp.tool(
    name="list_files",
    description=(
        "List saved files (newest first) with their category, size, mime, "
        "tags, summary.  Returns {total, limit, offset, entries} — if "
        "offset + len(entries) < total, pass a higher offset to page. "
        "Optionally filter by category (images/documents/archives/code/audio/"
        "video/data/other).  Use cabinet_read(path) for text content."
    ),
)
async def list_files(category: str = "", limit: int = 50, offset: int = 0) -> str:
    """List saved files, newest first.

    Args:
        category: Optional filter — see description for the category list.
        limit: Maximum entries to return.
        offset: Skip this many entries (for paging).
    """
    store = await _ensure_store()
    data = await store.list_files(category=category, limit=limit, offset=offset)
    return json.dumps(
        {"total": data["total"], "limit": len(data["entries"]),
         "offset": max(0, offset), "entries": data["entries"]},
        ensure_ascii=False, indent=2,
    )


# ── SSRF guard (shared with url_save) ─────────────────────────────


def _reject_non_public_url(url: str) -> str | None:
    """Return an error message if *url* is not a publicly reachable http(s)
    URL, else None.

    SSRF guard for ``url_save``: this plugin process fetches *url*, so a
    loopback / LAN / cloud-metadata target (``169.254.169.254`` etc.) would
    let the LLM read addresses the user's browser can't reach — and with the
    ngrok tunnel up, the response would be published as a public file. The
    host is validated as an IP literal or via DNS resolution; **every**
    resolved address must be globally routable. (Redirect chains are not
    re-checked per hop.)
    """
    import ipaddress
    import socket

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return f"only http(s) URLs are supported (got '{parsed.scheme or 'none'}')"
    host = parsed.hostname
    if not host:
        return "URL has no host"
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError as e:
        return f"cannot resolve host '{host}': {e}"
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if (
            ip.is_loopback
            or ip.is_private
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return f"refusing non-public host '{host}' ({ip})"
    return None


# ── Entry point ──────────────────────────────────────────────────────


def main() -> None:
    """Run the memfiles plugin on Streamable HTTP (generic — no own port,
    no tunnel; the sharefile plugin owns sharing)."""
    run_plugin_server(mcp)


if __name__ == "__main__":
    main()
