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

from catalog import BLOCKABLE_PRODUCTION

if TYPE_CHECKING:
    from bot import BuildOrderBot

# Deterministic anchor offsets for packing buildings around a pylon: the tight spot
# first, then step out in fixed directions. Used to retry when the nearest spot would
# seal a producer, WITHOUT random_alternative — so placement is a pure function of
# game state (identical runs reproduce exactly; see run.py GAME_SEED).
_PACK_OFFSETS = ((0, 0), (2, 0), (0, 2), (-2, 0), (0, -2),
                 (3, 3), (-3, 3), (3, -3), (-3, -3))


class Placement:
    def __init__(self, bot: BuildOrderBot) -> None:
        self.bot = bot

    # ---------------------------------------------------------------- helpers
    def _mineral_line_center(self, near: Unit) -> Point2 | None:
        """Middle of the mineral patches by `near` (a Nexus or a pylon), or None if there
        are none. Used as a DIRECTION, not a destination: callers build away from it so we
        don't drop structures into the mineral line and block mining."""
        mins = self.bot.mineral_field.closer_than(12, near)
        return mins.center if mins else None

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
                if pos is not None:
                    return pos

        # 2) otherwise spread: pick the buildable ring candidate around the main
        #    base that is FARTHEST from existing pylons. Base 1 IS the main — take it
        #    from world.py rather than re-deriving 'nearest our start' here.
        main = bot.ordered_bases()[0]
        mc = self._mineral_line_center(main)
        best, best_score = None, -1.0
        for radius in (7, 10, 13):
            for k in range(8):
                ang = 2 * math.pi * k / 8
                cand = Point2((main.position.x + radius * math.cos(ang),
                               main.position.y + radius * math.sin(ang)))
                if mc is not None and cand.distance_to(mc) < main.position.distance_to(mc):
                    continue  # on the mineral side of the Nexus — skip
                pos = await bot.find_placement(U.PYLON, near=cand, max_distance=3, random_alternative=False)
                if pos is None:
                    continue
                score = min((pos.distance_to(p.position) for p in bot.structures(U.PYLON)), default=99.0)
                if score > best_score:
                    best, best_score = pos, score
        if best is not None:
            return best

        # 3) fallback: anything near the main toward the map center
        return await bot.find_placement(
            U.PYLON, near=main.position.towards(bot.game_info.map_center, 6), max_distance=12,
            random_alternative=False,
        )

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
            for dx, dy in _PACK_OFFSETS:
                pos = await bot.find_placement(
                    unit, near=Point2((anchor.x + dx, anchor.y + dy)),
                    max_distance=10, placement_step=1, random_alternative=False,
                )
                if pos is not None and not self._would_seal_producer(pos):
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
