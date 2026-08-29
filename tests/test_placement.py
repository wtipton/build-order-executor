"""Tier 1 — Placement.would_trap, over synthetic terrain.

A placement check that says "no" too often is as bad as one that says "yes" too rarely:
the caller gets None and the build stalls. So these pin both directions — a candidate
that really does seal a pocket, and the ordinary cases that must be allowed through.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from placement import Placement, _tile_span
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from fakes import FakeUnits, fake_bot, fake_unit

PYLON_R = 1.0  # 2x2


def _bot(*, walls=(), structures=(), minerals=()):
    """A bot whose terrain is walkable everywhere except `walls` (tile coords)."""
    blocked = set(walls)
    return fake_bot(
        in_pathing_grid=lambda p: (int(p.x), int(p.y)) not in blocked,
        structures=FakeUnits(list(structures)),
        mineral_field=FakeUnits(list(minerals)),
        vespene_geyser=FakeUnits(),
        game_data=SimpleNamespace(
            units={U.PYLON.value: SimpleNamespace(footprint_radius=PYLON_R)}),
    )


def _pocket_walls():
    """A room at x 49..51, y 47..53 whose only way in is the single tile (48, 51)."""
    walls = set()
    walls |= {(52, y) for y in range(46, 55)}          # far side
    walls |= {(x, 46) for x in range(48, 53)}          # top
    walls |= {(x, 54) for x in range(48, 53)}          # bottom
    walls |= {(48, y) for y in range(46, 55) if y != 51}  # near side, one tile open
    return walls


# ------------------------------------------------------------------ tile span
@pytest.mark.parametrize("centre,radius,want", [
    (30.0, 1.0, [29, 30]),           # 2x2 Pylon
    (30.5, 1.5, [29, 30, 31]),       # 3x3 Gateway
    (30.5, 2.5, [28, 29, 30, 31, 32]),  # 5x5 Nexus
])
def test_tile_span_covers_exactly_the_footprint(centre, radius, want):
    # Tile n covers [n, n+1). Counting one extra per axis makes every structure read a
    # tile fatter than it is, which has would_trap reject placements that are fine.
    assert list(_tile_span(centre, radius)) == want


# ------------------------------------------------------------------ would_trap
def test_plugging_the_only_way_into_a_room_is_a_trap():
    p = Placement(_bot(walls=_pocket_walls()))
    assert p.would_trap(U.PYLON, Point2((48.0, 51.0))) is True


def test_open_ground_is_not_a_trap():
    p = Placement(_bot())
    assert p.would_trap(U.PYLON, Point2((50.0, 50.0))) is False


def test_a_room_that_was_already_sealed_is_not_blamed_on_this_building():
    # The room has NO opening before we place anything, so its ground was already
    # unreachable. Comparing before/after is what keeps us from rejecting every candidate
    # near a pre-existing pocket.
    walls = _pocket_walls() | {(48, 51)}
    p = Placement(_bot(walls=walls))
    assert p.would_trap(U.PYLON, Point2((44.0, 44.0))) is False


def test_building_beside_a_gap_it_does_not_cover_is_not_a_trap():
    # 2 tiles clear of the opening: the room is still reachable, so this must be allowed.
    p = Placement(_bot(walls=_pocket_walls()))
    assert p.would_trap(U.PYLON, Point2((45.0, 51.0))) is False


def test_no_walkable_ground_at_the_window_edge_is_not_judged():
    # Nothing reaches the edge, so there's no basis to say what this building cuts off.
    # Returning True here would refuse every placement in a walled-off corner of the map.
    p = Placement(_bot(walls={(x, y) for x in range(30, 70) for y in range(30, 70)}))
    assert p.would_trap(U.PYLON, Point2((50.0, 50.0))) is False


def test_structures_and_minerals_count_as_walls():
    # The terrain grid is the game's STATIC start-of-game pathing, so it knows nothing
    # about what we've built — and a probe pinned between a Pylon and the mineral line is
    # the case this exists for. Same room, but the near side is minerals rather than cliff.
    walls = {(52, y) for y in range(46, 55)}
    walls |= {(x, 46) for x in range(48, 53)} | {(x, 54) for x in range(48, 53)}
    # tile CENTRES: a radius-0.5 unit centred on a tile boundary straddles two tiles,
    # which would seal the gap before the candidate even goes down
    patches = [fake_unit(position=Point2((48.5, y + 0.5)), radius=0.5)
               for y in range(46, 55) if y != 51]
    p = Placement(_bot(walls=walls, minerals=patches))
    assert p.would_trap(U.PYLON, Point2((48.0, 51.0))) is True
