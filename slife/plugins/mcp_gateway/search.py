"""Hybrid search — shared RRF implementation (from the memdb plugin).

The Reciprocal Rank Fusion (RRF) merge, the 0–1 score annotator, and the
score-band guidance are identical across every hybrid retrieval path in
slife — turn search (memdb), cabinet search (memfiles), tool search (the
MCP gateway) — and memfiles already shares the memdb implementation.
This module is a thin re-export so the gateway stays on one implementation
instead of a drifting copy (review E6).
"""

from slife.plugins.memdb.search import (  # noqa: F401  (re-exported)
    RRF_K,
    SCORE_BAND_HINT,
    annotate_scores,
    merge_hybrid,
)

__all__ = [
    "RRF_K",
    "SCORE_BAND_HINT",
    "annotate_scores",
    "merge_hybrid",
]