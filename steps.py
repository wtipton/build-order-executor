"""The step executor for BuildOrderBot: the strict-order engine (trigger gating,
confirm/stall, builder hand-off) and every `do_<action>` handler.

`StepsMixin` is mixed into `BuildOrderBot` (see bot.py); its methods run on the
live bot via `self` and lean on the other mixins' helpers (`_free_probe_near`,
`ordered_bases`, `_resolve_place`, `_free_probe_near`, …).
"""

from __future__ import annotations

from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from catalog import (
    CHRONO_ABILITY,
    CHRONO_BUFF,
    CHRONO_ENERGY,
    HALLUCINATION_ABILITY,
    HALLUCINATION_CASTER,
    HALLUCINATION_ENERGY,
    MORPH,
    PRODUCER,
    RESEARCH,
    WARP_ABILITY,
    MorphSpec,
    PYLON_POWER_RADIUS,
    WARP_TILE_CLEARANCE,
    unit_id,
)
from schema import Step, Trigger


class StepsMixin:
    # ============================================================ engine
    @property
    def current_step(self) -> Step | None:
        """The step the build is working on, or None once every step has completed.
        The only place `cfg.steps` is indexed by progress, so the end-of-build
        boundary is handled once, here."""
        if self.steps_done >= len(self.cfg.steps):
            return None
        return self.cfg.steps[self.steps_done]

    @property
    def all_steps_done(self) -> bool:
        return self.steps_done >= len(self.cfg.steps)

    async def run_steps(self) -> None:
        while (step := self.current_step) is not None:
            if not self.step_state.trigger_fired:
                if not self.trigger_met(step.at):
                    return  # not due yet — this step (and everything after) waits
                self.step_state.trigger_fired = True  # commit; a non-monotonic trigger must not un-fire it
            if not await self.run_handler(step):
                return  # stall the line until this step can be done (strict order)
            print(f"[step] {self._clock():>4}  sup{self.supply_used:<3} {step}", flush=True)
            self.steps_done += 1
            self.step_state.reset(self.time)  # the finished step's in-flight state dies with it

    def trigger_met(self, at: Trigger) -> bool:
        if at.supply is not None:
            return self.supply_used >= at.supply
        if at.time is not None:
            return self.time >= at.time
        if at.minerals is not None:
            return self.minerals >= at.minerals
        if at.vespene is not None:
            return self.vespene >= at.vespene
        if at.count is not None:
            name, n = next(iter(at.count.items()))
            return self.count_of(name) >= n
        return True

    def _prewalk_due(self, step: Step) -> bool:
        """Whether to pull a probe off the mineral line for `step` yet.

        The default is simply "when we can afford the building" — i.e. exactly as if
        the author had written `prewalk:` with this building's cost. An explicit
        `prewalk:` overrides it, and is how you ask for the probe EARLIER (`prewalk:
        {minerals: 300}` on a 400-mineral Nexus sends it 100 short, so the walk
        overlaps the saving).

        Asked once per step, and weighs ONE building even when the step has a `count`:
        `count: 4` Gateways pulls a probe at 150 minerals, not 600. After that the probe is
        held for the whole group and walks to each next spot as soon as the previous
        building goes up (see BuilderState.reset_for_next_structure), so this is never consulted
        again — it decides when to take a probe off minerals, and that already happened.

        Deliberately NOT keyed off `step.at`: when the builder leaves the mineral line
        is then a property of the build itself, so reordering steps or retuning a
        trigger doesn't silently move it.

        For a mineral threshold (the default included) we reserve the cost of the
        probes still needed to reach the build's supply trigger and check what's left
        — the minerals we'd have AFTER committing those probes, not money that's about
        to become probes. Other trigger forms need no adjustment."""
        trig = getattr(step, "prewalk", None)
        if trig is None:
            cost = self.calculate_cost(unit_id(step.what))
            # gas needs no reservation of its own: probes cost no gas
            return (self.minerals - self._probe_reserve(step) >= cost.minerals
                    and self.vespene >= cost.vespene)
        if trig.minerals is None:
            return self.trigger_met(trig)
        return self.minerals - self._probe_reserve(step) >= trig.minerals

    def _probe_reserve(self, step: Step) -> int:
        """Minerals earmarked for the probes still needed to reach `step.at`, so a mineral
        threshold isn't satisfied by money that's already spoken for.

        Both worker-counting trigger forms get this — `supply: 20` and `count: {Probe: 20}`
        are the same instruction to a build-order author, so they must reserve alike — but
        they need different arithmetic, because what we're after is the probes we haven't
        PAID for yet:
          * supply_used already counts in-production probes (supply is reserved the moment
            training starts), so DON'T also subtract already_pending — that double-counts
            the building probe and fires the pull one supply early.
          * count_of(Probe) counts only COMPLETED probes, so here we DO subtract the ones
            in production; they're paid for already.
        Every other trigger form reserves nothing: it says nothing about future probes."""
        at = step.at
        if at.supply is not None:
            return 50 * max(0, at.supply - self.supply_used)
        if at.count is not None:
            (name, want), = at.count.items()
            if unit_id(name) == U.PROBE:
                return 50 * max(0, want - self.count_of(name) - int(self.already_pending(U.PROBE)))
        return 0

    async def run_handler(self, step: Step) -> bool:
        """Run the step's `do_<action>` handler once; True if the step completed."""
        self._status = ""  # handlers set this when they return False; shown by [status]
        handler = getattr(self, f"do_{step.do}")
        return await handler(step)

    # ----------------------------------------------------------- step handlers
    async def do_build(self, step: Step) -> bool:
        """Put up `count` structures (default 1), one after another. Get a probe assigned,
        issue the build once, hold until the structure actually appears, then go again if
        more are owed. Picks neither the probe nor the spot itself — `_assign_builder` owns
        both, for every build, and re-runs per structure so each gets its own spot."""
        unit = unit_id(step.what)
        want = step.count or 1
        bs = self.builder_state

        # A build isn't "done" when the command is issued — the probe may walk a long way,
        # and a probe over the tile can block placement so the order is dropped. Confirm
        # the structures actually appear, against two different baselines:
        #
        #   step_state.build.baseline — the count when the STEP started. How many we've
        #     added over the whole step is `amount - this`, which is what `count` is
        #     measured against. Deliberately not a tally of confirm events: `amount` can
        #     grow by 2 at once (the re-issue guard below can duplicate a build that had
        #     in fact landed), and counting that as one would leave us owing one too many.
        #   builder_state.baseline — the count when the CURRENT probe was put to work.
        #     Only decides when to release that probe and start the next structure.
        if self.step_state.build.baseline is None:
            self.step_state.build.baseline = self.structures(unit).amount
        if bs.baseline is None:
            bs.baseline = self.structures(unit).amount

        built = self.structures(unit).amount - self.step_state.build.baseline
        if built >= want:
            bs.clear()  # spans steps, so release the probe explicitly
            return True

        if self.structures(unit).amount > bs.baseline:
            # A structure appeared, but we still need more. Prep to use the same worker to
            # build the next building.
            bs.reset_for_next_structure()
            self._status = f"{step.what} {built}/{want} up"
            await self._assign_builder(step)

        if not self.can_afford(unit):
            self._status = f"can't afford {step.what}"
            return False  # save for it, stalling the line

        # Usually already assigned and walking (manage_builder ran first, possibly many
        # frames ago); assigning here covers a step that became current after it ran.
        if not await self._assign_builder(step):
            return False  # _assign_builder said why in _status

        builder = self._worker_by_tag(bs.builder_tag)
        if bs.issued:
            # Don't re-issue while the probe is still carrying out the build (walking to
            # the spot — possibly across the map — or placing it); re-issue only once it
            # has died or dropped the order without a structure appearing. Re-issuing
            # every frame during a long walk spawned duplicate builds in the wrong place,
            # and spurious structures that falsely satisfied the (type-wide) confirm.
            if builder is not None and not (builder.is_idle or builder.is_gathering):
                self._status = f"{step.what} under construction"
                return False
            bs.issued = False  # it fell through — re-issue below, to whoever we have now
        if builder is None:
            bs.clear()  # the assigned probe died; manage_builder assigns a new one
            self._status = f"builder for {step.what} died"
            return False

        if unit == U.ASSIMILATOR:
            builder.build_gas(bs.spot)
        elif unit == U.NEXUS:
            # placement_step=1: search every tile, so an expansion lands on the base's
            # exact location rather than the default 2-tile grid's approximation of it.
            await self.build(unit, near=bs.spot, build_worker=builder, placement_step=1,
                             random_alternative=False)
        else:
            await self.build(unit, near=bs.spot, build_worker=builder, random_alternative=False)
        bs.issued = True
        self._log_builder("building", step, builder)
        return False  # issued; stall the line until the structure appears (confirm)

    async def do_train(self, step: Step) -> bool:
        """Order `count` of a unit (default 1), as many this frame as we can pay for and
        find queue space for.

        Two things could go stale while issuing several in one frame, and neither does:
        `Unit.train` subtracts the cost immediately, so can_afford stays accurate, and
        the Scheduler records each producer it hands out. Any we can't order this frame
        are recorded in step_state and ordered on a later one."""
        unit = unit_id(step.what)
        want = step.count or 1
        ts = self.step_state.train

        while ts.issued < want:
            if not self.can_afford(unit):
                self._status = f"can't afford {step.what} ({ts.issued}/{want} ordered)"
                break  # save for it (costs money + supply), stalling the line
            producer = self.scheduler.producer_for(unit)
            if producer is None:
                self._status = f"every {PRODUCER.get(unit, 'producer')} queue is full ({ts.issued}/{want} ordered)"
                break
            producer.train(unit)
            ts.issued += 1

        return ts.issued >= want

    async def do_warp(self, step: Step) -> bool:
        """Warp in `count` of a unit (default 1) — a round across every ready Warpgate.

        A warp isn't "done" when we issue it: a Warpgate still reads as off-cooldown to
        get_available_abilities on the frame(s) right after we warp from it, so trusting
        the issue would over-warp (mark N done with only 2 gates). Instead advance once
        the units are on the map, collected by tag in step_state.warp — see WarpConfirm
        for why tags and not a count of the type.

        This is also why `count` can't over-warp. A gate used this frame may still read
        ready next frame, but the unit it produced appears in that same observation, so
        `remaining` drops by one at the same time."""
        unit = unit_id(step.what)
        want = step.count or 1
        wc = self.step_state.warp

        # Anything of this type that's part-built is mid-warp. Whatever was already
        # mid-warp on our first frame was ordered by an earlier step, so subtract it.
        warping_now = {u.tag for u in self.units(unit) if u.build_progress < 1}
        if wc.warping_at_start is None:
            wc.warping_at_start = warping_now
        wc.warped_in |= warping_now - wc.warping_at_start

        remaining = want - len(wc.warped_in)
        if remaining <= 0:
            return True

        if not self.can_afford(unit):
            self._status = f"can't afford {step.what} ({remaining} of {want} left)"
            return False  # save for it (costs money + supply), stalling the line
        ability = WARP_ABILITY[unit]
        warpgates = self.structures(U.WARPGATE).ready
        if not warpgates:
            self._status = "no ready Warpgate (research/morph pending)"
            return False  # no Warpgate yet (research/morph pending) — stall the line
        avail = await self.get_available_abilities(warpgates)
        ready = [wg for wg, abils in zip(warpgates, avail) if ability in abils]
        if not ready:
            self._status = "all Warpgates on cooldown"
            return False  # all on cooldown — stall until one is up
        pylon = self._warp_pylon(step.where)
        if pylon is None:
            self._status = f"no powered pylon at {step.where}"
            return False  # no powering pylon at that place yet

        # One warp per gate per frame, each onto its OWN tile — two gates sent to the same
        # tile means at most one of them lands. `Unit.warp_in` subtracts cost AND supply,
        # so can_afford stays honest as we go down the list.
        gates = ready[:remaining]
        tiles = await self._warp_tiles(ability, pylon, len(gates))
        if not tiles:
            self._status = f"nowhere clear to warp in at {step.where}"
            return False
        for gate, pos in zip(gates, tiles):
            if not self.can_afford(unit):
                self._status = f"can't afford the rest of {step.what} ({remaining} of {want} left)"
                break
            gate.warp_in(unit, pos)
        return False  # issued; stall until the units appear (confirm above)

    async def _warp_tiles(self, ability: AbilityId, pylon: Unit, needed: int) -> list[Point2]:
        """Up to `needed` distinct tiles by `pylon` that a unit can actually be warped
        onto, nearest first.

        Done by hand because find_placement cannot answer this question. Its placement
        query reports a tile with a friendly unit standing on it as free, which is right
        for a building — units walk out of the way for one — and wrong for a warp-in,
        where they don't."""
        near = pylon.position
        reach = int(PYLON_POWER_RADIUS)
        grid = [near.offset((dx, dy))
                for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1)]
        powered = sorted((p for p in grid if self.state.psionic_matrix.covers(p)),
                         key=lambda p: p.distance_to(near))
        placeable = await self.can_place(ability, powered)
        return [p for ok, p in zip(placeable, powered)
                if ok and not self.all_units.closer_than(WARP_TILE_CLEARANCE, p)][:needed]

    def _warp_pylon(self, where: str) -> Unit | None:
        """The ready pylon nearest the requested place — so `where: proxy` warps
        at the proxy pylon out on the map, `main` at home, etc."""
        pylons = self.structures(U.PYLON).ready
        if not pylons:
            return None
        return pylons.closest_to(self._resolve_place(where))

    async def do_morph(self, step: Step) -> bool:
        spec = MORPH[step.to]
        if self.step_state.morph.baseline is None:
            # start: how many `dest` to make — every ready source for a 1:1 convert
            # (gateway<->warpgate), or as many pairs as we have for a 2:1 combine
            # (2 HT/DT -> 1 Archon) — and the dest-count baseline to measure against.
            ready = self._morph_sources(spec).ready.amount
            self.step_state.morph.target = step.count if step.count is not None else ready // spec.consumes
            self.step_state.morph.baseline = self.all_own_units(spec.dest).amount

        remaining = self.step_state.morph.target - (self.all_own_units(spec.dest).amount - self.step_state.morph.baseline)
        if remaining <= 0:
            return True  # enough have morphed — done

        # issue to idle sources in groups of `consumes` (a converting/merging one
        # isn't idle, so it's never double-issued); stall until `remaining` appear as
        # `dest`. For the Archon combine, both templar of a pair get the ability and
        # merge into one Archon.
        idle = list(self._morph_sources(spec).idle)
        for i in range(min(remaining, len(idle) // spec.consumes)):
            for s in idle[i * spec.consumes:(i + 1) * spec.consumes]:
                s(spec.ability)
        self._status = f"morphing to {step.to} ({remaining} left)"
        return False

    def _morph_sources(self, spec: MorphSpec) -> Units:
        """Ready units/structures that can morph into `spec.dest` (both HT and DT
        for the Archon combine; the single source structure for a conversion)."""
        return self.all_own_units(set(spec.sources)).ready

    async def do_research(self, step: Step) -> bool:
        upgrade = RESEARCH[step.what]
        if self.already_pending_upgrade(upgrade) > 0 or upgrade in self.state.upgrades:
            return True  # already researching or done
        if not self.can_afford(upgrade):
            self._status = f"can't afford {step.what}"
            return False  # save for it, stalling the line
        self.research(upgrade)  # finds the structure + issues; confirm next frame
        return False

    async def do_hallucinate(self, step: Step) -> bool:
        casters = self.units(HALLUCINATION_CASTER).filter(lambda u: u.energy >= HALLUCINATION_ENERGY)
        if not casters:
            self._status = f"no Sentry with {HALLUCINATION_ENERGY} energy"
            return False  # no Sentry with enough energy yet — stall the line
        # By tag, not list position — observation order isn't stable between runs.
        max(casters, key=lambda c: (c.energy, c.tag))(HALLUCINATION_ABILITY)
        return True

    async def do_chrono(self, step: Step) -> bool:
        # Like every step, a chrono STALLS until it fires: if there's no Nexus with
        # enough energy, or nothing of the target type exists yet, it waits. Builds are
        # precise — place a chrono where it will actually have energy and something to
        # boost, not as a best-effort sprinkle. A chrono step ALWAYS spends a chrono.
        target_type = unit_id(step.target)
        target_pool = self.structures(target_type).ready
        if not target_pool:
            self._status = f"no {step.target} ready to boost"
            return False  # nothing of that type exists yet — wait
        # Rank targets: not-already-boosted first (a second boost on the same building
        # is wasted), then producing before idle. Tag breaks ties for determinism.
        target = min(target_pool, key=lambda s: (s.has_buff(CHRONO_BUFF), not s.orders, s.tag))
        # Ask AFTER we know there's something to boost: the scheduler commits the energy
        # when it hands back a caster, so bailing out later would waste it for this frame.
        caster = self.scheduler.chrono_caster()
        if caster is None:
            self._status = f"no Nexus with {CHRONO_ENERGY} energy left this frame"
            return False
        caster(CHRONO_ABILITY, target)
        return True

    async def do_send_probe(self, step: Step) -> bool:
        # Reuse the probe already under this name if it's still alive (e.g. move
        # the "scout" from the enemy main out to the proxy), else pull a fresh one.
        # A sent probe is held out of automation until a return_probe (see
        # _probes_unavailable_to_automation), so it stays put/on-task rather than drifting back to mine.
        dest = self._resolve_place(step.where)
        worker = self._worker_by_name(step.who)
        if worker is None:
            worker = self._free_probe_near(dest)
            if worker is None:
                self._status = "no free probe to send"
                return False  # no probe available yet — stall the line
            self.named_probes[step.who] = worker.tag
            # No "home base" for a sent prober: a sent probe is gone for a long time, so
            # it rejoins at the populating base (via return_probe), not wherever it
            # happened to be mining.
            self.home_base_by_builder.pop(worker.tag, None)
        worker.move(dest)
        return True

    async def do_return_probe(self, step: Step) -> bool:
        tag = self.named_probes.pop(step.who, None)
        worker = self._worker_by_tag(tag) if tag is not None else None
        if worker is not None:
            field = self._populating_field()  # our currently-populating base, never enemy minerals
            if field is not None:
                worker.gather(field)
        return True

    async def do_set_rally_point(self, step: Step) -> bool:
        self.rally_point = self._resolve_place(step.where)
        # Forget who's been set, so apply_rally re-issues to every EXISTING producer
        # next frame,  not just to ones built from here on.
        self._rallied.clear()
        return True

    async def do_rally_and_transfer_probes(self, step: Step) -> bool:
        # The build says to populate base N, so we do — no check that a Nexus is there
        # yet. The location is what matters; probes sent to its minerals will be mining
        # by the time it finishes.
        self.populating_base_num = self.BASE_RANK[step.where] + 1
        target = self.base_position(self.populating_base_num)
        field = self._base_field(target)
        if field is None:
            return True  # no patches there (mined out, or off the map's expansion list)
        # rally every Nexus's new probes onto that base's minerals
        for nexus in self.townhalls(U.NEXUS):
            nexus(AbilityId.RALLY_WORKERS, field)
        # transfer every OTHER base's excess mineral workers here, leaving each at
        # the cap. assigned_harvesters is the accurate count, so moving exactly
        # (assigned - ideal) of that base's workers lands it on the cap. Prefer ones
        # not carrying (no wasted trip), but include carriers if needed to reach it.
        pool = self.workers.tags_not_in(self._probes_unavailable_to_automation()).filter(lambda w: w.is_gathering)
        for th in self.ordered_bases():
            if th.position.distance_to(target) < 6:
                continue  # this IS the target base
            excess = th.assigned_harvesters - th.ideal_harvesters
            if excess <= 0:
                continue
            near = pool.closer_than(10, th).sorted(key=lambda w: w.is_carrying_minerals)
            for w in near:
                if excess <= 0:
                    break
                w.gather(field)
                excess -= 1
        return True

    async def do_set_gas_probes(self, step: Step) -> bool:
        self.gas_target = step.count
        return True

    async def do_wait(self, step: Step) -> bool:
        return True  # a pure trigger; holds the line until `at` fires, then completes

    async def do_cut_probes(self, step: Step) -> bool:
        self.continuously_build_workers = False
        return True

    async def do_resume_probes(self, step: Step) -> bool:
        self.continuously_build_workers = True
        return True
