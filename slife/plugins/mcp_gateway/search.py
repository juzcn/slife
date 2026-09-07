"""Hybrid search — merges keyword (FTS5/LIKE) and semantic (cosine) results.

Uses Reciprocal Rank Fusion (RRF) — a simple, parameter-free algorithm
that combines ranked lists without needing to tune weights.
"""

import logging
from typing import Any

logger = logging.getLogger(__name__)

# RRF smoothing constant. Higher = less influence from rank position.
# 60 is the standard value from the literature.
RRF_K = 60


def merge_hybrid(
    keyword_results: list[dict],
    semantic_results: list[dict],
    k: int = RRF_K,
    key_field: str = "full_name",
) -> list[dict]:
    """Merge keyword and semantic search results using RRF.

    Results that appear high in BOTH lists get the highest scores.
    Results in only one list still get a reasonable score.

    Field-agnostic: the two lists are aligned by ``key_field`` and each
    result entry passes through with its own fields — the only fields added
    are the RRF annotations.  Tool search aligns on ``full_name``
    (``"{server}__{tool}"``).

    Args:
        keyword_results: keyword (FTS5) results, each with ``key_field``
            and whatever caller fields it carries (``rank`` for FTS5).
        semantic_results: cosine results, each with ``key_field`` and
            ``distance``.
        k: RRF smoothing constant (default 60).
        key_field: field holding the identity used to align the two lists
            (default ``"full_name"``).

    Returns:
        Merged list sorted by RRF score descending. Each entry carries the
        union of its source item's fields, plus ``rrf_score``,
        ``keyword_rank``, ``semantic_rank``, ``snippet`` and ``distance``.
    """
    scores: dict[Any, float] = {}
    info: dict[Any, dict] = {}

    # Keyword contributions
    for rank, item in enumerate(keyword_results, start=1):
        key = item.get(key_field)
        if key is None:
            continue
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        if key not in info:
            entry = dict(item)
            entry.setdefault("snippet", "")
            entry.setdefault("distance", None)
            entry["keyword_rank"] = rank
            entry["semantic_rank"] = None
            info[key] = entry
        else:
            info[key]["keyword_rank"] = rank

    # Semantic contributions
    for rank, item in enumerate(semantic_results, start=1):
        key = item.get(key_field)
        if key is None:
            continue
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        if key not in info:
            entry = dict(item)
            entry.setdefault("snippet", "")
            entry["keyword_rank"] = None
            entry["semantic_rank"] = rank
            info[key] = entry
        else:
            info[key]["semantic_rank"] = rank
            info[key]["distance"] = item.get("distance")

    # Sort by RRF score descending
    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    results = []
    for key, rrf_score in merged:
        entry = info[key]
        entry["rrf_score"] = round(rrf_score, 6)
        results.append(entry)

    logger.debug(
        "rrf_merge keyword=%d semantic=%d merged=%d",
        len(keyword_results), len(semantic_results), len(results),
    )
    return results


#: Shared 0–1 score guidance appended to hybrid-search hints.  Mirrors the
#: slife-side contract (slife.plugins.memdb.search) so the normalized score
#: reads identically on mcp_tool_search / turn_search / cabinet_search.
SCORE_BAND_HINT = (
    "similarity is a normalized 0–1 readout (higher = more relevant; "
    "≈1 identical, ≥0.5 close, 0.1–0.5 weak, <0.1 mostly unrelated) — "
    "compare within one result set, not across embedding backends"
)


def annotate_scores(results: list[dict]) -> list[dict]:
    """Add a normalized 0–1 ``similarity`` next to each result's raw
    ``distance`` (mutates *results* in place, returns it for chaining).

    Tool search computes **cosine** distances (``_cosine_distance``), so
    the similarity is the true cosine similarity, ``max(0, 1-d)`` — the
    cosine branch of the slife-side contract.  Keyword-only results
    (``distance`` None) get no ``similarity`` key.  The mapping is
    strictly monotonic, so ranking is preserved; it only rescales the
    raw distance onto a readable 0–1 axis.
    """
    for r in results:
        d = r.get("distance")
        if d is None:
            continue
        r["similarity"] = round(max(0.0, 1.0 - d), 4)
    return results
