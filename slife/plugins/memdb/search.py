"""Hybrid search — merges keyword (FTS5) and semantic (vec0) results.

Uses Reciprocal Rank Fusion (RRF) — a simple, parameter-free algorithm
that combines ranked lists without needing to tune weights.
"""

import logging

logger = logging.getLogger(__name__)

# RRF smoothing constant. Higher = less influence from rank position.
# 60 is the standard value from the literature.
RRF_K = 60


def merge_hybrid(
    keyword_results: list[dict],
    semantic_results: list[dict],
    k: int = RRF_K,
) -> list[dict]:
    """Merge keyword and semantic search results using RRF.

    Results that appear high in BOTH lists get the highest scores.
    Results in only one list still get a reasonable score.

    Args:
        keyword_results: FTS5 results, each with at least ``rowid`` and ``rank``.
        semantic_results: vec0 results, each with at least ``rowid`` and ``distance``.
        k: RRF smoothing constant (default 60).

    Returns:
        Merged list sorted by RRF score descending. Each entry has:
        - rowid, user_message, summary, tags, created_at
        - rrf_score: combined RRF score
        - keyword_rank: 1-based rank in keyword results (or None)
        - semantic_rank: 1-based rank in semantic results (or None)
        - snippet: text snippet from keyword search (if available)
        - distance: cosine distance from semantic search (if available)
    """
    # Build RRF scores by rowid
    scores: dict[int, float] = {}
    info: dict[int, dict] = {}

    # Keyword contributions
    for rank, item in enumerate(keyword_results, start=1):
        rowid = item.get("rowid")
        if rowid is None:
            continue
        scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (k + rank)
        if rowid not in info:
            info[rowid] = {
                "rowid": rowid,
                "user_message": item.get("user_message", ""),
                "summary": item.get("summary", ""),
                "tags": item.get("tags", ""),
                "created_at": item.get("created_at", ""),
                                "keyword_rank": rank,
                "semantic_rank": None,
                "snippet": item.get("snippet", ""),
                "distance": None,
            }

    # Semantic contributions
    for rank, item in enumerate(semantic_results, start=1):
        rowid = item.get("rowid")
        if rowid is None:
            continue
        scores[rowid] = scores.get(rowid, 0.0) + 1.0 / (k + rank)
        if rowid not in info:
            info[rowid] = {
                "rowid": rowid,
                "user_message": item.get("user_message", ""),
                "summary": item.get("summary", ""),
                "tags": item.get("tags", ""),
                "created_at": item.get("created_at", ""),
                                "keyword_rank": None,
                "semantic_rank": rank,
                "snippet": "",
                "distance": item.get("distance"),
            }
        else:
            info[rowid]["semantic_rank"] = rank
            info[rowid]["distance"] = item.get("distance")

    # Sort by RRF score descending
    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)

    results = []
    for rowid, rrf_score in merged:
        entry = info[rowid]
        entry["rrf_score"] = round(rrf_score, 6)
        results.append(entry)

    logger.debug(
        "rrf_merge keyword=%d semantic=%d merged=%d",
        len(keyword_results), len(semantic_results), len(results),
    )
    return results
