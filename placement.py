"""Building placement heuristics, factored out of bot.py.

Kept deliberately simple (no walling yet — just "make it easy to fit buildings"):

  * pylon_position()    — give every Nexus a nearby pylon (offset off the mineral line
      so it's not buried in the minerals), then spread further pylons across the base so
      there's powered room everywhere.
  * building_position() — pack buildings tight against a powering pylon, but never take
      the last exit of an existing production building (BLOCKABLE_PRODUCTION): units must
      have somewhere to spawn (the bot also rallies them off the exit — see apply_rally).

All methods take the live bot via `self.bot` and return a Point2 (or None if no
spot is available yet, e.g. before a pylon exists).
"""

from __future__ import annotations

import math

from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.unit import Unit

from catalog import BLOCKABLE_PRODUCTION, unit_id

from schema import Step

if TYPE_CHECKING:
    from bot import BuildOrderBot

# Deterministic anchor offsets for packing buildings around a pylon: the tight spot
# first, then step out in fixed directions. Used to retry when the nearest spot would
# seal a producer, WITHOUT random_alternative — so placement is a pure function of
# game state (identical runs reproduce exactly; see run.py GAME_SEED).
PLACE_OFFSETS = ((0, 0), (2, 0), (0, 2), (-2, 0), (0, -2),
                 (3, 3), (-3, 3), (3, -3), (-3, -3))

# How far either side of a candidate we look for pockets it would seal off. Traps that
# catch a worker are local — a probe wedged between a Pylon and the mineral line — so a
# window is enough, and it keeps the check cheap enough to run on every candidate.
_TRAP_WINDOW = 12


def _tile_span(centre: float, radius: float) -> range:
    """Tile indices something of `radius` centred at `centre` covers on one axis.

    Tile n covers [n, n+1), so the last covered tile is ceil(centre + radius) - 1. Using
    int(centre + radius) + 1 as the bound instead counts one tile too many per axis — a
    2x2 Pylon reads as 3x3 — which makes occupancy fatter than it is and has would_trap
    reject placements that are actually fine."""
    return range(math.floor(centre - radius), math.ceil(centre + radius))


class Placement:
    def __init__(self, bot: BuildOrderBot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- dispatch
    async def find_building_location(self, step: Step) -> Point2 | Unit | None:
        """Where `step` should aim its build: a position, or the geyser itself for an
        Assimilator. The single place that decides, for every structure type.

        Dispatches on TYPE first and treats `where:` as an ANCHOR, never as a bypass. A
        Nexus belongs on a base location and an Assimilator on a geyser whether or not the
        author named a place; `where:` says WHICH base or WHICH geyser, and for everything
        else it says where to search. Ordering it the other way — testing `where:` before
        the type — sends `build Assimilator where: natural` off to place a refinery on
        open ground, which the game refuses, and the step stalls.

        Returning None stalls the step, so each case sets `bot._status` on the way out:
        do_build only has "no placement" to offer, which can't distinguish "not yet" from
        "never"."""
        unit = unit_id(step.what)
        where = getattr(step, "where", None)
        anchor = self.bot._resolve_place(where) if where is not None else None

        if unit == U.NEXUS:
            return self._expansion_target(where, anchor)
        if unit == U.ASSIMILATOR:
            return self._geyser_target(where, anchor)
        if anchor is not None:
            return await self._spot_at(unit, step.what, where, anchor)
        if unit == U.PYLON:
            pos = await self.pylon_position()
            if pos is None:
                self.bot._status = "nowhere left to put a Pylon"
            return pos
        pos = await self.building_position(unit)
        if pos is None:
            self.bot._status = f"no powered spot for {step.what} at any base"
        return pos

    def _expansion_target(self, where: str | None, anchor: Point2 | None) -> Point2 | None:
        """An expansion belongs ON a base location, not merely somewhere placeable near
        one, so this never falls back to a placement search."""
        if anchor is None:
            nxt = self.bot._find_next_expansion()
            if nxt is None:
                self.bot._status = "every expansion is already taken"
            return nxt
        if self.bot.townhalls.closer_than(3.0, anchor):
            self.bot._status = f"the {where} is already ours"  # stall, don't misplace it
            return None
        return anchor

    def _geyser_target(self, where: str | None, anchor: Point2 | None) -> Unit | None:
        geyser = self._free_geyser(anchor)
        if geyser is None:
            self.bot._status = (f"no free geyser at {where}" if where is not None
                                else "no geyser without an Assimilator at any of our bases")
        return geyser

    async def _spot_at(self, unit: U, what: str, where: str, anchor: Point2) -> Point2 | None:
        """A spot for `unit` around a named place.

        Sweeps offsets rather than taking find_placement's single nearest answer: pairing
        a rejection with one candidate can only turn a bad placement into NO placement.
        Anchored on a Nexus the nearest open tile is usually the gap between it and the
        minerals, which is exactly the pocket would_trap refuses — `where: sixth` stalled
        a build for 316s that way."""
        for dx, dy in PLACE_OFFSETS:
            pos = await self.bot.find_placement(
                unit, near=Point2((anchor.x + dx, anchor.y + dy)), max_distance=20,
                random_alternative=False)
            if pos is not None and not self.would_trap(unit, pos):
                return pos
        self.bot._status = f"nowhere safe to put {what} at {where}"
        return None

    def _free_geyser(self, near: Point2 | None = None) -> Unit | None:
        """A geyser with no Assimilator on it yet, or None.

        `near` restricts to that base — which is what `where:` means for gas. Without it,
        any of our ready bases will do. With it we look at geysers by the place itself
        rather than by a townhall, so naming a base we haven't taken yet still works."""
        if near is not None:
            candidates = self.bot.vespene_geyser.closer_than(10, near)
        else:
            candidates = [g for th in self.bot.townhalls.ready
                          for g in self.bot.vespene_geyser.closer_than(10, th)]
        for g in candidates:
            if not self.bot.gas_buildings.closer_than(1, g):
                return g
        return None

    # ---------------------------------------------------------------- helpers
    def _mineral_line_center(self, near: Point2 | Unit) -> Point2 | None:
        """Middle of the mineral patches by `near` (a Nexus or a pylon), or None if there
        are none. Used as a DIRECTION, not a destination: callers build away from it so we
        don't drop structures into the mineral line and block mining."""
        mins = self.bot.mineral_field.closer_than(12, near)
        return mins.center if mins else None

    def _blocked_tiles(self, cx: int, cy: int) -> set[tuple[int, int]]:
        """Tiles in the window that a ground unit can't walk through because something is
        standing on them.

        Structures and resources have to be collected by hand: `game_info.pathing_grid`
        is the STATIC terrain from the game's start, so it knows nothing about what we've
        built, and the mineral line is exactly what a probe gets pinned against."""
        out: set[tuple[int, int]] = set()
        for units in (self.bot.structures, self.bot.mineral_field, self.bot.vespene_geyser):
            for u in units:
                r = max(u.radius, 0.5)
                if (abs(u.position.x - cx) > _TRAP_WINDOW + r + 1
                        or abs(u.position.y - cy) > _TRAP_WINDOW + r + 1):
                    continue
                for tx in _tile_span(u.position.x, r):
                    for ty in _tile_span(u.position.y, r):
                        out.add((tx, ty))
        return out

    def _footprint(self, unit: U, pos: Point2) -> set[tuple[int, int]]:
        """Tiles a `unit` built at `pos` would stand on."""
        r = self.bot.game_data.units[unit.value].footprint_radius or 1.0
        return {(tx, ty) for tx in _tile_span(pos.x, r) for ty in _tile_span(pos.y, r)}

    def would_trap(self, unit: U, pos: Point2) -> bool:
        """Would putting `unit` at `pos` seal off ground a worker could walk into but not
        out of?

        Asked of every candidate placement. A probe pinned in a pocket can't build, and
        do_build re-issues to it forever — a live run lost a build order that way, stuck
        at `dist=7.5` for four minutes while supply-capped. The cost of being wrong is a
        dead run, and the executor exists to time build orders, so a placement that can
        trap a worker is not worth taking.

        Compares reachability with and without the candidate rather than just looking for
        enclosed ground, so a pocket that already existed isn't blamed on this building.
        Cheap enough to do per candidate because the game only advances when we return
        (run.py runs non-realtime), so thinking here costs wall-clock, never game time."""
        cx, cy = int(pos.x), int(pos.y)
        lo_x, lo_y, hi_x, hi_y = cx - _TRAP_WINDOW, cy - _TRAP_WINDOW, cx + _TRAP_WINDOW, cy + _TRAP_WINDOW
        blocked = self._blocked_tiles(cx, cy)
        footprint = self._footprint(unit, pos)

        def walkable(t: tuple[int, int], extra: set[tuple[int, int]]) -> bool:
            return (t not in blocked and t not in extra
                    and self.bot.in_pathing_grid(Point2((t[0] + 0.5, t[1] + 0.5))))

        edge = ([(x, lo_y) for x in range(lo_x, hi_x + 1)]
                + [(x, hi_y) for x in range(lo_x, hi_x + 1)]
                + [(lo_x, y) for y in range(lo_y, hi_y + 1)]
                + [(hi_x, y) for y in range(lo_y, hi_y + 1)])

        def reachable(extra: set[tuple[int, int]]) -> set[tuple[int, int]]:
            stack = [t for t in edge if walkable(t, extra)]
            seen = set(stack)
            while stack:
                x, y = stack.pop()
                for n in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if (lo_x <= n[0] <= hi_x and lo_y <= n[1] <= hi_y
                            and n not in seen and walkable(n, extra)):
                        seen.add(n)
                        stack.append(n)
            return seen

        before = reachable(set())
        if not before:
            return False  # nothing walkable reaches the window edge; no basis to judge
        return bool(before - reachable(footprint) - footprint)

    # ----------------------------------------------------------------- pylons
    async def pylon_position(self) -> Point2 | None:
        """Where to put the next Pylon: first any Nexus that hasn't got one, then spread
        out around the main, then anything near the main. None if nothing fits."""
        bot = self.bot
        # 1) every Nexus should have a pylon nearby (but off the mineral line). Includes
        #    Nexuses still warping in — the pylon can be built alongside, so it's up when
        #    the base is, instead of only starting once the Nexus finishes.
        for nexus in bot.townhalls:
            if not bot.structures(U.PYLON).closer_than(9, nexus):
                toward = self._mineral_line_center(nexus) or bot.game_info.map_center
                anchor = nexus.position.towards(toward, -5)  # 5 tiles AWAY from minerals
                pos = await bot.find_placement(U.PYLON, near=anchor, max_distance=7,
                                               random_alternative=False)
                if pos is not None and not self.would_trap(U.PYLON, pos):
                    return pos

        # 2) otherwise spread: pick the buildable ring candidate around the main
        #    base that is FARTHEST from existing pylons. Base 1 IS the main — take it
        #    from world.py rather than re-deriving 'nearest our start' here. A position,
        #    not the Nexus: we're picking ground around the base, not asking about it.
        main = bot._resolve_place("main")
        mc = self._mineral_line_center(main)
        best, best_score = None, -1.0
        for radius in (7, 10, 13):
            for k in range(8):
                ang = 2 * math.pi * k / 8
                cand = Point2((main.x + radius * math.cos(ang),
                               main.y + radius * math.sin(ang)))
                if mc is not None and cand.distance_to(mc) < main.distance_to(mc):
                    continue  # on the mineral side of the Nexus — skip
                pos = await bot.find_placement(U.PYLON, near=cand, max_distance=3, random_alternative=False)
                if pos is None or self.would_trap(U.PYLON, pos):
                    continue
                score = min((pos.distance_to(p.position) for p in bot.structures(U.PYLON)), default=99.0)
                if score > best_score:
                    best, best_score = pos, score
        if best is not None:
            return best

        # 3) fallback: anything near the main toward the map center
        pos = await bot.find_placement(
            U.PYLON, near=main.towards(bot.game_info.map_center, 6), max_distance=12,
            random_alternative=False,
        )
        return None if pos is not None and self.would_trap(U.PYLON, pos) else pos

    # -------------------------------------------------------------- buildings
    async def building_position(self, unit: U) -> Point2 | None:
        """Where to put `unit`: packed tight against the pylon nearest home that has room,
        never taking a producer's last exit. None if no powered spot fits yet."""
        bot = self.bot
        pylons = bot.structures(U.PYLON).ready
        if not pylons:
            return None
        # try pylons nearest the main first so the base grows outward from home
        for pylon in pylons.sorted(key=lambda p: p.distance_to(bot.start_location)):
            # Anchor on the side of the pylon AWAY from any nearby minerals, so we
            # don't build into the mineral line and block mining.
            mc = self._mineral_line_center(pylon)
            if mc is not None:
                anchor = pylon.position.towards(mc, -3)
            else:
                anchor = pylon.position.towards(bot.game_info.map_center, 3)
            # Pack tight (placement_step=1) so many buildings fit per pylon — but never
            # take the LAST exit of a nearby production building. Sweep the fixed anchor
            # offsets (deterministic — no random spread) and return the first buildable
            # spot that keeps every nearby producer's exit open (else the next pylon).
            for dx, dy in PLACE_OFFSETS:
                pos = await bot.find_placement(
                    unit, near=Point2((anchor.x + dx, anchor.y + dy)),
                    max_distance=10, placement_step=1, random_alternative=False,
                )
                if (pos is not None and not self._would_seal_producer(pos)
                        and not self.would_trap(unit, pos)):
                    return pos
        return None

    def _open_exits(self, producer: Unit) -> list[Point2]:
        """Points (8 compass directions) a unit could step onto to leave `producer`:
        walkable terrain not already under a structure.

        Measured from the producer's OWN edge (`radius`), not a fixed distance — a Nexus
        is 5x5 to a Gateway's 3x3, so a constant ring that clears a Gateway lands on the
        Nexus itself. Every probe then failed in_pathing_grid, `exits` came back empty,
        and a Nexus could never be reported as sealable.
        """
        bot = self.bot
        reach = producer.radius + 1.0
        pts = []
        for k in range(8):
            ang = k * math.pi / 4
            e = Point2((producer.position.x + reach * math.cos(ang),
                        producer.position.y + reach * math.sin(ang)))
            if bot.in_pathing_grid(e) and not bot.structures.closer_than(1.5, e):
                pts.append(e)
        return pts

    def _would_seal_producer(self, pos: Point2) -> bool:
        """Would a building at `pos` remove the LAST exit of a nearby production
        building? We keep tight packing but never fully wall a producer in — units
        must have somewhere to spawn (the bot then rallies them off it)."""
        bot = self.bot
        for prod in bot.structures(BLOCKABLE_PRODUCTION):
            if prod.position.distance_to(pos) > 6:
                continue
            exits = self._open_exits(prod)
            if exits and all(e.distance_to(pos) < 2.0 for e in exits):
                return True  # the new building would cover every remaining exit
        return False
