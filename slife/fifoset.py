"""FIFO-bounded id tracking — a set that evicts the OLDEST member.

``set.pop()`` removes an *arbitrary* member, so a size-capped ``set`` used
to bookkeep recent ids (cancelled tasks, late results, poll-mode task ids)
could evict a just-added id while an old one survives — the review's F10.
This wrapper keeps the plain-set API (``in``, ``== set()``, ``.add``,
``.discard``, ``.clear``, ``len``) that callers and tests rely on, but
eviction drops the oldest inserted id instead of a random one.
"""

from __future__ import annotations

from collections import deque as _deque


class FifoSet(set):
    """A ``set`` whose over-cap eviction removes the OLDEST member.

    Insertion order is tracked in an internal deque; ``pop_oldest()``
    removes the earliest-still-present member.  ``add`` of an existing id is
    a no-op (it keeps its original position — a fresh insert advances it to
    the back, so a re-registered id behaves as newest).  ``discard`` /
    ``clear`` prune the order queue too, so stale ids never linger.
    """

    __slots__ = ("_order",)

    def __init__(self, iterable=()):
        # Capture the iterable's ORDER first — set.__init__ bulk-loads
        # without calling our overridden ``add``, and ``_deque(self)`` would
        # iterate a real set in arbitrary hash order.
        seq = list(dict.fromkeys(iterable))  # dedupe, keep first-insert position
        super().__init__(seq)
        self._order = _deque(seq)

    def add(self, item: str) -> None:
        if item in self:
            return
        super().add(item)
        self._order.append(item)

    def discard(self, item: object) -> None:
        if item not in self:
            return
        super().discard(item)
        try:
            self._order.remove(item)  # O(n) — bounded by the cap (a few hundred)
        except ValueError:
            pass  # stale order entries are skipped by pop_oldest anyway

    def clear(self) -> None:
        super().clear()
        self._order.clear()

    def pop_oldest(self) -> str:
        """Remove and return the oldest member; KeyError when empty."""
        while self._order:
            oldest = self._order.popleft()
            if oldest in self:
                super().discard(oldest)
                return oldest
        raise KeyError("pop from an empty FifoSet")

    def evict_to(self, cap: int) -> None:
        """Drop oldest members until ``len(self) <= cap``."""
        while len(self) > cap:
            self.pop_oldest()