"""Drift gate: the two copies of the embedding model table must agree.

``slife.plugins.memdb.embeddings`` and ``local_embed.engine`` each carry a
model-family → (dimension, max_tokens) table, and neither can import the
other: slife only ever SPAWNS local-embed (see pyproject — "never imported"),
and local-embed is a standalone distribution that cannot depend on slife.

They still describe the same six models, and one half of the table is a real
coupling: local-embed REJECTS input over the token limit while slife PREDICTS
with its copy to skip a text it believes is too long — a divergence makes the
client skip text the server would have accepted, or send text it refuses.

The dimension half is asymmetric, and worth stating so nobody "fixes" the wrong
side: local-embed re-reads it from the loaded weights, so its copy is a hint;
slife treats a recognised family as known and skips the probe that would
correct it, so there a wrong width sizes vec0 wrong and silently drops inserts.

Read through the AST rather than importing, which is what keeps this check
inside the boundary above (the same way ``test_no_magic_timeouts`` reads
sources instead of running them).
"""
from __future__ import annotations

import pytest; pytestmark = pytest.mark.unit

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: The two copies, and whether a missing one is a failure or a skip.  The
#: slife copy always ships; the local-embed SOURCE is absent when slife is
#: installed from a wheel without the workspace, so it skips there.
SOURCES = {
    "slife": ROOT / "slife" / "plugins" / "memdb" / "embeddings.py",
    "local-embed": ROOT / "local-embed" / "local_embed" / "engine.py",
}

#: The names that make up the table — the mapping and its two fallbacks.
WANTED = ("_KNOWN_MODELS", "_DEFAULT_DIM", "_DEFAULT_MAX_TOKENS")


def _literals(path: Path) -> dict:
    """The module-level constants in WANTED, evaluated from the AST."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names = [node.target.id]
        else:
            continue
        for name in names:
            if name in WANTED:
                found[name] = ast.literal_eval(node.value)
    return found


def test_both_copies_of_the_embedding_table_agree():
    missing = {k: p for k, p in SOURCES.items() if not p.exists()}
    if "slife" in missing:
        pytest.fail(f"the slife table is missing: {missing['slife']}")
    if missing:
        pytest.skip(f"source not in this checkout: {sorted(missing)}")

    slife = _literals(SOURCES["slife"])
    local = _literals(SOURCES["local-embed"])

    assert set(slife) == set(WANTED), f"missing from slife: {set(WANTED) - set(slife)}"
    assert set(local) == set(WANTED), f"missing from local-embed: {set(WANTED) - set(local)}"
    # Field by field, so a failure names the value rather than printing two
    # whole dicts.
    for name in WANTED:
        assert slife[name] == local[name], (
            f"{name} drifted between slife and local-embed:\n"
            f"  slife       = {slife[name]!r}\n"
            f"  local-embed = {local[name]!r}"
        )
