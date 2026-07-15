"""Tests for slife_memory.search — merge_hybrid RRF algorithm."""

import pytest

from slife_memory.search import merge_hybrid, RRF_K


# ── merge_hybrid ─────────────────────────────────────────────────────────────


class TestMergeHybrid:
    """Tests for merge_hybrid."""

    def test_both_empty(self):
        result = merge_hybrid([], [])
        assert result == []

    def test_only_keyword_results(self):
        kw = [
            {"rowid": 1, "title": "Chat 1", "summary": "S1", "tags": "", "created_at": "2024-01-01"},
            {"rowid": 2, "title": "Chat 2", "summary": "S2", "tags": "", "created_at": "2024-01-02"},
        ]
        result = merge_hybrid(kw, [])
        assert len(result) == 2
        assert result[0]["rowid"] == 1  # first in keyword = highest RRF
        assert result[0]["keyword_rank"] == 1
        assert result[0]["semantic_rank"] is None
        assert result[1]["keyword_rank"] == 2

    def test_only_semantic_results(self):
        sem = [
            {"rowid": 10, "title": "S Chat 1", "summary": "S1", "tags": "", "created_at": "2024-01-01", "distance": 0.1},
        ]
        result = merge_hybrid([], sem)
        assert len(result) == 1
        assert result[0]["rowid"] == 10
        assert result[0]["keyword_rank"] is None
        assert result[0]["semantic_rank"] == 1
        assert result[0]["distance"] == 0.1

    def test_merge_both_lists(self):
        kw = [
            {"rowid": 1, "title": "K1", "summary": "KS1", "tags": "", "created_at": "2024-01-01"},
        ]
        sem = [
            {"rowid": 2, "title": "S1", "summary": "SS1", "tags": "", "created_at": "2024-01-02", "distance": 0.2},
        ]
        result = merge_hybrid(kw, sem)
        assert len(result) == 2
        # Both have same single rank, so scores should be tied
        assert all("rrf_score" in r for r in result)

    def test_same_item_in_both_boosted(self):
        """An item appearing in BOTH lists gets a higher RRF score."""
        kw = [
            {"rowid": 1, "title": "Important", "summary": "S", "tags": "", "created_at": "2024-01-01", "snippet": "match..."},
            {"rowid": 2, "title": "K only", "summary": "S", "tags": "", "created_at": "2024-01-02"},
        ]
        sem = [
            {"rowid": 1, "title": "Important", "summary": "S", "tags": "", "created_at": "2024-01-01", "distance": 0.05},
        ]
        result = merge_hybrid(kw, sem)
        assert len(result) == 2
        # Rowid 1 is in both lists → highest score
        assert result[0]["rowid"] == 1
        assert result[0]["keyword_rank"] == 1
        assert result[0]["semantic_rank"] == 1
        assert result[0]["rrf_score"] > result[1]["rrf_score"]

    def test_rrf_score_calculation(self):
        """RRF: score = 1/(k + rank), rounded to 6 decimal places."""
        kw = [
            {"rowid": 1, "title": "T1", "summary": "", "tags": "", "created_at": ""},
        ]
        # Single keyword match at rank 1: round(1/(60+1), 6) = round(0.0163934426..., 6) = 0.016393
        result = merge_hybrid(kw, [])
        assert len(result) == 1
        assert result[0]["rrf_score"] == round(1.0 / (RRF_K + 1), 6)

    def test_custom_k_value(self):
        kw = [{"rowid": 1, "title": "T", "summary": "", "tags": "", "created_at": ""}]
        result = merge_hybrid(kw, [], k=10)
        assert result[0]["rrf_score"] == round(1.0 / 11, 6)

    def test_skips_items_without_rowid(self):
        kw = [
            {"title": "no rowid"},
            {"rowid": 1, "title": "has rowid", "summary": "", "tags": "", "created_at": ""},
        ]
        result = merge_hybrid(kw, [])
        assert len(result) == 1
        assert result[0]["rowid"] == 1

    def test_info_populated_from_keyword(self):
        kw = [{
            "rowid": 42, "user_message": "My Chat", "summary": "Great chat",
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
            "rowid": 7, "user_message": "Semantic", "summary": "Sem summary",
            "tags": "sem", "created_at": "2024-07-01",
            "distance": 0.123,
        }]
        result = merge_hybrid([], sem)
        r = result[0]
        assert r["user_message"] == "Semantic"
        assert r["distance"] == 0.123
        assert r["keyword_rank"] is None
        assert r["snippet"] == ""
