"""Tests for the memfiles plugin — notes / diary / files cabinet + sharing.

Mocks the ngrok tunnel (no network) and exercises the MCP tool functions
directly, following the test_mqtt_plugin.py pattern.  Covers the token
registry, the note/diary/file/search tools (with a mocked store), the
harness-only tools, the SSRF guard, and the custom ``GET /share/{file_id}``
HTTP route.  Store internals (md mirroring, hybrid search, the SemanticManager
contract) are covered against a real temp DB in ``TestMemfilesStore``.
"""

import pytest; pytestmark = pytest.mark.unit


import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import quote

import slife.plugins.memfiles.server as plugin
from slife.plugins.memfiles.store import MemfilesStore


# ── Fixtures ────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    """Reset registry + store/manager globals + plugin port each test."""
    plugin._reset_registry()
    plugin._PLUGIN_PORT = 12345
    plugin._store = None
    plugin._manager = None
    plugin._db_path = None
    plugin._init_lock = None
    yield
    plugin._reset_registry()
    plugin._store = None
    plugin._manager = None


def _active_tunnel(url="https://slife.ngrok-free.dev"):
    """Patch the tunnel so it reports active with the given public URL."""
    return patch.multiple(
        plugin,
        is_active=MagicMock(return_value=True),
        public_url=MagicMock(return_value=url),
        share_url_for=MagicMock(side_effect=lambda fid: f"{url}/share/{fid}"),
    )


def _offline_tunnel():
    """Patch the tunnel so it reports inactive.

    Also clear ``_PLUGIN_PORT`` so ``_ensure_tunnel`` short-circuits without
    a real ngrok start attempt — without this the offline test spent ~9s
    trying to reach the actual ngrok service.
    """
    return patch.multiple(
        plugin,
        is_active=MagicMock(return_value=False),
        public_url=MagicMock(return_value=None),
        share_url_for=MagicMock(return_value=None),
        _PLUGIN_PORT=0,
    )


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
    store.resolve_safe_path = MagicMock(return_value=mem_dir / "notes" / "subj.md")
    return store


# ═══════════════════════════════════════════════════════════════════════
# Token registry
# ═══════════════════════════════════════════════════════════════════════


class TestRegistry:
    def test_returns_30_char_hex(self):
        tok = plugin._register_file("/some/file.txt")
        assert len(tok) == 30
        assert all(c in "0123456789abcdef" for c in tok)

    def test_roundtrip(self):
        tok = plugin._register_file("/data/report.pdf")
        assert plugin._lookup_file(tok) == "/data/report.pdf"

    def test_dedup_same_path(self):
        t1 = plugin._register_file("/tmp/a.txt")
        t2 = plugin._register_file("/tmp/a.txt")
        assert t1 == t2

    def test_distinct_paths(self):
        t1 = plugin._register_file("/a.txt")
        t2 = plugin._register_file("/b.txt")
        assert t1 != t2

    def test_unknown_token(self):
        assert plugin._lookup_file("deadbeef") is None


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
# expose_file
# ═══════════════════════════════════════════════════════════════════════


class TestExposeFile:
    @pytest.mark.asyncio
    async def test_active_tunnel_returns_url(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"pngdata")
        with _active_tunnel():
            result = await plugin.expose_file(path=str(f))
        assert "Public URL for photo.png" in result
        assert "https://slife.ngrok-free.dev/share/" in result

    @pytest.mark.asyncio
    async def test_offline_tunnel_returns_error(self, tmp_path):
        f = tmp_path / "photo.png"
        f.write_bytes(b"pngdata")
        with _offline_tunnel():
            result = await plugin.expose_file(path=str(f))
        assert result.startswith("Error:")
        assert "file sharing service is not available" in result

    @pytest.mark.asyncio
    async def test_missing_file(self):
        with _active_tunnel():
            result = await plugin.expose_file(path="D:\\nonexistent\\x.png")
        assert result.startswith("Error:")
        assert "file not found" in result

    @pytest.mark.asyncio
    async def test_directory(self, tmp_path):
        with _active_tunnel():
            result = await plugin.expose_file(path=str(tmp_path))
        assert result.startswith("Error:")
        assert "not a file" in result

    @pytest.mark.asyncio
    async def test_url_drops_after_register(self, tmp_path):
        f = tmp_path / "doc.pdf"
        f.write_bytes(b"pdf")
        with patch.multiple(
            plugin,
            is_active=MagicMock(return_value=True),
            share_url_for=MagicMock(return_value=None),
        ):
            result = await plugin.expose_file(path=str(f))
        assert result.startswith("Error:")
        assert "became unavailable" in result


# ═══════════════════════════════════════════════════════════════════════
# note_save / diary_write / file_save / url_save
# ═══════════════════════════════════════════════════════════════════════


class TestNoteSave:
    @pytest.mark.asyncio
    async def test_saves_and_returns_url(self, tmp_path):
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             _active_tunnel():
            result = await plugin.note_save(subject="Python", content="asyncio notes",
                                            tags="py")
        assert "Saved:" in result
        assert "URL:" in result
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
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             _active_tunnel():
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

        class _Resp:
            status = 200
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def read(self): return b"<html>Page</html>"

        def _fake_get(url, timeout=None):
            # sync: url_save does ``async with session.get(...)``
            return _Resp()

        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             _active_tunnel(), \
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
# search / read
# ═══════════════════════════════════════════════════════════════════════


class TestSearch:
    @pytest.mark.asyncio
    async def test_fts5(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.search(query="python", kind="note", mode="fts5")
        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert data["results"][0]["id"] == "note:1"
        store.search.assert_awaited_once()
        assert store.search.await_args.kwargs["embed_query"] is None

    @pytest.mark.asyncio
    async def test_hybrid_without_manager_degrades_to_fts5(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.search(query="python", mode="hybrid")
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
            out = await plugin.search(query="python", mode="hybrid")
        data = json.loads(out)
        assert data["mode"] == "hybrid"
        assert store.search.await_args.kwargs["embed_query"] == [0.1, 0.2]


class TestRead:
    @pytest.mark.asyncio
    async def test_reads_file(self, tmp_path):
        mem_dir = tmp_path / "files"
        (mem_dir / "notes").mkdir(parents=True)
        (mem_dir / "notes" / "subj.md").write_text("# content", encoding="utf-8")
        store = _fake_store(mem_dir)
        store.resolve_safe_path = MagicMock(return_value=mem_dir / "notes" / "subj.md")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.read("notes/subj.md")
        assert out == "# content"

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        store = _fake_store(tmp_path / "files")
        store.resolve_safe_path = MagicMock(return_value=tmp_path / "files" / "none.md")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.read("none.md")
        assert out.startswith("Error: not a file")


class TestEmbeddingCheck:
    """embedding_check reports the memfiles index's OWN gate — independent
    from memdb's memory_check_embedding (each plugin reindexes its own DB,
    so one can be semantically ready while the other is still building)."""

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
            out = await plugin.embedding_check()
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
            out = await plugin.embedding_check()
        data = json.loads(out)
        assert "configured" in data
        assert "hint" in data


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
# Harness-only tools
# ═══════════════════════════════════════════════════════════════════════


class TestHarnessTools:
    @pytest.mark.asyncio
    async def test_tunnel_status_active(self):
        with _active_tunnel():
            raw = await getattr(plugin, "__tunnel_status")()
        data = json.loads(raw)
        assert data["active"] is True
        assert data["url"] == "https://slife.ngrok-free.dev"

    @pytest.mark.asyncio
    async def test_tunnel_status_offline(self):
        with _offline_tunnel():
            raw = await getattr(plugin, "__tunnel_status")()
        data = json.loads(raw)
        assert data["active"] is False
        assert "hint" in data

    @pytest.mark.asyncio
    async def test_register_file(self, tmp_path):
        f = tmp_path / "a.png"
        f.write_bytes(b"x")
        with _active_tunnel():
            raw = await getattr(plugin, "__register_file")(str(f))
        data = json.loads(raw)
        assert len(data["file_id"]) == 30
        assert data["url"] == f"https://slife.ngrok-free.dev/share/{data['file_id']}"
        assert plugin._lookup_file(data["file_id"]) == str(f.resolve())


# ═══════════════════════════════════════════════════════════════════════
# Custom HTTP route — GET /share/{file_id}
# ═══════════════════════════════════════════════════════════════════════


def _request(file_id: str):
    req = MagicMock()
    req.path_params = {"file_id": file_id}
    return req


class TestShareRoute:
    @pytest.mark.asyncio
    async def test_unknown_token_403(self):
        resp = await plugin.handle_share(_request("deadbeef"))
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_missing_file_404(self, tmp_path):
        f = tmp_path / "gone.pdf"
        f.write_bytes(b"x")
        tok = plugin._register_file(str(f))
        f.unlink()
        resp = await plugin.handle_share(_request(tok))
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_streams_file(self, tmp_path):
        payload = b"file-content-bytes"
        f = tmp_path / "data.txt"
        f.write_bytes(payload)
        tok = plugin._register_file(str(f))

        resp = await plugin.handle_share(_request(tok))
        assert resp.status_code == 200
        assert resp.headers["Content-Type"].startswith("text/plain")
        assert "ngrok-skip-browser-warning" in resp.headers

        body = b"".join([c async for c in resp.body_iterator])
        assert body == payload

    @pytest.mark.asyncio
    async def test_sets_content_length(self, tmp_path):
        payload = b"1234567890"
        f = tmp_path / "ten.bin"
        f.write_bytes(payload)
        tok = plugin._register_file(str(f))

        resp = await plugin.handle_share(_request(tok))
        assert resp.headers["Content-Length"] == "10"

    @pytest.mark.asyncio
    async def test_non_ascii_filename_no_500(self, tmp_path):
        """A CJK filename must not blow up the Content-Disposition header.

        Regression: HTTP headers are Latin-1 — a non-ASCII filename used to
        raise UnicodeEncodeError in Starlette's init_headers (HTTP 500).
        """
        payload = b"\xe4\xb8\xad\xe6\x96\x87"  # some bytes
        f = tmp_path / "报告.pdf"      # 报告.pdf
        f.write_bytes(payload)
        tok = plugin._register_file(str(f))

        resp = await plugin.handle_share(_request(tok))
        assert resp.status_code == 200
        cd = resp.headers["Content-Disposition"]
        # RFC 5987 percent-encoded form carries the real name
        assert "filename*=UTF-8''" in cd
        assert quote("报告.pdf") in cd
        body = b"".join([c async for c in resp.body_iterator])
        assert body == payload

    def test_content_disposition_ascii(self):
        assert plugin._content_disposition("photo.png") == (
            'inline; filename="photo.png"'
        )

    def test_content_disposition_non_ascii(self):
        cd = plugin._content_disposition("报告.pdf")
        assert 'filename="' in cd
        assert "filename*=UTF-8''" in cd


# ═══════════════════════════════════════════════════════════════════════
# MemfilesStore — real temp DB: md mirroring, contract, hybrid search
# ═══════════════════════════════════════════════════════════════════════


async def _real_store(tmp_path, dim: int = 4) -> MemfilesStore:
    store = MemfilesStore(tmp_path / ".index.db")
    await store.setup(embedding_dim=dim, embedding_model="test:model")
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
        store = await _real_store(tmp_path)
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
        store = await _real_store(tmp_path)
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
        store = await _real_store(tmp_path)
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
