"""Tests for the per-turn context rebuild's selection policy.

``recall.py`` is pure — candidates and costs in, ids out — so the caps, the
ordering contract and the override semantics are all testable without a store,
a server or an embedding backend.
"""

import pytest; pytestmark = pytest.mark.unit

from slife.plugins.memdb.recall import (
    RecallPolicy, fit_budget, fit_window, gate_turns,
)


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

    def test_the_default_bar_is_the_calibrated_one(self):
        """The floor is a measured number, not a taste: on the recorded
        session a relevant turn scored 0.46+ and every irrelevant one ≤0.45
        (bge-m3, conversation-only index).  A default that drifts below the
        noise band is not a softer filter — the selection *overrides* the
        context, so it admits an arbitrary turn as though it had matched."""
        assert RecallPolicy().min_similarity == 0.45
        hits = [_hit(1, 0.52), _hit(2, 0.45), _hit(3, 0.44)]
        assert gate_turns(hits, policy=RecallPolicy()) == [1, 2]

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


class TestFitWindow:
    """The time branch's token cap — one rule, read from whichever end.

    ``ranked_ids`` arrives ordered from the anchor: newest-first for
    ``newest``, oldest-first for ``oldest``.  So the same input list means two
    different things, and the rule is stated over the *head* rather than over
    an end.
    """

    def test_returns_chronological_order_not_rank_order(self):
        assert fit_window([9, 2, 7], {9: 1, 2: 1, 7: 1}, 0) == [2, 7, 9]

    def test_the_head_is_taken_whatever_it_costs(self):
        """The head is the end that was named: an answer that dropped it would
        be the other end, which is not what was asked for.  A single turn
        larger than the whole budget is recalled alone."""
        assert fit_window([5, 4, 3], {5: 100, 4: 1, 3: 1}, 5) == [5]

    def test_the_run_stops_at_the_first_turn_that_does_not_fit(self):
        """A window is adjacency, so the run ends where the budget does: turn
        3 would have fit, and reaching it would have punched a hole."""
        assert fit_window([5, 4, 3], {5: 5, 4: 100, 3: 1}, 10) == [5]

    def test_oldest_first_reads_the_mirror(self):
        """The same costs, the other direction: the head is now the *oldest*
        turn, so it is the one the exemption protects."""
        assert fit_window([3, 4, 5], {5: 5, 4: 100, 3: 1}, 10) == [3]

    def test_a_window_that_fits_is_returned_whole_either_way(self):
        """The caps are the only reason the direction matters: nothing is cut,
        so nothing is chosen between."""
        costs = {1: 1, 2: 1, 3: 1}
        assert fit_window([3, 2, 1], costs, 100) == [1, 2, 3]
        assert fit_window([1, 2, 3], costs, 100) == [1, 2, 3]

    def test_the_two_branches_disagree_on_the_same_input(self):
        """Why this is not ``fit_budget`` with a flag: the same candidates,
        costs and budget answer differently under each order's meaning."""
        ranked, costs, budget = [5, 4, 3], {5: 100, 4: 1, 3: 1}, 5
        assert fit_budget(ranked, costs, budget) == [3, 4]
        assert fit_window(ranked, costs, budget) == [5]

    def test_zero_budget_is_unbounded(self):
        assert fit_window([3, 1, 2], {1: 999, 2: 999, 3: 999}, 0) == [1, 2, 3]

    def test_empty_candidates_select_nothing(self):
        assert fit_window([], {}, 100) == []

    def test_unknown_cost_counts_as_zero(self):
        assert fit_window([2, 1], {}, 1) == [1, 2]


class TestOverrideSemantics:
    """The selection REPLACES the context — nothing is merged or reconciled."""

    def test_a_turn_already_in_context_is_selected_normally(self):
        """Re-selecting an incumbent is the intended outcome, not a
        duplicate: recall does not exclude what is already in context."""
        hits = [_hit(3, 0.9), _hit(1, 0.9)]
        assert gate_turns(hits, policy=RecallPolicy()) == [3, 1]

    def test_empty_candidates_select_nothing(self):
        """An empty selection is the *deliberate* empty context (§2.3): the
        persisted list is emptied with it, which is why the harness's
        ``set_context_turns`` refuses an empty list and the loop has a separate
        clear for this path.  A *failed* store or a `{}` reply is what keeps
        the context instead."""
        assert gate_turns([], policy=RecallPolicy()) == []

    def test_the_two_phases_compose(self):
        hits = [_hit(5, 0.9), _hit(4, 0.1), _hit(3, 0.9)]
        policy = RecallPolicy(min_similarity=0.35, token_budget=10, limit=5)
        gated = gate_turns(hits, policy=policy)
        assert gated == [5, 3]  # relevance order
        # Only one fits the budget — the higher-ranked one wins the slot.
        assert fit_budget(gated, {5: 6, 3: 6}, policy.token_budget) == [5]
