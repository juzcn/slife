"""Tests for slife_memdb.search — merge_hybrid RRF algorithm."""

import pytest; pytestmark = pytest.mark.unit


import pytest

from slife.plugins.memdb.search import annotate_scores, merge_hybrid, RRF_K


# ── merge_hybrid ─────────────────────────────────────────────────────────────


class TestMergeHybrid:
    """Tests for merge_hybrid."""

    def test_both_empty(self):
        result = merge_hybrid([], [])
        assert result == []

    def test_only_keyword_results(self):
        kw = [
            {"turn_id": 1, "title": "Chat 1", "summary": "S1", "tags": "", "created_at": "2024-01-01"},
            {"turn_id": 2, "title": "Chat 2", "summary": "S2", "tags": "", "created_at": "2024-01-02"},
        ]
        result = merge_hybrid(kw, [])
        assert len(result) == 2
        assert result[0]["turn_id"] == 1  # first in keyword = highest RRF
        assert result[0]["keyword_rank"] == 1
        assert result[0]["semantic_rank"] is None
        assert result[1]["keyword_rank"] == 2

    def test_only_semantic_results(self):
        sem = [
            {"turn_id": 10, "title": "S Chat 1", "summary": "S1", "tags": "", "created_at": "2024-01-01", "distance": 0.1},
        ]
        result = merge_hybrid([], sem)
        assert len(result) == 1
        assert result[0]["turn_id"] == 10
        assert result[0]["keyword_rank"] is None
        assert result[0]["semantic_rank"] == 1
        assert result[0]["distance"] == 0.1

    def test_merge_both_lists(self):
        kw = [
            {"turn_id": 1, "title": "K1", "summary": "KS1", "tags": "", "created_at": "2024-01-01"},
        ]
        sem = [
            {"turn_id": 2, "title": "S1", "summary": "SS1", "tags": "", "created_at": "2024-01-02", "distance": 0.2},
        ]
        result = merge_hybrid(kw, sem)
        assert len(result) == 2
        # Both have same single rank, so scores should be tied
        assert all("rrf_score" in r for r in result)

    def test_same_item_in_both_boosted(self):
        """An item appearing in BOTH lists gets a higher RRF score."""
        kw = [
            {"turn_id": 1, "title": "Important", "summary": "S", "tags": "", "created_at": "2024-01-01", "snippet": "match..."},
            {"turn_id": 2, "title": "K only", "summary": "S", "tags": "", "created_at": "2024-01-02"},
        ]
        sem = [
            {"turn_id": 1, "title": "Important", "summary": "S", "tags": "", "created_at": "2024-01-01", "distance": 0.05},
        ]
        result = merge_hybrid(kw, sem)
        assert len(result) == 2
        # Rowid 1 is in both lists → highest score
        assert result[0]["turn_id"] == 1
        assert result[0]["keyword_rank"] == 1
        assert result[0]["semantic_rank"] == 1
        assert result[0]["rrf_score"] > result[1]["rrf_score"]

    def test_rrf_score_calculation(self):
        """RRF: score = 1/(k + rank), rounded to 6 decimal places."""
        kw = [
            {"turn_id": 1, "title": "T1", "summary": "", "tags": "", "created_at": ""},
        ]
        # Single keyword match at rank 1: round(1/(60+1), 6) = round(0.0163934426..., 6) = 0.016393
        result = merge_hybrid(kw, [])
        assert len(result) == 1
        assert result[0]["rrf_score"] == round(1.0 / (RRF_K + 1), 6)

    def test_custom_k_value(self):
        kw = [{"turn_id": 1, "title": "T", "summary": "", "tags": "", "created_at": ""}]
        result = merge_hybrid(kw, [], k=10)
        assert result[0]["rrf_score"] == round(1.0 / 11, 6)

    def test_skips_items_without_turn_id(self):
        kw = [
            {"title": "no turn_id"},
            {"turn_id": 1, "title": "has turn_id", "summary": "", "tags": "", "created_at": ""},
        ]
        result = merge_hybrid(kw, [])
        assert len(result) == 1
        assert result[0]["turn_id"] == 1

    def test_info_populated_from_keyword(self):
        kw = [{
            "turn_id": 42, "user_message": "My Chat", "summary": "Great chat",
            "tags": "ai,Slife", "created_at": "2024-06-01T10:00:00",
            "snippet": "matched text...",
        }]
        result = merge_hybrid(kw, [])
        r = result[0]
        assert r["user_message"] == "My Chat"
        assert r["summary"] == "Great chat"
        assert r["tags"] == "ai,Slife"
        assert r["created_at"] == "2024-06-01T10:00:00"
        assert r["snippet"] == "matched text..."
        assert r["distance"] is None

    def test_info_populated_from_semantic(self):
        sem = [{
            "turn_id": 7, "user_message": "Semantic", "summary": "Sem summary",
            "tags": "sem", "created_at": "2024-07-01",
            "distance": 0.123,
        }]
        result = merge_hybrid([], sem)
        r = result[0]
        assert r["user_message"] == "Semantic"
        assert r["distance"] == 0.123
        assert r["keyword_rank"] is None
        assert r["snippet"] == ""


class TestAnnotateScores:
    """annotate_scores 0–1 normalizes the semantic distance presented to
    the LLM — one contract shared with cabinet_search / mcp_tool_search
    (the MCP plugin mirrors this function)."""

    def test_l2_maps_via_1_over_1_plus_d(self):
        # Typical vec0 L2 distances (the raw 18–22 range) map to a
        # low-but-readable score; a near-identical match → ~1.0.
        assert annotate_scores([{"distance": 0.0}])[0]["similarity"] == 1.0
        assert annotate_scores([{"distance": 1.0}])[0]["similarity"] == 0.5
        r = annotate_scores([{"distance": 20.0}])[0]
        assert r["similarity"] == round(1.0 / 21.0, 4)

    def test_cosine_metric_maps_as_true_cosine_similarity(self):
        r = annotate_scores([{"distance": 0.2}], metric="cosine")[0]
        assert r["similarity"] == round(0.8, 4)  # 1 − 0.2
        # Clipped: cosine distance beyond 1 (opposite directions) → 0.
        r = annotate_scores([{"distance": 1.5}], metric="cosine")[0]
        assert r["similarity"] == 0.0

    def test_keyword_only_results_get_no_key(self):
        results = annotate_scores([{"distance": None}, {"x": 1}])
        assert all("similarity" not in r for r in results)

    def test_preserves_other_fields_and_order(self):
        results = [
            {"id": 1, "distance": 0.5},
            {"id": 2, "distance": 4.0},
        ]
        annotate_scores(results)
        assert results[0]["id"] == 1
        assert results[0]["similarity"] > results[1]["similarity"]
