"""Tests for the per-turn context rebuild's selection policy.

``recall.py`` is pure — candidates and costs in, ids out — so the caps, the
ordering contract and the override semantics are all testable without a store,
a server or an embedding backend.
"""

import pytest; pytestmark = pytest.mark.unit

from slife.plugins.memdb.recall import RecallPolicy, fit_budget, gate_turns


def _hit(turn_id, similarity=None):
    """A fused hit as ``run_hybrid`` produces one."""
    hit = {"turn_id": turn_id}
    if similarity is not None:
        hit["similarity"] = similarity
    return hit


class TestGateTurns:
    """Similarity and count caps, in relevance order."""

    def test_keeps_hits_above_the_similarity_bar(self):
        hits = [_hit(1, 0.9), _hit(2, 0.4), _hit(3, 0.34), _hit(4, 0.1)]
        assert gate_turns(hits, policy=RecallPolicy(min_similarity=0.35)) == [1, 2]

    def test_keyword_hits_are_exempt(self):
        """A keyword hit carries no measured similarity — nothing weighed it,
        so there is no number to threshold.  An exact match is a stronger
        signal than a cosine neighbourhood and must never be dropped for
        lacking a score."""
        hits = [_hit(1), _hit(2, 0.99), _hit(3, 0.5)]
        assert gate_turns(hits, policy=RecallPolicy(min_similarity=0.95)) == [1, 2]

    def test_respects_the_count_cap(self):
        hits = [_hit(i, 0.9) for i in range(1, 11)]
        assert gate_turns(hits, policy=RecallPolicy(limit=3)) == [1, 2, 3]

    def test_duplicate_ids_collapse(self):
        hits = [_hit(7, 0.9), _hit(7, 0.9)]
        assert gate_turns(hits, policy=RecallPolicy()) == [7]

    def test_id_less_hits_are_skipped(self):
        assert gate_turns([{"similarity": 0.9}], policy=RecallPolicy()) == []


class TestFitBudget:
    """The token cap, and the chronological output contract."""

    def test_returns_chronological_order_not_rank_order(self):
        """Membership comes from relevance, order comes from time — the list
        order is a contract (restore reads the last entry as the newest)."""
        ranked = [9, 2, 7]
        assert fit_budget(ranked, {9: 1, 2: 1, 7: 1}, 0) == [2, 7, 9]

    def test_drops_what_does_not_fit(self):
        assert fit_budget([5, 4, 3], {5: 10, 4: 10, 3: 10}, 25) == [4, 5]

    def test_skips_rather_than_stops_so_a_later_small_turn_still_fits(self):
        """Rank order is relevance order, so a big turn early must not
        blackball a small one behind it."""
        assert fit_budget([5, 4, 3], {5: 100, 4: 1, 3: 1}, 5) == [3, 4]

    def test_zero_budget_is_unbounded(self):
        assert fit_budget([3, 1, 2], {1: 999, 2: 999, 3: 999}, 0) == [1, 2, 3]

    def test_unknown_cost_counts_as_zero(self):
        assert fit_budget([2, 1], {}, 1) == [1, 2]


class TestOverrideSemantics:
    """The selection REPLACES the context — nothing is merged or reconciled."""

    def test_a_turn_already_in_context_is_selected_normally(self):
        """Re-selecting an incumbent is the intended outcome, not a
        duplicate: recall does not exclude what is already in context."""
        hits = [_hit(3, 0.9), _hit(1, 0.9)]
        assert gate_turns(hits, policy=RecallPolicy()) == [3, 1]

    def test_empty_candidates_select_nothing(self):
        """The caller treats this as "keep the existing context" — never as
        "empty it", which is why the harness refuses to persist an empty
        selection."""
        assert gate_turns([], policy=RecallPolicy()) == []

    def test_the_two_phases_compose(self):
        hits = [_hit(5, 0.9), _hit(4, 0.1), _hit(3, 0.9)]
        policy = RecallPolicy(min_similarity=0.35, token_budget=10, limit=5)
        gated = gate_turns(hits, policy=policy)
        assert gated == [5, 3]  # relevance order
        # Only one fits the budget — the higher-ranked one wins the slot.
        assert fit_budget(gated, {5: 6, 3: 6}, policy.token_budget) == [5]
