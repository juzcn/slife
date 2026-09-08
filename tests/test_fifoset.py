"""Tests for slife.fifoset.FifoSet — FIFO-bounded id tracking (review F10).

The regression: ``set.pop()`` evicts an *arbitrary* member, so a capped id
set (cancelled tasks / late results / poll-mode task ids) could drop a
just-added id while an old one survives.  FifoSet drops the OLDEST.
"""

import pytest; pytestmark = pytest.mark.unit


from slife.fifoset import FifoSet


def test_plain_set_api_preserved():
    """FifoSet still quacks like the set it replaces (in/eq/add/len)."""
    s = FifoSet(["a", "b"])
    assert s == {"a", "b"}
    s.add("c")
    assert "a" in s and len(s) == 3
    s.discard("b")
    assert s == {"a", "c"}
    assert len(s) == 2


def test_evict_to_drops_oldest_not_arbitrary():
    """F10 regression: over-cap eviction removes the OLDEST id first."""
    s = FifoSet(["a", "b", "c"])
    s.add("d")
    s.add("e")
    s.evict_to(3)
    assert s == {"c", "d", "e"}  # a, b (the inserted-first) went, not arbitrary


def test_evict_to_keeps_under_cap_untouched():
    s = FifoSet(["x"])
    s.add("y")
    s.evict_to(10)
    assert s == {"x", "y"}


def test_readd_is_noop_position():
    """Re-adding an existing id does not duplicate it or change order."""
    s = FifoSet(["a", "b", "c"])
    s.add("a")
    s.add("c")
    s.add("b")          # b exists — no-op, keeps first-insert position
    s.evict_to(2)
    assert s == {"b", "c"}  # oldest a evicted first; b never re-advanced

    s.add("b")          # still no-op even after an eviction cycle
    s.add("x")
    s.evict_to(2)
    assert s == {"c", "x"}


def test_discard_prunes_order():
    """A discarded id can never be evicted again (no stale entry)."""
    s = FifoSet(["a", "b", "c"])
    s.discard("a")
    s.add("d")
    s.evict_to(2)
    assert s == {"c", "d"}


def test_clear_resets_order():
    s = FifoSet(["a", "b"])
    s.clear()
    assert s == set()
    s.add("new")
    s.evict_to(1)
    assert s == {"new"}


def test_pop_oldest_empty_raises():
    with pytest.raises(KeyError):
        FifoSet().pop_oldest()