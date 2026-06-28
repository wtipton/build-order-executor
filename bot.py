"""Protoss 8-worker-start opening bot.

Executes the safe PvZ/PvT opening described in builds/pvz_pvt_opening_8worker.yaml
(first ~4 min of https://www.youtube.com/watch?v=KeXt_5oBd50), adapted for the
5.0.16 patch (8 starting workers, Nexus = 13 supply).

The ordered, supply-triggered structure mirrors the `steps:` list in that YAML.
The continuous behaviour (probes, chrono, gas saturation, rally) mirrors `rules:`.
"""

from __future__ import annotations

from sc2.bot_ai import BotAI
from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId as U

CHRONO = AbilityId.EFFECT_CHRONOBOOSTENERGYCOST


class OpeningBot(BotAI):
    # ---- ordered build steps: (supply_trigger, label, handler-name) -----------
    # Each fires once, in order, when supply_used >= trigger and we can afford it.
    BUILD = [
        (12, "Pylon", "step_pylon"),
        (14, "Gateway", "step_gateway"),
        (15, "Assimilator #1", "step_gas"),
        (19, "Nexus (expand)", "step_expand"),
        (19, "Cybernetics Core", "step_core"),
        (19, "2nd worker to gas", "step_gas_worker_2"),
        (20, "Assimilator #2", "step_gas"),
        (20, "Pylon", "step_pylon"),
    ]

    # End the game this many game-seconds after the opening finishes.
    CONCEDE_AFTER = 15

    def __init__(self):
        super().__init__()
        self.step_idx = 0
        self.target_gas_workers = 0   # ramped up by the gas steps
        self.scout_tag = None
        self.adept_started = False
        self.done_time = None
        self._wall_start = None

    async def on_start(self):
        import time
        self._wall_start = time.monotonic()
        self.client.game_step = 4  # ~5.6 decisions/sec; smooth for realtime

    async def on_step(self, iteration: int):
        if iteration % 50 == 0:
            import time
            wall = time.monotonic() - self._wall_start
            ratio = (self.time / wall) if wall > 0 else 0
            print(f"[perf] it={iteration} game={self.time:5.1f}s wall={wall:5.1f}s ratio={ratio:.2f}x")

        if not self.townhalls:
            return  # dead

        await self.manage_build_order()
        await self.manage_probes()
        await self.manage_chrono()
        await self.manage_gas_saturation()
        self.set_rally_to_natural()
        await self.manage_adept()

        # Once the opening is complete, concede after a short grace period so
        # the Adept pops on the replay, then end the game instead of idling.
        if self.step_idx >= len(self.BUILD) and self.adept_started:
            if self.done_time is None:
                self.done_time = self.time
                print(f"[build] opening complete at {self.time:.1f}s — conceding in {self.CONCEDE_AFTER}s")
            elif self.time - self.done_time >= self.CONCEDE_AFTER:
                print("[build] conceding to end the game")
                await self.client.leave()

    # --------------------------------------------------------------------- build
    async def manage_build_order(self):
        if self.step_idx >= len(self.BUILD):
            return
        supply, label, handler = self.BUILD[self.step_idx]
        if self.supply_used < supply:
            return
        # try to run the step; handler returns True once the order is issued/done
        done = await getattr(self, handler)()
        if done:
            print(f"[build] {supply} supply -> {label}")
            self.step_idx += 1

    def _next_step_cost_unaffordable(self) -> bool:
        """True if the upcoming ordered step is due but we can't yet afford it
        (so probe production should pause to not delay the build)."""
        if self.step_idx >= len(self.BUILD):
            return False
        supply, _, handler = self.BUILD[self.step_idx]
        if self.supply_used < supply:
            return False
        cost_unit = {
            "step_pylon": U.PYLON,
            "step_gateway": U.GATEWAY,
            "step_gas": U.ASSIMILATOR,
            "step_expand": U.NEXUS,
            "step_core": U.CYBERNETICSCORE,
        }.get(handler)
        if cost_unit is None:
            return False
        return not self.can_afford(cost_unit)

    async def step_pylon(self) -> bool:
        if not self.can_afford(U.PYLON):
            return False
        nexus = self.townhalls.first
        pos = nexus.position.towards(self.game_info.map_center, 6)
        await self.build(U.PYLON, near=pos)
        return True

    async def step_gateway(self) -> bool:
        if not self.can_afford(U.GATEWAY) or not self.structures(U.PYLON).ready:
            return False
        pylon = self.structures(U.PYLON).ready.first
        await self.build(U.GATEWAY, near=pylon.position.towards(self.game_info.map_center, 3))
        return True

    async def step_core(self) -> bool:
        if not self.can_afford(U.CYBERNETICSCORE) or not self.structures(U.PYLON).ready:
            return False
        if not self.structures(U.GATEWAY).ready:
            return False  # core needs a finished gateway
        pylon = self.structures(U.PYLON).ready.first
        await self.build(U.CYBERNETICSCORE, near=pylon.position.towards(self.game_info.map_center, 3))
        return True

    async def step_gas(self) -> bool:
        if not self.can_afford(U.ASSIMILATOR):
            return False
        nexus = self.townhalls.first
        geysers = self.vespene_geyser.closer_than(12, nexus)
        for g in geysers:
            if not self.gas_buildings.closer_than(1, g):
                worker = self.select_build_worker(g.position)
                if worker is None:
                    return False
                worker.build_gas(g)
                if self.target_gas_workers == 0:
                    self.target_gas_workers = 1  # first gas: 1 worker
                return True
        return False

    async def step_gas_worker_2(self) -> bool:
        self.target_gas_workers = 2
        return True

    async def step_expand(self) -> bool:
        if not self.can_afford(U.NEXUS):
            return False
        await self.expand_now()
        return True

    # -------------------------------------------------------------------- probes
    async def manage_probes(self):
        if self.supply_left <= 0:
            return
        if self.already_pending(U.PROBE) >= 1:
            return  # one at a time keeps the Nexus rhythm clean
        if len(self.workers) + self.already_pending(U.PROBE) >= 44:
            return  # ~2-base saturation
        if self._next_step_cost_unaffordable():
            return  # save minerals for the imminent build step
        for nexus in self.townhalls.ready.idle:
            if self.can_afford(U.PROBE):
                nexus.train(U.PROBE)

    # -------------------------------------------------------------------- chrono
    async def manage_chrono(self):
        core_started = self.structures(U.CYBERNETICSCORE) or self.already_pending(U.CYBERNETICSCORE)
        for nexus in self.townhalls.ready:
            if nexus.energy < 50:
                continue
            if self.adept_started:
                # boost the Adept out of the gateway
                gw = self.structures(U.GATEWAY).ready
                target = next((g for g in gw if g.orders), None)
                if target and not target.has_buff(BuffId.CHRONOBOOSTENERGYCOST):
                    nexus(CHRONO, target)
                    return
            elif core_started:
                # hold chrono so it's ready for the Adept
                return
            else:
                # pre-core: dump chrono into probe production
                if not nexus.has_buff(BuffId.CHRONOBOOSTENERGYCOST):
                    nexus(CHRONO, nexus)
                    return

    # ----------------------------------------------------------------------- gas
    async def manage_gas_saturation(self):
        target = self.target_gas_workers
        assigned = sum(a.assigned_harvesters for a in self.gas_buildings.ready)
        if assigned < target:
            for a in self.gas_buildings.ready:
                if a.assigned_harvesters < a.ideal_harvesters and assigned < target:
                    w = self.workers.gathering.filter(lambda u: not u.is_carrying_vespene).closest_to(a)
                    if w:
                        w.gather(a)
                        assigned += 1
        elif assigned > target:
            for a in self.gas_buildings.ready:
                if a.assigned_harvesters > 0 and assigned > target:
                    w = self.workers.filter(lambda u: u.is_carrying_vespene is False).closest_to(a)
                    mins = self.mineral_field.closer_than(10, self.townhalls.first)
                    if w and mins:
                        w.gather(mins.closest_to(w))
                        assigned -= 1

    # --------------------------------------------------------------------- rally
    def set_rally_to_natural(self):
        # rally gateway units toward our (future) natural
        if not self.structures(U.GATEWAY).ready:
            return
        target = self.townhalls.closest_to(self.game_info.map_center).position
        for gw in self.structures(U.GATEWAY).ready:
            gw(AbilityId.RALLY_BUILDING, target)

    # --------------------------------------------------------------------- adept
    async def manage_adept(self):
        if self.adept_started:
            return
        if not self.structures(U.CYBERNETICSCORE).ready:
            return
        if self.can_afford(U.ADEPT) and self.supply_left > 0:
            gw = self.structures(U.GATEWAY).ready.idle
            if gw:
                gw.first.train(U.ADEPT)
                self.adept_started = True
                print("[build] Cybernetics Core done -> Adept (chrono it)")
