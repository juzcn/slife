"""Hybrid search — merges keyword (FTS5) and semantic (vec0) results.

Uses Reciprocal Rank Fusion (RRF) — a simple, parameter-free algorithm
that combines ranked lists without needing to tune weights.

This module owns the ONE search surface, in two layers:

* :func:`run_search` — the whole **tool-level** logic: the mode dispatch,
  the limit clamp, the single query embed, the semantic gate, the fusion,
  the score annotation and the hint.  A search tool that calls it cannot
  get any of that wrong on its own, which is the point: the turns
  database and the file cabinet answer identically because they run the
  same code, not because two implementations were kept in step.
* :func:`run_hybrid` — the fusion underneath, for callers that need the
  hits and the leg's fate rather than a result envelope (the per-turn
  recall selector).

Both take a :class:`SearchLegs` — one searchable corpus, expressed as its
three legs.  One corpus is the requirement, not a simplification: a fusion
consumes *ranks*, and a rank is only meaningful inside the corpus that
produced it (an FTS5 ``rank`` is bm25, a per-table score).  So a store with
several kinds of document indexes them as one corpus — the cabinet reads
its four kinds through one view — rather than ranking each kind and then
inventing an order across the results.
"""

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 200

#: Rows a regex ``grep`` examines.  A regex cannot use an index, so grep scans
#: (newest first); the cap keeps the worst case bounded on a long-lived store
#: while sitting far above any realistic hit count.
GREP_SCAN_LIMIT = 20000


def _clamp_limit(limit: int) -> int:
    """Clamp a search limit to a sane positive range.

    SQLite treats a negative LIMIT as unlimited — a malformed/negative limit
    from the LLM would otherwise scan the whole table.
    """
    if limit is None or limit < 1:
        return 20
    return min(limit, _MAX_SEARCH_LIMIT)

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


# ── The one hybrid composition ────────────────────────────────────────


def rename_rowid_to_turn_id(entries: list[dict]) -> None:
    """Rewrite each entry's internal ``rowid`` to the model-visible
    ``turn_id`` (in place), and drop the semantic dedup key.

    The store keys both legs on the internal ``rowid``; the merge aligns on
    ``turn_id``, so the rename MUST happen before :func:`merge_hybrid` —
    otherwise every key lookup misses and the merge collapses to empty
    (keyword=6 semantic=6 merged=0).
    """
    for e in entries:
        if "rowid" in e:
            e["turn_id"] = e.pop("rowid")
        e.pop("diary_rowid", None)


@dataclass
class HybridResult:
    """One hybrid search: the fused hits plus how the semantic leg fared."""

    hits: list[dict]
    """Merged, RRF-ordered, already renamed to ``turn_id``.  Each entry
    carries ``rrf_score``, ``keyword_rank``, ``semantic_rank``, ``snippet``
    and ``distance`` — and a ``similarity`` when the semantic leg produced
    it (see :func:`annotate_scores`)."""

    semantic_available: bool
    """False → the semantic leg was skipped or failed and the result is
    keyword-only.  The signal a caller must branch on before trusting a
    retrieval as complete."""

    embedder_ready: bool
    """``manager.semantic_ready`` at call time — separates "the embedder is
    not ready" from "it is ready but this query's embedding failed"."""

    reason: str | None
    """``manager.reason`` (the prose degradation cause), or None when there
    is no manager at all."""


async def run_hybrid(
    store: Any,
    manager: Any,
    *,
    query: str,
    limit: int,
    since: str | None = None,
    until: str | None = None,
    overfetch: int = 2,
) -> HybridResult:
    """Fuse the turns database's two legs.

    The fusion's entry point for callers that want the hits and the leg's
    fate rather than a search-tool result envelope — the per-turn recall
    selector, which fits its own token budget to ``hits`` and reads
    ``semantic_available`` to log a degraded leg.  A search *tool* wants
    :func:`run_search` instead.

    *overfetch* multiplies the per-leg ``limit``.  Both legs truncate
    internally before returning, so a caller that will discard results
    (a similarity threshold, an exclusion set, a token budget) must
    over-fetch to have anything left to discard from.
    """
    return await fuse_hybrid(
        turn_legs(store), manager, query=query, limit=limit,
        since=since, until=until, overfetch=overfetch,
    )


def hybrid_hint(result: HybridResult, noun: str = "turns") -> str:
    """The degradation/no-match prose for a :class:`HybridResult`.

    One wording for every hybrid caller, so a degraded result reads the
    same wherever it surfaces — and an empty result is exactly when a
    silent fallback would mislead.

    *noun* is what the corpus calls a hit, so the no-match line agrees with
    the empty-result hints the other two modes produce for the same tool
    ("no turns contain …" / "no matching turns found").
    """
    if not result.semantic_available:
        if result.embedder_ready:
            hint = ("hybrid degraded to fts5 — query embedding generation "
                    "failed (API error or timeout). Check the API key, or "
                    "switch to a local model")
        else:
            hint = result.reason if result.reason is not None else (
                "hybrid degraded to fts5 — embedding backend unavailable")
        if not result.hits:
            hint += " — no keyword (fts5) matches either"
        return hint
    if not result.hits:
        return f"no matching {noun} found"
    return SCORE_BAND_HINT


# ── The one search surface ────────────────────────────────────────────


@dataclass
class SearchLegs:
    """One searchable corpus, as the three legs the search tools share.

    A plugin supplies the legs and what its hits are called; the
    composition supplies everything else.  That split is the point: the
    mode dispatch, the clamp, the single embed, the gate, the fusion, the
    score annotation and the hint are the same code for every corpus, so
    the turns database and the file cabinet answer identically because
    they *run* the same code rather than because two implementations were
    kept in step by hand.

    A multi-corpus caller (the cabinet's four kinds) still presents ONE
    of these: its legs query each kind and join the results with
    :func:`interleave_ranked`, which is what keeps the ranks real.
    """

    keyword: Callable[..., Awaitable[list[dict]]]
    """``(query, limit, since, until)`` → ranked hits, best first."""

    semantic: Callable[..., Awaitable[list[dict]]]
    """``(embedding, limit, since, until)`` → vector hits, nearest first."""

    regex: Callable[..., Awaitable[list[dict]]]
    """``(pattern, limit, since, until)`` → regex hits, newest first.
    Raises ``re.error`` for an unusable pattern, which the caller reports."""

    key_field: str
    """The field the two legs are aligned on before the fusion.  It must be
    unique across the whole corpus, since the merge keys on it."""

    normalize: Callable[[list[dict]], None] | None = None
    """Rewrite each leg's hits in place before the merge — the rename that
    makes the two legs align, and the place internal keys are dropped so
    they cannot reach the model.  ``None`` for legs that already emit the
    model-visible fields."""

    noun: str = "entries"
    """What a hit is called in the no-match hints."""

    browse: str = "list"
    """The tool(s) that BROWSE this corpus, named in the empty-query refusal
    so the caller is told what to call instead.  A search is not a browse:
    an empty query reaches FTS5 as a syntax error and embeds to noise, and a
    grep over the empty pattern matches everything — which is how a search
    tool used to become an accidental second browse path."""


@dataclass
class SearchOutcome:
    """One search tool call, resolved: the hits, what ran, and the hint."""

    results: list[dict]
    """The merged, sliced, already-normalized hits — at most ``limit``."""

    ran_mode: str
    """What actually RAN, not what was asked for: a hybrid request whose
    semantic leg did not run reports ``fts5``."""

    hint: str
    """``""`` when there is nothing to say, else the one prose line —
    the degradation reason, the no-match line, or the score band."""


def turn_legs(store: Any) -> SearchLegs:
    """The turns database's corpus, as the composition sees it."""
    return SearchLegs(
        keyword=store.search_keyword,
        semantic=store.search_semantic,
        regex=store.search_grep,
        key_field="turn_id",
        normalize=rename_rowid_to_turn_id,
        noun="turns",
        browse="turn_list",
    )


def _normalize(legs: SearchLegs, hits: list[dict]) -> None:
    if legs.normalize is not None:
        legs.normalize(hits)


async def _semantic_leg(
    legs: SearchLegs, manager: Any, *, query: str, limit: int,
    since: str | None, until: str | None,
) -> tuple[list[dict], bool]:
    """The embedded query's hits, and whether the leg ran at all.

    The gate is checked once, here, for every corpus: the embedder is the
    same object whichever plugin owns the drainer, so "the semantic leg
    did not run" has exactly one cause and one place it is decided.
    """
    if (
        manager is None
        or not manager.semantic_ready
        or manager.embedder is None
        or not manager.embedder.available
    ):
        return [], False
    emb = await manager.embedder.embed_one(query)
    if not emb:
        return [], False
    return (
        await legs.semantic(
            embedding=emb, limit=limit, since=since, until=until,
        ),
        True,
    )


async def fuse_hybrid(
    legs: SearchLegs,
    manager: Any,
    *,
    query: str,
    limit: int,
    since: str | None = None,
    until: str | None = None,
    overfetch: int = 2,
) -> HybridResult:
    """Run both legs and fuse them — the fusion every caller shares."""
    keyword_hits = await legs.keyword(
        query=query, limit=limit * overfetch, since=since, until=until,
    )
    semantic_hits, semantic_available = await _semantic_leg(
        legs, manager, query=query, limit=limit * overfetch,
        since=since, until=until,
    )

    _normalize(legs, keyword_hits)
    _normalize(legs, semantic_hits)
    hits = merge_hybrid(keyword_hits, semantic_hits, key_field=legs.key_field)

    if semantic_available and hits:
        annotate_scores(hits)

    return HybridResult(
        hits=hits,
        semantic_available=semantic_available,
        embedder_ready=bool(manager is not None and manager.semantic_ready),
        reason=manager.reason if manager is not None else None,
    )


async def run_search(
    legs: SearchLegs,
    manager: Any,
    *,
    query: str,
    mode: str,
    limit: int,
    since: str | None = None,
    until: str | None = None,
) -> SearchOutcome:
    """One search tool call, from the mode to the hint.

    The whole tool-level half of a search lives here, so a tool that calls
    it cannot get the shared part wrong on its own: the clamp, the gate,
    the single embed, the fusion, the annotation, the mode it reports and
    the prose it returns are decided once for every corpus.

    Raises :class:`ValueError` for a call the corpus cannot answer — a mode
    it does not have, or an empty query — which the tool reports as
    ``Error: …``.  A caller that asked for a mode it did not get has no way
    to tell, so an unknown one is refused rather than falling back to
    hybrid: silently answering a different question is the failure mode
    this whole surface exists to avoid.
    """
    mode = (mode or "").lower()
    if mode not in ("hybrid", "fts5", "grep"):
        raise ValueError(f"mode must be one of hybrid/fts5/grep — got {mode!r}")
    if not query.strip():
        raise ValueError(
            "query must not be empty — to browse instead of search, use "
            f"{legs.browse}"
        )
    limit = _clamp_limit(limit)

    if mode == "grep":
        # A real regex, run in Python (SQLite has no regexp engine).  An
        # unusable pattern raises ``re.error`` for the caller to report.
        hits = await legs.regex(
            pattern=query, limit=limit, since=since, until=until,
        )
        _normalize(legs, hits)
        return SearchOutcome(hits, "grep", _no_hits(legs, "contain", query, hits))

    if mode == "fts5":
        hits = await legs.keyword(
            query=query, limit=limit, since=since, until=until,
        )
        _normalize(legs, hits)
        return SearchOutcome(hits, "fts5", _no_hits(legs, "related to", query, hits))

    result = await fuse_hybrid(
        legs, manager, query=query, limit=limit, since=since, until=until,
    )
    return SearchOutcome(
        results=result.hits[:limit],
        ran_mode="hybrid" if result.semantic_available else "fts5",
        hint=hybrid_hint(result, noun=legs.noun),
    )


def _no_hits(
    legs: SearchLegs, relation: str, query: str, hits: list[dict],
) -> str:
    """The empty-result line for the two non-fused modes.

    Hybrid answers with :func:`hybrid_hint` instead — its empty case is
    entangled with the leg's fate, which is the part worth reporting there.
    """
    return "" if hits else f"no {legs.noun} {relation} '{query}'"
