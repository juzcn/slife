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

import ast
import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]

#: slife's own copy — this tree always has it.
SLIFE_TABLE = ROOT / "slife" / "plugins" / "memdb" / "embeddings.py"


def _local_embed_engine() -> Path:
    """The installed ``local_embed/engine.py``, located without importing it.

    local-embed ships from PyPI now, so the table is read from the installed
    distribution rather than a source directory in this repo.  ``find_spec``
    on the top-level name locates the package without executing it — importing
    a *submodule* would import the parent, which would break the no-import
    boundary this gate is built on (see the module docstring).
    """
    spec = importlib.util.find_spec("local_embed")
    if spec is None or not spec.submodule_search_locations:
        pytest.fail("local-embed is not installed (a runtime dependency of slife)")
    return Path(next(iter(spec.submodule_search_locations))) / "engine.py"


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
    engine = _local_embed_engine()
    if not engine.is_file():
        pytest.fail(f"the local-embed table is missing: {engine}")

    slife = _literals(SLIFE_TABLE)
    local = _literals(engine)

    assert set(slife) == set(WANTED), f"missing from slife: {set(WANTED) - set(slife)}"
    assert set(local) == set(WANTED), (
        f"missing from local-embed: {set(WANTED) - set(local)}"
    )
    # Field by field, so a failure names the value rather than printing two
    # whole dicts.
    for name in WANTED:
        assert slife[name] == local[name], (
            f"{name} drifted between slife and local-embed:\n"
            f"  slife       = {slife[name]!r}\n"
            f"  local-embed = {local[name]!r}"
        )
