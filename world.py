"""Map/world helpers for BuildOrderBot: named-location resolution, expansion
ordering, probe selection, the combat-unit rally point, and the pre-walk builder
reservation + placement dispatch.

`WorldMixin` is mixed into `BuildOrderBot` (see bot.py); its methods run on the
live bot via `self`. The building/pylon geometry itself lives in placement.py
(`self.placement`).
"""

from __future__ import annotations

from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from catalog import COMBAT_PRODUCTION, unit_id as _unit
from schema import Step


class WorldMixin:
    # How far out (by base rank) `proxy` sits from the enemy start: 0 = their
    # main, 1 = natural, 2 = third, 3 = fourth. The natural is too close, so we
    # aim a few bases out toward the middle of the map.
    PROXY_BASE_RANK = 3

    # Our bases named by distance-rank from our start (0 = main, 1 = natural, ...).
    BASE_RANK = {"main": 0, "natural": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5}

    # ============================================================ locations
    def _resolve_place(self, where: str) -> Point2:
        rank = self.BASE_RANK.get(where)
        if rank is not None:
            return self.start_location if rank == 0 else self._expansion_near(self.start_location, rank)
        if where == "main_ramp":
            return self.main_base_ramp.top_center
        if where == "enemy_main":
            return self._enemy_start()
        if where == "enemy_natural":
            return self._expansion_near(self._enemy_start(), 1)
        if where == "proxy":
            # out near the enemy but off their doorstep (their ~4th base)
            return self._expansion_near(self._enemy_start(), self.PROXY_BASE_RANK)
        # Every Place is validated at load, so this means a new one was added to the
        # schema without a case here — fail loudly rather than resolve somewhere wrong.
        raise ValueError(f"unhandled place {where!r}")

    def _enemy_start(self) -> Point2:
        return self.enemy_start_locations[0] if self.enemy_start_locations else self.game_info.map_center

    def _expansion_near(self, base: Point2, rank: int) -> Point2:
        """The `rank`-th expansion by distance from `base` (0 = the base itself,
        1 = its natural, 2 = third, ...), clamped to what the map provides."""
        exps = sorted(self.expansion_locations_list, key=lambda e: e.distance_to(base))
        if not exps:
            return base
        return exps[min(rank, len(exps) - 1)]

    def _next_expansion(self) -> Point2 | None:
        """The nearest expansion we haven't taken yet, using the SAME ordering as
        `_expansion_near` (distance from our start). We roll this rather than the
        library's `get_next_expansion` (which orders by pathing distance) so that
        the Nth Nexus lands on exactly the base `where: <Nth base>` resolves to."""
        for e in sorted(self.expansion_locations_list, key=lambda e: e.distance_to(self.start_location)):
            if not self.townhalls.closer_than(3.0, e):
                return e
        return None

    def ordered_bases(self) -> Units:
        """Our ready bases, nearest our start first — base 1 is the main, base 2 the
        natural, and so on. This is the numbering `rally_and_transfer_probes base: N`
        indexes into and that the [status] line reports.

        Deliberately the SAME ordering as `_expansion_near` / `_next_expansion` above,
        so the Nth Nexus we own is the one sitting on the base `where: <Nth>` resolves
        to. Nothing enforces that beyond both sorting by distance from our start.
        """
        return self.townhalls.ready.sorted(key=lambda t: t.distance_to(self.start_location))

    # =========================================================== named probes
    def _worker_by_tag(self, tag: int | None) -> Unit | None:
        if tag is None:
            return None
        found = self.workers.tags_in({tag})
        return found.first if found else None

    def _named_worker(self, label: str | None) -> Unit | None:
        """The live probe currently held under `label`, or None."""
        if label is None:
            return None
        return self._worker_by_tag(self.named_probes.get(label))

    def _excluded_tags(self) -> set[int]:
        """Probes not available to automation: those sent out via send_probe, plus
        the current prewalk."""
        tags = set(self.named_probes.values())
        if self.prewalk_state.builder_tag is not None:
            tags.add(self.prewalk_state.builder_tag)
        return tags

    def _free_probe_near(self, pos: Point2) -> Unit | None:
        """Nearest probe we're willing to pull off the economy, or None.

        Eligible = mining minerals or idle, empty-handed, and not on gas. Idle counts so a
        probe that just finished right at `pos` is reused instead of dragging a fresh one
        across the map. Carrying anything is out, because pulling it throws the load away.
        Gas is out because gas assignment is deliberate (`gas_target`): steal a gas probe
        and manage_economy just refills the geyser from minerals next frame, so we'd have
        pulled a mineral probe anyway, with a wasted round trip on top.

        No fallback tier — if nobody qualifies we return None and the caller retries next
        frame. The old "else anyone at all" fallback is what let a probe mid-return get
        picked, which then sat on its vespene for the whole build.
        """
        gas_tags = {g.tag for g in self.gas_buildings.ready}
        cands = self.workers.tags_not_in(self._excluded_tags()).filter(
            lambda w: (w.is_gathering or w.is_idle)
            and not w.is_carrying_minerals
            and not w.is_carrying_vespene
            and w.order_target not in gas_tags
        )
        if not cands:
            return None
        chosen = cands.closest_to(pos)
        # Remember where it was mining so it can go back there when it's done (see
        # home_base_by_builder). Every builder comes through here, so this is the one
        # place that needs to know.
        home = self.townhalls.ready.closest_to(chosen) if self.townhalls.ready else None
        if home is not None:
            self.home_base_by_builder[chosen.tag] = home.tag
        return chosen

    # ============================================================ rally
    def apply_rally(self) -> None:
        """Point every combat-unit producer at the one `rally_point`.

        Issued once per building, not every frame: `_rallied` remembers who is already
        set to the CURRENT point, and `do_set_rally_point` clears it so a moved rally
        point is re-issued to every existing producer on the next frame.

        This also keeps production exits clear — units walk off the spawn tile toward
        the rally instead of piling on it and jamming the building (a finished unit
        can't pop out through a blocked exit). placement.py avoids sealing that exit
        in the first place; this walks them out of it.
        """
        for b in self.structures(COMBAT_PRODUCTION).ready:
            if b.tag not in self._rallied:
                self._rallied.add(b.tag)
                b(AbilityId.RALLY_BUILDING, self.rally_point)

    # ========================================================= pre-walk builder
    async def manage_prebuild(self) -> None:
        """Reserve a probe for the next build step and walk it to the placement
        ahead of time, so `do_build` can start construction the instant we can
        afford it. WHEN to start walking is config-driven (the step's `prewalk`
        trigger, defaulting to its `at` trigger) — the bot does no estimating."""
        step = self._next_build_step()
        if self.prewalk_state.for_step is not None and self.prewalk_state.for_step is not step:
            self._clear_prewalk()
        if step is None or not self._prewalk_due(step):
            return
        # a labelled build brings its own (explicitly sent) probe — no prewalk
        if getattr(step, "label", None) is not None:
            return

        if self.prewalk_state.for_step is step:
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
        self.prewalk_state.for_step = step
        self.prewalk_state.builder_tag = worker.tag
        self.prewalk_state.target = target
        worker.move(pos)
        print(f"[prewalk] {self._clock():>4}  sup{self.supply_used} min={self.minerals} "
              f"reserved probe {worker.tag} for {step.what} @ ({pos.x:.0f},{pos.y:.0f}) "
              f"(build at {step.at})", flush=True)

    def _next_build_step(self) -> Step | None:
        for s in self.cfg.steps[self.steps_done:]:
            if s.do == "build":
                return s
            if s.do in ("train", "warp", "research"):
                return None  # a resource-committing step precedes the next build
        return None

    async def _placement_for(self, step: Step) -> Point2 | Unit | None:
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
            return await self.placement.pylon_position()
        return await self.placement.building_position(unit)

    def _free_geyser(self) -> Unit | None:
        for th in self.townhalls.ready:
            for g in self.vespene_geyser.closer_than(10, th):
                if not self.gas_buildings.closer_than(1, g):
                    return g
        return None

    def _prewalk_worker(self) -> Unit | None:
        if self.prewalk_state.builder_tag is None:
            return None
        res = self.workers.tags_in({self.prewalk_state.builder_tag})
        return res.first if res else None

    def _reposition_builder(self) -> None:
        w = self._prewalk_worker()
        if w is None:
            self._clear_prewalk()
            return
        pos = self.prewalk_state.target.position if isinstance(self.prewalk_state.target, Unit) else self.prewalk_state.target
        if pos is not None and w.is_idle and w.distance_to(pos) > 1:
            w.move(pos)

    def _clear_prewalk(self) -> None:
        self.prewalk_state.clear()
