#!/usr/bin/env python3
"""Measure mining rates in a live game, empirically, for reference/economic_data.md.

Mining rates are NOT in the game data tables (unlike costs/build times, see
gamedata_dump.py) -- they fall out of harvest durations, patch geometry and worker
pathing, so the only authoritative source is the running engine. This script drives
one game through a series of PHASES; each phase spawns an exact worker/patch
configuration with debug commands, lets it reach steady state, then measures for a
fixed window:

    setup  (debug_kill all workers, debug_create N fresh ones, assign them)
    settle (SETTLE_S -- walk out, get into the mining cycle)
    measure(MEASURE_S -- record resource + patch-content deltas)

Everything is measured two independent ways that must agree: resources DELIVERED
(self.minerals / self.vespene delta) and resources EXTRACTED (sum of mineral_contents
/ vespene_contents deltas over the patches). Both are quantized to whole trips, so
they differ by whatever is in transit at the window edges.

The engine calls an idle game a Tie at ~1320 game-seconds (~16 phases), so the program
is run in chunks with --slice; each chunk also starts on a fresh map, i.e. undepleted
patches. Usage (headless Docker image; drop --target for the retail patch under Wine):

    for s in 0:10 10:18 18:24 24:31 31:36; do
      docker run --rm -v "$PWD":/app build-order-executor-headless \
        python economy_measure.py --target linux --slice $s 2>&1 | grep -E '^\\[econ\\]'
    done

Output: one tab-separated `[econ]` row per phase, plus a final `[econ-json]` blob.
Results live in reference/economic_data.md.

Two things the harness has to defend against, both of which silently corrupt rates:
  * a probe harvesting vespene is INSIDE the Assimilator and absent from `self.workers`,
    so worker counts come from `supply_workers` (see _worker_count);
  * debug-created buildings do not snap onto their spot -- an Assimilator lands a couple
    of tiles off its geyser, and a Nexus conjured where one already stands becomes a
    SECOND one that shortens return trips. Hence _cover_geysers and _prepare_base.
Every phase asserts it ran with exactly n workers, and restarts itself if that drifts.
"""

from __future__ import annotations

import argparse
import json
import sys

# run.py resolves the launch target and sets SC2PF/SC2PATH/WINE(PREFIX) *before* it
# imports sc2 (sc2.paths reads them at import time) -- so importing it first is what
# configures this script's launcher too. It reads --target off our argv.
import run  # noqa: F401  (import for its side effects; must precede any sc2 import)

from sc2.bot_ai import BotAI
from sc2.data import Race, Result
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.main import run_game
from sc2.player import Bot

from catalog import CHRONO_ABILITY, CHRONO_BUFF

FRAMES_PER_SEC = 22.4
SETTLE_S = 20.0   # walk out + get into the harvest cycle before the clock starts
MEASURE_S = 60.0  # long enough that whole-trip quantization is < 2% on a saturated base
TRAIN_MEASURE_S = 120.0  # a probe is ~12s, so this is ~10 arrivals to average over
TRIP_MEASURE_S = 120.0   # a mining cycle is ~5s, so this is ~24 timed round trips
PATCHES_PER_BASE = 8     # the standard layout; bases with anything else are skipped
MIN_PATCH_RESERVE = 300  # move to a fresh base once the thinnest patch drops below this
MAX_NEXUS_OFFSET = 1.5   # a debug-placed Nexus further off the base centre than this is unusable
GEYSER_MATCH = 1.0       # a properly built Assimilator snaps onto its geyser; anything
                         # further off than this is a stray and gets cleaned up
DRAIN_S = 4.0            # hold the base empty this long before spawning a phase's probes,
                         # so an in-flight debug_create can't land mid-measurement


def phases() -> list[dict]:
    """The measurement program, in order.

    `assign`:
      rr        -- worker i -> patch[i % 8], patches sorted near->far (even spread)
      closest   -- every worker ordered onto the single nearest patch, which is what
                   economy.py actually does; measures whether the engine redistributes
      near      -- fill the NEAREST patches 2 deep before using a farther one
      patch:<i> -- every worker onto one specific patch (i indexes the near->far sort)
    """
    p: list[dict] = []
    # Mineral saturation curve: the marginal value of the Nth worker on one base.
    for n in [1, 2, 3, 4, 6, 8, 10, 12, 14, 16, 17, 18, 20, 22, 24]:
        p.append({"kind": "min", "n": n, "assign": "rr"})
    # Does the engine spread workers dumped on one patch? (economy.py assumes it does.)
    for n in [8, 16, 24]:
        p.append({"kind": "min", "n": n, "assign": "closest"})
    # Per-patch throughput: 1 worker (pure round trip) vs 3 (patch continuously busy,
    # so the rate is 5 minerals / harvest-duration and gives the harvest constant).
    for i in (0, 7):
        for n in (1, 2, 3):
            p.append({"kind": "min", "n": n, "assign": f"patch:{i}"})
    # Does patch DISTANCE change the mining rate? Timed per-trip (see _tick_trip) on
    # every patch of the base, nearest to farthest, because a 60s income window can't
    # resolve an effect this size. Two passes, to separate a real distance effect from
    # per-run noise.
    for _ in range(2):
        for i in range(PATCHES_PER_BASE):
            p.append({"kind": "trip", "n": 1, "assign": f"patch:{i}"})
    # Given that near patches ARE faster, does filling them first beat spreading out?
    # `rr` puts one worker on each of the 8 patches; `near` stacks 2 deep on the 4
    # nearest and leaves the far ones idle. Same worker count, different geometry.
    for assign in ("rr", "near"):
        for n in (4, 8, 12):
            p.append({"kind": "min", "n": n, "assign": assign})
    # Gas.
    for n in [1, 2, 3, 4]:
        p.append({"kind": "gas", "n": n, "geysers": 1})
    p.append({"kind": "gas", "n": 6, "geysers": 2})
    # Probe production: back-to-back training with and without a permanent Chrono
    # Boost, so the chrono multiplier is measured rather than assumed.
    p.append({"kind": "train", "n": 0, "chrono": False})
    p.append({"kind": "train", "n": 0, "chrono": True})
    # Probe travel speed, in the same game-seconds as everything else -- the game-data
    # `movement_speed` field is in different units, and travel time is what the
    # builder-walk opportunity cost is made of.
    p.append({"kind": "walk", "n": 1})
    # Repeats on OTHER bases: mining rate is a function of patch/geyser geometry, so
    # this is the base-to-base spread around the headline numbers above.
    for _ in range(2):
        p.append({"kind": "min", "n": 16, "assign": "rr", "fresh_base": True})
        p.append({"kind": "gas", "n": 3, "geysers": 1})
    return p


class EconomyMeasureBot(BotAI):
    def __init__(self) -> None:
        super().__init__()
        self.phases = phases()
        self.idx = -1
        self.stage = "init"
        self.stage_t = 0.0
        self.results: list[dict] = []
        self.static: dict = {}
        self.t0: dict = {}
        self._bases: list = []
        self._base_geysers: dict = {}
        self._base_i = 0
        self._prep_sent = -99.0
        self._prep_tries = 0
        self._last_kill = -99.0
        self._probes_requested = False
        self._requested = 0  # probes asked for this phase (see _setup)
        self._restarts = 0
        self._zero_since = None  # when the base last became worker-free
        self._probe_seen: dict[int, float] = {}  # tag -> time it finished (train phases)
        self._probe_baseline: set[int] = set()
        self._last_spawn = -99.0
        self._gas_builder = None
        self._walk_samples: list = []
        self._walk_ends: list = []
        self._trip_events: list = []   # (time, is_carrying) at each transition
        self._trip_targets: dict = {}  # order_target tag -> frames seen
        self._was_carrying = False

    # ------------------------------------------------------------------ helpers
    @property
    def base(self):
        """Where we're mining right now. Phases move to a fresh base as patches run
        low, so a measurement is never taken against a half-dead mineral line."""
        return self._bases[self._base_i]

    @property
    def nexus(self):
        return self.townhalls.closest_to(self.base)

    def _patches(self):
        """The current base's mineral patches, sorted near->far from the Nexus.

        By position rather than by the tags in expansion_locations_dict: those come from
        the first observation and do NOT survive a base coming out of fog. A patch that
        has been mined out simply stops being a unit, which is how _base_exhausted
        notices.
        """
        fields = self.mineral_field.closer_than(12, self.nexus)
        return sorted(fields, key=lambda m: (round(m.distance_to(self.nexus), 3), m.tag))

    def _worker_count(self) -> int:
        """How many probes we own -- INCLUDING the ones currently inside an Assimilator.

        A probe harvesting vespene physically enters the building and vanishes from the
        observation, so `self.workers` undercounts during any gas phase (a lone gas
        probe reads as 0 for a good part of every trip). Supply is tracked whether or
        not the unit is visible, so this is the count that is actually stable.
        """
        return int(self.supply_workers)

    def _geysers(self):
        gas = self.gas_buildings.ready.closer_than(12, self.base)
        return sorted(gas, key=lambda g: (round(g.distance_to(self.nexus), 3), g.tag))

    def _base_exhausted(self) -> bool:
        """True once this base can no longer support a full phase: a phase takes up to
        ~150 minerals out of a single patch, so anything under the reserve risks a patch
        dying mid-window and silently deflating the rate."""
        patches = self._patches()
        return (len(patches) < PATCHES_PER_BASE
                or min(m.mineral_contents for m in patches) < MIN_PATCH_RESERVE)

    def _snapshot(self) -> dict:
        return {
            "t": self.time,
            "minerals": self.minerals,
            "vespene": self.vespene,
            "patch": {m.tag: m.mineral_contents for m in self._patches()},
            "gas": {g.tag: g.vespene_contents for g in self._geysers()},
            # Resources already picked up but not yet dropped off. Counting them makes
            # the delivered figure independent of where in their trip the workers happen
            # to be at each window edge (a trip is all-or-nothing, 5 min / 4 gas).
            "carried_min": 5 * sum(1 for w in self.workers if w.is_carrying_minerals),
            "carried_gas": 4 * sum(1 for w in self.workers if w.is_carrying_vespene),
        }

    # ------------------------------------------------------------------ lifecycle
    async def on_start(self) -> None:
        self.client.game_step = 4
        await self.client.debug_all_resources()  # so nothing is ever resource-blocked
        await self.client.debug_food()  # ...nor supply-blocked, in the train phases
        # Reveal the map: `mineral_field` only holds patches we can SEE, so without this
        # every base but our own looks like it has zero patches (and _base_exhausted
        # would send us skipping through the whole map looking for a good one).
        await self.client.debug_show_map()
        # Static facts worth pinning down while we have a live client.
        probe = self.game_data.units[U.PROBE.value]
        self.static["probe"] = {
            "minerals": probe.cost.minerals,
            "build_frames": probe.cost.time,
            "build_s": round(probe.cost.time / FRAMES_PER_SEC, 3),
            "movement_speed": self.workers.first.movement_speed,
        }
        self.static["start_workers"] = int(self.supply_workers)
        self.static["game_step"] = self.client.game_step
        self._pick_bases()

    async def on_step(self, iteration: int) -> None:
        if not self.townhalls:
            return
        if self.stage == "init":
            self._watchdog()
            await self._init_base()
            return
        if self.stage == "setup":
            self._watchdog()
            await self._setup()
            return
        kind = self.phase["kind"]
        # A phase is only valid if it runs with exactly n workers for the whole window.
        # Probes can still turn up late (a debug_create landing seconds after it was
        # asked for) or be left over from setting the base up, so rather than trust that
        # setup got it right, check every frame and start the phase over if it drifts.
        # (Train phases are exempt -- producing probes is the point.)
        if kind != "train" and self._worker_count() != self.phase["n"]:
            print(f"[phase] restarting phase {self.idx + 1}: {self._worker_count()} "
                  f"workers, expected {self.phase['n']}", flush=True)
            self._restarts += 1
            assert self._restarts < 40, f"phase {self.idx + 1} will not hold a stable roster"
            # Start the phase's setup over from scratch. That is only safe because
            # setup drains (DRAIN_S of an empty base) before it spawns -- otherwise
            # re-requesting while the last batch is still in flight never converges.
            self._probes_requested = False
            self._requested = 0
            self._zero_since = None
            self._enter("setup")
            return
        await {"train": self._tick_train, "walk": self._tick_walk,
               "trip": self._tick_trip}.get(kind, self._tick_harvest)()
        window = {"train": TRAIN_MEASURE_S, "trip": TRIP_MEASURE_S}.get(kind, MEASURE_S)
        if self.stage == "settle" and self.time - self.stage_t >= SETTLE_S:
            self.t0 = self._snapshot()
            # Probes that already exist are mid-cycle leftovers from the settle stage;
            # only arrivals AFTER this point time a full production cycle.
            self._probe_baseline = set(self._probe_seen)
            self._walk_samples = []
            self._trip_events, self._trip_targets = [], {}
            self._enter("measure")
        elif self.stage == "measure" and self.time - self.stage_t >= window:
            self._record()
            await self._next_phase()

    async def _tick_harvest(self) -> None:
        self._keep_mining()

    async def _tick_trip(self) -> None:
        """Time one probe's mining cycle, frame by frame.

        Counting minerals over a fixed window can't resolve this: a 60s window holds ~12
        whole trips, so its quantum is 5 minerals (~8%) -- the same size as the close-vs-far
        effect we're trying to see. Timing the cycle instead is exact to the frame, and
        splits it into legs, so travel and harvest can be told apart.

        `is_carrying_minerals` flips true the instant the harvest completes and false on
        delivery, so the two transitions bracket each leg.
        """
        self._keep_mining()
        if not self.workers:
            return
        probe = self.workers.first
        carrying = probe.is_carrying_minerals
        if carrying != self._was_carrying:
            self._trip_events.append((self.time, carrying))
            self._was_carrying = carrying
        # Which patch it is REALLY working, every frame -- so the row can't claim a patch
        # the probe had wandered off. order_target is a tag while gathering, the Nexus
        # tag while hauling back.
        if probe.order_target is not None:
            self._trip_targets[probe.order_target] = \
                self._trip_targets.get(probe.order_target, 0) + 1

    async def _tick_walk(self) -> None:
        """March one probe back and forth across the map, sampling its position, so the
        measure stage sees a long stretch of steady-state travel."""
        probe = self.workers.first
        if not self._walk_ends:
            # Two base locations, as far apart as this map allows: both ends are
            # guaranteed pathable and the legs are long enough that the acceleration
            # ramps are a small part of each one.
            far = max(self._bases, key=lambda b: b.distance_to(self.base))
            self._walk_ends = [self.base, far]
        if probe.is_idle:
            self._walk_leg = 1 - getattr(self, "_walk_leg", 0)
            probe.move(self._walk_ends[self._walk_leg])
        self._walk_samples.append((self.time, probe.position))

    def _spawn_point(self):
        """Where debug-created probes appear. Offset off the Nexus itself so a big batch
        has somewhere to land."""
        return self.nexus.position.towards(self._patches()[0].position, 4)

    async def _tick_train(self) -> None:
        """Keep the Nexus producing probes back-to-back (and, for the chrono variant,
        permanently boosted) so the phase measures the inter-arrival time of probes."""
        nex = self.nexus
        if self.phase["chrono"]:
            # Chrono lasts ~20s and costs 50 energy; regen can't sustain it, so top the
            # Nexus back up rather than let the boost lapse mid-window.
            await self.client.debug_set_unit_value([nex.tag], 1, 200)
            if not nex.has_buff(CHRONO_BUFF):
                nex(CHRONO_ABILITY, nex)
        if len(nex.orders) < 2:
            nex.train(U.PROBE)
        for w in self.workers:
            self._probe_seen.setdefault(w.tag, self.time)

    def _pick_bases(self) -> None:
        """The bases we'll cycle through: our own start location first, then every
        STANDARD base (8 plain patches + 2 geysers) by distance from it.

        Gold/rich bases and anything with a non-standard patch count are skipped so
        every phase is measured against comparable geometry.
        """
        self._bases = []
        self._base_geysers = {}
        for pos, resources in sorted(self.expansion_locations_dict.items(),
                                     key=lambda kv: kv[0].distance_to(self.start_location)):
            patches = [r for r in resources if r.is_mineral_field]
            geysers = [r for r in resources if r.is_vespene_geyser]
            if len(patches) != PATCHES_PER_BASE or len(geysers) != 2:
                continue
            if any("Rich" in p.name for p in patches):
                continue
            self._bases.append(pos)
            self._base_geysers[pos] = [g.position for g in geysers]
        self.static["bases"] = [{"pos": [round(p.x, 1), round(p.y, 1)]} for p in self._bases]

    async def _init_base(self) -> None:
        """Cover the current base's geysers, record its geometry, then start phase 1."""
        if not await self._prepare_base():
            return
        nex = self.nexus
        self.static["patches"] = [
            {"tag": m.tag, "dist": round(m.distance_to(nex), 3), "contents": m.mineral_contents}
            for m in self._patches()
        ]
        self.static["geysers"] = [
            {"tag": g.tag, "dist": round(g.distance_to(nex), 3), "contents": g.vespene_contents}
            for g in self._geysers()
        ]
        self.static["nexus_radius"] = nex.radius
        self.static["probe_radius"] = self.workers.first.radius
        print(f"[phase] static {json.dumps(self.static)}", flush=True)
        print("[econ]\t" + "\t".join(self.COLUMNS), flush=True)
        await self._next_phase()

    async def _next_phase(self) -> None:
        self.idx += 1
        if self.idx >= len(self.phases):
            print(f"[econ-json] {json.dumps({'static': self.static, 'phases': self.results})}",
                  flush=True)
            await self.client.leave()
            return
        self._probes_requested = False
        self._requested = 0
        self._restarts = 0
        self._zero_since = None
        self._enter("setup")

    def _enter(self, stage: str) -> None:
        self.stage = stage
        self.stage_t = self.time

    # ------------------------------------------------------------------ phase run
    @property
    def phase(self) -> dict:
        return self.phases[self.idx]

    def _targets(self) -> list:
        """The harvest target for each of the phase's workers, in worker order."""
        ph, n = self.phase, self.phase["n"]
        if ph["kind"] in ("train", "walk"):
            return []  # these phases drive their probes themselves
        if ph["kind"] == "gas":
            gas = self._geysers()[: ph["geysers"]]
            return [gas[i % len(gas)] for i in range(n)]
        patches = self._patches()
        a = ph["assign"]
        if a == "rr":
            return [patches[i % len(patches)] for i in range(n)]
        if a == "near":
            # Fill the nearest patches two deep before using any farther one.
            return [patches[i // 2] for i in range(n)]
        if a == "closest":
            return [patches[0]] * n
        return [patches[int(a.split(":")[1])]] * n

    def _watchdog(self) -> None:
        """Setup is a handful of debug requests and should take a second. If it doesn't,
        say what we're still waiting for -- a stuck setup otherwise looks exactly like a
        slow one until the game times out with no rows printed."""
        stuck = self.time - self.stage_t
        if stuck < 15 or int(stuck) % 10:
            return
        print(f"[phase] STUCK {stuck:.0f}s in {self.stage} of phase {self.idx + 1} "
              f"base={self._base_i}@{self.base} prep_tries={self._prep_tries} "
              f"probes_requested={self._probes_requested} workers={self.workers.amount} "
              f"townhalls={[(t.name, round(t.build_progress, 2), t.position) for t in self.townhalls]} "
              f"gas={[(g.position, round(g.build_progress, 2)) for g in self.gas_buildings]} "
              f"want_geysers={self._base_geysers[self.base]} "
              f"ready_geysers={len(self._geysers())}",
              flush=True)

    async def _prepare_base(self) -> bool:
        """Make `self.base` the one and only base we own, fully covered in Assimilators.
        Returns True once it is ready; call it every frame until then.

        Debug create/kill only land on a later observation, so rather than latch each
        request this re-derives what's still missing every time and just rate-limits
        itself -- that way a request the engine silently drops is retried instead of
        deadlocking the run.
        """
        if self.time - self._prep_sent < 2.0:
            return False  # let the previous debug request land before re-reading the world
        # Kill anything of ours that isn't this base's own Nexus or Assimilators. A
        # leftover Nexus elsewhere would collect deliveries; a SECOND one at this base
        # (which is what you get if you debug-create one where a Nexus already stands --
        # the engine shunts it a few tiles off) would shorten some return trips and
        # quietly inflate every rate measured here.
        geysers = self._base_geysers[self.base]
        create = []
        if not self.townhalls.closer_than(MAX_NEXUS_OFFSET, self.base).exists:
            create.append([U.NEXUS, 1, self.base, self.player_id])
        if self.townhalls.closer_than(12, self.base).exists and not \
                self.townhalls.closer_than(MAX_NEXUS_OFFSET, self.base).exists:
            # The engine shunts a debug-created building to wherever it fits. A Nexus
            # sitting off the base centre means longer trips than a real base has, so
            # the geometry isn't comparable -- skip the base rather than measure it.
            off = self.townhalls.closest_to(self.base).distance_to(self.base)
            print(f"[phase] base {self._base_i} rejected: Nexus landed {off:.1f} tiles "
                  f"off centre", flush=True)
            self._move_to_fresh_base(f"Nexus landed {off:.1f} tiles off centre")
            return False
        if create:
            assert self._prep_tries < 20, f"cannot set up base {self._base_i}: {create}"
            self._prep_tries += 1
            self._prep_sent = self.time
            await self.client.debug_create_unit(create)
            return False
        if not self.townhalls.ready.closer_than(MAX_NEXUS_OFFSET, self.base).exists:
            return False
        # Only NOW tear down the last base. Doing it before the new Nexus exists can
        # leave us owning nothing at all for a frame, which the game scores as a defeat.
        doomed = [t.tag for t in self.townhalls if t.distance_to(self.base) > MAX_NEXUS_OFFSET]
        doomed += [g.tag for g in self.gas_buildings
                   if min(g.distance_to(p) for p in geysers) > GEYSER_MATCH]
        if doomed:
            self._prep_sent = self.time
            await self.client.debug_kill_unit(doomed)
            return False
        return await self._cover_geysers()

    async def _cover_geysers(self) -> bool:
        """Put a real Assimilator on each of this base's geysers.

        A debug-CREATED Assimilator does not snap onto its geyser -- the engine drops it
        at a free spot a couple of tiles off, which still yields gas but over a round
        trip that no real game ever has. So this builds them the ordinary way, with a
        probe, and just waits out the build time. (Not with `debug_fast_build`: that
        cheat also makes UNITS train instantly, which would silently wreck the train
        phases.)
        """
        uncovered = [p for p in self._base_geysers[self.base]
                     if not self.gas_buildings.closer_than(GEYSER_MATCH, p).exists]
        if not uncovered:
            return len(self._geysers()) == 2
        if not self._worker_count():
            self._prep_sent = self.time
            await self.client.debug_create_unit(
                [[U.PROBE, 1, self.nexus.position, self.player_id]])
            return False
        if not self.workers:
            return False  # we own one, but it is inside a geyser -- wait for it to come out
        geyser = self.vespene_geyser.closest_to(uncovered[0])
        probe = self.workers.closest_to(geyser)
        # Order it once and let it walk (it is mining, so waiting for `is_idle` would
        # wait forever); re-order only if we switch probes or it stops.
        if probe.tag != self._gas_builder or probe.is_idle:
            self._gas_builder = probe.tag
            self._prep_sent = self.time
            probe.build(U.ASSIMILATOR, geyser)
        return False

    def _move_to_fresh_base(self, why: str = "") -> None:
        patches = self._patches()
        print(f"[phase] base {self._base_i} -> {self._base_i + 1} ({why}); it had "
              f"{len(patches)} visible patches, min contents "
              f"{min((m.mineral_contents for m in patches), default=0)}", flush=True)
        self._base_i += 1
        assert self._base_i < len(self._bases), "ran out of un-mined bases on this map"
        self._prep_tries = 0

    async def _setup(self) -> None:
        """Relocate if this base is running low, then kill the last phase's workers and
        spawn exactly n fresh ones on their targets."""
        if not await self._prepare_base():
            return
        if not self._probes_requested:
            # Clear out everyone who isn't ours: the last phase's probes, and the one
            # _cover_geysers spawns to build Assimilators -- which appears AFTER a
            # one-shot kill would have fired, so this re-checks every time instead.
            if self._worker_count():
                self._zero_since = None
                # Only the visible ones can be killed; any probe currently inside an
                # Assimilator has to come out first, so this just keeps firing until
                # the count reaches zero.
                if self.workers and self.time - self._last_kill > 1.0:
                    self._last_kill = self.time
                    await self.client.debug_kill_unit(self.workers)
                return  # wait for them to actually go
            # Empty -- but a debug_create asked for earlier can still be in flight and
            # land seconds from now. Wait for the board to stay clear before ordering
            # this phase's probes, otherwise a straggler joins mid-window and the roster
            # never settles.
            if self._zero_since is None:
                self._zero_since = self.time
            if self.time - self._zero_since < DRAIN_S:
                return
            if self._base_exhausted() or self.phase.get("fresh_base"):
                # Only once the old workers are gone, so nothing keeps mining a base
                # we've stopped measuring.
                why = "phase asks for a fresh base" if self.phase.get("fresh_base") else "exhausted"
                self._move_to_fresh_base(why)
                self.phase.pop("fresh_base", None)  # one move, not one per frame
                return
            self._probes_requested = True
        n = self.phase["n"]
        missing = n - self._worker_count()
        if missing > 0:
            # Count what we have ASKED for, not just what has turned up: a debug-created
            # probe can take a couple of seconds to appear, and a plain "retry while
            # short" timer ends up ordering the same probe twice -- which is how a
            # 3-worker gas row got measured with 4 workers on it.
            if self._requested < n:
                ask = n - self._requested
                self._requested += ask
                self._last_spawn = self.time
                await self.client.debug_create_unit(
                    [[U.PROBE, ask, self._spawn_point(), self.player_id]])
            elif self.time - self._last_spawn > 10.0:
                self._requested = self._worker_count()  # request evidently lost; re-arm
            return
        if missing < 0:
            # Too many: a stray probe (e.g. the one _cover_geysers built Assimilators
            # with -- the game puts it straight onto gas afterwards) would be measured
            # as if it were one of ours. Cull the newest and re-check.
            if self.time - self._last_kill > 1.0:
                self._last_kill = self.time
                visible = sorted(self.workers, key=lambda w: w.tag)
                extra = visible[len(visible) + missing:]  # missing < 0, so drop that many
                if extra:
                    await self.client.debug_kill_unit([w.tag for w in extra])
            return
        if self._worker_count() == self.phase["n"]:
            self._keep_mining(force=True)
            print(f"[phase] {self.idx + 1}/{len(self.phases)} {self.phase} "
                  f"t={self.time:.1f}", flush=True)
            self._enter("settle")

    def _keep_mining(self, force: bool = False) -> None:
        """Order each worker onto its assigned target. Only re-issued to workers that
        have gone idle -- re-issuing a gather order every frame restarts the harvest."""
        targets = self._targets()
        for w, target in zip(sorted(self.workers, key=lambda u: u.tag), targets):
            if force or w.is_idle:
                w.gather(target)

    COLUMNS = ["kind", "n", "assign", "secs", "collected", "extracted", "rate_per_s",
               "extr_per_s", "per_worker_per_s", "per_worker_per_min", "detail"]

    def _record(self) -> None:
        t1 = self._snapshot()
        ph, n = self.phase, self.phase["n"]
        # The row is only meaningful if the window really did run with n workers. This
        # has been wrong before (a stray probe drifting onto gas), and a rate that is
        # quietly 4/3 too high is indistinguishable from a real result. Train phases are
        # exempt -- making probes is what they measure.
        assert ph["kind"] == "train" or self._worker_count() == n, (
            f"phase {self.idx + 1} ({ph}) ran with {self._worker_count()} workers, not {n}")
        row = {"train": self._record_train, "walk": self._record_walk,
               "trip": self._record_trip}.get(ph["kind"], self._record_harvest)(t1)
        row.update(kind=ph["kind"], n=n, secs=round(t1["t"] - self.t0["t"], 2))
        self.results.append(row)
        print("[econ]\t" + "\t".join(str(row.get(c, "")) for c in self.COLUMNS), flush=True)

    def _record_harvest(self, t1: dict) -> dict:
        ph, n = self.phase, self.phase["n"]
        gas = ph["kind"] == "gas"
        secs = t1["t"] - self.t0["t"]
        key, pool, carried = (("vespene", "gas", "carried_gas") if gas
                              else ("minerals", "patch", "carried_min"))
        collected = (t1[key] + t1[carried]) - (self.t0[key] + self.t0[carried])
        extracted = sum(self.t0[pool][k] - t1[pool].get(k, 0) for k in self.t0[pool])
        # A base's own harvester bookkeeping, to cross-check that every worker we spawned
        # is actually counted as mining (and how the engine spread them).
        base = self.nexus
        detail = (f"base={self._base_i} " +
                  (f"assim={[g.assigned_harvesters for g in self._geysers()]} "
                   f"per_geyser={[self.t0[pool][k] - t1[pool].get(k, 0) for k in self.t0[pool]]} "
                   f"dists={[round(float(g.distance_to(self.nexus)), 2) for g in self._geysers()]}"
                   if gas else
                   f"nexus={base.assigned_harvesters}/{base.ideal_harvesters} "
                   f"per_patch={[self.t0[pool][k] - t1[pool].get(k, 0) for k in self.t0[pool]]}"))
        return {
            "base": self._base_i,
            "patch_dists": [round(m.distance_to(base), 2) for m in self._patches()],
            "assign": ph.get("assign", f"{ph.get('geysers')}geyser"),
            "collected": collected, "extracted": extracted,
            "rate_per_s": round(collected / secs, 4),
            "extr_per_s": round(extracted / secs, 4),
            "per_worker_per_s": round(collected / secs / n, 4),
            "per_worker_per_min": round(collected / secs / n * 60, 2),
            "detail": detail,
            "per_patch_extracted": [self.t0[pool][k] - t1[pool].get(k, 0) for k in self.t0[pool]],
        }

    def _record_walk(self, t1: dict) -> dict:
        """Steady-state travel speed, in tiles per game-second.

        Speed is taken as the MEDIAN of per-frame displacement rather than
        distance/time: the median ignores the acceleration ramp at each end of a leg and
        the moments the probe is turning, which is what a straight distance/time would
        quietly average in.
        """
        steps = [(b[1].distance_to(a[1]) / (b[0] - a[0]))
                 for a, b in zip(self._walk_samples, self._walk_samples[1:])
                 if b[0] > a[0]]
        steps.sort()
        median = steps[len(steps) // 2]
        moving = [s for s in steps if s > 0.1]
        return {
            "assign": "probe",
            "collected": len(steps),
            "rate_per_s": round(median, 4),
            "detail": (f"median={median:.4f} tiles/game-s  mean_while_moving="
                       f"{sum(moving) / len(moving):.4f}  max={steps[-1]:.4f}  "
                       f"game_data_movement_speed={self.static['probe']['movement_speed']}"),
            "tiles_per_s": round(median, 4),
        }

    def _record_trip(self, t1: dict) -> dict:
        """Round-trip timing for one probe on one patch, split into legs.

        Events alternate pickup (harvest done, starts carrying) / delivery (drops off).
        So:
            cycle       = pickup -> next pickup   (the whole thing; 5 minerals per cycle)
            haul leg    = pickup -> delivery      (loaded, patch to Nexus)
            out+harvest = delivery -> next pickup (empty travel back, then the harvest)
        """
        ev = self._trip_events
        pickups = [t for t, carrying in ev if carrying]
        cycles = [b - a for a, b in zip(pickups, pickups[1:])]
        # Pair each pickup with the NEXT drop after it -- zipping the two lists directly
        # misaligns them whenever the window happens to open mid-haul.
        hauls = []
        for p in pickups:
            nxt = [t for t, carrying in ev if not carrying and t > p]
            if nxt:
                hauls.append(nxt[0] - p)
        # Which patch it actually worked. `on_target` is the share of frames its order
        # pointed at the intended patch (the rest is the haul leg, aimed at the Nexus);
        # `off_patch` is minerals that came out of any OTHER patch, which must be 0 or the
        # probe wandered and the row is meaningless.
        idx = int(self.phase["assign"].split(":")[1])
        patch = self._patches()[idx]
        frames_on_patch = self._trip_targets.get(patch.tag, 0)
        total_frames = sum(self._trip_targets.values()) or 1
        extracted = {k: self.t0["patch"][k] - t1["patch"].get(k, 0) for k in self.t0["patch"]}
        off_patch = sum(v for k, v in extracted.items() if k != patch.tag)
        mean = sum(cycles) / len(cycles) if cycles else float("nan")
        haul = sum(hauls) / len(hauls) if hauls else float("nan")
        return {
            "assign": self.phase["assign"],
            "collected": len(cycles),
            "rate_per_s": round(5 / mean, 4) if cycles else 0.0,   # minerals/s from timing
            "per_worker_per_min": round(300 / mean, 2) if cycles else 0.0,
            "detail": (f"dist={patch.distance_to(self.nexus):.2f} "
                       f"cycle={mean:.3f}s (min {min(cycles):.3f} max {max(cycles):.3f}) "
                       f"haul_leg={haul:.3f}s trips={len(cycles)} "
                       f"on_target={frames_on_patch / total_frames:.0%} "
                       f"mined_here={extracted.get(patch.tag, 0)} off_patch={off_patch}"),
            "patch_dist": round(float(patch.distance_to(self.nexus)), 3),
            "off_patch": off_patch,
            "cycle_s": round(mean, 4),
            "haul_leg_s": round(haul, 4),
            "cycles": [round(c, 4) for c in cycles],
        }

    def _record_train(self, t1: dict) -> dict:
        """Probes finished during the window -> the effective per-probe train time.

        Uses the interval between the first and last probe of the window divided by the
        gaps between them, so the partial cycles at either edge don't count.
        """
        times = sorted(t for tag, t in self._probe_seen.items()
                       if tag not in self._probe_baseline)
        gaps = len(times) - 1
        per_probe = (times[-1] - times[0]) / gaps if gaps > 0 else float("nan")
        return {
            "assign": "chrono" if self.phase["chrono"] else "no-chrono",
            "collected": len(times),
            "rate_per_s": round(per_probe, 3),
            "detail": (f"per_probe_s={per_probe:.3f} frames={per_probe * FRAMES_PER_SEC:.1f} "
                       f"probes={len(times)} arrivals={[round(x - times[0], 2) for x in times]}"),
            "per_probe_s": round(per_probe, 4),
        }

    async def on_end(self, result: Result) -> None:
        print(f"[phase] done t={self.time:.1f} result={result}", flush=True)


def main() -> None:
    global SETTLE_S, MEASURE_S, TRAIN_MEASURE_S, phases
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None, help="see run.py (wine | linux)")
    ap.add_argument("--map", default=None)
    ap.add_argument("--fullscreen", action="store_true")
    ap.add_argument("--smoke", action="store_true",
                    help="3 short phases, to check the harness itself")
    ap.add_argument("--slice", default=None, metavar="A:B",
                    help="run only phases [A:B) of the program. The engine calls the "
                         "game a Tie at ~1320 game-seconds (~16 phases), so the full "
                         "program has to be run in chunks; each chunk also gets a fresh "
                         "map, i.e. undepleted patches.")
    args = ap.parse_args()

    if args.slice:
        a, b = (int(x) if x else None for x in args.slice.split(":"))
        _full = phases
        phases = lambda: _full()[a:b]  # noqa: E731

    if args.smoke:
        SETTLE_S, MEASURE_S, TRAIN_MEASURE_S = 10.0, 20.0, 40.0
        phases = lambda: [{"kind": "min", "n": 4, "assign": "rr"},  # noqa: E731
                          {"kind": "min", "n": 16, "assign": "closest"},
                          {"kind": "gas", "n": 3, "geysers": 1},
                          {"kind": "train", "n": 0, "chrono": False},
                          {"kind": "train", "n": 0, "chrono": True}]

    from loguru import logger as _loguru
    _loguru.remove()
    _loguru.add(sys.stderr, level="WARNING")

    map_name = args.map or ("CatalystLE" if run._TARGET == "linux" else "LockdownLE")
    run_game(
        run.resolve_map(map_name),
        [
            Bot(Race.Protoss, EconomyMeasureBot(), name="EconMeasure",
                fullscreen=args.fullscreen),
            Bot(Race.Terran, run.PassiveBot(), name="Passive"),
        ],
        realtime=False,
        random_seed=run.GAME_SEED,
    )


if __name__ == "__main__":
    main()
