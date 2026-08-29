"""match_nearest — the optimal distance pairing behind the opening worker split.

Pure and game-free, unlike most of this suite, so it is exercised directly on plain
objects rather than through the fake-bot harness.
"""

from __future__ import annotations

from matching import match_nearest


class _P:
    """Something with a position on a line — stands in for a unit or a destination."""

    def __init__(self, x: float) -> None:
        self.x = x

    def distance_to(self, other: "_P") -> float:
        return abs(self.x - other.x)

    def __repr__(self) -> str:
        return f"@{self.x}"


def _total(pairs) -> float:
    return sum(a.distance_to(b) for a, b in pairs)


def test_pairs_each_item_with_its_own_target():
    items = [_P(0), _P(10), _P(5)]
    targets = [_P(10), _P(0), _P(5)]
    pairs = match_nearest(items, targets)
    assert all(a.x == b.x for a, b in pairs), pairs
    assert len({id(b) for _, b in pairs}) == 3, "a target was handed out twice"


class _Tabulated:
    """Distances read from a table, so an arbitrary cost matrix can be written down.

    Needed because on a line the best total is also what you get by pairing each item
    with its own nearest target, so a 1-D example can't tell the two apart.
    """

    def __init__(self, name: str, row: dict[str, float] | None = None) -> None:
        self.name = name
        self.row = row or {}

    def distance_to(self, other: "_Tabulated") -> float:
        return self.row[other.name]


def test_minimises_the_total_not_each_pair():
    """A has the shortest single option (A->X, 1), but taking it forces B->Y at 100. The
    total wins: both settle for 2."""
    x, y = _Tabulated("X"), _Tabulated("Y")
    a = _Tabulated("A", {"X": 1, "Y": 2})
    b = _Tabulated("B", {"X": 2, "Y": 100})
    pairs = match_nearest([a, b], [x, y])
    assert _total(pairs) == 4, pairs        # A->Y (2) + B->X (2), not 1 + 100
    assert [(p.name, q.name) for p, q in pairs] == [("A", "Y"), ("B", "X")]


def test_handles_nothing_to_match():
    """Reachable from opening_split once every probe has been filtered out (e.g. all of
    them on gas); scipy raises on an empty cost matrix rather than returning no pairs."""
    assert match_nearest([], []) == []
    assert match_nearest([_P(0)], []) == []
    assert match_nearest([], [_P(0)]) == []


def test_handles_a_repeated_target():
    """A target may appear more than once — that is how the split puts two probes on one
    patch — and each entry is consumed separately."""
    shared = _P(5)
    pairs = match_nearest([_P(4), _P(6)], [shared, shared])
    assert {id(b) for _, b in pairs} == {id(shared)}
    assert len(pairs) == 2


def test_truncates_to_the_shorter_list():
    pairs = match_nearest([_P(0), _P(1), _P(2)], [_P(0)])
    assert len(pairs) == 1
    assert pairs[0][0].x == 0
