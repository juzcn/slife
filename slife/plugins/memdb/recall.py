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
