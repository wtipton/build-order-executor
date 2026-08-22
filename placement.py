"""Building placement heuristics, factored out of bot.py.

Kept deliberately simple (no walling yet — just "make it easy to fit buildings"):

  * pylon():    give every Nexus a nearby pylon (offset off the mineral line so
                it's not buried in the minerals), then spread further pylons out
                across the base so there's powered room everywhere.
  * building(): pack buildings tight against a powering pylon, but never place one
                where it would take the last exit of an existing production building
                (PRODUCTION) — units must have somewhere to spawn (the bot also
                auto-rallies them off the exit — see apply_rally).

All methods take the live bot via `self.bot` and return a Point2 (or None if no
spot is available yet, e.g. before a pylon exists).
"""

from __future__ import annotations

import math

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

# Structures that train units — leave a tile in front so units can exit.
PRODUCTION = {U.GATEWAY, U.WARPGATE, U.ROBOTICSFACILITY, U.STARGATE}


class Placement:
    def __init__(self, bot):
        self.bot = bot

    # ---------------------------------------------------------------- helpers
    def _mineral_center(self, nexus) -> Point2 | None:
        mins = self.bot.mineral_field.closer_than(12, nexus)
        return mins.center if mins else None

    # ----------------------------------------------------------------- pylons
    async def pylon(self) -> Point2 | None:
        bot = self.bot
        if not bot.townhalls.ready:
            return None

        # 1) every Nexus should have a pylon nearby (but off the mineral line)
        for nexus in bot.townhalls.ready:
            if not bot.structures(U.PYLON).closer_than(9, nexus):
                toward = self._mineral_center(nexus) or bot.game_info.map_center
                anchor = nexus.position.towards(toward, -5)  # 5 tiles AWAY from minerals
                pos = await bot.find_placement(U.PYLON, near=anchor, max_distance=7)
                if pos is not None:
                    return pos

        # 2) otherwise spread: pick the buildable ring candidate around the main
        #    base that is FARTHEST from existing pylons
        main = bot.townhalls.ready.closest_to(bot.start_location)
        mc = self._mineral_center(main)
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
            U.PYLON, near=main.position.towards(bot.game_info.map_center, 6), max_distance=12
        )

    # -------------------------------------------------------------- buildings
    async def building(self, unit: U) -> Point2 | None:
        bot = self.bot
        pylons = bot.structures(U.PYLON).ready
        if not pylons:
            return None
        # try pylons nearest the main first so the base grows outward from home
        for pylon in pylons.sorted(key=lambda p: p.distance_to(bot.start_location)):
            # Anchor on the side of the pylon AWAY from any nearby minerals, so we
            # don't build into the mineral line and block mining.
            near_min = bot.mineral_field.closer_than(15, pylon)
            if near_min:
                anchor = pylon.position.towards(near_min.center, -3)
            else:
                anchor = pylon.position.towards(bot.game_info.map_center, 3)
            # Pack tight (placement_step=1, no random spread) so many buildings fit
            # per pylon — but never take the LAST exit of a nearby production
            # building. Sample a few candidates and return the first that keeps every
            # nearby producer's exit open (falls through to the next pylon otherwise).
            for attempt in range(6):
                pos = await bot.find_placement(
                    unit, near=anchor, max_distance=10, placement_step=1,
                    random_alternative=attempt > 0,
                )
                if pos is None:
                    break
                if not self._would_seal_producer(pos):
                    return pos
        return None

    def _open_exits(self, center: Point2) -> list[Point2]:
        """Perimeter points (8 compass directions, ~2.5 tiles out) a unit could step
        onto to leave a building: walkable terrain not already under a structure."""
        bot = self.bot
        pts = []
        for k in range(8):
            ang = k * math.pi / 4
            e = Point2((center.x + 2.5 * math.cos(ang), center.y + 2.5 * math.sin(ang)))
            if bot.in_pathing_grid(e) and not bot.structures.closer_than(1.5, e):
                pts.append(e)
        return pts

    def _would_seal_producer(self, pos: Point2) -> bool:
        """Would a building at `pos` remove the LAST exit of a nearby production
        building? We keep tight packing but never fully wall a producer in — units
        must have somewhere to spawn (the bot then rallies them off it)."""
        bot = self.bot
        for prod in bot.structures(PRODUCTION):
            if prod.position.distance_to(pos) > 6:
                continue
            exits = self._open_exits(prod.position)
            if exits and all(e.distance_to(pos) < 2.0 for e in exits):
                return True  # the new building would cover every remaining exit
        return False
