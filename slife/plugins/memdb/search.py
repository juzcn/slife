"""Hybrid search — merges keyword (FTS5) and semantic (vec0) results.

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
    key_field: str = "turn_id",
) -> list[dict]:
    """Merge keyword and semantic search results using RRF.

    Results that appear high in BOTH lists get the highest scores.
    Results in only one list still get a reasonable score.

    Field-agnostic: the two lists are aligned by ``key_field`` and each
    result entry passes through with its own fields — the only fields added
    are the RRF annotations.  memdb aligns turns on ``turn_id``; memfiles
    aligns notes/diary/files on a composite ``id`` (``"note:5"`` etc.).

    Args:
        keyword_results: keyword (FTS5) results, each with ``key_field``
            and whatever caller fields it carries (``rank`` for FTS5).
        semantic_results: vec0 results, each with ``key_field`` and
            ``distance``.
        k: RRF smoothing constant (default 60).
        key_field: field holding the identity used to align the two lists
            (default ``"turn_id"``).

    Returns:
        Merged list sorted by RRF score descending. Each entry carries the
        union of its source item's fields, plus:
        - rrf_score: combined RRF score
        - keyword_rank: 1-based rank in keyword results (or None)
        - semantic_rank: 1-based rank in semantic results (or None)
        - snippet: from keyword search ("" if semantic-only)
        - distance: cosine distance from semantic search (None if keyword-only)
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
