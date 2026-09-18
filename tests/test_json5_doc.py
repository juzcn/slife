"""Comment-preserving JSON5 writes — ``_json5_doc`` and ``write_config``.

The regression these exist for is concrete: a tool write used to re-serialize
the config from a dict, so every ``//`` annotation died.  On 2026-09-18 that
erased all 94 comment lines of ``tools.json5`` (the ``enabled``/``autoload``
header and every section's notes) after an ``mcp_set``.
"""

import pytest; pytestmark = pytest.mark.unit

import json5
from pathlib import Path

from slife.tools._config_io import read_config, write_config
from slife.tools._json5_doc import render, render_document

DOC = """\
// ═══════════════════════════════════════════════════════════════
//  the documentation header — this is what a write used to destroy
//    enabled:  false = off
//    autoload: true = born loaded
// ═══════════════════════════════════════════════════════════════

{
  // ── servers ─────────────────────────────────────────────────
  mcp: {
    servers: {
      // the first one
      fs: {
        command: "npx",          // how it starts
        enabled: true
      },
      "rest-api": {
        enabled: false
      }
    }
  },

  // ── the knob ────────────────────────────────────────────────
  tool_load: 12
}
"""


def _comments(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip().startswith("//"))


class TestRender:
    """The dict → JSON5 text renderer (replaces ``json5.dumps(dict)``)."""

    def test_bare_keys_and_quoted_where_illegal(self):
        out = render({"a": 1, "rest-api": 2})
        assert "a: 1" in out
        assert '"rest-api": 2' in out

    def test_non_ascii_stays_literal(self):
        """json-five's own dumper escapes it (\\u4e2d\\u6587) — configs carry
        Chinese and must not change character between writes."""
        out = render({"desc": "中文 — 破折号"})
        assert "中文 — 破折号" in out
        assert "\\u" not in out

    def test_no_trailing_commas(self):
        """The old call was ``dumps(..., trailing_commas=False)``."""
        out = render({"a": {"b": 1}, "c": [1, 2]})
        assert ",}" not in out.replace(" ", "").replace("\n", "")
        assert ",]" not in out.replace(" ", "").replace("\n", "")

    def test_matches_the_shape_configs_already_have(self):
        assert render({"tool_load": 12}) == '{\n  tool_load: 12\n}'

    def test_empty_containers(self):
        assert render({}) == "{}"
        assert render({"a": [], "b": {}}) == '{\n  a: [],\n  b: {}\n}'


class TestRenderDocument:
    """Editing the document instead of re-serializing it."""

    def test_untouched_document_is_byte_identical(self):
        assert render_document(DOC, json5.loads(DOC)) == DOC

    def test_scalar_change_keeps_every_comment(self):
        new = json5.loads(DOC)
        new["mcp"]["servers"]["fs"]["enabled"] = False
        out = render_document(DOC, new)
        assert json5.loads(out) == new
        assert _comments(out) == _comments(DOC)
        assert "the documentation header" in out
        assert "// how it starts" in out          # the trailing comment stays put

    def test_added_member_is_formatted_and_comments_survive(self):
        new = json5.loads(DOC)
        new["mcp"]["servers"]["probe-srv"] = {"command": "echo", "args": ["-y", "中文"]}
        out = render_document(DOC, new)
        assert json5.loads(out) == new
        assert _comments(out) == _comments(DOC)
        # Formatted like its siblings, not emitted bare on one line.
        assert '      "probe-srv": {\n' in out
        assert "中文" in out

    def test_removed_member_keeps_the_document_intact(self):
        new = json5.loads(DOC)
        del new["mcp"]["servers"]["fs"]
        out = render_document(DOC, new)
        assert json5.loads(out) == new
        assert _comments(out) == _comments(DOC)

    def test_nested_change_keeps_sibling_comments(self):
        new = json5.loads(DOC)
        new["mcp"]["servers"]["fs"]["command"] = "bun"
        out = render_document(DOC, new)
        assert json5.loads(out) == new
        assert "// the first one" in out
        assert "// ── the knob" in out

    def test_emptying_the_document_is_still_valid(self):
        out = render_document(DOC, {})
        assert json5.loads(out) == {}

    def test_unparseable_current_falls_back_to_a_plain_render(self):
        out = render_document("{ this is not json5", {"a": 1})
        assert json5.loads(out) == {"a": 1}

    def test_empty_current_renders_fresh(self):
        assert json5.loads(render_document("", {"a": [1, 2]})) == {"a": [1, 2]}

    def test_a_non_document_current_falls_back(self):
        """A JSON5 *list* is not a config document — no delta to apply."""
        out = render_document("[1, 2, 3]", {"a": 1})
        assert json5.loads(out) == {"a": 1}


class TestWriteConfigKeepsComments:
    """The integration: a tool write through the real writer."""

    def test_write_preserves_comments_on_disk(self, tmp_path: Path):
        p = tmp_path / "tools.json5"
        p.write_text(DOC, encoding="utf-8")

        cfg = read_config(p)
        cfg["mcp"]["servers"]["fs"]["enabled"] = False
        write_config(p, cfg)

        on_disk = p.read_text(encoding="utf-8")
        assert _comments(on_disk) == _comments(DOC)
        assert "the documentation header" in on_disk
        assert read_config(p) == cfg

    def test_a_brand_new_file_has_no_comments_to_keep(self, tmp_path: Path):
        p = tmp_path / "fresh.json5"
        write_config(p, {"a": {"b": 1}})
        assert read_config(p) == {"a": {"b": 1}}
        assert p.read_text(encoding="utf-8").startswith("{\n")

    def test_round_trips_through_repeated_writes(self, tmp_path: Path):
        """A config edited many times over a session must not decay."""
        p = tmp_path / "tools.json5"
        p.write_text(DOC, encoding="utf-8")
        for i in range(5):
            cfg = read_config(p)
            cfg["mcp"]["servers"][f"svc{i}"] = {"command": "echo", "n": i}
            write_config(p, cfg)
        on_disk = p.read_text(encoding="utf-8")
        assert _comments(on_disk) == _comments(DOC)
        assert read_config(p)["mcp"]["servers"]["svc4"] == {"command": "echo", "n": 4}
