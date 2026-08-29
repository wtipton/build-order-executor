"""Assigning one set of things to another by distance.

Generic on purpose: the only thing required of the arguments is a `distance_to` method,
which every python-sc2 Unit and Point2 has.
"""

from __future__ import annotations

from typing import Protocol, TypeVar

from scipy.optimize import linear_sum_assignment


class Positioned(Protocol):
    def distance_to(self, other) -> float: ...


A = TypeVar("A", bound=Positioned)
B = TypeVar("B", bound=Positioned)


def match_nearest(items: list[A], targets: list[B]) -> list[tuple[A, B]]:
    """Pair each item with a target so that TOTAL distance is minimal.

    Minimises the total, which is not the same as giving each item its own nearest
    target: one item may have to take a worse target so another can have a much better one.

    `targets` may name the same object more than once (a mineral patch that is to take two
    probes appears twice); each entry is used at most once, so the number of times an
    object appears is the number of items that end up on it.

    Returns min(len(items), len(targets)) pairs, in items order. Either list may be empty.
    """
    if not items or not targets:
        return []  # scipy rejects an empty cost matrix rather than returning no pairs
    cost = [[a.distance_to(b) for b in targets] for a in items]
    rows, cols = linear_sum_assignment(cost)
    return [(items[r], targets[c]) for r, c in zip(rows, cols)]
