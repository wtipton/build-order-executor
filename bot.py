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

This file is the thin orchestrator: it holds the run state and the on_step loop,
and mixes in the behaviour by concern —

  * steps.py    (StepsMixin)    — the step engine + every do_<action> handler
  * economy.py  (EconomyMixin)  — probes / saturation / rally
  * world.py    (WorldMixin)    — locations, probe selection, prewalk reservation
  * observe.py  (ObserveMixin)  — run summary, [end]/[done]/[stall] reporting
  * placement.py (Placement)    — building/pylon geometry (held as self.placement)

The YAML spec (triggers, actions, economy) is defined and validated in schema.py.
"""

from __future__ import annotations

import json

from sc2.bot_ai import BotAI

from economy import EconomyMixin
from observe import ObserveMixin
from placement import Placement
from schema import BuildConfig, Step, load_build  # noqa: F401  (load_build re-exported for run.py)
from state import BuildConfirm, MorphState, Reservation, WarpConfirm
from steps import StepsMixin
from world import WorldMixin

# Grace period (game-seconds) between the build finishing and conceding, so the
# last actions land and are watchable rather than cutting out instantly.
CONCEDE_GRACE = 10.0


class BuildOrderBot(StepsMixin, EconomyMixin, WorldMixin, ObserveMixin, BotAI):
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

        # Per-step in-flight state (see state.py). Issuing an order != it happening,
        # so each of these tracks a build/warp/morph until it's confirmed, and the
        # pre-walk reservation tracks a probe walked to the next build's spot ahead
        # of time so construction starts the instant we can afford it.
        self._reservation = Reservation()
        self._build = BuildConfirm()
        self._warp = WarpConfirm()
        self._morph = MorphState()
        self._active_step: Step | None = None
        # Producer tags already given an order THIS frame — the observation cache
        # doesn't refresh mid-frame, so do_train uses this to spread production
        # across idle producers instead of stacking onto the first one.
        self._issued_this_frame: set[int] = set()
        # Nexus tags that already cast a chrono THIS frame. Same stale-cache problem:
        # their energy still reads pre-spend, so two chrono steps firing in one frame
        # would both cast from the same Nexus on 50 energy — one silently no-ops and
        # both steps get marked done. Tracked separately from _issued_this_frame so a
        # Nexus can still train a probe and chrono in the same frame.
        self._chrono_cast_this_frame: set[int] = set()

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
        # Always emit a parseable [summary] line (eval graders key on it), and never
        # let a summary bug escape on_end: python-sc2 folds an on_end exception into
        # the game result, then asserts every result is a Result — crashing the run.
        try:
            summary = self._run_summary(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            summary = {"name": self.cfg.name, "result": str(result), "error": repr(e)}
        print(f"[summary] {json.dumps(summary)}", flush=True)

    async def on_step(self, iteration: int):
        if not self.townhalls:
            return
        self._issued_this_frame.clear()
        self._chrono_cast_this_frame.clear()
        if self.debug and self.time - self._last_hb >= 10:
            self._last_hb = self.time
            self._heartbeat()
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
