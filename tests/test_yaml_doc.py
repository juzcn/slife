"""Comment-preserving YAML writes — ``_yaml_doc`` and ``write_config``.

The regression these exist for is concrete: a tool write used to re-serialize
the config from a dict, so every ``#`` annotation died.  On 2026-09-18 that
erased all 94 comment lines of ``tools.json5`` (the ``enabled``/``autoload``
header and every section's notes) after an ``mcp_set``.
"""

import pytest; pytestmark = pytest.mark.unit

from pathlib import Path

from slife.tools._config_io import read_config, write_config
from slife.tools._yaml_doc import render, render_document
from tests.conftest import load_config_text

DOC = """\
# ═══════════════════════════════════════════════════════════════
#  the documentation header — this is what a write used to destroy
#    enabled:  false = off
#    autoload: true = born loaded
# ═══════════════════════════════════════════════════════════════

# ── servers ─────────────────────────────────────────────────
mcp:
  servers:
    # the first one
    fs:
      command: npx          # how it starts
      enabled: true
    rest-api:
      enabled: false

# ── the knob ────────────────────────────────────────────────
tool_load: 12
"""


def _comments(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip().startswith("#"))


class TestRender:
    """The dict → YAML text renderer (replaces ``json5.dumps(dict)``)."""

    def test_round_trips_a_dict(self):
        data = {
            "tool_load": 12,
            "mcp": {"servers": {"fs": {"command": "npx", "args": ["-y", "中文"]}}},
        }
        assert load_config_text(render(data)) == data

    def test_bare_keys_and_block_style(self):
        """The configs are hand-edited documents, not JSON — no flow braces
        around a mapping and no quoting of an ordinary key."""
        out = render({"a": 1, "rest-api": 2})
        assert "a: 1" in out
        assert "rest-api: 2" in out
        assert "{" not in out and "}" not in out

    def test_nested_mapping_and_sequence_indentation(self):
        assert render({"a": {"b": 1}, "c": [1, 2]}) == (
            "a:\n  b: 1\nc:\n  - 1\n  - 2\n"
        )

    def test_non_ascii_stays_literal(self):
        """json-five's own dumper escaped it (\\u4e2d\\u6587) — configs carry
        Chinese and must not change character between writes."""
        out = render({"desc": "中文 — 破折号"})
        assert "中文 — 破折号" in out
        assert "\\u" not in out

    def test_matches_the_shape_configs_already_have(self):
        assert render({"tool_load": 12}) == "tool_load: 12\n"

    def test_empty_containers(self):
        assert render({}) == "{}\n"
        assert render({"a": [], "b": {}}) == "a: []\nb: {}\n"


class TestRenderDocument:
    """Editing the document instead of re-serializing it."""

    def test_untouched_document_is_byte_identical(self):
        assert render_document(DOC, load_config_text(DOC)) == DOC

    def test_comment_survives_an_unrelated_key_change(self):
        new = load_config_text(DOC)
        new["tool_load"] = 20
        out = render_document(DOC, new)
        assert load_config_text(out) == new
        assert _comments(out) == _comments(DOC)
        assert "the documentation header" in out
        assert "tool_load: 20" in out

    def test_changing_a_sibling_keeps_a_trailing_comment_in_place(self):
        new = load_config_text(DOC)
        new["mcp"]["servers"]["fs"]["enabled"] = False
        out = render_document(DOC, new)
        assert load_config_text(out) == new
        assert _comments(out) == _comments(DOC)
        assert "command: npx          # how it starts" in out

    def test_added_nested_key_is_indented_correctly(self):
        new = load_config_text(DOC)
        new["mcp"]["servers"]["probe-srv"] = {"command": "echo", "args": ["-y", "中文"]}
        out = render_document(DOC, new)
        assert load_config_text(out) == new
        assert _comments(out) == _comments(DOC)
        # Nested one level under ``servers`` like its siblings, and formatted
        # over lines — not emitted bare or at the wrong depth.
        assert "\n    probe-srv:\n" in out
        assert "\n      command: echo\n" in out
        assert "\n        - 中文\n" in out

    def test_removed_key_goes(self):
        new = load_config_text(DOC)
        del new["mcp"]["servers"]["fs"]
        out = render_document(DOC, new)
        assert load_config_text(out) == new
        assert "fs:" not in out
        assert _comments(out) == _comments(DOC)

    def test_nested_change_keeps_sibling_comments(self):
        new = load_config_text(DOC)
        new["mcp"]["servers"]["fs"]["command"] = "bun"
        out = render_document(DOC, new)
        assert load_config_text(out) == new
        assert "command: bun" in out
        assert "# the first one" in out
        assert "# ── the knob" in out

    def test_emptying_the_document_is_still_valid(self):
        out = render_document(DOC, {})
        assert load_config_text(out) == {}
        # The document's own header belongs to no key, so it survives; the
        # per-key comments go with the keys they were written beside.
        assert "the documentation header" in out
        assert "tool_load" not in out

    def test_unparseable_current_falls_back_to_a_plain_render(self):
        out = render_document("key: [unclosed", {"a": 1})
        assert load_config_text(out) == {"a": 1}

    def test_empty_current_renders_fresh(self):
        assert load_config_text(render_document("", {"a": [1, 2]})) == {"a": [1, 2]}

    def test_a_non_document_current_falls_back(self):
        """A YAML *list* is not a config document — no delta to apply."""
        out = render_document("- a\n- b\n", {"a": 1})
        assert load_config_text(out) == {"a": 1}


class TestWriteConfigKeepsComments:
    """The integration: a tool write through the real writer."""

    def test_write_preserves_comments_on_disk(self, tmp_path: Path):
        p = tmp_path / "tools.yaml"
        p.write_text(DOC, encoding="utf-8")

        cfg = read_config(p)
        cfg["mcp"]["servers"]["fs"]["enabled"] = False
        write_config(p, cfg)

        on_disk = p.read_text(encoding="utf-8")
        assert _comments(on_disk) == _comments(DOC)
        assert "the documentation header" in on_disk
        assert read_config(p) == cfg

    def test_a_brand_new_file_has_no_comments_to_keep(self, tmp_path: Path):
        p = tmp_path / "fresh.yaml"
        write_config(p, {"a": {"b": 1}})
        assert read_config(p) == {"a": {"b": 1}}
        assert p.read_text(encoding="utf-8") == "a:\n  b: 1\n"

    def test_round_trips_through_repeated_writes(self, tmp_path: Path):
        """A config edited many times over a session must not decay."""
        p = tmp_path / "tools.yaml"
        p.write_text(DOC, encoding="utf-8")
        for i in range(5):
            cfg = read_config(p)
            cfg["mcp"]["servers"][f"svc{i}"] = {"command": "echo", "n": i}
            write_config(p, cfg)
        on_disk = p.read_text(encoding="utf-8")
        assert _comments(on_disk) == _comments(DOC)
        assert read_config(p)["mcp"]["servers"]["svc4"] == {"command": "echo", "n": 4}
