"""Turn recall — choose the turns that become the agent's context.

A query answers *"what matches this text"*.  The per-turn recall answers
*"what should be in the context now"*, and its answer **overrides** the
previous context: the returned list *is* the new live context, with no reconciliation
against what was there before.  That simplicity is the point — there is
nothing to merge, no incumbent to defend, and therefore no need to exclude
turns that are already in context (re-selecting one is the intended outcome,
not a duplicate).

What remains is the three caps from the design note, applied to the fused
candidates in relevance order:

1. **Count** — at most :attr:`RecallPolicy.limit` turns.
2. **Similarity** — a semantic hit must clear :attr:`RecallPolicy.min_similarity`.
   Keyword-leg hits are exempt: they carry no measured similarity, and an
   exact match is a stronger signal than a cosine neighbourhood (see
   ``search.py``'s note that inventing a number for them "would be a lie").
3. **Token budget** — the selection is what the model will be sent, so it is
   bounded in tokens, not just in rows.

Membership comes from relevance; **order comes from time** — the result is
returned chronologically, because the list order is a contract (restore reads
the last entry as the newest, and the renderer pairs position with message
order).

The policy is a pure function over already-retrieved candidates, so it can be
tested without a store or a server.  Retrieval (the hybrid legs and the
similarity measurement) happens in the caller — see
``server.turn_recall``.
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
    """Maximum estimated tokens for the selection (0 = unbounded)."""

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
