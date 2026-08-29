#!/usr/bin/env python3
"""Measure what an opening worker split is worth, from a REAL game start.

This is deliberately not part of economy_measure.py's phase machinery: that harness
debug-spawns workers at an already-running base, which is exactly what an opening split
is not. The thing being measured here is the transient in the first seconds of a game --
12 probes stacked on the Nexus, all issued a gather order at once -- so it has to run
from t=0 on an untouched start location.

Modes:
    default  -- leave Blizzard's own start-of-game worker assignment alone
    split    -- one probe per patch, nearest first, held there (extras double up on the
                nearest patches); every patch gets mined
    near     -- two probes per patch filling the NEAREST patches first, so the far patches
                are left completely unmined. Strictly better per probe if they all fit on
                close patches; the engine fights this hardest, since it wants to put a
                probe on every free patch.

Nothing is built or trained, so the mineral count is pure income.

    for m in default split; do
      docker run --rm -v "$PWD":/app build-order-executor-headless \
        python opening_split_measure.py --target linux --mode $m 2>&1 | grep '^\\[open\\]'
    done
"""

from __future__ import annotations

import argparse
import os  # noqa: F401  (HOLD_S override)
import sys

import run  # noqa: F401  (sets SC2PF/SC2PATH before sc2 is imported; see economy_measure)

from sc2.bot_ai import BotAI
from sc2.data import Race, Result
from sc2.main import run_game
from sc2.player import Bot

from matching import match_nearest

SAMPLE_EVERY = 5.0    # seconds between [open] samples
RUN_S = 90.0          # long enough to cover the opening and a bit of steady state
HOLD_S = float(__import__('os').environ.get('HOLD_S', 25.0))  # enforcement window
NUDGE_INTERVAL = float(os.environ.get('NUDGE_INTERVAL', 1.0))  # min gap between re-orders


class OpeningSplitBot(BotAI):
    def __init__(self, mode: str, assign: str = "tag") -> None:
        super().__init__()
        self.mode = mode
        self.assign_mode = assign
        self.assignment: dict[int, int] = {}  # worker tag -> patch tag
        self._last_nudge: dict[int, float] = {}
        self._next_sample = 0.0
        self.samples: list[tuple[float, int]] = []
        self.nudges = 0
        self.start_contents: dict[int, int] = {}
        # A probe is LATCHED once we have seen it actually harvest its assigned patch;
        # from then on it returns there by itself and must never be nudged again --
        # re-ordering a working probe just cancels the harvest it is in the middle of.
        self.latched: dict[int, float] = {}      # tag -> time it latched
        self.per_probe_nudges: dict[int, int] = {}
        self._prev_target: dict[int, int] = {}   # tag -> order_target last frame

    async def on_start(self) -> None:
        self.client.game_step = 4
        self.start_contents = {m.tag: m.mineral_contents for m in self._patches()}
        if self.mode in ("split", "near"):
            self._assign()

    def _patches(self):
        fields = self.mineral_field.closer_than(12, self.start_location)
        return sorted(fields, key=lambda m: (round(m.distance_to(self.start_location), 3), m.tag))

    def _assign(self) -> None:
        """One probe per patch, nearest first; extras double up on the nearest again.

        That ordering is the whole point: the default assignment pays no attention to
        distance, so the probes that end up doubled land wherever, while this puts the
        doubles on the fastest patches.
        """
        patches = self._patches()
        probes = sorted(self.workers, key=lambda w: w.tag)
        print("[open] patches " + " ".join(
            f"{m.distance_to(self.start_location):.2f}" for m in patches), flush=True)
        # The slot list: which patch the i-th probe should end up on. `split` spreads
        # 1-deep over every patch; `near` stacks 2-deep on the closest, leaving the far
        # ones empty.
        slots = [patches[i % len(patches)] if self.mode == "split" else patches[i // 2]
                 for i in range(len(probes))]
        if self.assign_mode == "nearest":
            pairs = match_nearest(probes, slots)
        else:
            pairs = list(zip(probes, slots))
        walk = sum(w.distance_to(p) for w, p in pairs)
        by_tag = sum(w.distance_to(p) for w, p in zip(probes, slots))
        print(f"[open] assign={self.assign_mode} total_walk={walk:.1f} tiles "
              f"(by_tag={by_tag:.1f})", flush=True)
        for w, patch in pairs:
            self.assignment[w.tag] = patch.tag
            w.gather(patch)

    async def on_step(self, iteration: int) -> None:
        if self.mode in ("split", "near") and self.time < HOLD_S:
            self._hold()
        if self.time >= self._next_sample:
            self._next_sample += SAMPLE_EVERY
            # minerals mined, i.e. net of the 50 we start the game holding
            self.samples.append((round(self.time, 1), self.minerals - 50))
            print(f"[open] {self.mode}\tt={self.time:6.1f}\tmined={self.minerals - 50:5d}"
                  f"\tworkers={int(self.supply_workers)}\tnudges={self.nudges}", flush=True)
        if self.time >= RUN_S:
            await self.client.leave()

    def _hold(self) -> None:
        by_tag = {m.tag: m for m in self._patches()}
        for w in self.workers:
            want = self.assignment.get(w.tag)
            if want is None:
                continue
            # Latch check: carrying now, and aimed at our patch on the previous frame,
            # means it just harvested OUR patch. (Comparing on this frame would be wrong:
            # the target flips to the Nexus the instant the probe picks the load up.)
            if w.tag not in self.latched:
                if w.is_carrying_minerals and self._prev_target.get(w.tag) == want:
                    self.latched[w.tag] = self.time
            self._prev_target[w.tag] = w.order_target
            if w.is_carrying_minerals or w.order_target == want:
                continue
            if self.time - self._last_nudge.get(w.tag, -99.0) < NUDGE_INTERVAL:
                continue
            self._last_nudge[w.tag] = self.time
            self.nudges += 1
            self.per_probe_nudges[w.tag] = self.per_probe_nudges.get(w.tag, 0) + 1
            w.gather(by_tag[want])

    async def on_end(self, result: Result) -> None:
        final = self.samples[-1] if self.samples else (0, 0)
        # Per-patch extraction, nearest first: the proof that a mode did what it claims.
        # For `near` the far half must read 0 -- if it doesn't, the engine won the fight
        # and the row is really a spread, not a concentration.
        now = {m.tag: m.mineral_contents for m in self._patches()}
        per_patch = [self.start_contents[t] - now.get(t, 0) for t in self.start_contents]
        # What each probe actually did: how many re-orders it took and when it settled.
        # This is the bit that is otherwise invisible without watching the replay.
        probes = sorted(self.assignment)
        latch = [(round(self.latched[t], 1) if t in self.latched else None) for t in probes]
        print(f"[open] PROBES latch_times={latch} "
              f"nudges_each={[self.per_probe_nudges.get(t, 0) for t in probes]} "
              f"never_latched={sum(1 for t in probes if t not in self.latched)}", flush=True)
        print(f"[open] RESULT mode={self.mode} mined_at_{final[0]:.0f}s={final[1]} "
              f"nudges={self.nudges} per_patch={per_patch} series={self.samples}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None, help="see run.py (wine | linux)")
    ap.add_argument("--map", default=None)
    ap.add_argument("--mode", choices=("default", "split", "near"), default="default")
    ap.add_argument("--assign", choices=("tag", "nearest"), default="tag",
                    help="how probes are matched to patch slots (see _assign)")
    ap.add_argument("--fullscreen", action="store_true")
    args = ap.parse_args()

    from loguru import logger as _loguru
    _loguru.remove()
    _loguru.add(sys.stderr, level="WARNING")

    map_name = args.map or ("CatalystLE" if run._TARGET == "linux" else "LockdownLE")
    run_game(
        run.resolve_map(map_name),
        [Bot(Race.Protoss, OpeningSplitBot(args.mode, args.assign), name="Split",
             fullscreen=args.fullscreen),
         Bot(Race.Terran, run.PassiveBot(), name="Passive")],
        realtime=False,
        random_seed=run.GAME_SEED,
    )


if __name__ == "__main__":
    main()
