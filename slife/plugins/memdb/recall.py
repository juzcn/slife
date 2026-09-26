"""Turn recall — choose the turns a turn adds to its context.

A query answers *"what matches this text"*.  The per-turn recall answers
*"what does this turn's context need that it has not got"*, and its answer is
**added to** the turns the turn keeps — the harness composes the two by id, so
a turn already in context that recall names again is simply the same turn, and
one the turn never asked to keep is not dragged in.  That is what leaves the
selector with nothing to reconcile: it has no incumbent to defend, and no need
to exclude turns that are already in context (re-selecting one is free).

What remains is the three caps from the design note, applied to the fused
candidates in relevance order:

1. **Count** — at most :attr:`RecallPolicy.limit` turns.
2. **Similarity** — a semantic hit must clear :attr:`RecallPolicy.min_similarity`.
   Keyword-leg hits are exempt: they carry no measured similarity, and an
   exact match is a stronger signal than a cosine neighbourhood (see
   ``search.py``'s note that inventing a number for them "would be a lie").
3. **Token budget** — what recall adds is what the model will be sent on top,
   so it is bounded in tokens, not just in rows.  The bound is the headroom
   the caller passes as ``reserved_tokens`` below the context floor
   (``server.__memory_turn_recall``), because the turns being kept spend that
   floor too.

Membership comes from relevance; **order comes from time** — the result is
returned chronologically, because the list order is a contract (restore reads
the last entry as the newest, and the renderer pairs position with message
order).

A **time-only** recall is the exception to that first half: with no query there
is nothing to be relevant to, so its candidates are the window's turns and its
**anchor** decides which end leads — ``newest`` (the default) or ``oldest``.
Its token cap is therefore :func:`fit_window` and not :func:`fit_budget`: the
same budget spent from the anchored end and contiguously, because a window's
order means adjacency where a relevance order does not.

The policy is a pure function over already-retrieved candidates, so it can be
tested without a store or a server.  Retrieval (the hybrid legs and the
similarity measurement) happens in the caller — see
``server.__memory_turn_recall``.
"""

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecallPolicy:
    """The knobs of a recall selection."""

    min_similarity: float = 0.45
    """Semantic similarity a turn must reach to enter the context.  Applied
    only to hits that were actually measured — keyword-leg hits pass
    unconditionally.

    Calibrated by measurement, because the scale is a property of the pair
    that produces it (the embedding model *and* the text the index holds) and
    a floor set below the noise is worse than no floor: it admits an
    arbitrary turn as if it had been matched, and the selection then
    *overrides* the context with it.  On a recorded session, queries against
    the conversation-only index put every genuinely relevant turn at
    0.46–0.55, every irrelevant one at ≤0.45 — and an unrelated query (news
    no earlier turn mentions) topped out at 0.33, so it now selects nothing
    instead of the 0.37 tool-dump hit the old 0.35 floor let through.

    Re-measure when the embedding model or ``_turn_text_for_embedding``
    changes — this value does not transfer to another model's scale (the
    reasoning is in DESIGN.md §2.3)."""

    token_budget: int = 0
    """Maximum estimated tokens for the selection (0 = unbounded).

    Under the per-turn union this is the *selection's own* size — the context
    floor — and it stays the cap whenever the caller reserves nothing.  What
    the caller keeps is bounded separately, against :attr:`ceiling_tokens`."""

    ceiling_tokens: int = 0
    """Where the caller's window starts forcing the context down (0 = unset).

    The bound the *total* has to respect.  A context's life runs between the
    floor and the ceiling — the trim only fires at the ceiling and compacts
    *to* the floor (``AgentLoop._trim_context``) — so the floor is the
    wrong denominator for a caller that already has turns in hand: it grants
    no headroom the moment the context reaches it, which is most of the time.
    The headroom below the ceiling is what is actually left to spend.

    Applied as ``min(token_budget, ceiling_tokens - reserved)``
    (``server.__memory_turn_recall``), so it can only ever *narrow* the
    selection's own cap, never raise it."""

    limit: int = 40
    """Maximum number of turns in the selection."""


def similarity_of(hit: dict) -> float | None:
    """The measured cosine similarity of a fused hit, or None.

    ``merge_hybrid`` keeps each member's ``distance`` and
    :func:`~slife.plugins.memdb.search.annotate_scores` adds ``similarity``
    for the semantic leg.  Keyword-only members have neither — nothing
    measured them, so there is no number to threshold.
    """
    sim = hit.get("similarity")
    return sim if isinstance(sim, (int, float)) else None


def gate_turns(candidates: list[dict], *, policy: RecallPolicy) -> list[int]:
    """Apply the similarity and count caps; return ids in **relevance** order.

    This phase needs only the hits.  The token budget does not — it needs
    each turn's stored messages — so it is a second phase
    (:func:`fit_budget`) once the caller has fetched what this returned.
    Splitting them keeps both halves pure.
    """
    picked: list[int] = []
    seen: set[int] = set()
    skipped_low_similarity = 0

    for hit in candidates:
        if len(picked) >= policy.limit:
            break
        tid = hit.get("turn_id")
        if tid is None or tid in seen:
            continue
        sim = similarity_of(hit)
        if sim is not None and sim < policy.min_similarity:
            skipped_low_similarity += 1
            continue
        seen.add(tid)
        picked.append(tid)

    logger.info(
        "recall_gated gated=%d low_similarity=%d",
        len(picked), skipped_low_similarity,
    )
    return picked


def fit_budget(
    ranked_ids: list[int],
    costs: dict[int, int],
    budget: int,
) -> list[int]:
    """Trim *ranked_ids* to *budget* estimated tokens, **chronological** out.

    Rank order decides membership (relevance), time decides render order.
    A turn that does not fit is skipped rather than stopping the scan — a
    later turn can still be small enough.

    This is the **query** branch's rule, and both halves of it are statements
    about relevance order: a candidate behind an unaffordable one is still a
    candidate, and no position is privileged.  A time window's order means
    adjacency instead, so its budget phase is :func:`fit_window`.
    """
    kept: list[int] = []
    spent = 0
    skipped = 0
    for tid in ranked_ids:
        cost = costs.get(tid, 0)
        if budget > 0 and spent + cost > budget:
            skipped += 1
            continue
        kept.append(tid)
        spent += cost
    ordered = sorted(kept)
    logger.info(
        "recall_budgeted selected=%d tokens=%d over_budget=%d",
        len(ordered), spent, skipped,
    )
    return ordered


def fit_window(
    ranked_ids: list[int],
    costs: dict[int, int],
    budget: int,
) -> list[int]:
    """Take what fits *budget* from the head of *ranked_ids*, **chronological**
    out — the time branch's rule.

    Direction-agnostic: *ranked_ids* must arrive ordered **from the anchor** —
    newest-first for ``newest``, oldest-first for ``oldest``
    (``store.search_time``) — so position reads as time and the head is the end
    the caller named.  One rule serves both directions; the direction lives in
    the ordering, not here.

    Two things follow from reading position as time, and both are why this is
    not :func:`fit_budget` with a flag:

    * **The head is taken whatever it costs.**  It is the end that was named,
      so anything that would drop it answers a different question — a turn
      larger than the whole budget is recalled *alone*, which is the answer and
      not a failure, and one turn's overshoot is what the ceiling absorbs.
    * **The run behind it is contiguous.**  The scan stops at the first turn
      that does not fit instead of skipping it: relevance order may reach past
      an unaffordable turn, but a time window is adjacency — a hole is a piece
      of the conversation missing with nothing in the result to say so.
    """
    kept: list[int] = []
    spent = 0
    unreached = 0
    for index, tid in enumerate(ranked_ids):
        cost = costs.get(tid, 0)
        # The head is the anchored end: the budget never decides it.
        if index > 0 and budget > 0 and spent + cost > budget:
            unreached = len(ranked_ids) - index
            break
        kept.append(tid)
        spent += cost
    ordered = sorted(kept)
    logger.info(
        "recall_window selected=%d tokens=%d budget=%d unreached=%d",
        len(ordered), spent, budget, unreached,
    )
    return ordered
