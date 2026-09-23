"""Tests for the memfiles plugin — the private notes / diary / files cabinet.

The public file-sharing functionality (token registry, ``share_file``, the
ngrok tunnel, the ``GET /share/{file_id}`` HTTP route) moved out of memfiles
into the standalone ``sharefile`` plugin and is covered by
``test_sharefile_plugin.py`` / ``test_sharefile_tunnel.py``.  Memfiles is
now cabinet-only: every save tool returns the local path and never
auto-publishes, so there is no sharing, no token registry, and no tunnel
to mock here.

This module exercises the MCP tool functions directly (following the
test_mqtt_plugin.py pattern) with a mocked store.  Store internals (md
mirroring, hybrid search, the SemanticManager contract) are covered against
a real temp DB in ``TestMemfilesStore``.
"""

import pytest; pytestmark = pytest.mark.unit


import asyncio
import json
import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

import slife.plugins.memfiles.server as plugin
from slife.plugins.memfiles.store import MemfilesStore
from slife.timeutil import InvalidTimeBound


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state():
    """Reset store/manager globals each test (no registry, no tunnel here)."""
    plugin._store = None
    plugin._manager = None
    plugin._db_path = None
    plugin._init_lock = None
    yield
    plugin._store = None
    plugin._manager = None


def _fake_store(mem_dir: Path) -> MagicMock:
    """A store stand-in: mem_dir real, writes/searches mocked."""
    store = AsyncMock()
    store.mem_dir = mem_dir
    store.upsert_note = AsyncMock(return_value={
        "kind": "note", "doc_id": 1, "key": "subj",
        "file_path": "notes/subj.md", "content": "# subj\n\nbody"})
    store.upsert_diary = AsyncMock(return_value={
        "kind": "diary", "doc_id": 2, "key": "2026-08-15",
        "file_path": "diary/2026-08-15.md", "content": "# 2026-08-15\n\nbody"})
    store.add_file = AsyncMock(return_value={
        "kind": "file", "doc_id": 3, "key": "a.txt", "file_path": "a.txt"})
    store.search = AsyncMock(return_value=[
        {"id": "note:1", "file_path": "notes/subj.md",
         "snippet": "…", "rrf_score": 0.01}])
    store.note_list = AsyncMock(return_value={
        "entries": [
            {"id": 1, "subject": "subj", "tags": "", "file_path": "notes/subj.md",
             "created_at": "2026-01-01", "updated_at": "2026-01-02"},
        ],
        "total": 1,
    })
    store.diary_list = AsyncMock(return_value={
        "entries": [
            {"id": 2, "date": "2026-08-15", "tags": "", "file_path": "diary/2026-08-15.md",
             "created_at": "2026-08-15", "updated_at": "2026-08-15"},
        ],
        "total": 1,
    })
    store.get_note = AsyncMock(return_value={
        "id": 1, "subject": "subj", "content": "# subj\n\nbody", "tags": "",
        "file_path": "notes/subj.md", "created_at": "2026-01-01", "updated_at": "2026-01-02"})
    store.get_diary = AsyncMock(return_value={
        "id": 2, "date": "2026-08-15", "content": "# 2026-08-15\n\nbody", "tags": "",
        "file_path": "diary/2026-08-15.md", "created_at": "2026-08-15", "updated_at": "2026-08-15"})
    store.file_list = AsyncMock(return_value={
        "entries": [
            {"id": 3, "title": "a.txt", "saved_path": "files/documents/a.txt",
             "category": "documents", "mime": "text/plain", "size": 3,
             "tags": "", "summary": "", "created_at": "2026-08-15"},
        ],
        "total": 1,
    })
    store.resolve_safe_path = MagicMock(return_value=mem_dir / "notes" / "subj.md")
    return store


# ═══════════════════════════════════════════════════════════════════════
# Filename helpers (from store)
# ═══════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_slugify(self):
        assert plugin._slugify("Project Notes 2026!") == "project-notes-2026"
        assert plugin._slugify("--hello--") == "hello"

    def test_unique_path_no_conflict(self, tmp_path):
        assert plugin._unique_path(tmp_path, "notes", ".md") == tmp_path / "notes.md"

    def test_unique_path_conflict(self, tmp_path):
        (tmp_path / "notes.md").write_text("x")
        assert plugin._unique_path(tmp_path, "notes", ".md") == tmp_path / "notes_1.md"


# ═══════════════════════════════════════════════════════════════════════
# note_save / diary_save / file_save / url_save
# ═══════════════════════════════════════════════════════════════════════


class TestNoteSave:
    @pytest.mark.asyncio
    async def test_saves_and_returns_local_path(self, tmp_path):
        """A note is private — the result names the local md file, with no
        share URL and no file registered for public sharing."""
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            result = await plugin.note_save(subject="Python", content="asyncio notes",
                                            tags="py")
        assert "Saved:" in result
        assert result.rstrip().replace("\\", "/").endswith("notes/subj.md")
        assert "URL:" not in result
        store.upsert_note.assert_awaited_once_with("Python", "asyncio notes", "py")

    @pytest.mark.asyncio
    async def test_wakes_drainer(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        manager = MagicMock()
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            await plugin.note_save(subject="s", content="c")
        manager.on_saved.assert_called_once()


class TestDiaryWrite:
    @pytest.mark.asyncio
    async def test_defaults_to_today(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.diary_save(content="today's entry")
        store.upsert_diary.assert_awaited_once()
        call_date = store.upsert_diary.await_args.args[0]
        assert call_date == "2026-08-15" or len(call_date.split("-")) == 3

    @pytest.mark.asyncio
    async def test_explicit_date(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.diary_save(date="2026-08-10", content="x")
        store.upsert_diary.assert_awaited_once_with("2026-08-10", "x", "")

    @pytest.mark.asyncio
    async def test_returns_local_path_not_share_url(self, tmp_path):
        """A diary is private — the result names the local md file, with no
        share URL and no file registered for public sharing."""
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            result = await plugin.diary_save(date="2026-08-10", content="x")
        assert "Saved:" in result
        assert result.rstrip().replace("\\", "/").endswith("diary/2026-08-15.md")
        assert "URL:" not in result


class TestFileSave:
    @pytest.mark.asyncio
    async def test_saves_multiple_files(self, tmp_path):
        a = tmp_path / "a.txt"
        a.write_text("aaa")
        b = tmp_path / "b.txt"
        b.write_text("bbb")
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            result = await plugin.file_save(paths=[str(a), str(b)])
        assert "Saved:" in result
        assert store.add_file.await_count == 2
        # .txt → documents category
        assert (mem_dir / "files" / "documents" / "a.txt").read_text() == "aaa"
        assert (mem_dir / "files" / "documents" / "b.txt").read_text() == "bbb"

    @pytest.mark.asyncio
    async def test_auto_categories_by_extension(self, tmp_path):
        png = tmp_path / "shot.png"
        png.write_bytes(b"png")
        pdf = tmp_path / "report.pdf"
        pdf.write_bytes(b"pdf")
        unknown = tmp_path / "archive.xyz"
        unknown.write_bytes(b"xyz")
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.file_save(paths=[str(png), str(pdf), str(unknown)])
        assert (mem_dir / "files" / "images" / "shot.png").is_file()
        assert (mem_dir / "files" / "documents" / "report.pdf").is_file()
        assert (mem_dir / "files" / "other" / "archive.xyz").is_file()

    @pytest.mark.asyncio
    async def test_category_override(self, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_bytes(b"pdf")
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.file_save(paths=[str(f)], category="books")
        assert (mem_dir / "files" / "books" / "report.pdf").is_file()

    @pytest.mark.asyncio
    async def test_missing_file_reports_error(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            result = await plugin.file_save(paths=["D:\\nope\\x.pdf"])
        assert "Error: file not found" in result

    @pytest.mark.asyncio
    async def test_summary_wakes_drainer(self, tmp_path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"pdf")
        store = _fake_store(tmp_path / "files")
        manager = MagicMock()
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            await plugin.file_save(paths=[str(f)], summary="a doc about pdfs")
        manager.on_saved.assert_called_once()


class TestUrlSave:
    @pytest.mark.asyncio
    async def test_downloads_and_records(self, tmp_path):
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store = _fake_store(mem_dir)

        class _Content:
            def __init__(self, data):
                self._data = data

            async def iter_chunked(self, chunk_size):
                if self._data:
                    yield self._data

        class _Resp:
            status = 200
            @property
            def content(self):
                return _Content(b"<html>Page</html>")
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def read(self): return b"<html>Page</html>"

        def _fake_get(url, timeout=None, **kwargs):
            # sync: url_save does ``async with session.get(...)``
            return _Resp()

        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch("aiohttp.ClientSession") as sess_cls:
            sess = MagicMock()
            sess.get.side_effect = _fake_get
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__ = AsyncMock(return_value=None)
            sess_cls.return_value = sess
            result = await plugin.url_save(url="https://8.8.8.8/page.html")
        assert "Saved:" in result
        store.add_file.assert_awaited_once()
        assert store.add_file.await_args.kwargs["original_path"] == "https://8.8.8.8/page.html"

    @pytest.mark.asyncio
    async def test_refuses_non_public(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            result = await plugin.url_save(url="http://169.254.169.254/latest/meta-data/")
        assert result.startswith("Error: refusing URL")
        store.add_file.assert_not_awaited()

    def test_name_is_derived_from_the_url_not_invented(self):
        """The file name is built from what the URL actually says.

        Two ways that used to go wrong: a root or directory URL has no
        basename and was filed as the literal "untitled"; and a URL *with* an
        extension had the whole basename slugified — which drops the dot — and
        then had the extension appended again, so "paper.pdf" landed as
        "paperpdf.pdf".

        Tested through the pure derivation rather than a live ``url_save``:
        the real path runs an SSRF guard that resolves the host via
        ``socket.getaddrinfo``, so driving it with invented hostnames makes
        the assertion depend on the CI machine's DNS.  The wiring from these
        parts to the written file is covered by ``test_downloads_and_records``.
        """
        for url, title, expected in (
            # no basename: fall back to the path segment, else the host
            ("https://example.com/", "", "example-com"),
            ("https://example.com/docs/", "", "docs"),
            # a host that reads like a filename must not be taken for one —
            # dots become dashes, keeping it out of the archive category
            ("https://example.zip/", "", "example-zip"),
            # an extension appears exactly once
            ("https://example.com/paper.pdf", "", "paper.pdf"),
            ("https://example.com/a/report.html", "", "report.html"),
            # a title supplies the STEM and the URL still supplies the
            # extension — the same division file_save makes between a title
            # and src.suffix
            ("https://example.com/dl", "My Paper", "my-paper"),
            ("https://example.com/other.pdf", "My Paper", "my-paper.pdf"),
        ):
            _, _, stem, ext = plugin._url_name_parts(urlparse(url), title)
            assert stem + ext == expected, (url, title)
            assert "untitled" not in stem, url


# ═══════════════════════════════════════════════════════════════════════
# cabinet_search / file_read
# ═══════════════════════════════════════════════════════════════════════


class TestCabinetSearch:
    @pytest.mark.asyncio
    async def test_fts5(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_search(query="python", kind="note", mode="fts5")
        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert data["results"][0]["id"] == "note:1"
        store.search.assert_awaited_once()
        assert store.search.await_args.kwargs["embed_query"] is None

    @pytest.mark.asyncio
    async def test_kind_report_is_honoured_not_rewritten_to_all(self, tmp_path):
        """`kind="report"` used to be silently rewritten to `"all"`: the server's
        kind list predates reports joining `_KIND_SPECS`, so asking for reports
        got every kind instead — while `report_save` / `report_list` /
        `report_read` already treated reports as first-class, and `search()`'s
        own kind map accepted `"report"`.  An unknown kind still falls back."""
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.cabinet_search(query="weekly", kind="report")
        assert store.search.await_args.kwargs["kind"] == "report"

        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.cabinet_search(query="weekly", kind="nonsense")
        assert store.search.await_args.kwargs["kind"] == "all"

    @pytest.mark.asyncio
    async def test_empty_query_is_rejected_in_every_mode(self, tmp_path):
        """An empty query is not a search, and it must not be one.

        `mode="grep"` used to compile the empty pattern — which matches EVERY
        string — so an empty query silently returned the whole window: no
        `total`, no paging, invisible in every description.  That was an
        accidental second browse path, one unrelated "reject empty patterns"
        fix away from vanishing without a trace."""
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            for mode in ("hybrid", "fts5", "grep"):
                for q in ("", "   "):
                    data = json.loads(
                        await plugin.cabinet_search(query=q, mode=mode)
                    )
                    assert "must not be empty" in data["error"], (mode, q)
                    # And it names where browsing actually lives.
                    assert "note_list" in data["error"]
        store.search.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hybrid_without_manager_degrades_to_fts5(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_search(query="python", mode="hybrid")
        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert "hybrid degraded" in data["hint"]

    @pytest.mark.asyncio
    async def test_grep_reports_grep_not_fts5(self, tmp_path):
        """The envelope reports the mode that RAN.

        Derived from `semantic_available` alone it answered "fts5" for a grep
        request — so a caller reading an empty result concluded the keyword
        search had found nothing, when the regex was what ran.  A ready
        manager must not change the answer either: grep never consults it.
        """
        store = _fake_store(tmp_path / "files")
        manager = MagicMock()
        manager.semantic_ready = True
        embedder = MagicMock()
        embedder.available = True
        embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
        manager.embedder = embedder
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            out = await plugin.cabinet_search(query="depl.y", mode="grep")
        data = json.loads(out)
        assert data["mode"] == "grep"
        # The mode reached the store, so the search really was a regex…
        assert store.search.await_args.kwargs["mode"] == "grep"
        # …and it never touched the embedding path.
        assert store.search.await_args.kwargs["embed_query"] is None
        embedder.embed_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fts5_is_not_reported_as_hybrid(self, tmp_path):
        """The mirror case: an explicit fts5 request with a ready manager is
        fts5, not the hybrid the manager could have served."""
        store = _fake_store(tmp_path / "files")
        manager = MagicMock()
        manager.semantic_ready = True
        embedder = MagicMock()
        embedder.available = True
        embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
        manager.embedder = embedder
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            out = await plugin.cabinet_search(query="python", mode="fts5")
        assert json.loads(out)["mode"] == "fts5"
        embedder.embed_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_hybrid_with_ready_manager_embeds_query(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        manager = MagicMock()
        manager.semantic_ready = True
        embedder = MagicMock()
        embedder.available = True
        embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
        manager.embedder = embedder
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            out = await plugin.cabinet_search(query="python", mode="hybrid")
        data = json.loads(out)
        assert data["mode"] == "hybrid"
        assert store.search.await_args.kwargs["embed_query"] == [0.1, 0.2]


class TestCabinetRead:
    @pytest.mark.asyncio
    async def test_reads_file(self, tmp_path):
        mem_dir = tmp_path / "files"
        (mem_dir / "notes").mkdir(parents=True)
        (mem_dir / "notes" / "subj.md").write_text("# content", encoding="utf-8")
        store = _fake_store(mem_dir)
        store.resolve_safe_path = MagicMock(return_value=mem_dir / "notes" / "subj.md")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.file_read("notes/subj.md")
        assert out == "# content"

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        store.resolve_safe_path = MagicMock(return_value=tmp_path / "files" / "none.md")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.file_read("none.md")
        assert out.startswith("Error: not a file")


class TestNoteDiaryBrowse:
    """note_list / diary_list / note_read / diary_read — browse by key."""

    @pytest.mark.asyncio
    async def test_note_list(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.note_list()
        data = json.loads(out)
        assert data["total"] == 1
        assert data["entries"][0]["subject"] == "subj"
        assert data["entries"][0]["file_path"] == "notes/subj.md"
        store.note_list.assert_awaited_once_with(
            since=None, until=None, limit=50, offset=0,
        )

    @pytest.mark.asyncio
    async def test_diary_list_range(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.diary_list(since="2026-08-01", until="2026-08-31")
        data = json.loads(out)
        assert data["entries"][0]["date"] == "2026-08-15"
        store.diary_list.assert_awaited_once_with(
            since="2026-08-01", until="2026-08-31", limit=50, offset=0,
        )

    @pytest.mark.asyncio
    async def test_note_read(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.note_read(subject="subj")
        assert out == "# subj\n\nbody"

    @pytest.mark.asyncio
    async def test_note_read_missing(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        store.get_note = AsyncMock(return_value=None)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.note_read(subject="nope")
        assert out.startswith("Error: note not found")

    @pytest.mark.asyncio
    async def test_diary_read(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.diary_read(date="2026-08-15")
        assert out == "# 2026-08-15\n\nbody"

    @pytest.mark.asyncio
    async def test_diary_read_missing(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        store.get_diary = AsyncMock(return_value=None)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.diary_read(date="2020-01-01")
        assert out.startswith("Error: diary not found")

    @pytest.mark.asyncio
    async def test_file_list(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.file_list(category="documents")
        data = json.loads(out)
        assert data["total"] == 1
        assert data["entries"][0]["category"] == "documents"
        assert data["entries"][0]["saved_path"] == "files/documents/a.txt"
        store.file_list.assert_awaited_once_with(
            category="documents", since=None, until=None, limit=50, offset=0,
        )


class TestCabinetStatus:
    """__check — the internal tool the harness's check_memfiles probes."""

    @pytest.mark.asyncio
    async def test_store_error_reports_failure(self, tmp_path):
        with patch.object(plugin, "_ensure_store",
                          AsyncMock(side_effect=RuntimeError("boom"))):
            out = await getattr(plugin, "__check")()
        data = json.loads(out)
        assert data["ok"] is False
        assert data["state"] == "store_error"
        assert "boom" in data["reason"]

    @pytest.mark.asyncio
    async def test_reports_semantic_index_state(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        manager = MagicMock()
        manager.semantic_ready = False
        manager.state = "indexing"
        manager.reason = "index building"
        manager.unembedded = AsyncMock(return_value=5)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_store", store), \
             patch.object(plugin, "_manager", manager):
            out = await getattr(plugin, "__check")()
        data = json.loads(out)
        assert data["ok"] is True
        assert data["connected"] is True
        assert data["semantic_ready"] is False
        assert data["state"] == "indexing"
        assert data["unembedded"] == 5

    @pytest.mark.asyncio
    async def test_no_manager(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_store", store), \
             patch.object(plugin, "_manager", None):
            out = await getattr(plugin, "__check")()
        data = json.loads(out)
        assert data["ok"] is True
        assert data["state"] == "no_manager"
        assert data["semantic_ready"] is False


# ── SSRF guard for url_save ───────────────────────────────────────────


class TestSaveUrlPublicGuard:
    """url_save only fetches publicly reachable http(s) URLs — loopback /
    LAN / cloud-metadata targets are refused (REVIEW §1-1)."""

    def test_allows_public_http(self):
        assert plugin._reject_non_public_url("http://8.8.8.8/x") is None
        assert plugin._reject_non_public_url("https://1.1.1.1") is None

    def test_allows_fake_ip_resolver_range(self):
        """Clash / sing-box fake-ip resolvers answer public hostnames from
        the RFC 2544 benchmarking range (198.18.0.0/15) — that's the proxy's
        front door, not LAN infra, and must not be refused."""
        assert plugin._reject_non_public_url("http://198.18.0.1/x") is None
        assert plugin._reject_non_public_url("http://198.18.255.9/") is None

    def test_allows_fake_ip_resolver_range_v6(self):
        """sing-box's default IPv6 fake-ip pool (fdfe:dcba:9876::/48) is ULA
        — Python flags it private, but a fake-ip resolver answering public
        hostnames from it is the proxy's front door, not LAN infra."""
        assert plugin._reject_non_public_url(
            "http://[fdfe:dcba:9876::1]/x"
        ) is None
        assert plugin._reject_non_public_url(
            "http://[fdfe:dcba:9876:ffff::2]/x"
        ) is None

    def test_rejects_ula_outside_the_fake_ip_pool(self):
        """Only the documented fake-ip prefixes are exempt — any other ULA
        (fc00::/7) answer is still refused."""
        assert plugin._reject_non_public_url("http://[fdfe::1]/x")
        assert plugin._reject_non_public_url("http://[fd12:3456::1]/x")

    def test_rejects_non_http_schemes(self):
        assert plugin._reject_non_public_url("ftp://example.com/x")
        assert plugin._reject_non_public_url("file:///etc/passwd")
        assert plugin._reject_non_public_url("gopher://localhost/1")

    def test_rejects_loopback(self):
        assert plugin._reject_non_public_url("http://127.0.0.1:8080/admin")
        assert plugin._reject_non_public_url("http://[::1]/x")
        assert plugin._reject_non_public_url("http://localhost/admin")

    def test_rejects_cloud_metadata(self):
        assert plugin._reject_non_public_url(
            "http://169.254.169.254/latest/meta-data/"
        )

    def test_rejects_private_and_link_local(self):
        assert plugin._reject_non_public_url("http://10.0.0.1/")
        assert plugin._reject_non_public_url("http://192.168.1.1/")
        assert plugin._reject_non_public_url("http://172.16.0.1/")


# ═══════════════════════════════════════════════════════════════════════
# MemfilesStore — real temp DB: md mirroring, contract, hybrid search
# ═══════════════════════════════════════════════════════════════════════


async def _real_store(tmp_path, dim: int = 4, require_vec: bool = False) -> MemfilesStore:
    store = MemfilesStore(tmp_path / ".index.db")
    await store.setup(embedding_dim=dim, embedding_model="test:model")
    if require_vec and store._embedding_dim <= 0:
        # sqlite-vec can't load on this platform (e.g. macOS Python built
        # without enable_load_extension) — the vec0 tables are skipped, so
        # semantic tests can't run.
        await store.close()
        pytest.skip("sqlite-vec unavailable on this platform (vec_dim=0)")
    return store


class TestMemfilesStore:
    @pytest.mark.asyncio
    async def test_upsert_note_appends_and_mirrors_md(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            first = await store.upsert_note("Python", "asyncio basics", "py")
            second = await store.upsert_note("Python", "more on await", "py")
            # Same subject → same doc_id, md appended with a timestamped section
            assert first["doc_id"] == second["doc_id"]
            md = (tmp_path / "notes" / "python.md").read_text(encoding="utf-8")
            assert "asyncio basics" in md and "more on await" in md
            assert "##" in md  # appended section header
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_concurrent_same_subject_upserts_serialized(self, tmp_path):
        """D1 regression: concurrent upsert_note for the SAME subject (main
        agent + subagent sharing the plugin) must not race — both seeing
        'no row' would hit notes.subject UNIQUE, and the md read-append-
        rewrite would drop one writer's section.  The write lock serializes."""
        store = await _real_store(tmp_path)
        try:
            n = 8
            results = await asyncio.gather(*[
                store.upsert_note("same", f"payload {i}", "t")
                for i in range(n)
            ])
            assert len({r["doc_id"] for r in results}) == 1  # one row
            md = (tmp_path / "notes" / "same.md").read_text(encoding="utf-8")
            for i in range(n):
                assert f"payload {i}" in md
            cur = await store._c.execute(
                "SELECT COUNT(*) FROM notes WHERE subject='same'",
            )
            row = await cur.fetchone()
            assert row[0] == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_slug_collisions_do_not_bleed_notes(self, tmp_path):
        """D2 regression: distinct subjects whose slugs collide ("API Design"
        vs "API-Design" → both "api-design") must own DISTINCT files/rows —
        the second upsert must not store the merged first file, and updating
        the first must not re-read the second's content."""
        store = await _real_store(tmp_path)
        try:
            a = await store.upsert_note("API Design", "first", "t")
            b = await store.upsert_note("API-Design", "second", "t")
            assert a["doc_id"] != b["doc_id"]
            assert a["file_path"] != b["file_path"]

            fa = (tmp_path / a["file_path"]).read_text(encoding="utf-8")
            fb = (tmp_path / b["file_path"]).read_text(encoding="utf-8")
            assert "first" in fa and "second" not in fa
            assert "second" in fb and "first" not in fb

            # Updating A re-reads only A's own file.
            a2 = await store.upsert_note("API Design", "third", "t")
            assert a2["file_path"] == a["file_path"]
            fa2 = (tmp_path / a2["file_path"]).read_text(encoding="utf-8")
            assert "third" in fa2 and "second" not in fa2
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_slug_collision_reports_stay_separate_rows(self, tmp_path):
        """D2: report rows whose titles slug-collide ("Daily Report" vs
        "Daily-Report") must not collapse into one row/file (which would
        mislink scheduled_runs.report_id across tasks)."""
        store = await _real_store(tmp_path)
        try:
            task = await store.upsert_scheduled_task("t1", schedule="0 0 * * *")
            r1 = await store.upsert_report(task["task_id"], "Daily Report", "one")
            r2 = await store.upsert_report(task["task_id"], "Daily-Report", "two")
            assert r1["doc_id"] != r2["doc_id"]
            assert r1["file_path"] != r2["file_path"]
            f1 = (tmp_path / r1["file_path"]).read_text(encoding="utf-8")
            f2 = (tmp_path / r2["file_path"]).read_text(encoding="utf-8")
            assert "one" in f1 and "two" not in f1
            assert "two" in f2 and "one" not in f2
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_upsert_diary_rejects_bad_date(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            with pytest.raises(ValueError):
                await store.upsert_diary("15/08/2026", "bad", "")
            await store.upsert_diary("2026-08-15", "good", "dev")
            assert (tmp_path / "diary" / "2026-08-15.md").is_file()
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_count_and_get_unembedded_docs(self, tmp_path):
        store = await _real_store(tmp_path, require_vec=True)
        try:
            await store.upsert_note("Python", "asyncio concurrency", "")
            await store.upsert_diary("2026-08-15", "shipped the refactor", "")
            await store.add_file(title="report", original_path="/x/r.pdf",
                                 saved_path="r.pdf", mime="pdf", size=1,
                                 tags="", summary="Q2 financial results")
            await store.add_file(title="memo", original_path="/x/m.txt",
                                 saved_path="m.txt", mime="text", size=2,
                                 tags="", summary="")
            # 3 embeddable docs: note + diary + file-with-summary (memo excluded)
            assert await store.count_unembedded() == 3
            docs = await store.get_unembedded_docs(10)
            kinds = {d["kind"] for d in docs}
            assert kinds == {"note", "diary", "file"}
            assert all(d["text"] for d in docs)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_replace_embedding_chunks_routes_by_kind(self, tmp_path):
        store = await _real_store(tmp_path, require_vec=True)
        try:
            note = await store.upsert_note("Python", "asyncio concurrency", "")
            doc = {"kind": "note", "doc_id": note["doc_id"],
                   "summary": "Python", "tags": "", "created_at": "2026-01-01"}
            await store.replace_embedding_chunks(doc, [[0.1, 0.2, 0.3, 0.4]])
            assert await store.count_unembedded() == 0
            # updated note is re-marked unembedded (stale vectors cleared)
            await store.upsert_note("Python", "more asyncio", "")
            assert await store.count_unembedded() == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_list_and_get_notes_diary(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("Python", "asyncio", "py")
            await store.upsert_note("Go", "goroutines", "")
            await store.upsert_diary("2026-08-15", "refactor day", "dev")
            await store.upsert_diary("2026-08-14", "setup day", "")

            notes = await store.note_list()
            # newest-updated first; timestamps share second precision in-tests,
            # so don't assert tie order
            assert {n["subject"] for n in notes["entries"]} == {"Python", "Go"}
            assert notes["total"] == 2
            assert "content" not in notes["entries"][0]  # lightweight

            days = await store.diary_list(since="2026-08-14", until="2026-08-15")
            assert [d["date"] for d in days["entries"]] == ["2026-08-15", "2026-08-14"]
            assert days["total"] == 2
            days = await store.diary_list(since="2026-08-15")
            assert [d["date"] for d in days["entries"]] == ["2026-08-15"]

            # paging: limit 1 of 2 → total tells the caller more remain
            paged = await store.diary_list(limit=1)
            assert len(paged["entries"]) == 1 and paged["total"] == 2
            rest = await store.diary_list(limit=1, offset=1)
            assert len(rest["entries"]) == 1 and rest["total"] == 2

            note = await store.get_note("Python")
            assert note is not None and "asyncio" in note["content"]
            assert await store.get_note("missing") is None
            day = await store.get_diary("2026-08-15")
            assert day is not None and "refactor" in day["content"]
            assert await store.get_diary("2020-01-01") is None
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_diary_list_range_grammar(self, tmp_path):
        """since/until follow the shared grammar: ISO datetimes and relative
        words are reduced to the date-only column."""
        from datetime import date as _date
        store = await _real_store(tmp_path)
        try:
            await store.upsert_diary("2026-08-14", "setup day", "")
            await store.upsert_diary("2026-08-15", "refactor day", "dev")
            today = _date.today().isoformat()
            await store.upsert_diary(today, "today's entry", "")

            # ISO datetime bounds reduce to their date part
            days = await store.diary_list(
                since="2026-08-14T00:00:00+08:00",
                until="2026-08-15T23:59:59Z",
            )
            assert [d["date"] for d in days["entries"]] == ["2026-08-15", "2026-08-14"]
            assert days["total"] == 2

            # relative words resolve against today's calendar
            todays = await store.diary_list(since="today")
            assert [d["date"] for d in todays["entries"]] == [today]

            # a date-only until stays on the day — no +1-day drift on a
            # date-only column
            day = await store.diary_list(until="2026-08-14")
            assert [d["date"] for d in day["entries"]] == ["2026-08-14"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_file_list(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            await store.add_file(title="a", original_path="/x/a",
                                 saved_path="files/documents/a.pdf",
                                 mime="pdf", size=1, tags="", summary="")
            await store.add_file(title="b", original_path="/x/b",
                                 saved_path="files/images/b.png",
                                 mime="png", size=2, tags="", summary="s")

            data = await store.file_list()
            assert data["total"] == 2
            assert {e["category"] for e in data["entries"]} == {"documents", "images"}

            docs = await store.file_list(category="documents")
            assert docs["total"] == 1
            assert docs["entries"][0]["category"] == "documents"
            assert docs["entries"][0]["saved_path"] == "files/documents/a.pdf"

            paged = await store.file_list(limit=1)
            assert len(paged["entries"]) == 1 and paged["total"] == 2

            # The window merges with the category filter, and `total` counts
            # their intersection rather than the whole table.
            assert (await store.file_list(category="documents"))["total"] == 1
            assert (await store.file_list(
                category="documents", until="2020-01-01"))["total"] == 0
            assert (await store.file_list(
                category="documents", since="2020-01-01"))["total"] == 1
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_diary_search_windows_on_date_not_created_at(self, tmp_path):
        """A diary's window is its ``date`` — the kind's axis — in the SEARCH
        legs too, not only in ``diary_list``.

        The entry is dated 2020 but written now, which is the only construction
        that separates the two candidate columns: windowed on ``date`` it falls
        inside "up to last month", windowed on ``created_at`` it does not.  That
        asymmetry is what had ``cabinet_search(kind="diary")`` and ``diary_list``
        answering "last month" from two different columns."""
        store = await _real_store(tmp_path)
        try:
            await store.upsert_diary("2020-01-15", "asyncio retro", "")

            early = await store.search("asyncio", kind="diary", mode="fts5",
                                       until="last month")
            assert [h["id"] for h in early] == ["diary:1"]
            assert (await store.diary_list(until="last month"))["total"] == 1

            # The same axis from the other side, in both entry points.
            assert await store.search("asyncio", kind="diary", mode="fts5",
                                      since="2020-06-01") == []
            assert (await store.diary_list(since="2020-06-01"))["total"] == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_report_list_window_is_production_time(self, tmp_path):
        """A report's axis is when it was PRODUCED.  ``period_start`` /
        ``period_end`` say what it COVERS — a different dimension, and nullable —
        so a window must not read them.

        The covering period here is years old while the report was produced just
        now: a window on ``period_start`` would have kept it."""
        store = await _real_store(tmp_path)
        try:
            await store.upsert_report(None, "Weekly", "body", "",
                                      period_start="2020-01-01",
                                      period_end="2020-01-31")
            assert (await store.report_list(since="last month"))["total"] == 1
            assert (await store.report_list(until="last month"))["total"] == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_note_list_windows_on_updated_at(self, tmp_path):
        """The list window lands on the column the list is ORDERED by — that is
        the rule `diary_list` already followed (ordered and windowed on `date`).

        Backdating one note's ``updated_at`` while leaving its ``created_at``
        alone is what separates the two candidate columns: a window on
        ``created_at`` would have kept both notes."""
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("Python", "asyncio", "py")
            await store.upsert_note("Go", "goroutines", "")
            assert (await store.note_list())["total"] == 2

            await store._c.execute(
                "UPDATE notes SET updated_at = '2020-06-01T00:00:00' "
                "WHERE subject = 'Go'",
            )
            await store._c.commit()

            recent = await store.note_list(since="2026-01-01")
            assert [n["subject"] for n in recent["entries"]] == ["Python"]
            assert recent["total"] == 1        # total counts the window
            old = await store.note_list(until="2026-01-01")
            assert [n["subject"] for n in old["entries"]] == ["Go"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_search_fts5_and_cjk_like(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("Python", "Everything about asyncio concurrency", "")
            hits = await store.search("asyncio", kind="note", mode="fts5")
            assert [h["id"] for h in hits] == ["note:1"]
            hits = await store.search("并发", kind="note", mode="fts5")  # CJK → LIKE
            assert hits == []  # no CJK content yet
            await store.upsert_note("并发编程", "Python 异步并发笔记", "")
            hits = await store.search("异步", kind="note", mode="fts5")
            assert [h["id"] for h in hits] == ["note:2"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_search_cjk_ands_words_across_columns(self, tmp_path):
        """Regression: the CJK fallback LIKE'd the WHOLE query as a single
        pattern, so it needed the words adjacent and in order — a note with
        "子agent" in the subject and "委托" in the content was invisible to
        ``cabinet_search`` while ``turn_search`` answered with it.  Both stores
        now build the predicate with ``_like_terms``, so one query has one
        answer."""
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("子agent 调度", "已经委托给子进程处理了", "")
            hits = await store.search("子agent 委托", kind="note", mode="fts5")
            assert [h["id"] for h in hits] == ["note:1"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_search_hybrid_with_vectors(self, tmp_path):
        store = await _real_store(tmp_path, require_vec=True)
        try:
            await store.upsert_note("Python", "Everything about asyncio concurrency", "")
            await store.add_file(title="report", original_path="/x/r.pdf",
                                 saved_path="r.pdf", mime="pdf", size=1,
                                 tags="", summary="Q2 financial results")
            docs = await store.get_unembedded_docs(10)
            for d in docs:
                emb = [0.1, 0.2, 0.3, 0.4] if d["kind"] != "file" else [0.4, 0.3, 0.2, 0.1]
                await store.replace_embedding_chunks(d, [emb])
            hits = await store.search("python concurrency", kind="all",
                                      mode="hybrid", embed_query=[0.1, 0.2, 0.3, 0.4])
            assert hits[0]["id"] == "note:1"
            assert all("rrf_score" in h for h in hits)

            # A window applies to BOTH legs.  The semantic leg cannot take one
            # in SQL — sqlite-vec forbids an auxiliary-column constraint inside
            # a KNN query — so it filters a widened pool in Python.  If it
            # ignored the window, this note (which HAS an embedding and is the
            # closest hit) would still come back through the semantic leg, and
            # a windowed search would silently answer with out-of-window rows.
            assert await store.search(
                "python concurrency", kind="all", mode="hybrid",
                embed_query=[0.1, 0.2, 0.3, 0.4], until="last month",
            ) == []
            assert len(await store.search(
                "python concurrency", kind="all", mode="hybrid",
                embed_query=[0.1, 0.2, 0.3, 0.4], since="last month",
            )) > 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_search_window_covers_every_mode(self, tmp_path):
        """since/until window each mode on ``created_at`` — the column and the
        datetime granularity memdb's ``turn_search`` uses, so one bound narrows
        both stores.

        A bound in no known grammar RAISES: it used to pass through, and
        SQLite compared the text, matched nothing, and reported a bound nobody
        understood as "no matches"."""
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("Python", "Everything about asyncio", "")
            await store.upsert_note("并发编程", "Python 异步笔记", "")
            common = {"kind": "note", "limit": 10}

            # Written just now: inside a window open since last month, outside
            # one that closed at the end of it — in every mode.
            assert len(await store.search("asyncio", mode="fts5",
                                          since="last month", **common)) == 1
            assert await store.search("asyncio", mode="fts5",
                                      until="last month", **common) == []
            assert len(await store.search("asyncio", mode="grep",
                                          since="last month", **common)) == 1
            assert await store.search("asyncio", mode="grep",
                                      until="last month", **common) == []
            # …including the CJK (LIKE) fallback.
            assert len(await store.search("并发", mode="fts5",
                                          since="last month", **common)) == 1
            assert await store.search("并发", mode="fts5",
                                      until="last month", **common) == []

            with pytest.raises(InvalidTimeBound):
                await store.search("asyncio", mode="fts5", since="昨天", **common)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_search_kind_report_resolves_to_the_reports_index(self, tmp_path):
        """The far end of the ``kind="report"`` path: the kind has to resolve to
        ``reports_fts`` and nothing else.  The mock test in TestCabinetSearch
        proves the server passes the kind through; this proves the pass-through
        lands on a real index."""
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("Python", "asyncio concurrency notes", "")
            await store.upsert_report(None, "Weekly review", "asyncio report body", "")
            assert [h["id"] for h in await store.search(
                "asyncio", kind="report", mode="fts5")] == ["report:1"]
            assert [h["id"] for h in await store.search(
                "asyncio", kind="note", mode="fts5")] == ["note:1"]
            assert {h["id"] for h in await store.search(
                "asyncio", kind="all", mode="fts5")} == {"note:1", "report:1"}
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_dim_zero_no_embedding_writes_and_searches(self, tmp_path):
        """No embedding backend (dim 0) must still work end-to-end: the vec0
        tables are skipped, and writes/search run keyword-only (regression:
        the header comment's 'vec0' used to swallow the notes CREATE TABLE)."""
        store = MemfilesStore(tmp_path / ".index.db")
        await store.setup(embedding_dim=0, embedding_model="")
        try:
            await store.upsert_note("Python", "asyncio notes", "py")
            await store.upsert_diary("2026-08-15", "refactor day", "")
            await store.add_file(title="f", original_path="/x/f", saved_path="f.txt",
                                 mime="text", size=1, tags="", summary="")
            hits = await store.search("asyncio", kind="note", mode="fts5")
            assert [h["id"] for h in hits] == ["note:1"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_read_path_traversal_guard(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("Python", "body", "")
            assert store.resolve_safe_path("notes/python.md").is_file()
            with pytest.raises(ValueError):
                store.resolve_safe_path("../escape.md")
            with pytest.raises(ValueError):
                store.resolve_safe_path("/etc/passwd")
        finally:
            await store.close()


class _QueryCursor:
    """An aiosqlite-like cursor: async context manager with async fetchone."""

    async def __aenter__(self) -> "_QueryCursor":
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def fetchone(self) -> tuple:
        return (1,)


class TestMemfilesLifespan:
    """Readiness (MCP plugin contract): the lifespan gates ``initialize``.

    The store is the plugin's serving requirement, now encoded in
    initialization — an unusable store raises in the lifespan (the port
    signal never fires, so the harness reports FAILED) instead of answering
    a ``__ready`` tool with ``ready: false``.
    """

    @staticmethod
    def _ok_store(tmp_path):
        store = _fake_store(tmp_path / "files")
        # aiosqlite's ``Connection.execute`` returns a cursor that supports
        # ``async with`` — a MagicMock (not AsyncMock) models that.
        store._c.execute = MagicMock(return_value=_QueryCursor())
        return store

    @pytest.mark.asyncio
    async def test_store_failure_raises(self):
        """Store cannot be established → the lifespan raises on enter."""
        with patch.object(plugin, "_ensure_store",
                          AsyncMock(side_effect=RuntimeError("db locked"))):
            with pytest.raises(RuntimeError, match="db locked"):
                async with plugin._memfiles_lifespan(None):
                    pass

    @pytest.mark.asyncio
    async def test_store_ok_yields_then_closes(self, tmp_path):
        """Store can serve → lifespan yields, and teardown closes it."""
        store = self._ok_store(tmp_path)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            plugin._store = store
            plugin._manager = None
            entered = False
            async with plugin._memfiles_lifespan(None):
                entered = True
        assert entered
        store.close.assert_awaited_once()


class TestCabinetGrep:
    """``mode="grep"`` is a real grep — a regex, not a LIKE substring.

    Before this it was SQL ``LIKE %pattern%``: ``summ.rize`` matched nothing
    and a ``|`` was a literal pipe, which made the name a misnomer.
    """

    @pytest.mark.asyncio
    async def test_regex_matches_what_like_could_not(self, tmp_path):
        store = await _real_store(tmp_path, dim=0)
        await store.upsert_note("Deploy runbook", "how to deploy the service", "")
        await store.upsert_note("Unrelated", "nothing to see", "")

        assert [r["id"] for r in await store.search("depl.y", mode="grep")] == ["note:1"]
        # Alternation and a wildcard — the two things LIKE cannot express.
        assert len(await store.search("runbook|noth.ng", mode="grep")) == 2
        # …and the FTS path cannot do it, which is why the mode exists.
        assert await store.search("depl.y", mode="fts5") == []
        await store.close()

    @pytest.mark.asyncio
    async def test_an_invalid_pattern_raises(self, tmp_path):
        store = await _real_store(tmp_path, dim=0)
        with pytest.raises(re.error):
            await store.search("a(b", mode="grep")
        await store.close()
