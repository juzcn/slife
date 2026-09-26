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
import codecs
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlparse

from slife import net
from slife.plugins.memdb.search import SearchLegs, run_search

import slife.plugins.memfiles.server as plugin
from slife.plugins.memfiles.store import CABINET_BROWSE_TOOLS, MemfilesStore
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


#: One cabinet hit, in the shape the legs return (already normalized: the
#: composition's legs carry the model-facing keys).
_HIT = {"id": "note:1", "kind": "note", "key": "subj", "title": "subj",
        "file_path": "notes/subj.md", "time": "2026-01-02", "tags": "",
        "snippet": "…", "rrf_score": 0.01}


def _fake_legs(**overrides) -> SearchLegs:
    """The cabinet's three legs as mocks — the seam a search test asserts on."""
    legs = SearchLegs(
        keyword=AsyncMock(return_value=[dict(_HIT)]),
        semantic=AsyncMock(return_value=[dict(_HIT)]),
        regex=AsyncMock(return_value=[dict(_HIT)]),
        key_field="id", noun="entries", browse=CABINET_BROWSE_TOOLS,
    )
    for name, value in overrides.items():
        setattr(legs, name, value)
    return legs


class _StubManager:
    """Just enough SemanticManager for a search: the gate plus one embed.

    The real one owns a lifecycle, a drainer and a binary gate; a search only
    ever asks it two things, so a store test answers those two and nothing
    else is pretended.
    """

    def __init__(self, vector: list[float] | None = None, reason: str = ""):
        self.semantic_ready = vector is not None
        self.reason = reason
        self.embedder = SimpleNamespace(
            available=vector is not None,
            embed_one=AsyncMock(return_value=vector),
        )


def _stub_manager(vector=None, reason: str = "") -> _StubManager:
    return _StubManager(vector, reason)


def _ready_manager() -> MagicMock:
    """A SemanticManager whose embedder is up, returning a known vector."""
    manager = MagicMock()
    manager.semantic_ready = True
    embedder = MagicMock()
    embedder.available = True
    embedder.embed_one = AsyncMock(return_value=[0.1, 0.2])
    manager.embedder = embedder
    return manager


def _fake_store(mem_dir: Path) -> tuple[MagicMock, SearchLegs]:
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
    # The three legs the composition drives.  A test asserting WHICH search
    # ran, and with what, reads them here — the store's own ``search_legs``
    # builds the same object over the real corpus.
    store.search_legs = MagicMock(return_value=_fake_legs())
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
    return store, store.search_legs.return_value


# ═══════════════════════════════════════════════════════════════════════
# Filename helpers (from store)
# ═══════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_slugify(self):
        assert plugin._slugify("Project Notes 2026!") == "project-notes-2026"
        assert plugin._slugify("--hello--") == "hello"

    def test_name_to_stem_keeps_dots_and_takes_one_extension(self):
        """A *name* keeps its dots — a subject or a stem does not (there the
        extension is appended afterwards).  The name's own extension is dropped
        only when it matches the one the caller will append, so "notes.v2"
        keeps its dot while "paper.pdf" against a .txt source does not become
        "paper.pdf.txt"."""
        assert plugin._name_to_stem("memfiles_test_doc.txt", ".txt") == "memfiles_test_doc"
        assert plugin._name_to_stem("notes.v2.txt", ".txt") == "notes.v2"
        assert plugin._name_to_stem("Release 2.0", ".txt") == "release-2.0"
        assert plugin._name_to_stem("report.pdf", ".txt") == "report.pdf"
        assert plugin._name_to_stem("My Paper", ".txt") == "my-paper"
        # a stem may not be hidden or end in a dot
        assert plugin._name_to_stem(".hidden", ".txt") == "hidden"
        assert plugin._name_to_stem("name.", ".txt") == "name"

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
        store, _ = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            result = await plugin.note_save(subject="Python", content="asyncio notes",
                                            tags="py")
        assert "Saved:" in result
        assert result.rstrip().replace("\\", "/").endswith("notes/subj.md")
        assert "URL:" not in result
        store.upsert_note.assert_awaited_once_with("Python", "asyncio notes", "py")

    @pytest.mark.asyncio
    async def test_wakes_drainer(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        manager = MagicMock()
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            await plugin.note_save(subject="s", content="c")
        manager.on_saved.assert_called_once()


class TestDiaryWrite:
    @pytest.mark.asyncio
    async def test_defaults_to_today(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.diary_save(content="today's entry")
        store.upsert_diary.assert_awaited_once()
        call_date = store.upsert_diary.await_args.args[0]
        assert call_date == "2026-08-15" or len(call_date.split("-")) == 3

    @pytest.mark.asyncio
    async def test_explicit_date(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.diary_save(date="2026-08-10", content="x")
        store.upsert_diary.assert_awaited_once_with("2026-08-10", "x", "")

    @pytest.mark.asyncio
    async def test_returns_local_path_not_share_url(self, tmp_path):
        """A diary is private — the result names the local md file, with no
        share URL and no file registered for public sharing."""
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store, _ = _fake_store(mem_dir)
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
        store, _ = _fake_store(mem_dir)
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
        store, _ = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.file_save(paths=[str(png), str(pdf), str(unknown)])
        assert (mem_dir / "files" / "images" / "shot.png").is_file()
        assert (mem_dir / "files" / "documents" / "report.pdf").is_file()
        assert (mem_dir / "files" / "other" / "archive.xyz").is_file()

    @pytest.mark.asyncio
    async def test_a_title_that_is_a_filename_keeps_its_dot(self, tmp_path):
        """The tester's 2026-09-26 finding: saving with a title of
        "memfiles_test_doc.txt" wrote "memfiles_test_doctxt.txt" — ``_slugify``
        dropped the dot and the extension went on again.  A caller holding a
        filename passes the filename, so a title is not always a stem, and the
        extension comes from the source either way."""
        doc = tmp_path / "memfiles_test_doc.txt"
        doc.write_text("body")
        png = tmp_path / "shot.png"
        png.write_bytes(b"png")
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store, _ = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.file_save(
                paths=[str(doc)], title="memfiles_test_doc.txt")
            await plugin.file_save(
                paths=[str(png)], title="slife_r2_vision_cjk-page-1.png")
        assert (mem_dir / "files" / "documents" / "memfiles_test_doc.txt").is_file()
        # src is a .png, so the title's own extension is dropped, not doubled
        assert (mem_dir / "files" / "images" / "slife_r2_vision_cjk-page-1.png").is_file()

    @pytest.mark.asyncio
    async def test_category_override(self, tmp_path):
        f = tmp_path / "report.pdf"
        f.write_bytes(b"pdf")
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store, _ = _fake_store(mem_dir)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            await plugin.file_save(paths=[str(f)], category="books")
        assert (mem_dir / "files" / "books" / "report.pdf").is_file()

    @pytest.mark.asyncio
    async def test_missing_file_reports_error(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            result = await plugin.file_save(paths=["D:\\nope\\x.pdf"])
        assert "Error: file not found" in result

    @pytest.mark.asyncio
    async def test_summary_wakes_drainer(self, tmp_path):
        f = tmp_path / "a.pdf"
        f.write_bytes(b"pdf")
        store, _ = _fake_store(tmp_path / "files")
        manager = MagicMock()
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            await plugin.file_save(paths=[str(f)], summary="a doc about pdfs")
        manager.on_saved.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_save_without_a_summary_wakes_it_too(self, tmp_path):
        """A file's body is its title, its paths and its summary, so it is
        embeddable from the save — waking only when a summary was given left
        the file unindexed until the next wake of any kind."""
        f = tmp_path / "a.pdf"
        f.write_bytes(b"pdf")
        store, _ = _fake_store(tmp_path / "files")
        manager = MagicMock()
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            await plugin.file_save(paths=[str(f)])
        manager.on_saved.assert_called_once()


class TestUrlSave:
    @pytest.mark.asyncio
    async def test_downloads_and_records(self, tmp_path):
        mem_dir = tmp_path / "files"
        mem_dir.mkdir()
        store, _ = _fake_store(mem_dir)

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
        store, _ = _fake_store(tmp_path / "files")
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
            # a title that is itself a filename: the dot is part of the name,
            # and the extension still appears exactly once (2026-09-26)
            ("https://example.com/dl", "memfiles_test_doc.txt",
             "memfiles_test_doc.txt"),
            ("https://example.com/other.pdf", "My Paper.pdf", "my-paper.pdf"),
            # a dot inside the basename survives too
            ("https://example.com/a.b.c", "", "a.b.c"),
        ):
            _, _, stem, ext = plugin._url_name_parts(urlparse(url), title)
            assert stem + ext == expected, (url, title)
            assert "untitled" not in stem, url


# ═══════════════════════════════════════════════════════════════════════
# cabinet_search / file_read
# ═══════════════════════════════════════════════════════════════════════


class TestCabinetSearch:
    """The tool's own half: choose the corpus, validate, build the envelope.

    Everything else a search decides — the clamp, the gate, the single embed,
    the mode it reports, the hint — belongs to the shared composition in
    ``slife.plugins.memdb.search``, so these tests drive the tool and read the
    legs it hands over.  The composition's own rules are tested once, there.
    """

    @pytest.mark.asyncio
    async def test_fts5(self, tmp_path):
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_search(query="python", kind="note", mode="fts5")
        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert data["kind"] == "note"
        assert data["results"][0]["id"] == "note:1"
        # The kind reached the corpus, and only the keyword leg ran.
        assert store.search_legs.call_args.kwargs["kind"] == "note"
        legs.keyword.assert_awaited_once()
        assert legs.keyword.await_args.kwargs["query"] == "python"
        legs.semantic.assert_not_awaited()
        legs.regex.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_bad_kind_is_refused_not_rewritten_to_all(self, tmp_path):
        """``kind="nonsense"`` used to be silently rewritten to ``"all"``, so a
        caller that asked for the wrong kind got every kind and no way to tell.
        A value the tool does not have is a mistake, not something to swallow —
        the same rule the mode follows."""
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_search(query="weekly", kind="nonsense")
        assert out.startswith("Error")
        assert "nonsense" in out
        # It names the values that DO exist, so the next call can be right.
        for kind in ("all", "note", "diary", "file", "report"):
            assert kind in out
        legs.keyword.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_bad_mode_is_refused(self, tmp_path):
        """Same rule one parameter over: ``mode="regex"`` used to become hybrid,
        and the caller read hybrid results believing it had asked for a regex."""
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_search(query="python", mode="regex")
        assert out.startswith("Error")
        assert "regex" in out
        for mode in ("hybrid", "fts5", "grep"):
            assert mode in out
        legs.keyword.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_query_is_rejected_in_every_mode(self, tmp_path):
        """An empty query is not a search, and it must not be one.

        `mode="grep"` used to compile the empty pattern — which matches EVERY
        string — so an empty query silently returned the whole window: no
        `total`, no paging, invisible in every description.  That was an
        accidental second browse path, one unrelated "reject empty patterns"
        fix away from vanishing without a trace."""
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            for mode in ("hybrid", "fts5", "grep"):
                for q in ("", "   "):
                    out = await plugin.cabinet_search(query=q, mode=mode)
                    assert out.startswith("Error"), (mode, q)
                    assert "must not be empty" in out
                    # And it names where browsing actually lives — all four
                    # kind lists, since there is no single cabinet browse.
                    for tool in ("note_list", "diary_list", "file_list",
                                 "report_list"):
                        assert tool in out, (mode, q, tool)
        legs.keyword.assert_not_awaited()
        legs.regex.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_browse_pointer_names_real_tools(self, tmp_path):
        """The refusal's pointer is the corpus's own ``browse`` field, so the
        store must fill it — the fake legs in this file supply it themselves,
        which is exactly how "use list" (a tool that does not exist) survived
        every search test."""
        store = await _real_store(tmp_path)
        try:
            assert store.search_legs().browse == CABINET_BROWSE_TOOLS
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                out = await plugin.cabinet_search(query="")
            assert "cabinet_list" not in out
            for tool in ("note_list", "diary_list", "file_list", "report_list"):
                assert tool in out, tool
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_hybrid_without_manager_degrades_to_fts5(self, tmp_path):
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_search(query="python", mode="hybrid")
        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert "hybrid degraded" in data["hint"]
        legs.semantic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grep_reports_grep_not_fts5(self, tmp_path):
        """The envelope reports the mode that RAN.

        Derived from `semantic_available` alone it answered "fts5" for a grep
        request — so a caller reading an empty result concluded the keyword
        search had found nothing, when the regex was what ran.  A ready
        manager must not change the answer either: grep never consults it.
        """
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", _ready_manager()):
            out = await plugin.cabinet_search(query="depl.y", mode="grep")
        data = json.loads(out)
        assert data["mode"] == "grep"
        # The regex leg ran…
        assert legs.regex.await_args.kwargs["pattern"] == "depl.y"
        legs.keyword.assert_not_awaited()
        # …and it never touched the embedding path.
        legs.semantic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fts5_is_not_reported_as_hybrid(self, tmp_path):
        """The mirror case: an explicit fts5 request with a ready manager is
        fts5, not the hybrid the manager could have served."""
        store, legs = _fake_store(tmp_path / "files")
        manager = _ready_manager()
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", manager):
            out = await plugin.cabinet_search(query="python", mode="fts5")
        assert json.loads(out)["mode"] == "fts5"
        manager.embedder.embed_one.assert_not_awaited()
        legs.semantic.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_silent_gate_still_explains_the_degradation(self, tmp_path):
        """A manager that exists but has not come up yet carries an EMPTY
        ``reason``.  Keying the fallback on None alone answered that with an
        empty hint — the silent fallback the degradation prose exists to
        prevent."""
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", _stub_manager(reason="")):
            out = await plugin.cabinet_search(query="python", mode="hybrid")

        data = json.loads(out)
        assert data["mode"] == "fts5"
        assert data["hint"] == (
            "hybrid degraded to fts5 — embedding backend unavailable"
        )
        assert data["results"], "the keyword leg still answered"

    @pytest.mark.asyncio
    async def test_hybrid_with_ready_manager_embeds_query(self, tmp_path):
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", _ready_manager()):
            out = await plugin.cabinet_search(query="python", mode="hybrid")
        data = json.loads(out)
        assert data["mode"] == "hybrid"
        # The one embed the composition makes reaches the semantic leg.
        assert legs.semantic.await_args.kwargs["embedding"] == [0.1, 0.2]
        legs.keyword.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_hit_hint_names_the_mode_that_missed(self, tmp_path):
        """Each mode says what it looked for and did not find.

        One "no matching memories found" for all three conflated them: a
        no-hit grep and a no-hit keyword search are different answers, and a
        caller deciding what to try next needs to know which ran."""
        store, legs = _fake_store(tmp_path / "files")
        legs.keyword = AsyncMock(return_value=[])
        legs.regex = AsyncMock(return_value=[])
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            grep = json.loads(await plugin.cabinet_search(query="depl.y", mode="grep"))
            fts5 = json.loads(await plugin.cabinet_search(query="python", mode="fts5"))
        assert grep["hint"] == "no entries contain 'depl.y'"
        assert fts5["hint"] == "no entries related to 'python'"

    @pytest.mark.asyncio
    async def test_the_limit_is_clamped_before_the_legs_run(self, tmp_path):
        """A negative limit means "no limit" to SQLite, and a huge one means a
        scan — so the composition clamps before any leg sees it."""
        store, legs = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            for asked, expected in ((0, 20), (-5, 20), (1000, 200)):
                await plugin.cabinet_search(query="python", mode="fts5", limit=asked)
                assert legs.keyword.await_args.kwargs["limit"] == expected


class TestCabinetRead:
    @pytest.mark.asyncio
    async def test_reads_file(self, tmp_path):
        mem_dir = tmp_path / "files"
        (mem_dir / "notes").mkdir(parents=True)
        (mem_dir / "notes" / "subj.md").write_text("# content", encoding="utf-8")
        store, _ = _fake_store(mem_dir)
        store.resolve_safe_path = MagicMock(return_value=mem_dir / "notes" / "subj.md")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.file_read("notes/subj.md")
        assert out == "# content"

    @pytest.mark.asyncio
    async def test_missing_file(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        store.resolve_safe_path = MagicMock(return_value=tmp_path / "files" / "none.md")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.file_read("none.md")
        assert out.startswith("Error: not a file")

    @pytest.mark.asyncio
    async def test_a_binary_file_is_refused_not_decoded(self, tmp_path):
        """The cabinet holds bytes as well as text — an image, a PDF, an
        archive — and a read returns TEXT or says it has none.

        Decoding with errors="replace" used to succeed on any of them and hand
        the model a page of replacement characters: a successful-looking read
        of a file with no text in it, indistinguishable from one that is
        genuinely gibberish.  The bytes stay on disk, so the refusal names the
        type, the size and the path."""
        mem_dir = tmp_path / "files"
        (mem_dir / "files" / "documents").mkdir(parents=True)
        pdf = mem_dir / "files" / "documents" / "paper.pdf"
        pdf.write_bytes(b"%PDF-1.4\n\x00\x01\x02\xff\xfe binary payload")
        png = mem_dir / "files" / "images"
        png.mkdir(parents=True)
        (png / "shot.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")

        store, _ = _fake_store(mem_dir)
        for rel, expect in (
            ("files/documents/paper.pdf", "application/pdf"),
            ("files/images/shot.png", "image/png"),
        ):
            store.resolve_safe_path = MagicMock(return_value=mem_dir / rel)
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                out = await plugin.file_read(rel)
            assert out.startswith("Error"), rel
            assert "is not text" in out
            assert expect in out, "the refusal names the type"
            assert str(mem_dir / rel) in out, "and where the bytes are"
        # An image points at the tool that takes one.
        store.resolve_safe_path = MagicMock(return_value=png / "shot.png")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.file_read("files/images/shot.png")
        assert "attach_image" in out

    @pytest.mark.asyncio
    async def test_a_text_file_in_another_encoding_is_read_and_named(self, tmp_path):
        """The 2026-09-26 tester's file: a .txt written by cmd, so GBK.  It is
        text and it has to be readable — refusing it and calling it "not text"
        was wrong twice over, once about the content and once about the reason.

        The bytes must be one of the tester's own, because that is the case
        that failed: 中文编码样本 retest-R3."""
        mem_dir = tmp_path / "files"
        mem_dir.mkdir(parents=True, exist_ok=True)
        gbk = mem_dir / "样本.txt"
        gbk.write_bytes("中文编码样本 retest-R3\r\n第二行中文\r\n".encode("gbk"))
        latin = mem_dir / "cafe.txt"
        latin.write_bytes("café au lait écrit à la main".encode("latin-1"))

        store, _ = _fake_store(mem_dir)
        for rel, target, needle in (
            ("样本.txt", gbk, "中文编码样本 retest-R3"),
            ("cafe.txt", latin, "café au lait"),
        ):
            store.resolve_safe_path = MagicMock(return_value=target)
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                out = await plugin.file_read(rel)
            assert not out.startswith("Error"), out
            assert needle in out, "the text comes back, not mojibake"
            # Which encoding the detector names is the library's business; that
            # it says so at all is the contract — a silent wrong decode is the
            # outcome this refuses to risk.
            assert out.startswith("[decoded as "), out
            assert "the file is not utf-8" in out.splitlines()[0]

    @pytest.mark.asyncio
    async def test_a_utf16_file_with_a_bom_is_read(self, tmp_path):
        """PowerShell's ``>`` writes UTF-16LE with a BOM — text, carrying a NUL
        on every ASCII character, which is why the BOM is settled before the
        NUL test rather than after it."""
        mem_dir = tmp_path / "files"
        mem_dir.mkdir(parents=True, exist_ok=True)
        target = mem_dir / "ps.txt"
        target.write_bytes("第一行\r\nsecond line\r\n".encode("utf-16"))
        store, _ = _fake_store(mem_dir)
        store.resolve_safe_path = MagicMock(return_value=target)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.file_read("ps.txt")
        assert "第一行" in out and "second line" in out
        assert out.startswith("[decoded as utf-16")

    @pytest.mark.asyncio
    async def test_utf8_is_returned_verbatim_with_no_header(self, tmp_path):
        """The common case stays exactly what it was: the file's bytes as text,
        and not a word about encodings.  A UTF-8 BOM is not text either."""
        mem_dir = tmp_path / "files"
        mem_dir.mkdir(parents=True, exist_ok=True)
        for name, raw, expected in (
            ("plain.txt", "中文 plain\n".encode("utf-8"), "中文 plain\n"),
            ("bom.txt", codecs.BOM_UTF8 + "中文 bom\n".encode("utf-8"), "中文 bom\n"),
        ):
            target = mem_dir / name
            target.write_bytes(raw)
            store, _ = _fake_store(mem_dir)
            store.resolve_safe_path = MagicMock(return_value=target)
            with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
                out = await plugin.file_read(name)
            assert out == expected, name


class TestNoteDiaryBrowse:
    """note_list / diary_list / note_read / diary_read — browse by key."""

    @pytest.mark.asyncio
    async def test_note_list(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
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
        store, _ = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.diary_list(since="2026-08-01", until="2026-08-31")
        data = json.loads(out)
        assert data["entries"][0]["date"] == "2026-08-15"
        store.diary_list.assert_awaited_once_with(
            since="2026-08-01", until="2026-08-31", limit=50, offset=0,
        )

    @pytest.mark.asyncio
    async def test_note_read(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.note_read(subject="subj")
        assert out == "# subj\n\nbody"

    @pytest.mark.asyncio
    async def test_note_read_missing(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        store.get_note = AsyncMock(return_value=None)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.note_read(subject="nope")
        assert out.startswith("Error: note not found")

    @pytest.mark.asyncio
    async def test_diary_read(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.diary_read(date="2026-08-15")
        assert out == "# 2026-08-15\n\nbody"

    @pytest.mark.asyncio
    async def test_diary_read_missing(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
        store.get_diary = AsyncMock(return_value=None)
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.diary_read(date="2020-01-01")
        assert out.startswith("Error: diary not found")

    @pytest.mark.asyncio
    async def test_file_list(self, tmp_path):
        store, _ = _fake_store(tmp_path / "files")
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
        store, _ = _fake_store(tmp_path / "files")
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
        store, _ = _fake_store(tmp_path / "files")
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
    LAN / cloud-metadata targets are refused (REVIEW §1-1).

    The rule has three steps, and which one judges a *name* depends on whether
    the machine's resolver tells the truth, so the tests say which machine they
    are on.  Neither fixture touches the network: a fake-ip machine never
    reaches the resolution step, and an honest one has it stubbed.
    """

    @pytest.fixture
    def honest(self, monkeypatch):
        """A machine whose resolver answers where a name really goes."""
        monkeypatch.setattr(net, "resolver_uses_fake_ip", lambda: False)

    @pytest.fixture
    def fake_ip(self, monkeypatch):
        """A machine behind a TUN proxy: the proxy answers every name, so an
        answer says nothing about the destination."""
        monkeypatch.setattr(net, "resolver_uses_fake_ip", lambda: True)

    @staticmethod
    def _resolves_to(monkeypatch, address: str) -> None:
        import socket as socket_mod

        monkeypatch.setattr(
            socket_mod,
            "getaddrinfo",
            lambda *a, **kw: [
                (socket_mod.AF_INET, socket_mod.SOCK_STREAM, 6, "", (address, 0)),
            ],
        )

    def test_allows_public_http(self):
        assert plugin._reject_non_public_url("http://8.8.8.8/x") is None
        assert plugin._reject_non_public_url("https://1.1.1.1") is None

    def test_a_public_name_is_allowed_on_a_fake_ip_machine(self, fake_ip):
        """The 2026-09-26 failure: every public URL was refused because the
        proxy's answer was read as "LAN or metadata".  The address is not
        evidence about the destination, so it is not consulted at all."""
        assert plugin._reject_non_public_url("https://example.com/") is None
        assert plugin._reject_non_public_url("https://docs.astral.sh/uv/") is None

    def test_a_lan_name_is_refused_on_an_honest_machine(self, honest, monkeypatch):
        self._resolves_to(monkeypatch, "192.168.1.5")
        err = plugin._reject_non_public_url("https://intranet.example.com/x")
        assert err and "192.168.1.5" in err

    def test_a_public_name_is_allowed_on_an_honest_machine(self, honest, monkeypatch):
        self._resolves_to(monkeypatch, "93.184.216.34")
        assert plugin._reject_non_public_url("https://example.com/") is None

    def test_literals_are_refused_on_every_machine(self, fake_ip):
        """An address the caller wrote *is* the destination, whatever the
        resolver does — including one inside the range this machine's proxy
        answers names from, which is the proxy's front door, not a host."""
        for url in (
            "http://127.0.0.1:8080/admin", "http://[::1]/x",
            "http://169.254.169.254/latest/meta-data/", "http://[fe80::1]/x",
            "http://10.0.0.1/", "http://192.168.1.1/", "http://172.16.0.1/",
            "http://[fd00::1]/x", "http://[fd00:ec2::254]/x",
            "http://198.18.0.1/x", "http://[2001:2::127]/x",
            "http://[fdfe:dcba:9876::1]/x", "http://[2001:db8::1]/x",
            "http://[::ffff:127.0.0.1]/x",   # an IPv4 address, IPv6 spelling
        ):
            assert plugin._reject_non_public_url(url), url

    def test_names_that_cannot_be_public_are_refused_without_dns(self, fake_ip):
        """Refused by name, so no resolver's answer can affect it.  A bare
        label carries an encoded literal the same way: 2130706433 is
        127.0.0.1, and a trailing dot is legal FQDN syntax for the same host."""
        for url in (
            "http://localhost/admin", "http://nas.lan/", "http://nas.lan./",
            "http://router.local/", "http://printer/", "http://foo.internal/",
            "http://metadata.google.internal/", "http://2130706433/",
        ):
            assert plugin._reject_non_public_url(url), url

    def test_rejects_non_http_schemes(self):
        assert plugin._reject_non_public_url("ftp://example.com/x")
        assert plugin._reject_non_public_url("file:///etc/passwd")
        assert plugin._reject_non_public_url("gopher://localhost/1")


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


async def _search(store, query, *, kind="all", mode="fts5", limit=20, **kw):
    """Run the leg *mode* names, the way the corpus exposes it.

    ``MemfilesStore.search`` is gone: the mode dispatch, the clamp, the gate
    and the fusion are the shared composition's job now, and a store-level
    test is about the LEG — the SQL, the columns, the window — so it names the
    leg it means instead of routing through a dispatcher that no longer owns
    the decision.
    """
    if mode == "grep":
        return await store.regex_hits(pattern=query, limit=limit, kind=kind, **kw)
    return await store.keyword_hits(query=query, limit=limit, kind=kind, **kw)


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
            memo = await store.add_file(title="memo", original_path="/x/m.txt",
                                        saved_path="m.txt", mime="text", size=2,
                                        tags="", summary="")
            # 4 embeddable docs, the summary-less memo among them: a file has
            # no content of its own, so its body — what every leg reads and the
            # drainer embeds — is what it IS (title, source path, saved path)
            # plus its summary.  The summary alone would not do: it is empty by
            # default, so a file would have nothing to embed or search until a
            # model described it.
            assert await store.count_unembedded() == 4
            docs = await store.get_unembedded_docs(10)
            assert {d["kind"] for d in docs} == {"note", "diary", "file"}
            assert all(d["text"] for d in docs)
            memo_doc = next(d for d in docs if d["kind"] == "file"
                            and d["doc_id"] == memo["doc_id"])
            assert memo_doc["text"] == "memo /x/m.txt m.txt"
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
    async def test_file_list_category_matches_literally(self, tmp_path):
        """``_slugify`` keeps ``_`` (a word character), and ``_`` is a LIKE
        wildcard — so an unescaped pattern listed the files of every
        slug-adjacent category beside the one that was asked for."""
        store = await _real_store(tmp_path)
        try:
            await store.add_file(title="a", original_path="/x/a",
                                 saved_path="files/my_docs/a.txt",
                                 mime="txt", size=1, tags="", summary="")
            await store.add_file(title="b", original_path="/x/b",
                                 saved_path="files/my-docs/b.txt",
                                 mime="txt", size=1, tags="", summary="")

            for category, expected in (("my_docs", "files/my_docs/a.txt"),
                                       ("my-docs", "files/my-docs/b.txt")):
                data = await store.file_list(category=category)
                assert data["total"] == 1, category
                assert data["entries"][0]["saved_path"] == expected
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

            early = await store.keyword_hits(
                query="asyncio", kind="diary", limit=20, until="last month")
            assert [h["id"] for h in early] == ["diary:1"]
            assert (await store.diary_list(until="last month"))["total"] == 1

            # The same axis from the other side, in both entry points.
            assert await store.keyword_hits(
                query="asyncio", kind="diary", limit=20, since="2020-06-01") == []
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
            hits = await _search(store, "asyncio", kind="note", mode="fts5")
            assert [h["id"] for h in hits] == ["note:1"]
            hits = await _search(store, "并发", kind="note", mode="fts5")  # CJK → LIKE
            assert hits == []  # no CJK content yet
            await store.upsert_note("并发编程", "Python 异步并发笔记", "")
            hits = await _search(store, "异步", kind="note", mode="fts5")
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
            hits = await _search(store, "子agent 委托", kind="note", mode="fts5")
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
            # The two legs fuse — and the fusion, the gate and the one embed
            # are the shared composition's, so a store test drives it the way
            # the tools do.  ``embed_query`` is gone: the composition embeds.
            legs = store.search_legs("all")
            manager = _stub_manager([0.1, 0.2, 0.3, 0.4])
            out = await run_search(
                legs, manager, query="python concurrency", mode="hybrid", limit=20)
            hits = out.results
            assert hits[0]["id"] == "note:1"
            assert all("rrf_score" in h for h in hits)
            assert out.ran_mode == "hybrid"
            # The closest hit is the note, and it carries a measured similarity;
            # a keyword-only hit does not, because nothing measured it.
            assert "similarity" in hits[0]

            # A window applies to BOTH legs.  The semantic leg cannot take one
            # in SQL — sqlite-vec forbids an auxiliary-column constraint inside
            # a KNN query — so it filters a widened pool in Python.  If it
            # ignored the window, this note (which HAS an embedding and is the
            # closest hit) would still come back through the semantic leg, and
            # a windowed search would silently answer with out-of-window rows.
            assert (await run_search(
                store.search_legs("all"), manager, query="python concurrency",
                mode="hybrid", limit=20, until="last month",
            )).results == []
            assert len((await run_search(
                store.search_legs("all"), manager, query="python concurrency",
                mode="hybrid", limit=20, since="last month",
            )).results) > 0
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
            assert len(await store.keyword_hits(query="asyncio",
                                          since="last month", **common)) == 1
            assert await store.keyword_hits(query="asyncio",
                                      until="last month", **common) == []
            assert len(await store.regex_hits(pattern="asyncio",
                                          since="last month", **common)) == 1
            assert await store.regex_hits(pattern="asyncio",
                                      until="last month", **common) == []
            # …including the CJK (LIKE) fallback.
            assert len(await store.keyword_hits(query="并发",
                                          since="last month", **common)) == 1
            assert await store.keyword_hits(query="并发",
                                      until="last month", **common) == []

            with pytest.raises(InvalidTimeBound):
                await store.keyword_hits(query="asyncio", since="昨天", **common)
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
            assert [h["id"] for h in await _search(store, 
                "asyncio", kind="report", mode="fts5")] == ["report:1"]
            assert [h["id"] for h in await _search(store, 
                "asyncio", kind="note", mode="fts5")] == ["note:1"]
            assert {h["id"] for h in await _search(store, 
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
            hits = await _search(store, "asyncio", kind="note", mode="fts5")
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
        store, _ = _fake_store(tmp_path / "files")
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


class TestStaleCabinet:
    """A cabinet from before the one-corpus index is named, not guessed at.

    There is no migration layer — a deliberate project rule — so an old database
    is deleted and rebuilt.  The schema is what fails (it creates a view over
    columns an old database does not have), and "no such column: summary" says
    nothing about what to do, so the store refuses it up front with the path and
    with what a rebuild costs."""

    @pytest.mark.asyncio
    async def test_a_pre_one_corpus_db_is_refused_with_the_path(self, tmp_path):
        db = tmp_path / ".index.db"
        old = sqlite3.connect(db)
        # The shape this DB had before: no summary column on the kinds.
        old.execute(
            "CREATE TABLE notes (id INTEGER PRIMARY KEY, subject TEXT NOT NULL "
            "UNIQUE, content TEXT NOT NULL, tags TEXT DEFAULT '', "
            "file_path TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
        old.commit()
        old.close()

        store = MemfilesStore(db)
        with pytest.raises(RuntimeError) as err:
            await store.setup(embedding_dim=0, embedding_model="")
        message = str(err.value)
        assert str(db) in message, "it names the file to delete"
        assert "scheduled tasks" in message, "and what a rebuild would cost"

    @pytest.mark.asyncio
    async def test_a_fresh_or_current_db_is_left_alone(self, tmp_path):
        # Nothing on disk: the schema builds it.
        store = MemfilesStore(tmp_path / ".index.db")
        await store.setup(embedding_dim=0, embedding_model="")
        await store.upsert_note("Python", "asyncio", "")
        await store.close()
        # …and a second start over that same (now current) database is fine.
        again = MemfilesStore(tmp_path / ".index.db")
        await again.setup(embedding_dim=0, embedding_model="")
        assert (await again.get_note("Python")) is not None
        await again.close()


class TestCabinetSummarize:
    """The cabinet's counterpart of ``turn_summarize``: annotate a saved row.

    What it can reach differs by kind, and that follows from which column holds
    the text a search matches — a file's summary IS its vector's text, while a
    note/diary/report embeds its content, which its save tool already wrote.
    """

    @pytest.mark.asyncio
    async def test_a_file_summary_makes_it_searchable_and_reembeddable(
        self, tmp_path,
    ):
        """A file saved without a summary is absent from semantic search
        forever — the drainer only embeds rows whose summary is non-empty, and
        ``file_save`` was the only writer.  This is what closes that."""
        store = await _real_store(tmp_path, require_vec=True)
        try:
            await store.add_file(title="paper", original_path="/dl/paper.pdf",
                                 saved_path="files/documents/paper.pdf",
                                 mime="application/pdf", size=10, tags="",
                                 summary="")
            # No summary yet, so "survey" matches nothing — but the file is
            # not a blank: its identity is already its text, in the vector and
            # in the keyword index both.
            assert await store.keyword_hits(
                query="survey", kind="file", limit=20) == []
            assert (await store.get_unembedded_docs(10))[0]["text"] == (
                "paper /dl/paper.pdf files/documents/paper.pdf")
            assert [h["id"] for h in await store.keyword_hits(
                query="paper", kind="file", limit=20)] == ["file:1"]
            assert [h["id"] for h in await store.regex_hits(
                pattern=r"paper\.pdf", limit=20)] == ["file:1"]

            await store.summarize("file", "files/documents/paper.pdf",
                                  summary="a survey of retrieval", tags="nlp")
            # The AFTER UPDATE trigger carried it into the one index…
            assert [h["id"] for h in await store.keyword_hits(
                query="survey", kind="file", limit=20)] == ["file:1"]
            # …and it is now the drainer's to embed.
            assert await store.count_unembedded() == 1
            docs = await store.get_unembedded_docs(10)
            assert docs[0]["text"] == (
                "paper /dl/paper.pdf files/documents/paper.pdf "
                "a survey of retrieval"), "identity, then the summary"
            assert docs[0]["kind"] == "file"
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_rewriting_a_summary_drops_the_stale_vector(self, tmp_path):
        """The vector was built from the OLD text, so keeping it would leave a
        row matching its previous summary — a stale index nothing would ever
        correct."""
        store = await _real_store(tmp_path, require_vec=True)
        try:
            await store.add_file(title="paper", original_path="/dl/paper.pdf",
                                 saved_path="p.pdf", mime="application/pdf",
                                 size=10, tags="", summary="first text")
            doc = (await store.get_unembedded_docs(10))[0]
            await store.replace_embedding_chunks(doc, [[0.1, 0.2, 0.3, 0.4]])
            assert await store.count_unembedded() == 0

            await store.summarize("file", "p.pdf", summary="second text")
            assert await store.count_unembedded() == 1, "chunks must be cleared"
            assert (await store.get_unembedded_docs(10))[0]["text"] == (
                "paper /dl/paper.pdf p.pdf second text")
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_tags_are_annotatable_on_every_kind(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("Python", "asyncio notes", "")
            await store.upsert_diary("2026-08-15", "refactor day", "")
            await store.upsert_report(None, "Weekly", "all good", "")

            await store.summarize("note", "Python", tags="py,async")
            await store.summarize("diary", "2026-08-15", tags="work")
            await store.summarize("report", "1", tags="weekly")

            assert [h["id"] for h in await store.keyword_hits(
                query="async", kind="note", limit=20)] == ["note:1"]
            assert [h["id"] for h in await store.keyword_hits(
                query="work", kind="diary", limit=20)] == ["diary:1"]
            assert [h["id"] for h in await store.keyword_hits(
                query="weekly", kind="report", limit=20)] == ["report:1"]
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_a_summary_is_taken_by_every_kind(self, tmp_path):
        """A summary is the LLM's abstract of the row, and a summary is what
        a KEYWORD search reads — the same division memdb draws, where a turn's
        vector is built from the conversation and its summary is there to make
        the turn findable by words."""
        store = await _real_store(tmp_path, require_vec=True)
        try:
            await store.upsert_note("Python", "asyncio notes", "")
            doc = (await store.get_unembedded_docs(10))[0]
            await store.replace_embedding_chunks(doc, [[0.1, 0.2, 0.3, 0.4]])
            assert await store.count_unembedded() == 0, "indexed"

            await store.summarize("note", "Python",
                                  summary="an asyncio retrospective")
            # Keyword: the summary went into the one index…
            assert [h["id"] for h in await store.keyword_hits(
                query="retrospective", kind="note", limit=20)] == ["note:1"]
            # …and NOT into the vector, so the stored one is still current and
            # the row is NOT re-queued.  A note is embedded from its own text,
            # exactly as a turn is embedded from the conversation; only a file
            # (whose summary IS its text) is re-embedded by an annotation.
            assert await store.count_unembedded() == 0
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_an_unknown_key_or_an_empty_call_is_refused(self, tmp_path):
        store = await _real_store(tmp_path)
        try:
            await store.upsert_note("Python", "asyncio notes", "")
            with pytest.raises(ValueError) as err:
                await store.summarize("note", "Nope", tags="x")
            assert "not found" in str(err.value)

            with pytest.raises(ValueError) as err:
                await store.summarize("note", "Python")
            assert "nothing to write" in str(err.value)

            with pytest.raises(ValueError) as err:
                await store.summarize("nonsense", "Python", tags="x")
            assert "kind must be one of" in str(err.value)

            # A report's key is its numeric id, and saying so beats comparing
            # text against an INTEGER column and reporting nothing matched.
            with pytest.raises(ValueError) as err:
                await store.summarize("report", "Weekly", tags="x")
            assert "numeric report_id" in str(err.value)
        finally:
            await store.close()

    @pytest.mark.asyncio
    async def test_the_tool_reports_a_missing_row_as_a_failure(self, tmp_path):
        """``is_error`` keys on the "Error" prefix, so a refusal has to be a
        plain string — and a success an envelope."""
        store, _ = _fake_store(tmp_path / "files")
        store.summarize = AsyncMock(return_value={
            "kind": "file", "id": 3, "key": "files/documents/a.txt",
            "tags": "nlp", "summary": "a survey", "reembedded": True})
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)), \
             patch.object(plugin, "_manager", _ready_manager()):
            out = await plugin.cabinet_summarize(
                kind="file", key="files/documents/a.txt",
                summary="a survey", tags="nlp")
        data = json.loads(out)
        assert data["status"] == "updated"
        assert data["key"] == "files/documents/a.txt"
        assert store.summarize.await_args.kwargs == {
            "summary": "a survey", "tags": "nlp"}

        store.summarize = AsyncMock(side_effect=ValueError("file not found — x"))
        with patch.object(plugin, "_ensure_store", AsyncMock(return_value=store)):
            out = await plugin.cabinet_summarize(kind="file", key="x", tags="t")
        assert out.startswith("Error")
        assert "not found" in out


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

        assert [r["id"] for r in await _search(store, "depl.y", mode="grep")] == ["note:1"]
        # A tag, a title and a summary are how a row is named and classified;
        # grep reads what it SAYS, so none of them matches here — the summary
        # is findable by keyword search and not by grep, exactly as in memdb.
        await store.summarize("note", "Deploy runbook", tags="deployish",
                              summary="deployish")
        # A SUMMARY is the one column a model wrote, and it is the keyword
        # leg's; a TITLE is the row's own name, so grep reads it.
        assert await _search(store, "deployish", mode="grep") == []
        assert len(await _search(store, "deployish", mode="fts5")) == 1
        runbooks = await _search(store, "runbook", mode="grep")
        assert [r["id"] for r in runbooks] == ["note:1"], "the title matched"
        # Alternation and a wildcard — the two things LIKE cannot express.
        assert len(await _search(store, "runbook|noth.ng", mode="grep")) == 2
        # …and the FTS path cannot do it, which is why the mode exists.
        assert await _search(store, "depl.y", mode="fts5") == []
        await store.close()

    @pytest.mark.asyncio
    async def test_an_invalid_pattern_raises(self, tmp_path):
        store = await _real_store(tmp_path, dim=0)
        with pytest.raises(re.error):
            await _search(store, "a(b", mode="grep")
        await store.close()
