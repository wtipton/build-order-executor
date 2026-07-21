"""Generic build-order bot for python-sc2.

`BuildOrderBot` executes a build described entirely by a validated config (see
schema.py + builds/*.yaml). Nothing about a specific build is hard-coded here.

Two layers:

  * STEPS  — the deliberate actions a human performs and must learn: build,
             train, chrono, scout, rally, plus economy overrides. Each step has
             a trigger and fires once, in order.
  * ECONOMY — automatic behaviour the bot runs on its own unless a step
             overrides it: make probes continuously, never leave a worker idle,
             saturate minerals to N per base and then fill gas, return builders
             to mining.

The YAML spec (triggers, actions, economy) is defined and validated in schema.py.
"""

from __future__ import annotations

from sc2.bot_ai import BotAI
from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit

from placement import Placement
from schema import BuildConfig, Step, Trigger, load_build  # noqa: F401  (re-exported for run.py)

CHRONO = AbilityId.EFFECT_CHRONOBOOSTENERGYCOST
CHRONO_BUFF = BuffId.CHRONOBOOSTENERGYCOST

# Grace period (game-seconds) between the build finishing and conceding, so the
# last actions land and are watchable rather than cutting out instantly.
CONCEDE_GRACE = 10.0

# Which producer builds/trains a given unit.
PRODUCER: dict[U, U] = {
    U.PROBE: U.NEXUS,
    U.ZEALOT: U.GATEWAY,
    U.STALKER: U.GATEWAY,
    U.SENTRY: U.GATEWAY,
    U.ADEPT: U.GATEWAY,
    U.HIGHTEMPLAR: U.GATEWAY,
    U.DARKTEMPLAR: U.GATEWAY,
    U.IMMORTAL: U.ROBOTICSFACILITY,
    U.OBSERVER: U.ROBOTICSFACILITY,
    U.WARPPRISM: U.ROBOTICSFACILITY,
    U.COLOSSUS: U.ROBOTICSFACILITY,
    U.DISRUPTOR: U.ROBOTICSFACILITY,
    U.PHOENIX: U.STARGATE,
    U.ORACLE: U.STARGATE,
    U.VOIDRAY: U.STARGATE,
    U.TEMPEST: U.STARGATE,
    U.CARRIER: U.STARGATE,
}

# Warp-in ability per unit (checked against get_available_abilities to know when
# a Warpgate is off cooldown for that unit). Unit.warp_in() maps the ability
# itself; this table is only for the readiness check.
WARP_ABILITY: dict[U, AbilityId] = {
    U.ZEALOT: AbilityId.WARPGATETRAIN_ZEALOT,
    U.STALKER: AbilityId.WARPGATETRAIN_STALKER,
    U.SENTRY: AbilityId.WARPGATETRAIN_SENTRY,
    U.HIGHTEMPLAR: AbilityId.WARPGATETRAIN_HIGHTEMPLAR,
    U.DARKTEMPLAR: AbilityId.WARPGATETRAIN_DARKTEMPLAR,
    U.ADEPT: AbilityId.TRAINWARP_ADEPT,
}

# morph `to` -> (source structure, destination structure, morph ability).
MORPH: dict[str, tuple[U, U, AbilityId]] = {
    "warpgate": (U.GATEWAY, U.WARPGATE, AbilityId.MORPH_WARPGATE),
    "gateway": (U.WARPGATE, U.GATEWAY, AbilityId.MORPH_GATEWAY),
}

# Friendly research names -> UpgradeId (issued via self.research()).
RESEARCH: dict[str, UpgradeId] = {
    "Warpgate": UpgradeId.WARPGATERESEARCH,
    "Blink": UpgradeId.BLINKTECH,
    "Charge": UpgradeId.CHARGE,
}

# Friendly spell names -> (ability, caster unit type, energy cost). Instant self-
# casts (Hallucination spawns the fake unit next to the Sentry; no target needed).
CAST: dict[str, tuple[AbilityId, U, int]] = {
    "Hallucination": (AbilityId.HALLUCINATION_PHOENIX, U.SENTRY, 75),  # default: Phoenix (scout)
    "HallucinationPhoenix": (AbilityId.HALLUCINATION_PHOENIX, U.SENTRY, 75),
    "HallucinationArchon": (AbilityId.HALLUCINATION_ARCHON, U.SENTRY, 75),
}


def _unit(name: str) -> U:
    return U[name.upper()]


class BuildOrderBot(BotAI):
    def __init__(self, config: BuildConfig, debug: bool = False):
        super().__init__()
        self.cfg = config
        self.debug = debug
        self.placement = Placement(self)
        self.steps: list[Step] = config.steps
        self._done: list[bool] = [False] * len(self.steps)
        self.idx = 0

        self.continuous_workers: bool = config.economy.continuous_workers
        self.minerals_per_base: int = config.economy.minerals_per_base
        self.gas_target: int = 0  # no workers in gas until a `gas_workers` step says so

        self.rally_target = None
        self.scout_sent = False
        self._last_hb = -999
        self._conceded = False
        self._build_done_at: float | None = None  # game-time the last step finished

        # pre-walk reservation: a probe sent to the next build's placement ahead
        # of time so construction starts the instant we can afford it.
        self._reserved_step: Step | None = None
        self._builder_tag: int | None = None
        self._build_target = None      # Point2 (placement) or geyser Unit (gas)
        self._active_step: Step | None = None

        # confirm a build actually starts (re-issue if a probe blocked the tile)
        self._building_step: Step | None = None
        self._build_baseline = 0

        # confirm a warp-in actually happened (a Warpgate looks off-cooldown to
        # get_available_abilities right after we've used it, so issuing != warped;
        # hold until a new unit of that type appears, like the build confirm above)
        self._warping_step: Step | None = None
        self._warp_baseline = 0

        # a `morph` step converts `count` buildings and holds until that many have
        # actually converted (tracked by growth in the destination-type count, so a
        # rejected order — e.g. ->warpgate before research — correctly keeps waiting)
        self._morph_step: Step | None = None
        self._morph_target = 0
        self._morph_baseline = 0

    async def on_start(self):
        self.client.game_step = 4

    async def on_end(self, result):
        print(f"[end] t={self.time:.1f}s result={result} supply={self.supply_used} "
              f"workers={self.workers.amount} idx={self.idx}/{len(self.steps)}", flush=True)

    def _clock(self) -> str:
        s = int(self.time)
        return f"{s // 60}:{s % 60:02d}"

    @staticmethod
    def _trig_str(t: Trigger) -> str:
        if t.count is not None:
            name, n = next(iter(t.count.items()))
            return f"count:{name}={n}"
        for k in ("supply", "time", "minerals", "vespene"):
            v = getattr(t, k)
            if v is not None:
                return f"{k}:{v}"
        return "?"

    def _describe(self, step: Step) -> str:
        args = step.model_dump(exclude={"at", "prewalk", "note", "do"}, exclude_none=True)
        argstr = " ".join(f"{k}={v}" for k, v in args.items())
        return f"{step.do} {argstr}".rstrip()

    async def on_step(self, iteration: int):
        if not self.townhalls:
            return
        if self.debug and self.time - self._last_hb >= 10:
            self._last_hb = self.time
            nxt = self._describe(self.steps[self.idx]) if self.idx < len(self.steps) else "DONE"
            print(f"[hb] t={self.time:5.0f}s sup={self.supply_used}/{self.supply_cap} "
                  f"w={self.workers.amount} pend_probe={self.already_pending(U.PROBE)} "
                  f"min={self.minerals} gas={self.vespene} sup_left={self.supply_left} "
                  f"next={nxt}", flush=True)
        await self.manage_prebuild()
        await self.make_workers()  # probes first: continuous, first claim on minerals each frame
        await self.run_steps()
        await self.manage_economy()
        self.apply_rally()

        # The build is the whole job: once every step has fired, keep macroing for
        # a short grace period, then concede (rather than idling to the game timer).
        if self.idx >= len(self.steps):
            if self._build_done_at is None:
                self._build_done_at = self.time
                print(f"[build] {self._clock():>4}  build complete", flush=True)
            elif not self._conceded and self.time - self._build_done_at >= CONCEDE_GRACE:
                self._conceded = True
                print(f"[build] {self._clock():>4}  conceding ({int(CONCEDE_GRACE)}s after build)", flush=True)
                await self.client.leave()

    # ============================================================ build steps
    async def run_steps(self):
        i = self.idx
        while i < len(self.steps):
            if self._done[i]:
                i += 1
                continue
            step = self.steps[i]
            if not self.trigger_met(step.at):
                break  # this step (and everything after) isn't due yet
            if not await self.execute(step):
                break  # hold the line until this step can be done (strict order)
            self._done[i] = True
            note = f"  # {step.note}" if step.note else ""
            print(f"[build] {self._clock():>4}  sup{self.supply_used:<3} {self._describe(step)}{note}", flush=True)
            i += 1
        # advance the pointer past any leading completed steps
        while self.idx < len(self.steps) and self._done[self.idx]:
            self.idx += 1

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
        self._active_step = step
        handler = getattr(self, f"do_{step.do}")
        return await handler(step)

    # ----------------------------------------------------------- step handlers
    async def do_build(self, step: Step) -> bool:
        unit = _unit(step.what)

        # A build isn't "done" when the command is issued — a probe walking over
        # the tile can block placement and the order is dropped silently. Confirm
        # the structure actually appears (count grows past the baseline captured
        # when this step started), and re-issue whenever nothing is in flight.
        if self._building_step is not step:
            self._building_step = step
            self._build_baseline = self.structures(unit).amount

        if self.structures(unit).amount > self._build_baseline:
            self._building_step = None
            self._clear_reservation()
            return True

        if not self.can_afford(unit):
            return False  # save for it, holding the line
        if self.already_pending(unit):
            return False  # a worker is en route / it's warping in — hold, don't re-issue

        # (re)issue with the pre-walked builder + cached placement if reserved
        reserved = self._reserved_step is self._active_step
        builder = self._reserved_worker() if reserved else None
        target = self._build_target if reserved else None
        if self.debug:
            pos = target.position if isinstance(target, Unit) else target
            d = f" builder dist={builder.distance_to(pos):.1f}" if (builder and pos is not None) else ""
            print(f"[build*] {self._clock():>4}  issuing {step.what}{d}", flush=True)

        if unit == U.ASSIMILATOR:
            await self._build_gas(builder, target if isinstance(target, Unit) else None)
        elif unit == U.NEXUS:
            loc = target if isinstance(target, Point2) else await self.get_next_expansion()
            if loc is not None:
                await self.build(U.NEXUS, near=loc, build_worker=builder, placement_step=1)
        else:
            loc = target if isinstance(target, Point2) else await self._placement_for(step)
            if loc is not None:
                await self.build(unit, near=loc, build_worker=builder)
        return False  # issued; hold the line until the structure appears (confirm)

    async def _build_gas(self, builder=None, geyser=None) -> bool:
        if geyser is None:
            geyser = self._free_geyser()
        if geyser is None:
            return False
        worker = builder or self.select_build_worker(geyser.position)
        if worker is None:
            return False
        worker.build_gas(geyser)
        return True

    async def do_train(self, step: Step) -> bool:
        unit = _unit(step.what)
        if not self.can_afford(unit):
            return False  # save for it (costs money + supply), holding the line
        producer = PRODUCER.get(unit)
        if producer == U.NEXUS or unit == U.PROBE:
            havers = self.townhalls.ready.idle
        else:
            havers = self.structures(producer).ready.idle
        if not havers:
            return False  # producer busy — wait for it (correct if the order is right)
        havers.first.train(unit)
        return True  # issued (producer now busy); the next step proceeds concurrently

    async def do_warp(self, step: Step) -> bool:
        unit = _unit(step.what)

        # A warp isn't "done" when we issue it: a Warpgate still reads as
        # off-cooldown to get_available_abilities on the frame(s) right after we
        # warp from it, so trusting the issue would over-warp (mark N steps done
        # with only 2 gates). Confirm the unit actually appears — its count grows
        # past the baseline captured when this step started — and only then advance.
        if self._warping_step is not step:
            self._warping_step = step
            self._warp_baseline = self.units(unit).amount

        if self.units(unit).amount > self._warp_baseline:
            self._warping_step = None
            return True

        if not self.can_afford(unit):
            return False  # save for it (costs money + supply), holding the line
        ability = WARP_ABILITY[unit]
        warpgates = self.structures(U.WARPGATE).ready
        if not warpgates:
            return False  # no Warpgate yet (research/morph pending) — hold the line
        avail = await self.get_available_abilities(warpgates)
        ready = [wg for wg, abils in zip(warpgates, avail) if ability in abils]
        if not ready:
            return False  # all on cooldown — hold until one is up
        pylon = self._warp_pylon(step.where)
        if pylon is None:
            return False  # no powering pylon at that place yet
        pos = await self.find_placement(ability, near=pylon.position, placement_step=1)
        if pos is None:
            return False  # no free powered tile by that pylon right now
        ready[0].warp_in(unit, pos)
        return False  # issued; hold until the unit appears (confirm above)

    def _warp_pylon(self, where: str):
        """The ready pylon nearest the requested place — so `where: proxy` warps
        at the proxy pylon out on the map, `main` at home, etc."""
        pylons = self.structures(U.PYLON).ready
        if not pylons:
            return None
        return pylons.closest_to(self._resolve_place(where))

    async def do_morph(self, step: Step) -> bool:
        src, dst, ability = MORPH[step.to]
        if self._morph_step is not step:
            # start: how many to convert (all currently-built source structures if
            # no count given), and the destination-count baseline to measure against
            self._morph_step = step
            self._morph_target = step.count if step.count is not None else self.structures(src).ready.amount
            self._morph_baseline = self.structures(dst).amount

        remaining = self._morph_target - (self.structures(dst).amount - self._morph_baseline)
        if remaining <= 0:
            self._morph_step = None
            return True  # enough have converted — done

        # issue the morph to that many idle source buildings (a busy/converting one
        # isn't idle, so it's never double-issued); hold until `remaining` convert
        for s in self.structures(src).ready.idle:
            if remaining <= 0:
                break
            s(ability)
            remaining -= 1
        return False

    async def do_research(self, step: Step) -> bool:
        upgrade = RESEARCH[step.what]
        if self.already_pending_upgrade(upgrade) > 0 or upgrade in self.state.upgrades:
            return True  # already researching or done
        if not self.can_afford(upgrade):
            return False  # save for it, holding the line
        self.research(upgrade)  # finds the structure + issues; confirm next frame
        return False

    async def do_cast(self, step: Step) -> bool:
        ability, caster_type, energy = CAST[step.what]
        casters = self.units(caster_type).filter(lambda u: u.energy >= energy)
        if not casters:
            return False  # no caster with enough energy yet — hold the line
        casters.first(ability)
        return True

    async def do_chrono(self, step: Step) -> bool:
        target_type = _unit(step.target)
        nexuses = self.townhalls(U.NEXUS).ready.filter(lambda n: n.energy >= 50)
        if not nexuses:
            return False  # wait until a Nexus has 50 energy
        if target_type == U.NEXUS:
            target = next((n for n in self.townhalls(U.NEXUS).ready if n.orders), None)
            target = target or self.townhalls(U.NEXUS).ready.first
        else:
            target = next((s for s in self.structures(target_type).ready if s.orders), None)
        if target is None:
            return False  # nothing of that type is producing yet
        if target.has_buff(CHRONO_BUFF):
            return True  # already boosted — count the action as done
        nexuses.first(CHRONO, target)
        return True

    async def do_scout(self, step: Step) -> bool:
        if self.scout_sent:
            return True
        if not self.enemy_start_locations:
            return True
        worker = self.workers.gathering.random_or(self.workers.random) if self.workers else None
        if worker is None:
            return False
        worker.move(self.enemy_start_locations[0])
        self.scout_sent = True
        return True

    async def do_rally(self, step: Step) -> bool:
        self.rally_target = self._resolve_place(step.where)
        return True

    async def do_gas_workers(self, step: Step) -> bool:
        self.gas_target = step.count
        return True

    async def do_minerals_cap(self, step: Step) -> bool:
        self.minerals_per_base = step.count
        return True

    async def do_workers(self, step: Step) -> bool:
        self.continuous_workers = step.state == "start"
        return True

    # How far out (by base rank) `proxy` sits from the enemy start: 0 = their
    # main, 1 = natural, 2 = third, 3 = fourth. The natural is too close, so we
    # aim a few bases out toward the middle of the map.
    PROXY_BASE_RANK = 3

    def _resolve_place(self, where: str):
        if where == "main":
            return self.start_location
        if where == "proxy":
            return self._expansion_near(self._enemy_start(), self.PROXY_BASE_RANK)
        # "natural": our own natural (the base one rank out from our start)
        return self._expansion_near(self.start_location, 1)

    def _enemy_start(self):
        return self.enemy_start_locations[0] if self.enemy_start_locations else self.game_info.map_center

    def _expansion_near(self, base, rank: int):
        """The `rank`-th expansion by distance from `base` (0 = the base itself,
        1 = its natural, 2 = third, ...), clamped to what the map provides."""
        exps = sorted(self.expansion_locations_list, key=lambda e: e.distance_to(base))
        if not exps:
            return base
        return exps[min(rank, len(exps) - 1)]

    # ========================================================= pre-walk builder
    async def manage_prebuild(self):
        """Reserve a probe for the next build step and walk it to the placement
        ahead of time, so `do_build` can start construction the instant we can
        afford it. WHEN to start walking is config-driven (the step's `prewalk`
        trigger, defaulting to its `at` trigger) — the bot does no estimating."""
        step = self._next_build_step()
        if self._reserved_step is not None and self._reserved_step is not step:
            self._clear_reservation()
        if step is None or not self._prewalk_due(step):
            return

        if self._reserved_step is step:
            self._reposition_builder()
            return

        # do_build may have already issued this build itself (its trigger fired
        # before prewalk got to reserve — e.g. a research/train/warp step just
        # ahead of it completed on the same frame, so _next_build_step only now
        # returns this step). A probe is then en route with the build order, which
        # for Protoss is exactly what already_pending() counts — don't reserve a
        # SECOND probe on top of it.
        if self.already_pending(_unit(step.what)):
            return

        target = await self._placement_for(step)
        if target is None:
            return  # can't determine placement yet (e.g. no pylon) — retry later
        pos = target.position if isinstance(target, Unit) else target
        worker = self.select_build_worker(pos)
        if worker is None:
            return
        self._reserved_step = step
        self._builder_tag = worker.tag
        self._build_target = target
        worker.move(pos)
        if self.debug:
            print(f"[prewalk] {self._clock():>4}  sup{self.supply_used} min={self.minerals} "
                  f"reserved probe for {step.what} @ ({pos.x:.0f},{pos.y:.0f}) "
                  f"(build at {self._trig_str(step.at)})", flush=True)

    def _next_build_step(self) -> Step | None:
        for j in range(self.idx, len(self.steps)):
            if self._done[j]:
                continue
            s = self.steps[j]
            if s.do == "build":
                return s
            if s.do in ("train", "warp", "research"):
                return None  # a resource-committing step precedes the next build
        return None

    async def _placement_for(self, step: Step):
        unit = _unit(step.what)
        # A `where` proxies the building out on the map (e.g. a Pylon near the
        # enemy). Anchor placement at that place instead of the home heuristics;
        # the prewalk machinery then walks a probe there ahead of time for free.
        where = getattr(step, "where", None)
        if where is not None:
            return await self.find_placement(unit, near=self._resolve_place(where), max_distance=20)
        if unit == U.NEXUS:
            return await self.get_next_expansion()
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
        if self._builder_tag is None:
            return None
        res = self.workers.tags_in({self._builder_tag})
        return res.first if res else None

    def _reposition_builder(self):
        w = self._reserved_worker()
        if w is None:
            self._clear_reservation()
            return
        pos = self._build_target.position if isinstance(self._build_target, Unit) else self._build_target
        if pos is not None and w.is_idle and w.distance_to(pos) > 1:
            w.move(pos)

    def _clear_reservation(self):
        self._reserved_step = None
        self._builder_tag = None
        self._build_target = None

    def _econ_workers(self):
        """Workers available to the economy — excludes the reserved builder so it
        isn't yanked back to mining while waiting in position."""
        if self._builder_tag is None:
            return self.workers
        return self.workers.tags_not_in({self._builder_tag})

    # ============================================================ auto economy
    async def make_workers(self):
        # Make probes continuously, full stop. If a build needs to pause worker
        # production it says so with a `workers: stop` step (which clears
        # continuous_workers). No target/cap maths — supply blocking from the
        # build's own pylons is the only thing that throttles it.
        if not self.continuous_workers or self.supply_left <= 0:
            return
        for nexus in self.townhalls.ready.idle:
            if self.can_afford(U.PROBE):
                nexus.train(U.PROBE)

    async def manage_economy(self):
        """Saturate minerals up to the per-base cap, then fill gas to gas_target,
        and keep no worker idle."""
        workers = self._econ_workers()

        # A probe that just warped in an Assimilator gets auto-queued to harvest
        # it and stands on the (still-building) geyser doing nothing — it reads as
        # "gathering", not idle, so send it back to minerals until the gas is done
        # (the gas-fill below re-assigns as needed once it's ready).
        building_gas = {g.tag for g in self.gas_buildings.not_ready}
        if building_gas:
            for w in workers.gathering:
                if w.order_target in building_gas:
                    mins = self._minerals_under_cap() or self.mineral_field
                    if mins:
                        w.gather(mins.closest_to(w))

        gas_ready = self.gas_buildings.ready
        capacity = sum(g.ideal_harvesters for g in gas_ready)
        desired_gas = min(self.gas_target, capacity)
        assigned_gas = sum(g.assigned_harvesters for g in gas_ready)

        if assigned_gas < desired_gas:
            movable = workers.filter(
                lambda w: (w.is_gathering and not w.is_carrying_vespene) or w.is_idle
            )
            for g in gas_ready:
                while g.assigned_harvesters < g.ideal_harvesters and assigned_gas < desired_gas and movable:
                    w = movable.closest_to(g)
                    w.gather(g)
                    movable = movable.tags_not_in({w.tag})
                    assigned_gas += 1
        elif assigned_gas > desired_gas:
            for g in gas_ready:
                while g.assigned_harvesters > 0 and assigned_gas > desired_gas:
                    w = workers.filter(lambda u: u.is_carrying_vespene is False).closest_to(g)
                    mins = self._minerals_under_cap()
                    if not (w and mins):
                        break
                    w.gather(mins.closest_to(w))
                    assigned_gas -= 1

        # never idle: park idle workers on minerals (under cap), else spare gas,
        # else nearest minerals anyway (over-saturation beats standing still)
        for w in workers.idle:
            mins = self._minerals_under_cap()
            if mins:
                w.gather(mins.closest_to(w))
                continue
            spare = gas_ready.filter(lambda g: g.assigned_harvesters < g.ideal_harvesters)
            if spare:
                w.gather(spare.closest_to(w))
                continue
            if self.mineral_field and self.townhalls.ready:
                w.gather(self.mineral_field.closest_to(self.townhalls.ready.first))

    def _minerals_under_cap(self):
        """Mineral fields belonging to a base that is below the per-base cap."""
        for th in self.townhalls.ready.sorted(key=lambda t: t.assigned_harvesters):
            if th.assigned_harvesters < self.minerals_per_base:
                fields = self.mineral_field.closer_than(10, th)
                if fields:
                    return fields
        return None

    def apply_rally(self):
        if self.rally_target is None:
            return
        for gw in self.structures(U.GATEWAY).ready:
            gw(AbilityId.RALLY_BUILDING, self.rally_target)
