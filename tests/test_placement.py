"""Tier 1 — Placement.would_trap, over synthetic terrain.

A placement check that says "no" too often is as bad as one that says "yes" too rarely:
the caller gets None and the build stalls. So these pin both directions — a candidate
that really does seal a pocket, and the ordinary cases that must be allowed through.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from placement import Placement, _tile_span
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from fakes import FakeUnits, fake_bot, fake_unit

PYLON_R = 1.0  # 2x2
MAP_W = MAP_H = 64  # the fake map's grid, big enough that the cases above never clamp


def _bot(*, walls=(), structures=(), minerals=()):
    """A bot whose terrain is walkable everywhere except `walls` (tile coords).

    `in_pathing_grid` ASSERTS off the grid, exactly as the real one does (it indexes a
    PixelMap, whose __getitem__ asserts bounds). Without that the fake silently accepts
    negative tiles and a window running off the map reads as ordinary open ground —
    which is how an out-of-bounds scan reached a real game and killed the run.
    """
    blocked = set(walls)

    def in_pathing_grid(p):
        x, y = int(p.x), int(p.y)
        assert 0 <= x < MAP_W, f"x is {x}, self.width is {MAP_W}"
        assert 0 <= y < MAP_H, f"y is {y}, self.height is {MAP_H}"
        return (x, y) not in blocked

    return fake_bot(
        in_pathing_grid=in_pathing_grid,
        structures=FakeUnits(list(structures)),
        mineral_field=FakeUnits(list(minerals)),
        vespene_geyser=FakeUnits(),
        game_info=SimpleNamespace(
            pathing_grid=SimpleNamespace(width=MAP_W, height=MAP_H)),
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


def _corner_pocket_walls():
    """_pocket_walls shifted into the low corner: a room at x 3..5, y 3..9 whose only way
    in is (2, 7). Close enough to the edge that a _TRAP_WINDOW-sized scan runs off it."""
    walls = set()
    walls |= {(6, y) for y in range(2, 11)}           # far side
    walls |= {(x, 2) for x in range(2, 7)}            # top
    walls |= {(x, 10) for x in range(2, 7)}           # bottom
    walls |= {(2, y) for y in range(2, 11) if y != 7}  # near side, one tile open
    return walls


@pytest.mark.parametrize("pos", [
    Point2((5.0, 5.0)),                    # low corner: lo_x/lo_y would go negative
    Point2((MAP_W - 4.0, MAP_H - 4.0)),    # high corner: hi_x/hi_y would run past the grid
])
def test_a_candidate_near_the_map_edge_does_not_index_off_the_grid(pos):
    # The window is centred on the candidate and _TRAP_WINDOW (12) wide, so anything
    # within 12 tiles of an edge used to scan tiles that don't exist. in_pathing_grid
    # asserts on those, which took down the whole run: "AssertionError: y is -1".
    p = Placement(_bot())
    assert p.would_trap(U.PYLON, pos) is False


def test_the_trap_check_still_works_next_to_a_map_edge():
    # The window is CLAMPED to the grid, not abandoned. Were it abandoned, the edge ring
    # would be entirely off-map, `before` would come back empty, and would_trap would
    # return False for everything near an edge — quietly disabling the check exactly
    # where the map is tightest.
    p = Placement(_bot(walls=_corner_pocket_walls()))
    assert p.would_trap(U.PYLON, Point2((2.0, 7.0))) is True


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


# ------------------------------------------------------------------ find_building_location
def _target_bot(**over):
    base = dict(_resolve_place=lambda where: Point2((40.0, 40.0)), _status="")
    base.update(over)
    return fake_bot(**base)


def _placement_for(bot):
    """A real Placement over a fake bot, so find_building_location's dispatch is the production one."""
    return Placement(bot)


async def test_nexus_where_aims_at_the_exact_base_location():
    # NOT a placement search near it — an expansion even a tile off-centre from its
    # mineral patches mines slower for the rest of the game.
    bot = _target_bot(townhalls=MagicMock(closer_than=lambda d, p: FakeUnits()))
    step = fake_bot(what="Nexus", where="natural")
    assert await _placement_for(bot).find_building_location(step) == Point2((40.0, 40.0))


async def test_nexus_where_stalls_when_that_base_is_already_ours():
    # the alternative is handing build() an occupied tile, which wedges the Nexus into
    # whatever spot a placement search can scrape together nearby
    bot = _target_bot(townhalls=MagicMock(closer_than=lambda d, p: FakeUnits([object()])))
    step = fake_bot(what="Nexus", where="natural")
    assert await _placement_for(bot).find_building_location(step) is None
    assert bot._status == "the natural is already ours"


async def test_nexus_without_where_reports_when_the_map_is_full():
    bot = _target_bot(_find_next_expansion=lambda: None)
    step = fake_bot(what="Nexus", where=None)
    assert await _placement_for(bot).find_building_location(step) is None
    assert bot._status == "every expansion is already taken"


async def test_gas_at_a_named_base_still_goes_on_a_geyser():
    # `where:` picks WHICH geyser; it must not send a refinery to open ground. Testing
    # `where` before the type did exactly that, and the game refuses the placement.
    geyser = fake_unit(position=Point2((41.0, 41.0)))
    bot = _target_bot(vespene_geyser=FakeUnits([geyser]),
                      gas_buildings=FakeUnits(),
                      find_placement=AsyncMock(return_value=Point2((9.0, 9.0))))
    step = fake_bot(what="Assimilator", where="natural")
    assert await _placement_for(bot).find_building_location(step) is geyser
    bot.find_placement.assert_not_awaited()


async def test_gas_at_a_named_base_says_so_when_that_base_has_none_free():
    taken = fake_unit(position=Point2((41.0, 41.0)))
    bot = _target_bot(vespene_geyser=FakeUnits([taken]),
                      gas_buildings=MagicMock(closer_than=lambda d, g: FakeUnits([object()])))
    step = fake_bot(what="Assimilator", where="natural")
    assert await _placement_for(bot).find_building_location(step) is None
    assert bot._status == "no free geyser at natural"


async def test_stalled_build_says_there_is_nowhere_to_put_it():
    """Without this, the stall reaches [status] as "issued; waiting to confirm", which is
    indistinguishable from the build working."""
    bot = _target_bot()
    p = _placement_for(bot)
    p.building_position = AsyncMock(return_value=None)
    assert await p.find_building_location(fake_bot(what="Gateway", where=None)) is None
    assert bot._status == "no powered spot for Gateway at any base"


async def test_pylon_with_nowhere_to_go_says_so():
    bot = _target_bot()
    p = _placement_for(bot)
    p.pylon_position = AsyncMock(return_value=None)
    assert await p.find_building_location(fake_bot(what="Pylon", where=None)) is None
    assert bot._status == "nowhere left to put a Pylon"


async def test_successful_placement_leaves_no_stall_reason():
    bot = _target_bot()
    p = _placement_for(bot)
    p.building_position = AsyncMock(return_value=Point2((5.0, 5.0)))
    assert await p.find_building_location(fake_bot(what="Gateway", where=None)) == Point2((5.0, 5.0))
    assert bot._status == ""


async def test_where_anchored_build_sweeps_offsets_for_a_safe_spot():
    # Covers the `where:` branch end to end. Nothing did, which is how a missing
    # PLACE_OFFSETS import once shipped green: the unit suite passed while any `where:`
    # build raised NameError on its first frame in a real game.
    trapping, safe = Point2((1.0, 1.0)), Point2((9.0, 9.0))
    seen = []

    async def find_placement(unit, near, **kw):
        seen.append((near.x, near.y))
        return trapping if len(seen) == 1 else safe

    bot = _target_bot(find_placement=find_placement)
    p = _placement_for(bot)
    p.would_trap = lambda unit, pos: pos == trapping
    assert await p.find_building_location(fake_bot(what="Gateway", where="proxy")) == safe
    assert len(seen) > 1, "a rejected candidate must not end the search"


async def test_where_anchored_build_says_so_when_every_offset_traps():
    bot = _target_bot(find_placement=AsyncMock(return_value=Point2((1.0, 1.0))))
    p = _placement_for(bot)
    p.would_trap = lambda unit, pos: True
    assert await p.find_building_location(fake_bot(what="Gateway", where="proxy")) is None
    assert bot._status == "nowhere safe to put Gateway at proxy"

