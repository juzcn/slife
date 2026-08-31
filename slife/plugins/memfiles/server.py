"""slife-memfiles — standard plugin: notes / diary / files cabinet.

A self-contained, replaceable plugin exactly like memdb / media.  Slife
(the main process) is a thin MCP client: it spawns this plugin over
Streamable HTTP, registers the memfiles tools, and never touches
file-serving state directly.  Public sharing lives in a separate plugin
(``sharefile``) — memfiles is the private cabinet only.

Four typed knowledge stores (each md-mirrored on disk + SQLite-indexed):
  - ``note_save(subject, …)`` — a private note keyed by subject, appended
    to ``notes/<subject>.md``
  - ``diary_write(date, …)``   — a private day's diary, appended to
    ``diary/<date>.md``
  - ``file_save`` / ``url_save`` — saved attachments (bytes on disk,
    metadata + optional LLM ``summary`` in the SQLite index)
  - ``report_save(name?, …)`` — a report (notes/diary are also report
    documents); an optional ``name`` binds it to a scheduled task and
    confirms that task's run (pending → ran).  Reports double-write to
    ``reports/<slug>.md``.
All save tools return the saved **local path** (clickable) — they never
auto-publish.  ``cabinet_search`` hybrid-searches them (FTS5 + vec0, reusing
memdb's SemanticManager and RRF merge); ``cabinet_read`` re-opens a saved file.

LLM-visible tools: ``note_save``, ``diary_write``, ``file_save``, ``url_save``,
``note_list``, ``diary_list``, ``note_read``, ``diary_read``, ``list_files``,
``cabinet_search``, ``cabinet_read``, ``memfiles_semantic_status``,
``report_save``, ``report_list``, ``report_read``.
The scheduled-task tools (``scheduled_task_*`` / ``scheduled_run_*``) are native
in ``slife/tools/schedule.py`` (category "Schedule"); this plugin only exposes
the ``__scheduled_*`` data layer they call over the memfiles MCP client.
Internal tools (``__`` prefix, never LLM-visible): ``__check``,
``__memfiles_reload_semantic``, and the ``__scheduled_*`` registry ops.

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
from slife.plugins.memdb.search import SCORE_BAND_HINT, annotate_scores
from slife.plugins.memdb.semantic import SemanticManager
from slife.plugins.memfiles.store import MemfilesStore, _slugify, _unique_path
from slife.server_utils import (
    create_plugin_server,
    run_plugin_server,
    warm_after_handshake,
)

# Hard cap on url_save downloads — a multi-GB public URL must not OOM the
# plugin process by buffering the whole body.
_MAX_SAVE_BYTES = 50 * 1024 * 1024  # 50 MB


@asynccontextmanager
async def _memfiles_lifespan(_app):
    """Establish the store, then serve.  Public sharing lives in the
    separate ``sharefile`` plugin, so no tunnel lifecycle here.

    Readiness (MCP plugin contract): the store must be able to serve before
    the server answers the harness's ``initialize`` — a store that can't (no
    connection, missing schema, failing query) is fatal to startup.  It
    raises here, so the lifespan fails, the port signal never fires, and the
    harness reports the plugin FAILED instead of serving broken.
    """
    await _ensure_store_ready()
    try:
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


async def _ensure_store_ready() -> None:
    """Establish the plugin's serving capacity (the cabinet/schedule store).

    The readiness requirement encoded in initialization: the store can serve
    — connection open, schema in place, a query succeeds.  A failure here is
    fatal (the lifespan raises, so ``initialize`` never completes); the
    harness's watchdog retries the whole process instead.
    """
    try:
        store = await _ensure_store()
        if store is None:
            raise RuntimeError("store not initialized")
        async with store._c.execute("SELECT 1") as cur:
            await cur.fetchone()
    except Exception as e:
        logger.error("store_unusable_at_startup err=%s", e)
        raise


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
        "separate sharefile plugin's share_file explicitly. "
        "The plugin's health is probed by the harness through the internal "
        "__check tool — never called by the LLM directly."
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
    return _store


# Mirror memdb: warm the semantic manager only AFTER the first tools/list
# completed the wrapper handshake — the llama_cpp model load holds the GIL
# and would otherwise freeze the lifespan startup path (port signal never
# fires).  Handshake-first keeps startup readiness intact; a slow or failed
# load stays a warning, never a startup gate.
async def _warm_semantic() -> None:
    manager = _manager
    if manager is None:
        return
    await manager.start()


warm_after_handshake(mcp, _warm_semantic, name="semantic")


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

    if semantic_available and hits:
        annotate_scores(hits)
        hint = SCORE_BAND_HINT if not hint else f"{hint} · {SCORE_BAND_HINT}"

    return json.dumps(
        {
            "mode": "hybrid" if semantic_available else "fts5",
            "query": query, "kind": kind, "results": hits, "hint": hint,
        },
        ensure_ascii=False, indent=2,
    )


@mcp.tool(
    name="memfiles_semantic_status",
    description=(
        "File-cabinet semantic-search status: shared embedding config plus "
        "the memfiles index's own gate (semantic_ready, state, unembedded). "
        "Independent from memdb_semantic_status — the two plugins reindex "
        "their own DBs, so one can be semantically ready while the other is "
        "still building."
    ),
)
async def memfiles_semantic_status() -> str:
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
                report["model"] = e._model
                report["dimension"] = e.dimension
                report["available"] = e.available
                report["loaded"] = e.loaded
            # hint by state
            state = manager.state
            if state == "ready":
                report["hint"] = (
                    f"Embedding model ready: "
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
                    "Memfiles semantic search disabled. Re-enable with "
                    "embeddings_enable true (shared embedding config)."
                )
        return json.dumps(report, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("memfiles_check_embedding_failed")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


@mcp.tool(
    name="__check",
    description=(
        "Internal — file-cabinet status as JSON, probed by the harness's "
        "system_health.  Never exposed to the LLM."
    ),
)
async def __check() -> str:
    """Return cabinet status for the harness's ``system_health`` check.

    Reports the store/index connection state and the semantic-search gate
    (semantic_ready, state, unembedded).  The harness probes this via the
    plugin's MCP client; the LLM never sees it (``__`` internal).
    """
    try:
        await _ensure_store()
    except Exception as e:
        return json.dumps(
            {"ok": False, "state": "store_error", "hint": f"store init failed: {e}"},
            ensure_ascii=False,
        )
    store = _store
    manager = _manager
    result = {
        "ok": store is not None,
        "connected": store is not None,
        "state": manager.state if manager is not None else "no_manager",
        "semantic_ready": bool(manager is not None and manager.semantic_ready),
        "unembedded": await manager.unembedded() if manager is not None else 0,
        "reason": manager.reason if manager is not None else "",
    }
    if manager is None:
        result["hint"] = "Cabinet plugin loaded, but no semantic index manager."
    elif not manager.semantic_ready:
        result["hint"] = (
            f"Cabinet semantic index not ready — {result['unembedded']} "
            "notes/diary/files pending embedding. Keyword search remains available."
        )
    else:
        result["hint"] = "Cabinet connected; semantic index ready."
    return json.dumps(result, ensure_ascii=False)


@mcp.tool(
    name="__memfiles_reload_semantic",
    description="Reload the semantic index after an embeddings config change. Internal — called by the harness.",
)
async def __memfiles_reload_semantic(enabled: bool = True) -> str:
    """Re-read the shared embeddings config and rebuild (or tear down) the
    file-cabinet semantic index.  ``enabled=True`` → ``SemanticManager.enable()``
    (stops the drainer, migrates vec0 in place, restarts the drainer);
    ``False`` → ``disable()``.  Called by the harness's ``embeddings_*``
    native tools after a config change."""
    try:
        await _ensure_store()
        manager = _manager
        assert manager is not None
        if enabled:
            status = await manager.enable()
            status["status"] = "reloaded"
            status["message"] = "Cabinet semantic index reloaded (reindexing in background)."
        else:
            status = await manager.disable()
            status["status"] = "disabled"
        return json.dumps(status, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.exception("memfiles_reload_semantic_failed enabled=%s", enabled)
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


# ── Scheduled tasks & reports ───────────────────────────────────────


async def _task_id_by_name(name: str) -> int | None:
    """Resolve a scheduled-task name to its id, or None if absent."""
    store = await _ensure_store()
    task = await store.get_scheduled_task(name)
    return task["id"] if task else None


# Internal (``__``) tools — used by the main process's schedule loop, never
# exposed to the LLM (filtered by is_internal_tool).


@mcp.tool(
    name="__scheduled_tasks_state",
    description="Internal: enabled scheduled tasks with their last run anchor.",
)
async def __scheduled_tasks_state() -> str:
    """Return enabled tasks, each with its newest run ``due_at`` (anchor)
    and whether it currently has a ``pending`` run."""
    store = await _ensure_store()
    tasks = await store.list_scheduled_tasks(enabled_only=True)
    pending_ids = await store.pending_run_task_ids()
    out = []
    for t in tasks:
        t = dict(t)
        t["last_run_due"] = await store.last_run_due(t["id"])
        t["has_pending_run"] = t["id"] in pending_ids
        out.append(t)
    return json.dumps(out, ensure_ascii=False)


@mcp.tool(
    name="__scheduled_task_by_name",
    description="Internal: return one scheduled task by name (any enabled state).",
)
async def __scheduled_task_by_name(name: str) -> str:
    store = await _ensure_store()
    task = await store.get_scheduled_task(name)
    if task is None:
        return "null"
    task = dict(task)
    task["last_run_due"] = await store.last_run_due(task["id"])
    return json.dumps(task, ensure_ascii=False)


@mcp.tool(
    name="__scheduled_record_run",
    description="Internal: record a scheduled run (status=pending) for a task.",
)
async def __scheduled_record_run(task_id: int, due_at: str) -> str:
    store = await _ensure_store()
    info = await store.record_scheduled_run(task_id, due_at, status="pending")
    return json.dumps(info, ensure_ascii=False)


@mcp.tool(
    name="__scheduled_mark_missed",
    description="Internal: mark a due-but-not-fired run as missed.",
)
async def __scheduled_mark_missed(task_id: int, due_at: str) -> str:
    store = await _ensure_store()
    await store.mark_run_missed(task_id, due_at)
    return json.dumps({"task_id": task_id, "due_at": due_at, "status": "missed"},
                      ensure_ascii=False)


@mcp.tool(
    name="__scheduled_fail_unconfirmed",
    description=(
        "Internal: startup sweep — mark dispatched-but-unconfirmed runs of a "
        "previous process lifetime as failed.  Returns the runs that flipped."
    ),
)
async def __scheduled_fail_unconfirmed() -> str:
    store = await _ensure_store()
    stale = await store.fail_unconfirmed_runs()
    return json.dumps({"failed": len(stale), "runs": stale},
                      ensure_ascii=False)


@mcp.tool(
    name="__scheduled_mark_run_failed",
    description=(
        "Internal: best-effort — mark a pending run failed (its work never "
        "reached a report).  Errors are detail only; pending without a report "
        "is already treated as failed."
    ),
)
async def __scheduled_mark_run_failed(
    task_id: int, due_at: str, error: str = "",
) -> str:
    store = await _ensure_store()
    await store.mark_run_failed(task_id, due_at, error)
    return "{}"


# Data-layer tools for the schedule registry.  The LLM-visible ``scheduled_*``
# tools are native (``slife/tools/schedule.py``, category "Schedule") and reach
# these over the memfiles MCP client — the main process never touches the
# plugin's SQLite directly.  Pure validation (safe task-name regex, cron
# expression check) lives on the native side, not here.


@mcp.tool(
    name="__scheduled_task_upsert",
    description="Internal: create or update a scheduled task (idempotent upsert by name).",
)
async def __scheduled_task_upsert(
    name: str, description: str = "", schedule: str = "",
    timezone: str = "", enabled: bool = True,
) -> str:
    store = await _ensure_store()
    try:
        info = await store.upsert_scheduled_task(
            name=name.strip(), description=description, schedule=schedule,
            timezone=timezone, enabled=enabled,
        )
    except Exception as e:
        return f"Error: {e}"
    return json.dumps(
        {"name": info["name"], "task_id": info["task_id"],
         "schedule": schedule, "enabled": enabled},
        ensure_ascii=False,
    )


@mcp.tool(
    name="__scheduled_task_remove",
    description="Internal: delete a scheduled task and its run history by name (reports kept).",
)
async def __scheduled_task_remove(name: str) -> str:
    if not name.strip():
        return "Error: name is required."
    store = await _ensure_store()
    removed = await store.remove_scheduled_task(name.strip())
    if not removed:
        return f"Scheduled task not found: {name}"
    return f"Scheduled task '{name}' removed (its run history was cleared; reports are kept)."


@mcp.tool(
    name="__scheduled_tasks_list",
    description="Internal: list scheduled tasks (all rows, or enabled only — no run anchors).",
)
async def __scheduled_tasks_list(enabled_only: bool = False) -> str:
    store = await _ensure_store()
    tasks = await store.list_scheduled_tasks(enabled_only=enabled_only)
    return json.dumps({"total": len(tasks), "tasks": tasks},
                      ensure_ascii=False)


@mcp.tool(
    name="__scheduled_runs_list",
    description="Internal: list scheduled-run records, newest first (task name / status filters).",
)
async def __scheduled_runs_list(
    name: str = "", status: str = "", limit: int = 50,
) -> str:
    store = await _ensure_store()
    task_id = None
    if name.strip():
        task_id = await _task_id_by_name(name.strip())
        if task_id is None:
            return f"Scheduled task not found: {name}"
    runs = await store.list_scheduled_runs(
        task_id=task_id, status=status or None, limit=limit,
    )
    return json.dumps({"total": len(runs), "runs": runs},
                      ensure_ascii=False)


@mcp.tool(
    name="__scheduled_run_skip",
    description="Internal: mark a missed or failed run as skipped (only those two statuses change).",
)
async def __scheduled_run_skip(name: str, due_at: str) -> str:
    task_id = await _task_id_by_name(name.strip())
    if task_id is None:
        return f"Scheduled task not found: {name}"
    store = await _ensure_store()
    await store.mark_run_skipped(task_id, due_at)
    return f"Run '{name}' @ {due_at} marked skipped."


@mcp.tool(
    name="report_save",
    description=(
        "Save a report into the cabinet (reports/<slug>.md).  Reports are a "
        "general document type here (like notes and diary); the optional "
        "'name' binds the report to a scheduled task and confirms the run it "
        "was dispatched for (pending → ran), which is how a schedule worker "
        "completes its task.  Pass due_at (from the schedule.j2 task text) to "
        "confirm an exact run, e.g. a backfilled missed/failed run; omit to "
        "confirm the newest un-reported run.  Returns the report's file path."
    ),
)
async def report_save(
    name: str = "", title: str = "", content: str = "", tags: str = "",
    period_start: str | None = None, period_end: str | None = None,
    due_at: str | None = None,
) -> str:
    """Save a report (bound to a scheduled task when name is given).

    Args:
        name: Optional scheduled task's name (from scheduled_task_set).  Given
            and known: the report is bound to the task and confirms the run it
            was dispatched for.  Empty: saves a standalone report with no run
            to confirm.
        title: Report title — also its filename (reports/<title>.md).
        content: The report body (Markdown).
        tags: Optional comma-separated tags for search.
        period_start: Optional ISO start of the period the report covers.
        period_end: Optional ISO end of the period the report covers.
        due_at: Optional ISO due time of the exact run to confirm (from the
            worker's task text).  Backfills pass it so the missed/failed run
            they transitioned is the one confirmed.
    """
    store = await _ensure_store()
    task_id = None
    if name.strip():
        task_id = await _task_id_by_name(name.strip())
        if task_id is None:
            return f"Error: scheduled task not found — {name}"
    else:
        # Standalone report (no scheduled task).  Databases created before
        # reports.task_id became nullable keep NOT NULL and are never migrated
        # (schema changes only affect new DBs) — reject clearly BEFORE any md
        # write so no orphan file appears next to the error.
        cursor = await store._c.execute("PRAGMA table_info(reports)")
        cols = await cursor.fetchall()
        if any(c["name"] == "task_id" and c["notnull"] for c in cols):
            return (
                "Error: this cabinet's database predates standalone reports "
                "(reports.task_id is NOT NULL) — pass name=<task> to bind the "
                "report to a scheduled task."
            )
    try:
        info = await store.upsert_report(
            task_id=task_id, title=title, content=content, tags=tags,
            period_start=period_start, period_end=period_end,
            due_at=(due_at if task_id is not None else None),
        )
    except ValueError as e:
        return f"Error: {e}"
    return _saved_result(store.mem_dir / info["file_path"])


@mcp.tool(
    name="report_list",
    description=(
        "List reports (newest first) with title, task_id, period and "
        "file_path.  Optionally filter by task name.  Use "
        "report_read(report_id) for full content."
    ),
)
async def report_list(name: str = "", limit: int = 50, offset: int = 0) -> str:
    """List reports.

    Args:
        name: Optional task name to filter on (empty = all tasks).
        limit: Maximum entries to return.
        offset: Skip this many entries (for paging).
    """
    store = await _ensure_store()
    task_id = None
    if name.strip():
        task_id = await _task_id_by_name(name.strip())
        if task_id is None:
            return f"Scheduled task not found: {name}"
    data = await store.list_reports(task_id=task_id, limit=limit, offset=offset)
    return json.dumps(
        {"total": data["total"], "limit": len(data["entries"]),
         "offset": max(0, offset), "entries": data["entries"]},
        ensure_ascii=False, indent=2,
    )


@mcp.tool(
    name="report_read",
    description="Read a scheduled-task report's full content by its report_id.",
)
async def report_read(report_id: int) -> str:
    """Read a report's full content.

    Args:
        report_id: The report's id (from report_list).
    """
    store = await _ensure_store()
    report = await store.get_report(report_id)
    if report is None:
        return f"Error: report not found — {report_id}"
    return report["content"]


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
