"""The step executor for BuildOrderBot: the strict-order engine (trigger gating,
confirm/stall, prewalk hand-off) and every `do_<action>` handler.

`StepsMixin` is mixed into `BuildOrderBot` (see bot.py); its methods run on the
live bot via `self` and lean on the other mixins' helpers (`_free_probe_near`,
`_ordered_bases`, `_resolve_place`, `_describe`, …).
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
    CHRONO_CASTER,
    CHRONO_ENERGY,
    HALLUCINATION_ABILITY,
    HALLUCINATION_CASTER,
    FULL_MINERAL_SATURATION,
    HALLUCINATION_ENERGY,
    MORPH,
    PRODUCER,
    RESEARCH,
    WARP_ABILITY,
    MorphSpec,
    unit_id as _unit,
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
            if not await self.execute(step):
                return  # stall the line until this step can be done (strict order)
            note = f"  # {step.note}" if step.note else ""
            print(f"[step] {self._clock():>4}  sup{self.supply_used:<3} {self._describe(step)}{note}", flush=True)
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
            return self.all_own_units(_unit(name)).ready.amount >= n
        return True

    def _prewalk_due(self, step: Step) -> bool:
        """Whether to start walking the builder. For a resource-based prewalk
        ({minerals}/{vespene}) we reserve the cost of the probes still needed to
        reach the build's supply, then check the threshold against what's left —
        i.e. the minerals we'd have AFTER committing those probes, not minerals
        that are about to be spent on them. (Probes cost 50 minerals / 0 gas.)
        Other trigger forms use trigger_met directly."""
        trig = getattr(step, "prewalk", None) or step.at
        if trig.minerals is not None or trig.vespene is not None:
            reserved = 0
            if step.at.supply is not None:
                # supply_used already counts in-production probes (supply is
                # reserved the moment training starts), so DON'T also subtract
                # already_pending — that double-counts the building probe and
                # fires the pull one supply early.
                need = step.at.supply - self.supply_used
                reserved = 50 * max(0, need)
            if trig.minerals is not None and self.minerals - reserved < trig.minerals:
                return False
            if trig.vespene is not None and self.vespene < trig.vespene:
                return False
            return True
        return self.trigger_met(trig)

    async def execute(self, step: Step) -> bool:
        self._status = ""  # handlers set this when they return False; shown by [status]
        handler = getattr(self, f"do_{step.do}")
        return await handler(step)

    # ----------------------------------------------------------- step handlers
    async def do_build(self, step: Step) -> bool:
        unit = _unit(step.what)

        # A build isn't "done" when the command is issued — the probe may walk a
        # long way, and a probe over the tile can block placement so the order is
        # dropped. Confirm the structure actually appears: the type's count grows
        # past the baseline captured on our first frame on this step.
        if self.step_state.build.baseline is None:
            self.step_state.build.baseline = self.structures(unit).amount

        if self.structures(unit).amount > self.step_state.build.baseline:
            self._clear_prewalk()  # the prewalk outlives steps, so clear it explicitly
            return True

        if not self.can_afford(unit):
            self._status = f"can't afford {step.what}"
            return False  # save for it, stalling the line

        # Don't re-issue while the probe we already sent is still carrying out the
        # build (walking to the spot — possibly across the map — or placing it);
        # re-issue only once it's died or dropped the order without a structure
        # appearing. Re-issuing every frame during a long walk spawned duplicate
        # builds in the wrong place and spurious structures that falsely satisfied
        # the (type-wide) count confirm.
        builder = self._worker_by_tag(self.step_state.build.builder_tag)
        if builder is not None and not (builder.is_idle or builder.is_gathering):
            self._status = f"{step.what} under construction"
            return False

        # Builder: an explicitly sent probe (step.label), else the pre-walked probe,
        # else auto-selected from the free pool — never a sent probe.
        label = getattr(step, "label", None)
        if label is not None:
            builder = self._named_worker(label)  # may be None if it died -> auto-select
            target = None
        else:
            prewalked = self.prewalk_state.for_step is step
            builder = self._prewalk_worker() if prewalked else None
            target = self.prewalk_state.target if prewalked else None
        # how the builder was picked, for the [builder] line (None here => auto-selected below)
        origin = "auto" if builder is None else (f"label:{label}" if label is not None else "prewalk")

        # BotAI.build() defaults to random_alternative=True, which makes find_placement
        # return random.choice(valid_spots) from an unseeded RNG whenever our own spot
        # isn't placeable this frame (a probe standing on it, say). That silently
        # undoes placement.py's deterministic geometry, so pin it off here too.
        if unit == U.ASSIMILATOR:
            dest = target if isinstance(target, Unit) else None  # the geyser, if one was reserved
            chosen = await self._build_gas(builder, dest)
        elif unit == U.NEXUS:
            loc = target if isinstance(target, Point2) else self._next_expansion()
            if loc is None:
                self._status = "no expansion location"
                return False
            dest = loc
            chosen = builder or self._free_probe_near(loc)
            if chosen is not None:
                await self.build(U.NEXUS, near=loc, build_worker=chosen, placement_step=1,
                                 random_alternative=False)
        else:
            loc = target if isinstance(target, Point2) else await self._placement_for(step)
            if loc is None:
                self._status = f"no placement for {step.what}"
                return False
            dest = loc
            chosen = builder or self._free_probe_near(loc)
            if chosen is not None:
                await self.build(unit, near=loc, build_worker=chosen, random_alternative=False)

        # Remember the committed probe so we wait for it instead of re-issuing.
        if chosen is not None:
            self.step_state.build.builder_tag = chosen.tag
        self._log_builder(step, origin, chosen, dest)
        return False  # issued; stall the line until the structure appears (confirm)

    def _log_builder(self, step: Step, origin: str, chosen: Unit | None, dest) -> None:
        """One `[builder]` line per build issued: what, where, which probe got it and how
        that probe was picked, plus how far it still has to walk.

        The prewalk hand-off is the subtlest machinery in the bot — reserve a probe for a
        future step, walk it across the map, hand it to do_build without re-issuing — and
        this is the only window into which probe actually got the job. Unconditional: a
        build issues ~30 times a game, and needing a flag means re-running to diagnose."""
        where = getattr(step, "where", None)
        pos = dest.position if isinstance(dest, Unit) else dest
        dist = f" dist={chosen.distance_to(pos):.1f}" if (chosen is not None and pos is not None) else ""
        print(f"[builder] {self._clock():>4}  {step.what}{'/' + where if where else ''}"
              f" <- probe {chosen.tag if chosen else None} ({origin}){dist}", flush=True)

    async def _build_gas(self, builder: Unit | None = None, geyser: Unit | None = None) -> Unit | None:
        """Issue an Assimilator on a free geyser; returns the worker used (or None)."""
        if geyser is None:
            geyser = self._free_geyser()
        if geyser is None:
            return None
        worker = builder or self._free_probe_near(geyser.position)
        if worker is None:
            return None
        worker.build_gas(geyser)
        return worker

    async def do_train(self, step: Step) -> bool:
        unit = _unit(step.what)
        if not self.can_afford(unit):
            self._status = f"can't afford {step.what}"
            return False  # save for it (costs money + supply), stalling the line
        producer = PRODUCER.get(unit)
        if producer == U.NEXUS or unit == U.PROBE:
            havers = self.townhalls.ready.idle
        else:
            havers = self.structures(producer).ready.idle
        if not havers:
            self._status = f"no idle {producer.name if producer else 'producer'}"
            return False  # producer busy — wait for it (correct if the order is right)
        # The unit cache doesn't refresh mid-frame, so a producer we just ordered still
        # reads as idle. Without preferring one we haven't used this frame, a burst of
        # train steps all stack onto havers.first while its siblings sit empty
        # (measured: 5 Gateways at queue depths [1,1,2,2,4] instead of [1,1,1,1,1]).
        # This is a PREFERENCE, not a gate: once every producer has been used this
        # frame we still issue (queuing on one) rather than stalling. Stalling here
        # starves the step queue — each train step would then have to wait for a
        # genuinely idle producer, so everything ordered behind the trains crawls.
        # Pick by tag, never by list position: the observation's structure order is not
        # stable between runs, so `.first` made which Gateway got the unit a coin flip.
        fresh = havers.tags_not_in(self._issued_this_frame)
        chosen = min(fresh or havers, key=lambda p: p.tag)
        self._issued_this_frame.add(chosen.tag)
        chosen.train(unit)
        return True  # issued (producer now busy); the next step proceeds concurrently

    async def do_warp(self, step: Step) -> bool:
        unit = _unit(step.what)

        # A warp isn't "done" when we issue it: a Warpgate still reads as
        # off-cooldown to get_available_abilities on the frame(s) right after we
        # warp from it, so trusting the issue would over-warp (mark N steps done
        # with only 2 gates). Confirm the unit actually appears — its count grows
        # past the baseline captured when this step started — and only then advance.
        if self.step_state.warp.baseline is None:
            self.step_state.warp.baseline = self.units(unit).amount

        if self.units(unit).amount > self.step_state.warp.baseline:
            return True

        if not self.can_afford(unit):
            self._status = f"can't afford {step.what}"
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
        pos = await self.find_placement(ability, near=pylon.position, placement_step=1,
                                        random_alternative=False)
        if pos is None:
            self._status = f"no free warp tile at {step.where}"
            return False  # no free powered tile by that pylon right now
        ready[0].warp_in(unit, pos)
        return False  # issued; stall until the unit appears (confirm above)

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
        target_type = _unit(step.target)
        nexuses = self.townhalls(CHRONO_CASTER).ready.filter(
            lambda n: n.energy >= CHRONO_ENERGY and n.tag not in self._chrono_cast_this_frame)
        if not nexuses:
            self._status = f"no Nexus with {CHRONO_ENERGY} energy"
            return False  # no Nexus with enough energy yet — wait
        pool = (self.townhalls(U.NEXUS) if target_type == U.NEXUS
                else self.structures(target_type)).ready
        if not pool:
            self._status = f"no {step.target} ready to boost"
            return False  # nothing of that type exists yet — wait
        # Rank targets: not-already-boosted first (a second boost on the same building
        # is wasted), then producing before idle. Tag breaks ties LAST — the
        # observation's structure order is not stable between runs, so without a fixed
        # tie-break the same build boosts a different Gateway each run.
        target = min(pool, key=lambda s: (s.has_buff(CHRONO_BUFF), not s.orders, s.tag))
        # Spend from the FULLEST Nexus, not whichever happens to be first in the
        # observation list. Draining the fullest also keeps any one Nexus off the
        # 200-energy cap, where further regen is thrown away.
        caster = max(nexuses, key=lambda n: (n.energy, n.tag))
        self._chrono_cast_this_frame.add(caster.tag)
        caster(CHRONO_ABILITY, target)
        return True

    async def do_send_probe(self, step: Step) -> bool:
        # Reuse the probe already under this label if it's still alive (e.g. move
        # the "scout" from the enemy main out to the proxy), else pull a fresh one.
        # A sent probe is held out of automation until a return_probe (see
        # _excluded_tags), so it stays put/on-task rather than drifting back to mine.
        dest = self._resolve_place(step.where)
        worker = self._named_worker(step.label)
        if worker is None:
            worker = self._free_probe_near(dest)
            if worker is None:
                self._status = "no free probe to send"
                return False  # no probe available yet — stall the line
            self.named_probes[step.label] = worker.tag
        worker.move(dest)
        return True

    async def do_return_probe(self, step: Step) -> bool:
        tag = self.named_probes.pop(step.label, None)
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
        bases = self._ordered_bases()
        if step.base > len(bases):
            self._status = f"base {step.base} not up yet (have {len(bases)})"
            return False  # that base isn't up yet — stall the line until it exists
        self.populating_base_num = step.base
        base = bases[step.base - 1]
        field = self._base_field(base)
        if field is None:
            return True
        # rally every Nexus's new probes onto this base's minerals
        for nexus in self.townhalls(U.NEXUS).ready:
            nexus(AbilityId.RALLY_WORKERS, field)
        # transfer every OTHER base's excess mineral workers here, leaving each at
        # the cap. assigned_harvesters is the accurate count, so moving exactly
        # (assigned - cap) of that base's workers lands it on the cap. Prefer ones
        # not carrying (no wasted trip), but include carriers if needed to reach it.
        pool = self.workers.tags_not_in(self._excluded_tags()).filter(lambda w: w.is_gathering)
        for th in bases:
            if th.tag == base.tag:
                continue
            excess = th.assigned_harvesters - FULL_MINERAL_SATURATION
            if excess <= 0:
                continue
            near = pool.closer_than(10, th).sorted(key=lambda w: w.is_carrying_minerals)
            for w in near:
                if excess <= 0:
                    break
                w.gather(field)
                excess -= 1
        return True

    async def do_gas_workers(self, step: Step) -> bool:
        self.gas_target = step.count
        return True

    async def do_wait(self, step: Step) -> bool:
        return True  # a pure trigger; holds the line until `at` fires, then completes

    async def do_workers(self, step: Step) -> bool:
        self.continuously_build_workers = step.state == "start"
        return True
