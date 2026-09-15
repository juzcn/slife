"""Hybrid search adapter for the unified tool catalog.

The RRF merge and 0–1 score annotator are shared with every other hybrid
retrieval path in slife (memdb turns, memfiles cabinet) — this module is
a thin adapter so the host catalog stays on the one implementation
(memdb.search) instead of a drifting copy.  The tool catalog computes
COSINE distances (not vec0/L2), so the annotator's default metric here is
*cosine* — the memdb default (l2) only applies to vec0-based stores.
"""

from __future__ import annotations

from slife.plugins.memdb.search import (  # noqa: F401  (re-exported)
    RRF_K,
    SCORE_BAND_HINT,
    annotate_scores as _annotate_scores_memdb,
    merge_hybrid,
)


def annotate_scores(results: list[dict], metric: str = "cosine") -> list[dict]:
    """Add the normalized 0–1 ``similarity`` — cosine by default here."""
    return _annotate_scores_memdb(results, metric=metric)


__all__ = [
    "RRF_K",
    "SCORE_BAND_HINT",
    "annotate_scores",
    "merge_hybrid",
]