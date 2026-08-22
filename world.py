"""Map/world helpers for BuildOrderBot: named-location resolution, expansion
ordering, probe selection, and the pre-walk builder reservation + placement
dispatch.

`WorldMixin` is mixed into `BuildOrderBot` (see bot.py); its methods run on the
live bot via `self`. The building/pylon geometry itself lives in placement.py
(`self.placement`).
"""

from __future__ import annotations

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.unit import Unit

from catalog import unit_id as _unit


class WorldMixin:
    # How far out (by base rank) `proxy` sits from the enemy start: 0 = their
    # main, 1 = natural, 2 = third, 3 = fourth. The natural is too close, so we
    # aim a few bases out toward the middle of the map.
    PROXY_BASE_RANK = 3

    # Our bases named by distance-rank from our start (0 = main, 1 = natural, ...).
    BASE_RANK = {"main": 0, "natural": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5}

    # ============================================================ locations
    def _resolve_place(self, where: str):
        rank = self.BASE_RANK.get(where)
        if rank is not None:
            return self.start_location if rank == 0 else self._expansion_near(self.start_location, rank)
        if where == "enemy_main":
            return self._enemy_start()
        if where == "enemy_natural":
            return self._expansion_near(self._enemy_start(), 1)
        # "proxy": out near the enemy but off their doorstep (their ~4th base)
        return self._expansion_near(self._enemy_start(), self.PROXY_BASE_RANK)

    def _enemy_start(self):
        return self.enemy_start_locations[0] if self.enemy_start_locations else self.game_info.map_center

    def _expansion_near(self, base, rank: int):
        """The `rank`-th expansion by distance from `base` (0 = the base itself,
        1 = its natural, 2 = third, ...), clamped to what the map provides."""
        exps = sorted(self.expansion_locations_list, key=lambda e: e.distance_to(base))
        if not exps:
            return base
        return exps[min(rank, len(exps) - 1)]

    def _next_expansion(self):
        """The nearest expansion we haven't taken yet, using the SAME ordering as
        `_expansion_near` (distance from our start). We roll this rather than the
        library's `get_next_expansion` (which orders by pathing distance) so that
        the Nth Nexus lands on exactly the base `where: <Nth base>` resolves to."""
        for e in sorted(self.expansion_locations_list, key=lambda e: e.distance_to(self.start_location)):
            if not self.townhalls.closer_than(3.0, e):
                return e
        return None

    # =========================================================== named probes
    def _worker_by_tag(self, tag: int | None):
        if tag is None:
            return None
        found = self.workers.tags_in({tag})
        return found.first if found else None

    def _named_worker(self, label: str | None):
        """The live probe currently held under `label`, or None."""
        if label is None:
            return None
        return self._worker_by_tag(self.named_probes.get(label))

    def _excluded_tags(self) -> set[int]:
        """Probes not available to automation: those sent out via send_probe, plus
        the current prewalk reservation."""
        tags = set(self.named_probes.values())
        if self._reservation.builder_tag is not None:
            tags.add(self._reservation.builder_tag)
        return tags

    def _free_probe_near(self, pos):
        """Nearest probe available to automation (not a sent/reserved one). Consider
        idle probes too, not just gathering ones, and pick the CLOSEST — so a probe
        that just finished or gave up right at `pos` (e.g. a far enemy/proxy build)
        is reused, instead of pulling a fresh one across the map."""
        pool = self.workers.tags_not_in(self._excluded_tags())
        cands = pool.filter(lambda w: (w.is_gathering or w.is_idle) and not w.is_carrying_minerals)
        cands = cands or pool.filter(lambda w: w.is_gathering or w.is_idle) or pool
        return cands.closest_to(pos) if cands else None

    # ========================================================= pre-walk builder
    async def manage_prebuild(self):
        """Reserve a probe for the next build step and walk it to the placement
        ahead of time, so `do_build` can start construction the instant we can
        afford it. WHEN to start walking is config-driven (the step's `prewalk`
        trigger, defaulting to its `at` trigger) — the bot does no estimating."""
        step = self._next_build_step()
        if self._reservation.step is not None and self._reservation.step is not step:
            self._clear_reservation()
        if step is None or not self._prewalk_due(step):
            return
        # a labelled build brings its own (explicitly sent) probe — no prewalk
        if getattr(step, "label", None) is not None:
            return

        if self._reservation.step is step:
            self._reposition_builder()
            return

        # do_build may have already issued this build itself (its trigger fired
        # before prewalk got to reserve). A probe is then en route with the build
        # order, which for Protoss is what already_pending() counts — don't reserve
        # a SECOND probe on top of it.
        if self.already_pending(_unit(step.what)):
            return

        target = await self._placement_for(step)
        if target is None:
            return  # can't determine placement yet (e.g. no pylon) — retry later
        pos = target.position if isinstance(target, Unit) else target
        worker = self._free_probe_near(pos)
        if worker is None:
            return
        self._reservation.step = step
        self._reservation.builder_tag = worker.tag
        self._reservation.target = target
        worker.move(pos)
        if self.debug:
            print(f"[prewalk] {self._clock():>4}  sup{self.supply_used} min={self.minerals} "
                  f"reserved probe {worker.tag} for {step.what} @ ({pos.x:.0f},{pos.y:.0f}) "
                  f"(build at {self._trig_str(step.at)})", flush=True)

    def _next_build_step(self):
        for j in range(self.idx, len(self.steps)):
            if self._done[j]:
                continue
            s = self.steps[j]
            if s.do == "build":
                return s
            if s.do in ("train", "warp", "research"):
                return None  # a resource-committing step precedes the next build
        return None

    async def _placement_for(self, step):
        unit = _unit(step.what)
        # A `where` proxies the building out on the map (e.g. a Pylon near the
        # enemy). Anchor placement at that place instead of the home heuristics;
        # the prewalk machinery then walks a probe there ahead of time for free.
        where = getattr(step, "where", None)
        if where is not None:
            return await self.find_placement(unit, near=self._resolve_place(where), max_distance=20,
                                             random_alternative=False)
        if unit == U.NEXUS:
            return self._next_expansion()
        if unit == U.ASSIMILATOR:
            return self._free_geyser()
        if unit == U.PYLON:
            return await self.placement.pylon()
        return await self.placement.building(unit)

    def _free_geyser(self):
        for th in self.townhalls.ready:
            for g in self.vespene_geyser.closer_than(10, th):
                if not self.gas_buildings.closer_than(1, g):
                    return g
        return None

    def _reserved_worker(self) -> Unit | None:
        if self._reservation.builder_tag is None:
            return None
        res = self.workers.tags_in({self._reservation.builder_tag})
        return res.first if res else None

    def _reposition_builder(self):
        w = self._reserved_worker()
        if w is None:
            self._clear_reservation()
            return
        pos = self._reservation.target.position if isinstance(self._reservation.target, Unit) else self._reservation.target
        if pos is not None and w.is_idle and w.distance_to(pos) > 1:
            w.move(pos)

    def _clear_reservation(self):
        self._reservation.clear()
