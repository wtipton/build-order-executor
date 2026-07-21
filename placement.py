"""Building placement heuristics, factored out of bot.py.

Kept deliberately simple (no walling yet — just "make it easy to fit buildings"):

  * pylon():    give every Nexus a nearby pylon (offset off the mineral line so
                it's not buried in the minerals), then spread further pylons out
                across the base so there's powered room everywhere.
  * building(): pack a building tight against a powering pylon; for production
                structures, bias toward open ground so trained units can get out.

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
            # don't build into the mineral line and block mining. Pack tight
            # (placement_step=1, no random spread) so many buildings fit per pylon.
            near_min = bot.mineral_field.closer_than(15, pylon)
            if near_min:
                anchor = pylon.position.towards(near_min.center, -3)
            else:
                anchor = pylon.position.towards(bot.game_info.map_center, 3)
            pos = await bot.find_placement(
                unit, near=anchor, max_distance=10, placement_step=1, random_alternative=False
            )
            if pos is not None:
                return pos
        return None
