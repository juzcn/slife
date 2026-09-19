"""Tests for slife.a2a.inbound_store — inbound tasks across a restart.

An inbound task's completion bridge lives and dies with the process that
received it, so a fresh process has to be able to tell "the last one was
holding this" from "I have never seen this id".  These tests cover that
classification directly; ``test_a2a_mesh`` covers what the mesh does with it.
"""

import json

import pytest; pytestmark = pytest.mark.unit

from slife.a2a.inbound_store import InboundStore, _MAX_ENTRIES


def _restart(path):
    """A fresh store over the same file — a restarted process."""
    return InboundStore(path)


class TestRestartOrphansPending:
    def test_pending_becomes_stale_across_a_restart(self, tmp_path):
        path = tmp_path / "state.json"
        store = InboundStore(path)
        store.add("t1", "jack")
        assert store.stale() == []  # still in flight — nothing orphaned yet

        restarted = _restart(path)
        stale = restarted.stale()
        assert [t.task_id for t in stale] == ["t1"]
        assert stale[0].peer == "jack"
        assert stale[0].since  # arrival time survives too

    def test_a_completed_task_is_not_stale(self, tmp_path):
        path = tmp_path / "state.json"
        store = InboundStore(path)
        store.add("t1", "jack")
        store.drop("t1")  # answered before the restart

        assert _restart(path).stale() == []

    def test_stale_accumulates_across_repeated_restarts(self, tmp_path):
        """A task orphaned two restarts ago is no more completable than one
        orphaned by the last — dropping it would forgive a reply still owed."""
        path = tmp_path / "state.json"
        first = InboundStore(path)
        first.add("old", "jack")

        second = _restart(path)
        second.add("new", "jack")

        third = _restart(path)
        assert sorted(t.task_id for t in third.stale()) == ["new", "old"]

    def test_dropping_an_unknown_id_is_a_noop(self, tmp_path):
        store = InboundStore(tmp_path / "state.json")
        store.drop("never-seen")
        assert store.stale() == []


class TestRetryRevives:
    def test_a_resent_task_is_live_again(self, tmp_path):
        """A peer that retries re-sends the same Task.id, and the retry
        genuinely re-registers the bridge — so it is completable again."""
        path = tmp_path / "state.json"
        InboundStore(path).add("t1", "jack")

        restarted = _restart(path)
        assert len(restarted.stale()) == 1

        restarted.add("t1", "jack")  # the retry arrives
        assert restarted.stale() == []

    def test_the_revival_survives_another_restart(self, tmp_path):
        path = tmp_path / "state.json"
        InboundStore(path).add("t1", "jack")
        second = _restart(path)
        second.add("t1", "jack")
        second.drop("t1")  # answered

        assert _restart(path).stale() == []


class TestClearStalePeer:
    def test_clears_only_that_peer(self, tmp_path):
        path = tmp_path / "state.json"
        first = InboundStore(path)
        first.add("a1", "jack")
        first.add("b1", "jill")

        second = _restart(path)
        assert len(second.stale()) == 2

        second.clear_stale_peer("jack")
        assert [t.task_id for t in second.stale()] == ["b1"]

    def test_the_clear_persists(self, tmp_path):
        path = tmp_path / "state.json"
        InboundStore(path).add("a1", "jack")
        _restart(path).clear_stale_peer("jack")

        assert _restart(path).stale() == []

    def test_an_unrelated_peer_changes_nothing(self, tmp_path):
        path = tmp_path / "state.json"
        InboundStore(path).add("a1", "jack")
        store = _restart(path)
        store.clear_stale_peer("nobody")
        assert len(store.stale()) == 1


class TestRobustness:
    def test_a_corrupt_file_is_survivable(self, tmp_path):
        """Losing the state costs a reminder, not protocol state — it must
        never block the mesh."""
        path = tmp_path / "state.json"
        path.write_text("{not json", encoding="utf-8")

        assert InboundStore(path).stale() == []

    def test_a_non_dict_payload_is_survivable(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text('["nope"]', encoding="utf-8")
        assert InboundStore(path).stale() == []

    def test_malformed_entries_are_skipped(self, tmp_path):
        path = tmp_path / "state.json"
        path.write_text(json.dumps({
            "pending": {"good": {"peer": "jack", "since": "2026-01-01T00:00:00Z"},
                        "bad": "not-a-dict"},
        }), encoding="utf-8")

        stale = InboundStore(path).stale()
        assert [t.task_id for t in stale] == ["good"]

    def test_entries_are_capped(self, tmp_path):
        path = tmp_path / "state.json"
        store = InboundStore(path)
        for i in range(_MAX_ENTRIES + 20):
            store.add(f"t{i:04d}", "jack")
        # The cap holds the most recent _MAX_ENTRIES; the oldest are gone.
        assert len(_restart(path).stale()) == _MAX_ENTRIES

    def test_a_missing_file_is_not_an_error(self, tmp_path):
        assert InboundStore(tmp_path / "absent.json").stale() == []

    def test_state_is_written_on_disk(self, tmp_path):
        path = tmp_path / "state.json"
        InboundStore(path).add("t1", "jack")
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["pending"]["t1"]["peer"] == "jack"
