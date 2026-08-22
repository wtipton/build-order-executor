"""Generic build-order bot for python-sc2.

`BuildOrderBot` executes a build described entirely by a validated config (see
schema.py + builds/*.yaml). Nothing about a specific build is hard-coded here.

Two layers:

  * STEPS  — the deliberate actions a human performs and must learn: build,
             train, warp, morph, research, hallucinate, chrono, send_probe/return_probe,
             rally, rally_and_transfer, plus economy overrides. Each step has a
             trigger and fires once, in order.
  * ECONOMY — automatic behaviour the bot runs on its own unless a step
             overrides it: make probes continuously, never leave a worker idle,
             saturate minerals to N per base and then fill gas, return builders
             to mining.

The YAML spec (triggers, actions, economy) is defined and validated in schema.py.
"""

from __future__ import annotations

import json

from sc2.bot_ai import BotAI
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2
from sc2.unit import Unit

from catalog import (
    BUILDABLE_STRUCTURES,
    CHRONO_ABILITY,
    CHRONO_BUFF,
    CHRONO_CASTER,
    CHRONO_ENERGY,
    HALLUCINATION_ABILITY,
    HALLUCINATION_CASTER,
    HALLUCINATION_ENERGY,
    MORPH,
    PRODUCER,
    RESEARCH,
    TRAINABLE_UNITS,
    WARP_ABILITY,
    unit_id as _unit,
)
from placement import PRODUCTION, Placement
from schema import BuildConfig, Step, Trigger, load_build  # noqa: F401  (re-exported for run.py)

# Name -> game-object tables and the spell constants (PRODUCER, WARP_ABILITY,
# MORPH, RESEARCH, CHRONO_*, HALLUCINATION_*) live in catalog.py — the single
# source of truth shared with schema.py's load-time validation, so every
# `what`/`to`/`target` a build names is checked before the game starts (see
# catalog.require_*). bot.py only consumes them.

# Grace period (game-seconds) between the build finishing and conceding, so the
# last actions land and are watchable rather than cutting out instantly.
CONCEDE_GRACE = 10.0

# How far (tiles) a production building rallies its units by default — off the
# spawn tile toward open ground, so units don't pile up and jam the exit.
RALLY_OFFSET = 10.0

# Stall diagnostics: how long the head step may stall before we report why, and
# how often to re-report while it's still stuck. Set high so legitimate long
# waits (saving for a Nexus, a slow count trigger) don't cry wolf — a `[stall]`
# line means "this has been sitting a while; here's what it's waiting on".
STALL_FIRST = 60.0
STALL_REPEAT = 30.0


class BuildOrderBot(BotAI):
    def __init__(self, config: BuildConfig, debug: bool = False, dump_data: bool = False):
        super().__init__()
        self.cfg = config
        self.debug = debug
        self.dump_data = dump_data
        self.placement = Placement(self)
        self.steps: list[Step] = config.steps
        self._done: list[bool] = [False] * len(self.steps)
        # A step's trigger gates only STARTING it; once fired we commit (`_started`)
        # and drive it to completion without re-checking the trigger — otherwise a
        # non-monotonic count trigger (e.g. morph archon gated on HighTemplar:2,
        # which the morph consumes) would go false mid-flight and strand the step.
        self._started: list[bool] = [False] * len(self.steps)
        self.idx = 0

        self.continuous_workers: bool = config.economy.continuous_workers
        self.minerals_per_base: int = config.economy.minerals_per_base
        self.gas_target: int = 0  # no workers in gas until a `gas_workers` step says so

        self.rally_target = None
        self._rallied: set[int] = set()  # production buildings given their default exit-clearing rally
        # label -> probe tag for probes sent out via `send_probe`. These are held
        # OUT of all worker automation (mining, prewalk, build auto-select) until a
        # `return_probe` step hands them back.
        self.named_probes: dict[str, int] = {}
        self._named_binds: dict[str, set[int]] = {}  # every probe tag ever bound per label (re-send should reuse one)
        # The base a returned/idle worker mines at (1 = main, 2 = natural, ...).
        # `rally_and_transfer` moves it; workers never fall back to random minerals.
        self.populating_base_num: int = 1
        self._last_hb = -999
        self._conceded = False
        self._build_done_at: float | None = None  # game-time the last step finished
        self._milestones: dict[str, float] = {}    # exact completion times (deadline checks)

        # stall diagnostics: when the head step (self.idx) got there, when we last
        # warned about it, and why the current handler is stalled (set by handlers
        # on a False return; surfaced by the [stall] line).
        self._head_idx = -1
        self._head_since = 0.0
        self._stall_warned = -1e9
        self._stall_reason = ""

        # pre-walk reservation: a probe sent to the next build's placement ahead
        # of time so construction starts the instant we can afford it.
        self._reserved_step: Step | None = None
        self._builder_tag: int | None = None
        self._build_target = None      # Point2 (placement) or geyser Unit (gas)
        self._active_step: Step | None = None

        # confirm a build actually starts (re-issue if a probe blocked the tile);
        # _build_builder_tag = the probe committed to the current build, so we wait
        # for it (even a long walk) instead of re-issuing and spawning duplicates.
        self._building_step: Step | None = None
        self._build_baseline = 0
        self._build_builder_tag: int | None = None

        # confirm a warp-in actually happened (a Warpgate looks off-cooldown to
        # get_available_abilities right after we've used it, so issuing != warped;
        # stall until a new unit of that type appears, like the build confirm above)
        self._warping_step: Step | None = None
        self._warp_baseline = 0

        # a `morph` step converts `count` buildings and stalls until that many have
        # actually converted (tracked by growth in the destination-type count, so a
        # rejected order — e.g. ->warpgate before research — correctly keeps waiting)
        self._morph_step: Step | None = None
        self._morph_target = 0
        self._morph_baseline = 0

    async def on_start(self):
        self.client.game_step = 4
        if self.dump_data:
            import gamedata_dump
            gamedata_dump.dump(self)

    async def on_end(self, result):
        print(f"[end] t={self.time:.1f}s result={result} supply={self.supply_used} "
              f"workers={self.workers.amount} idx={self.idx}/{len(self.steps)}", flush=True)
        print(f"[end] {self._army_report()}", flush=True)
        # Machine-readable summary as one tagged line on stdout, so a caller can
        # grep `^[summary] ` and json.loads the rest.
        print(f"[summary] {json.dumps(self._run_summary(result))}", flush=True)

    def _run_summary(self, result) -> dict:
        """Machine-readable end-of-run summary (the `[summary]` stdout line) so a
        caller asserts on what ACTUALLY happened (structures/units/upgrades produced,
        where the queue stalled) rather than parsing the human log."""
        completed = self.idx >= len(self.steps)
        census = {}
        for t in (BUILDABLE_STRUCTURES | TRAINABLE_UNITS | {U.WARPGATE, U.ARCHON}):
            n = self.all_own_units(t).ready.amount
            if n:
                census[t.name] = n
        friendly = {v: k for k, v in RESEARCH.items()}
        upgrades = sorted(friendly.get(u, u.name) for u in self.state.upgrades)
        # Upgrades that STARTED but haven't finished — so "researched but unfinished"
        # is distinguishable from "never started" (already_pending_upgrade is the
        # research progress in [0,1)).
        researching = sorted(
            name for name, u in RESEARCH.items()
            if 0 < self.already_pending_upgrade(u) < 1
        )
        return {
            "name": self.cfg.name,
            "result": str(result),
            "completed": completed,
            "steps_done": self.idx,
            "steps_total": len(self.steps),
            "next_step": None if completed else self._describe(self.steps[self.idx]),
            "final_time": round(self.time, 1),
            "milestones": {k: round(v, 1) for k, v in self._milestones.items()},
            "census": census,
            "upgrades": upgrades,
            "researching": researching,
            "bases": [int(t.assigned_harvesters) for t in self._ordered_bases()],
            "pylons_by_place": self._pylons_by_place(),
            "nexus_by_place": {p: 1 for p in self.BASE_RANK
                               if self.townhalls.closer_than(6, self._resolve_place(p)).exists},
            "named_probe_binds": {k: len(v) for k, v in self._named_binds.items()},
            "named_probes_held": sorted(self.named_probes),
            "workers": self.workers.amount,
            "supply_used": self.supply_used,
        }

    def _pylons_by_place(self) -> dict:
        """Count our Pylons near each named location — so a build placing pylons at
        `enemy_main`/`proxy`/etc. can be verified to have put one at each distinct
        spot (not all clustered in one place)."""
        out = {}
        for place in list(self.BASE_RANK) + ["enemy_main", "enemy_natural", "proxy"]:
            n = self.structures(U.PYLON).closer_than(12, self._resolve_place(place)).amount
            if n:
                out[place] = n
        return out

    def _note_milestones(self) -> None:
        """Log the exact game-time key units/upgrades first complete — heartbeats are
        only every 10s, too coarse to check tight deadlines (Void Ray 4:15, Charge 5:45)."""
        def hit(key: str, ok: bool):
            if ok and key not in self._milestones:
                self._milestones[key] = self.time
                print(f"[done] {self._clock():>4}  {key}", flush=True)
        hit("Adept#2", self.units(U.ADEPT).ready.amount >= 2)
        hit("VoidRay", self.units(U.VOIDRAY).ready.amount >= 1)
        hit("Warpgate", UpgradeId.WARPGATERESEARCH in self.state.upgrades)
        hit("Charge", UpgradeId.CHARGE in self.state.upgrades)

    def _army_report(self) -> str:
        """Actual, observed army state — so we don't infer counts from supply math."""
        z = self.units(U.ZEALOT)
        done = z.ready.amount
        proxy = self._resolve_place("proxy")
        at_proxy = z.ready.closer_than(15, proxy).amount if done else 0
        in_prod = int(self.already_pending(U.ZEALOT))  # warping-in + gateway queues
        gw = self.structures(U.GATEWAY).ready.amount
        wg = self.structures(U.WARPGATE).ready.amount
        energy = sum(n.energy for n in self.townhalls(U.NEXUS).ready)
        chronos = int(energy // CHRONO_ENERGY)
        vr = self.units(U.VOIDRAY).ready.amount
        ad = self.units(U.ADEPT).ready.amount
        wgr = "Y" if UpgradeId.WARPGATERESEARCH in self.state.upgrades else "n"
        chg = "Y" if UpgradeId.CHARGE in self.state.upgrades else "n"
        return (f"zealots done={done} (at_proxy={at_proxy}) in_prod={in_prod} | "
                f"gateways={gw} warpgates={wg} | voidray={vr} adepts={ad} "
                f"warpgate_done={wgr} charge_done={chg} | gas={self.vespene} "
                f"nexus_energy={energy:.0f}(~{chronos} chronos)")

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
            bases = " ".join(f"b{i+1}={t.assigned_harvesters}/{self.minerals_per_base}"
                             for i, t in enumerate(self._ordered_bases()))
            print(f"[hb] t={self.time:5.0f}s sup={self.supply_used}/{self.supply_cap} "
                  f"w={self.workers.amount} pend_probe={self.already_pending(U.PROBE)} "
                  f"min={self.minerals} gas={self.vespene} sup_left={self.supply_left} "
                  f"mins[{bases}] next={nxt}", flush=True)
            print(f"[hb] {self._army_report()}", flush=True)
        self._note_milestones()
        await self.manage_prebuild()
        await self.make_workers()  # probes first: continuous, first claim on minerals each frame
        await self.run_steps()
        self._check_stall()
        await self.manage_economy()
        self.apply_rally()

        # The build is the whole job: once every step has fired, keep macroing for
        # a short grace period, then concede (rather than idling to the game timer).
        if self.idx >= len(self.steps):
            if self._build_done_at is None:
                self._build_done_at = self.time
                print(f"[run] {self._clock():>4}  build complete", flush=True)
            elif not self._conceded and self.time - self._build_done_at >= CONCEDE_GRACE:
                self._conceded = True
                print(f"[run] {self._clock():>4}  conceding ({int(CONCEDE_GRACE)}s after build)", flush=True)
                await self.client.leave()

    # ============================================================ build steps
    async def run_steps(self):
        i = self.idx
        while i < len(self.steps):
            if self._done[i]:
                i += 1
                continue
            step = self.steps[i]
            if not self._started[i]:
                if not self.trigger_met(step.at):
                    break  # not due yet — this step (and everything after) waits
                self._started[i] = True  # trigger fired: commit; don't re-gate it
            if not await self.execute(step):
                break  # stall the line until this step can be done (strict order)
            self._done[i] = True
            note = f"  # {step.note}" if step.note else ""
            print(f"[step] {self._clock():>4}  sup{self.supply_used:<3} {self._describe(step)}{note}", flush=True)
            i += 1
        # advance the pointer past any leading completed steps
        while self.idx < len(self.steps) and self._done[self.idx]:
            self.idx += 1

    def _check_stall(self) -> None:
        """When the head step sits stalled for a long while, report WHY — so a
        build author can see what the queue is waiting on."""
        if self.idx >= len(self.steps):
            return
        if self.idx != self._head_idx:  # advanced to a new head — reset the timer
            self._head_idx = self.idx
            self._head_since = self.time
            self._stall_warned = -1e9
            return
        stalled = self.time - self._head_since
        if stalled < STALL_FIRST or self.time - self._stall_warned < STALL_REPEAT:
            return
        self._stall_warned = self.time
        step = self.steps[self.idx]
        if not self._started[self.idx]:
            reason = f"waiting on trigger {self._trigger_status(step.at)}"
        else:
            reason = self._stall_reason or "handler can't complete yet"
        print(f"[stall] {self._clock():>4}  step {self.idx} {self._describe(step)} "
              f"stalled {int(stalled)}s — {reason}", flush=True)

    def _trigger_status(self, at: Trigger) -> str:
        """A trigger described with its CURRENT value, so a stuck count/resource
        gate is obvious (e.g. `count HighTemplar=2 (have 0)`)."""
        if at.count is not None:
            name, n = next(iter(at.count.items()))
            return f"count {name}={n} (have {self.all_own_units(_unit(name)).ready.amount})"
        if at.supply is not None:
            return f"supply>={at.supply} (have {self.supply_used})"
        if at.minerals is not None:
            return f"minerals>={at.minerals} (have {self.minerals})"
        if at.vespene is not None:
            return f"vespene>={at.vespene} (have {self.vespene})"
        if at.time is not None:
            return f"time>={at.time:.0f} (now {self.time:.0f})"
        return "?"

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
        self._stall_reason = ""  # handlers set this when they return False (for [stall])
        handler = getattr(self, f"do_{step.do}")
        return await handler(step)

    # ----------------------------------------------------------- step handlers
    async def do_build(self, step: Step) -> bool:
        unit = _unit(step.what)

        # A build isn't "done" when the command is issued — the probe may walk a
        # long way, and a probe over the tile can block placement so the order is
        # dropped. Confirm the structure actually appears: the type's count grows
        # past the baseline captured when this step started.
        if self._building_step is not step:
            self._building_step = step
            self._build_baseline = self.structures(unit).amount
            self._build_builder_tag = None

        if self.structures(unit).amount > self._build_baseline:
            self._building_step = None
            self._build_builder_tag = None
            self._clear_reservation()
            return True

        if not self.can_afford(unit):
            self._stall_reason = f"can't afford {step.what}"
            return False  # save for it, stalling the line

        # Don't re-issue while the probe we already sent is still carrying out the
        # build (walking to the spot — possibly across the map — or placing it);
        # re-issue only once it's died or dropped the order without a structure
        # appearing. Re-issuing every frame during a long walk spawned duplicate
        # builds in the wrong place and spurious structures that falsely satisfied
        # the (type-wide) count confirm.
        builder = self._worker_by_tag(self._build_builder_tag)
        if builder is not None and not (builder.is_idle or builder.is_gathering):
            self._stall_reason = f"{step.what} under construction"
            return False

        # Builder: an explicitly sent probe (step.label), else the pre-walked
        # reservation, else auto-selected from the free pool — never a sent probe.
        label = getattr(step, "label", None)
        if label is not None:
            builder = self._named_worker(label)  # may be None if it died -> auto-select
            target = None
        else:
            reserved = self._reserved_step is self._active_step
            builder = self._reserved_worker() if reserved else None
            target = self._build_target if reserved else None
        if self.debug:
            pos = target.position if isinstance(target, Unit) else target
            d = f" builder dist={builder.distance_to(pos):.1f}" if (builder and pos is not None) else ""
            print(f"[step*] {self._clock():>4}  issuing {step.what}{d}", flush=True)

        if unit == U.ASSIMILATOR:
            chosen = await self._build_gas(builder, target if isinstance(target, Unit) else None)
        elif unit == U.NEXUS:
            loc = target if isinstance(target, Point2) else self._next_expansion()
            if loc is None:
                self._stall_reason = "no expansion location"
                return False
            chosen = builder or self._free_probe_near(loc)
            if chosen is not None:
                await self.build(U.NEXUS, near=loc, build_worker=chosen, placement_step=1)
        else:
            loc = target if isinstance(target, Point2) else await self._placement_for(step)
            if loc is None:
                self._stall_reason = f"no placement for {step.what}"
                return False
            chosen = builder or self._free_probe_near(loc)
            if chosen is not None:
                await self.build(unit, near=loc, build_worker=chosen)

        # Remember the committed probe so we wait for it instead of re-issuing.
        if chosen is not None:
            self._build_builder_tag = chosen.tag
        if self.debug:
            where = getattr(step, "where", None)
            print(f"[dispatch] {self._clock():>4}  {step.what}"
                  f"{'/' + where if where else ''} <- probe {chosen.tag if chosen else None}", flush=True)
        return False  # issued; stall the line until the structure appears (confirm)

    async def _build_gas(self, builder=None, geyser=None):
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
            self._stall_reason = f"can't afford {step.what}"
            return False  # save for it (costs money + supply), stalling the line
        producer = PRODUCER.get(unit)
        if producer == U.NEXUS or unit == U.PROBE:
            havers = self.townhalls.ready.idle
        else:
            havers = self.structures(producer).ready.idle
        if not havers:
            self._stall_reason = f"no idle {producer.name if producer else 'producer'}"
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
            self._stall_reason = f"can't afford {step.what}"
            return False  # save for it (costs money + supply), stalling the line
        ability = WARP_ABILITY[unit]
        warpgates = self.structures(U.WARPGATE).ready
        if not warpgates:
            self._stall_reason = "no ready Warpgate (research/morph pending)"
            return False  # no Warpgate yet (research/morph pending) — stall the line
        avail = await self.get_available_abilities(warpgates)
        ready = [wg for wg, abils in zip(warpgates, avail) if ability in abils]
        if not ready:
            self._stall_reason = "all Warpgates on cooldown"
            return False  # all on cooldown — stall until one is up
        pylon = self._warp_pylon(step.where)
        if pylon is None:
            self._stall_reason = f"no powered pylon at {step.where}"
            return False  # no powering pylon at that place yet
        pos = await self.find_placement(ability, near=pylon.position, placement_step=1)
        if pos is None:
            self._stall_reason = f"no free warp tile at {step.where}"
            return False  # no free powered tile by that pylon right now
        ready[0].warp_in(unit, pos)
        return False  # issued; stall until the unit appears (confirm above)

    def _warp_pylon(self, where: str):
        """The ready pylon nearest the requested place — so `where: proxy` warps
        at the proxy pylon out on the map, `main` at home, etc."""
        pylons = self.structures(U.PYLON).ready
        if not pylons:
            return None
        return pylons.closest_to(self._resolve_place(where))

    async def do_morph(self, step: Step) -> bool:
        spec = MORPH[step.to]
        if self._morph_step is not step:
            # start: how many `dest` to make — every ready source for a 1:1 convert
            # (gateway<->warpgate), or as many pairs as we have for a 2:1 combine
            # (2 HT/DT -> 1 Archon) — and the dest-count baseline to measure against.
            self._morph_step = step
            ready = self._morph_sources(spec).ready.amount
            self._morph_target = step.count if step.count is not None else ready // spec.consumes
            self._morph_baseline = self.all_own_units(spec.dest).amount

        remaining = self._morph_target - (self.all_own_units(spec.dest).amount - self._morph_baseline)
        if remaining <= 0:
            self._morph_step = None
            return True  # enough have morphed — done

        # issue to idle sources in groups of `consumes` (a converting/merging one
        # isn't idle, so it's never double-issued); stall until `remaining` appear as
        # `dest`. For the Archon combine, both templar of a pair get the ability and
        # merge into one Archon.
        idle = list(self._morph_sources(spec).idle)
        for i in range(min(remaining, len(idle) // spec.consumes)):
            for s in idle[i * spec.consumes:(i + 1) * spec.consumes]:
                s(spec.ability)
        self._stall_reason = f"morphing to {step.to} ({remaining} left)"
        return False

    def _morph_sources(self, spec):
        """Ready units/structures that can morph into `spec.dest` (both HT and DT
        for the Archon combine; the single source structure for a conversion)."""
        return self.all_own_units(set(spec.sources)).ready

    async def do_research(self, step: Step) -> bool:
        upgrade = RESEARCH[step.what]
        if self.already_pending_upgrade(upgrade) > 0 or upgrade in self.state.upgrades:
            return True  # already researching or done
        if not self.can_afford(upgrade):
            self._stall_reason = f"can't afford {step.what}"
            return False  # save for it, stalling the line
        self.research(upgrade)  # finds the structure + issues; confirm next frame
        return False

    async def do_hallucinate(self, step: Step) -> bool:
        casters = self.units(HALLUCINATION_CASTER).filter(lambda u: u.energy >= HALLUCINATION_ENERGY)
        if not casters:
            self._stall_reason = f"no Sentry with {HALLUCINATION_ENERGY} energy"
            return False  # no Sentry with enough energy yet — stall the line
        casters.first(HALLUCINATION_ABILITY)
        return True

    async def do_chrono(self, step: Step) -> bool:
        # Like every step, a chrono STALLS until it fires: if there's no
        # Nexus with enough energy, or nothing of the target type is producing yet,
        # it waits. Builds are precise — place a chrono where it will actually have
        # energy and something to boost, not as a best-effort sprinkle.
        target_type = _unit(step.target)
        nexuses = self.townhalls(CHRONO_CASTER).ready.filter(lambda n: n.energy >= CHRONO_ENERGY)
        if not nexuses:
            self._stall_reason = f"no Nexus with {CHRONO_ENERGY} energy"
            return False  # no Nexus with enough energy yet — wait
        if target_type == U.NEXUS:
            target = next((n for n in self.townhalls(U.NEXUS).ready if n.orders), None)
            target = target or self.townhalls(U.NEXUS).ready.first
        else:
            target = next((s for s in self.structures(target_type).ready if s.orders), None)
        if target is None:
            self._stall_reason = f"no {step.target} is producing/researching"
            return False  # nothing of that type is producing yet — wait
        if target.has_buff(CHRONO_BUFF):
            return True  # already boosted — count the action as done
        nexuses.first(CHRONO_ABILITY, target)
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
                self._stall_reason = "no free probe to send"
                return False  # no probe available yet — stall the line
            self.named_probes[step.label] = worker.tag
        self._named_binds.setdefault(step.label, set()).add(worker.tag)  # track probes bound per label
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

    async def do_rally(self, step: Step) -> bool:
        self.rally_target = self._resolve_place(step.where)
        return True

    async def do_rally_and_transfer(self, step: Step) -> bool:
        bases = self._ordered_bases()
        if step.base > len(bases):
            self._stall_reason = f"base {step.base} not up yet (have {len(bases)})"
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
            excess = th.assigned_harvesters - self.minerals_per_base
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

    # Our bases named by distance-rank from our start (0 = main, 1 = natural, ...).
    BASE_RANK = {"main": 0, "natural": 1, "third": 2, "fourth": 3, "fifth": 4, "sixth": 5}

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
        if self._builder_tag is not None:
            tags.add(self._builder_tag)
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
        if self._reserved_step is not None and self._reserved_step is not step:
            self._clear_reservation()
        if step is None or not self._prewalk_due(step):
            return
        # a labelled build brings its own (explicitly sent) probe — no prewalk
        if getattr(step, "label", None) is not None:
            return

        if self._reserved_step is step:
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
        self._reserved_step = step
        self._builder_tag = worker.tag
        self._build_target = target
        worker.move(pos)
        if self.debug:
            print(f"[prewalk] {self._clock():>4}  sup{self.supply_used} min={self.minerals} "
                  f"reserved probe {worker.tag} for {step.what} @ ({pos.x:.0f},{pos.y:.0f}) "
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
        """Workers available to the economy — excludes the prewalk builder and any
        probes sent out via send_probe, so they aren't yanked back to mining."""
        ex = self._excluded_tags()
        return self.workers.tags_not_in(ex) if ex else self.workers

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
                    fields = self._minerals_under_cap()
                    field = fields.closest_to(w) if fields else self._populating_field()
                    if field is not None:
                        w.gather(field)

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
                    fields = self._minerals_under_cap()
                    field = fields.closest_to(w) if (fields and w) else self._populating_field()
                    if not (w and field):
                        break
                    w.gather(field)
                    assigned_gas -= 1

        # never idle: park idle workers on minerals (under cap), else spare gas,
        # else the populating base (over-saturation beats standing still — but
        # always one of OUR bases, never the nearest field on the map)
        for w in workers.idle:
            mins = self._minerals_under_cap()
            if mins:
                w.gather(mins.closest_to(w))
                continue
            spare = gas_ready.filter(lambda g: g.assigned_harvesters < g.ideal_harvesters)
            if spare:
                w.gather(spare.closest_to(w))
                continue
            field = self._populating_field()
            if field is not None:
                w.gather(field)

    def _minerals_under_cap(self):
        """Mineral fields of the first (in base order: main, natural, third, ...)
        base below the per-base cap — so bases fill up in order."""
        for th in self._ordered_bases():
            if th.assigned_harvesters < self.minerals_per_base:
                fields = self.mineral_field.closer_than(10, th)
                if fields:
                    return fields
        return None

    def _ordered_bases(self):
        """Our ready bases in expansion order (nearest our start first = base 1)."""
        return self.townhalls.ready.sorted(key=lambda t: t.distance_to(self.start_location))

    def _populating_base(self):
        bases = self._ordered_bases()
        if not bases:
            return None
        return bases[min(self.populating_base_num, len(bases)) - 1]

    def _base_field(self, base):
        """A mineral field at `base` (closest patch to it), or None."""
        if base is None:
            return None
        fields = self.mineral_field.closer_than(10, base)
        return fields.closest_to(base) if fields else None

    def _populating_field(self):
        """Where homeless workers mine: the populating base's minerals, else any of
        our bases in order. Never the nearest field on the map (could be the enemy's)."""
        f = self._base_field(self._populating_base())
        if f is not None:
            return f
        for th in self._ordered_bases():
            f = self._base_field(th)
            if f is not None:
                return f
        return None

    def apply_rally(self):
        # Keep production exits clear: the instant a production building is up, rally
        # its units a few tiles toward open ground so they walk off the spawn tile
        # rather than piling on it and jamming the building (a finished unit can't
        # pop out through a blocked exit). Set once per building.
        for b in self.structures(PRODUCTION).ready:
            if b.tag not in self._rallied:
                self._rallied.add(b.tag)
                b(AbilityId.RALLY_BUILDING, b.position.towards(self.game_info.map_center, RALLY_OFFSET))
        # An explicit `rally` step overrides where gateway units gather.
        if self.rally_target is not None:
            for gw in self.structures(U.GATEWAY).ready:
                gw(AbilityId.RALLY_BUILDING, self.rally_target)
