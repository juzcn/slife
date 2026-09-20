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

#: Shared 0–1 score guidance, appended to hybrid-search hints.  ONE wording
#: and ONE implementation (``annotate_scores`` below) for every hybrid
#: retrieval path — ``turn_search`` / ``cabinet_search`` / ``tool_search`` —
#: so the normalized score reads identically wherever it is asked for.
SCORE_BAND_HINT = (
    "similarity is a normalized 0–1 readout (higher = more relevant; "
    "≈1 identical, ≥0.5 close, 0.1–0.5 weak, <0.1 mostly unrelated) — "
    "the same scale in every search, comparable across them; only scores "
    "from one embedding model are comparable to each other"
)


def annotate_scores(results: list[dict]) -> list[dict]:
    """Add a normalized 0–1 ``similarity`` next to each result's raw
    ``distance`` (mutates *results* in place, returns it for chaining).

    **One metric, all three hybrid paths.**  ``similarity`` is the COSINE
    similarity — what a neighbourhood in an embedding space actually means,
    and what :data:`SCORE_BAND_HINT` reads — and cosine is what every store
    measures: the vec0 tables declare ``distance_metric=cosine``, and the tool
    catalog scores cosine in Python.  So the conversion is ``1 - distance``
    everywhere, and a caller comparing across searches is never comparing
    apples to oranges.

    It used to convert an L2 distance with ``1 - d²/2``, which is the cosine
    only for **unit-norm** vectors — and nothing here established that.  The
    transformer backend normalizes, but llama.cpp's raw output (local-embed's
    gguf path) does not, so distances ran far past the [0,2] that identity
    allows and the clamp turned every strong hit into ``0.0``; at smaller
    distances it produced plausible-but-wrong numbers instead (0.9 where the
    cosine was 0.99).  Measuring cosine removes the assumption rather than
    relying on it: a vector's norm is not part of what "how close is this
    document" means.

    Keyword-only results (``distance`` None) get no ``similarity`` key —
    nothing measured them, and inventing a number would be a lie about the
    match.
    """
    for r in results:
        d = r.get("distance")
        if d is None:
            continue
        r["similarity"] = round(max(0.0, 1.0 - d), 4)
    return results


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
