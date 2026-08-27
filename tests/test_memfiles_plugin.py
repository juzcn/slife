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


import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import slife.plugins.memfiles.server as plugin
from slife.plugins.memfiles.store import MemfilesStore


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
    store.list_notes = AsyncMock(return_value={
        "entries": [
            {"id": 1, "subject": "subj", "tags": "", "file_path": "notes/subj.md",
             "created_at": "2026-01-01", "updated_at": "2026-01-02"},
        ],
        "total": 1,
    })
    store.list_diary = AsyncMock(return_value={
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
    store.list_files = AsyncMock(return_value={
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
# note_save / diary_write / file_save / url_save
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
            await plugin.diary_write(content="today's entry")
        store.upsert_diary.assert_awaited_once()
        call_date = store.upsert_diary.await_args.args[0]
        assert call_date == "2026-08-15" or len(call_date.split("-")) == 3

    @pytest.mark.asyncio
    async def test_explicit_date(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.diary_write(date="2026-08-10", content="x")
        store.upsert_diary.assert_awaited_once_with("2026-08-10", "x", "")

    @pytest.mark.asyncio
    async def test_returns_local_path_not_share_url(self, tmp_path):
        """A diary is private — the result names the local md file, with no
        share URL and no file registered for public sharing."""
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            result = await plugin.diary_write(date="2026-08-10", content="x")
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


# ═══════════════════════════════════════════════════════════════════════
# cabinet_search / cabinet_read
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
    async def test_hybrid_without_manager_degrades_to_fts5(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_search(query="python", mode="hybrid")
        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert "hybrid degraded" in data["hint"]

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
            out = await plugin.cabinet_read("notes/subj.md")
        assert out == "# content"

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        store.resolve_safe_path = MagicMock(return_value=tmp_path / "files" / "none.md")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_read("none.md")
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
        store.list_notes.assert_awaited_once_with(limit=50, offset=0)

    @pytest.mark.asyncio
    async def test_diary_list_range(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.diary_list(since="2026-08-01", until="2026-08-31")
        data = json.loads(out)
        assert data["entries"][0]["date"] == "2026-08-15"
        store.list_diary.assert_awaited_once_with(
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
    async def test_list_files(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.list_files(category="documents")
        data = json.loads(out)
        assert data["total"] == 1
        assert data["entries"][0]["category"] == "documents"
        assert data["entries"][0]["saved_path"] == "files/documents/a.txt"
        store.list_files.assert_awaited_once_with(
            category="documents", limit=50, offset=0,
        )


class TestCabinetEmbeddingCheck:
    """cabinet_embedding_check reports the memfiles index's OWN gate —
    independent from memdb's semantic_index_status (each plugin reindexes its
    own DB, so one can be semantically ready while the other is still
    building)."""

    @pytest.mark.asyncio
    async def test_reports_memfiles_manager_state(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        manager = MagicMock()
        manager.semantic_ready = False
        manager.state = "indexing"
        manager.reason = "hybrid degraded to fts5 — semantic index is building"
        manager.unembedded = AsyncMock(return_value=5)
        embedder = MagicMock()
        embedder.backend = "gguf"
        embedder._model = "bge-m3"
        embedder.dimension = 1024
        embedder.available = True
        embedder.loaded = True
        manager.embedder = embedder
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            out = await plugin.cabinet_embedding_check()
        data = json.loads(out)
        assert data["semantic_ready"] is False
        assert data["state"] == "indexing"
        assert data["backend"] == "gguf"
        assert data["model"] == "bge-m3"
        assert "pending embedding" in data["hint"]

    @pytest.mark.asyncio
    async def test_without_manager_reports_config_probe(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_embedding_check()
        data = json.loads(out)
        assert "configured" in data
        assert "hint" in data


class TestCabinetStatus:
    """__cabinet_status — the internal tool the harness's check_memfiles probes."""

    @pytest.mark.asyncio
    async def test_store_error_reports_failure(self, tmp_path):
        with patch.object(plugin, "_ensure_store",
                          AsyncMock(side_effect=RuntimeError("boom"))):
            out = await getattr(plugin, "__cabinet_status")()
        data = json.loads(out)
        assert data["ok"] is False
        assert data["state"] == "store_error"
        assert "boom" in data["hint"]

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
            out = await getattr(plugin, "__cabinet_status")()
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
            out = await getattr(plugin, "__cabinet_status")()
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

            notes = await store.list_notes()
            # newest-updated first; timestamps share second precision in-tests,
            # so don't assert tie order
            assert {n["subject"] for n in notes["entries"]} == {"Python", "Go"}
            assert notes["total"] == 2
            assert "content" not in notes["entries"][0]  # lightweight

            days = await store.list_diary(since="2026-08-14", until="2026-08-15")
            assert [d["date"] for d in days["entries"]] == ["2026-08-15", "2026-08-14"]
            assert days["total"] == 2
            days = await store.list_diary(since="2026-08-15")
            assert [d["date"] for d in days["entries"]] == ["2026-08-15"]

            # paging: limit 1 of 2 → total tells the caller more remain
            paged = await store.list_diary(limit=1)
            assert len(paged["entries"]) == 1 and paged["total"] == 2
            rest = await store.list_diary(limit=1, offset=1)
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
    async def test_list_files(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            await store.add_file(title="a", original_path="/x/a",
                                 saved_path="files/documents/a.pdf",
                                 mime="pdf", size=1, tags="", summary="")
            await store.add_file(title="b", original_path="/x/b",
                                 saved_path="files/images/b.png",
                                 mime="png", size=2, tags="", summary="s")

            data = await store.list_files()
            assert data["total"] == 2
            assert {e["category"] for e in data["entries"]} == {"documents", "images"}

            docs = await store.list_files(category="documents")
            assert docs["total"] == 1
            assert docs["entries"][0]["category"] == "documents"
            assert docs["entries"][0]["saved_path"] == "files/documents/a.pdf"

            paged = await store.list_files(limit=1)
            assert len(paged["entries"]) == 1 and paged["total"] == 2
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
