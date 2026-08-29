"""Map/world helpers for BuildOrderBot: named-location resolution, expansion
ordering, probe selection, the combat-unit rally point, and builder assignment
(including the pre-walk) + placement dispatch. The `[builder]` lines its
transitions print live in observe.py with the rest of the reporting.

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

from catalog import COMBAT_PRODUCTION, unit_id
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
            return self.base_position(rank + 1)
        if where == "main_ramp":
            return self.main_base_ramp.top_center
        if where == "enemy_main":
            return self._enemy_start_position()
        if where == "enemy_natural":
            return self._find_expansion_near(self._enemy_start_position(), 1)
        if where == "proxy":
            # out near the enemy but off their doorstep (their ~4th base)
            return self._find_expansion_near(self._enemy_start_position(), self.PROXY_BASE_RANK)
        # Every Place is validated at load, so this means a new one was added to the
        # schema without a case here — fail loudly rather than resolve somewhere wrong.
        raise ValueError(f"unhandled place {where!r}")

    def _enemy_start_position(self) -> Point2:
        return self.enemy_start_locations[0] if self.enemy_start_locations else self.game_info.map_center

    def _find_expansion_near(self, base: Point2, rank: int) -> Point2:
        """The `rank`-th expansion by distance from `base` (0 = the base itself,
        1 = its natural, 2 = third, ...), clamped to what the map provides."""
        exps = sorted(self.expansion_locations_list, key=lambda e: e.distance_to(base))
        if not exps:
            return base
        return exps[min(rank, len(exps) - 1)]

    def _find_next_expansion(self) -> Point2 | None:
        """The nearest expansion we haven't taken yet, using the SAME ordering as
        `_find_expansion_near` (distance from our start). We roll this rather than the
        library's `get_next_expansion` (which orders by pathing distance) so that
        the Nth Nexus lands on exactly the base `where: <Nth base>` resolves to."""
        for e in sorted(self.expansion_locations_list, key=lambda e: e.distance_to(self.start_location)):
            if not self.townhalls.closer_than(3.0, e):
                return e
        return None

    def base_position(self, n: int) -> Point2:
        """Where our Nth base is on the map (1 = main, 2 = natural, ...), whether or not
        we own one there yet. Clamped to what the map provides by `_find_expansion_near`.

        Prefer this to `ordered_bases()[n - 1]` whenever you only need a location: it
        doesn't require the Nexus to exist, and it's the same numbering `where: <Nth>`
        resolves through, so the two can't disagree.
        """
        return self.start_location if n <= 1 else self._find_expansion_near(self.start_location, n - 1)

    def ordered_bases(self) -> Units:
        """Our ready bases, nearest our start first — base 1 is the main, base 2 the
        natural, and so on. This is the numbering `rally_and_transfer_probes base: N`
        indexes into and that the [status] line reports.

        Deliberately the SAME ordering as `_find_expansion_near` / `_find_next_expansion` above,
        so the Nth Nexus we own is the one sitting on the base `where: <Nth>` resolves
        to. Nothing enforces that beyond both sorting by distance from our start.
        """
        return self.townhalls.ready.sorted(key=lambda t: t.distance_to(self.start_location))

    # ============================================================ counting
    def count_of(self, name: str) -> int:
        """`_count_type` by build-order name — what a `count:` trigger names."""
        return self._count_type(unit_id(name))

    def _count_type(self, unit: U) -> int:
        """How many completed `unit` we have. Backs `count:` triggers, the [status] line
        that reports their progress, AND the [summary] census, so none of the three can
        disagree about what we 'have'.

        Probes come from `supply_workers`, NOT the unit list: a probe harvesting gas is
        INSIDE the Assimilator for ~1.4s of every trip, and for that time it is absent
        from the observation entirely, so `workers.amount` (and `all_own_units`) read low
        at random once gas is running. Empirically, supply_workers excludes probes still
        in production, so we can use it here instead.

        Hallucinations don't count: a Sentry's hallucinated Phoenix is one of our own
        units in the observation, so without this a `hallucinate` step would fire a later
        `count:` trigger (and pad the census) with a unit we never actually built."""
        if unit == U.PROBE:
            return int(self.supply_workers)
        return self.all_own_units(unit).ready.filter(lambda u: not u.is_hallucination).amount

    # ======================================================== labelled probes
    def _worker_by_tag(self, tag: int | None) -> Unit | None:
        if tag is None:
            return None
        found = self.workers.tags_in({tag})
        return found.first if found else None

    def _worker_by_label(self, label: str | None) -> Unit | None:
        """The live probe currently held under `label`, or None."""
        if label is None:
            return None
        return self._worker_by_tag(self.labelled_probes.get(label))

    def _probes_unavailable_to_automation(self) -> set[int]:
        """Tags of probes that are on a specific assignment and must not be reassigned:
        those sent out by `send_probe` (held until a `return_probe`), plus the one walking
        to the next build site. Everything else is fair game for the economy."""
        tags = set(self.labelled_probes.values())
        if self.builder_state.builder_tag is not None:
            tags.add(self.builder_state.builder_tag)
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
        cands = self.workers.tags_not_in(self._probes_unavailable_to_automation()).filter(
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

    # ========================================================= builder assignment
    async def manage_builder(self) -> None:
        """Own `builder_state`'s lifecycle, once per frame: retire a reservation we've
        moved past, keep the assigned probe on task, and — this is the pre-walk —
        assign the NEXT build's probe early so it's already standing on the spot when
        the build becomes affordable.

        WHEN to pull the probe is config-driven (the step's `prewalk` trigger,
        defaulting to its `at`) — the bot does no estimating. Assignment itself is
        `_assign_builder`, which do_build also calls, so a build whose step became
        current after this ran doesn't lose a frame waiting for us."""
        step = self._find_next_build_step()
        if self.builder_state.for_step is not None and self.builder_state.for_step is not step:
            self.builder_state.clear()  # assigned for a build we've moved past
        if step is None:
            return

        if self.builder_state.for_step is step:
            if self.builder_state.issued:
                return  # do_build owns it from here — it handles a dead builder itself
            # keep it heading for the spot: drop a dead probe (a fresh one gets
            # assigned next frame) and re-issue if it drifted or stopped short
            worker = self._worker_by_tag(self.builder_state.builder_tag)
            if worker is None:
                self.builder_state.clear()
                return
            # TODO: a little suspicious that we don't need this nudge behavior
            spot = self.builder_state.spot
            if worker.is_idle and worker.distance_to(spot) > 1:
                worker.move(spot)
            return

        if self._prewalk_due(step):
            await self._assign_builder(step)

    async def _assign_builder(self, step: Step) -> bool:
        """Make `builder_state` ready to build `step`: pick the spot, pull a probe off the
        line, start it walking. True once assigned.

        The single place a builder is chosen, for every build — called early by
        manage_builder (the pre-walk) and inline by do_build when the step came due
        before manage_builder saw it. Idempotent: already assigned for `step` is True
        with nothing done. False means not yet possible (no placement, no free probe)
        and `_status` says which, so the caller can just stall."""
        if self.builder_state.for_step is step and self.builder_state.builder_tag is not None:
            return True
        spot = await self._find_build_target(step)
        if spot is None:
            return False  # _find_build_target set _status where it can explain itself
        pos = spot.position  # Point2.position is itself, so this covers both
        # A labelled build names its own probe (already off the line via send_probe).
        # If that probe is dead we fall back to the pool rather than stalling: losing the
        # author's choice of probe is bad, hanging the whole build order over it is worse.
        label = getattr(step, "label", None)
        worker = self._worker_by_label(label) if label is not None else None
        origin = f"label:{label}" if worker is not None else "pool"
        if worker is None:
            worker = self._free_probe_near(pos)
        if worker is None:
            self._status = "no free probe to build with"
            return False
        self.builder_state.for_step = step
        self.builder_state.builder_tag = worker.tag
        self.builder_state.spot = spot
        worker.move(pos)
        self._log_builder("assigned", step, worker,
                          f"@ ({pos.x:.0f},{pos.y:.0f})  {origin}; build at {step.at}")
        return True

    def _find_next_build_step(self) -> Step | None:
        """The next `build` step worth pre-walking a probe for, or None if something that
        commits resources comes first — no point tying up a builder behind it."""
        for s in self.cfg.steps[self.steps_done:]:
            if s.do == "build":
                return s
            if s.do in ("train", "warp", "research"):
                return None  # a resource-committing step precedes the next build
        return None

    async def _find_build_target(self, step: Step) -> Point2 | Unit | None:
        """Where `step` should aim its build: a position, or the geyser itself for an
        Assimilator. Dispatches over the ways a target can be decided — a named place,
        the next expansion, a free geyser, or placement.py's geometry.

        Returning None stalls the step, so the cases that can explain themselves set
        `_status` on the way out — do_build only has "no placement for X" to offer,
        which doesn't distinguish "not yet" from "never"."""
        unit = unit_id(step.what)
        where = getattr(step, "where", None)
        if unit == U.NEXUS:
            # An expansion belongs ON a base location, not merely somewhere placeable
            # near one. So for nexuses, we don't ever fall back to the generic find_placement.
            if where is None:
                nxt = self._find_next_expansion()
                if nxt is None:
                    self._status = "every expansion is already taken"
                return nxt
            base = self._resolve_place(where)
            if self.townhalls.closer_than(3.0, base):
                self._status = f"the {where} is already ours"  # stall, don't misplace it
                return None
            return base

        if where is not None:
            return await self.find_placement(unit, near=self._resolve_place(where), max_distance=20,
                                             random_alternative=False)
        if unit == U.ASSIMILATOR:
            geyser = self._find_free_geyser()
            if geyser is None:
                self._status = "no geyser without an Assimilator at any of our bases"
            return geyser
        if unit == U.PYLON:
            pos = await self.placement.pylon_position()
            if pos is None:
                self._status = "nowhere left to put a Pylon"
            return pos
        pos = await self.placement.building_position(unit)
        if pos is None:
            self._status = f"no powered spot for {step.what} at any base"
        return pos

    def _find_free_geyser(self) -> Unit | None:
        """A geyser at one of our bases with no Assimilator on it yet, or None."""
        for th in self.townhalls.ready:
            for g in self.vespene_geyser.closer_than(10, th):
                if not self.gas_buildings.closer_than(1, g):
                    return g
        return None

