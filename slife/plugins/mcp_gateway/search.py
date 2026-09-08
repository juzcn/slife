"""Hybrid search — shared RRF implementation (from the memdb plugin).

The Reciprocal Rank Fusion (RRF) merge, the 0–1 score annotator, and the
score-band guidance are identical across every hybrid retrieval path in
slife — turn search (memdb), cabinet search (memfiles), tool search (the
MCP gateway) — and memfiles already shares the memdb implementation.
This module is a thin adapter so the gateway stays on one implementation
instead of a drifting copy (review E6).

The one divergence: tool search computes COSINE distances (not vec0/L2), so
the annotator's default metric here is *cosine* — the memdb default (l2)
only applies to vec0-based stores.
"""



from slife.plugins.memdb.search import (  # noqa: F401  (re-exported)
    RRF_K,
    SCORE_BAND_HINT,
    annotate_scores as _annotate_scores_memdb,
    merge_hybrid,
)


def annotate_scores(results: list[dict], metric: str = "cosine") -> list[dict]:
    """Add the normalized 0–1 ``similarity`` (tool search = cosine by default).

    The gateway embeds tool names/descriptions as cosine distances, so
    ``metric`` defaults to ``"cosine"`` here — unlike the shared memdb
    default (``"l2"`` for vec0 stores).  Callers may still override.
    """
    return _annotate_scores_memdb(results, metric=metric)


__all__ = [
    "RRF_K",
    "SCORE_BAND_HINT",
    "annotate_scores",
    "merge_hybrid",
]