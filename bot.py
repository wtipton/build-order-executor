"""Generic build-order bot for python-sc2.

`BuildOrderBot` executes a build described entirely by a validated config (see
schema.py + builds/*.yaml). Nothing about a specific build is hard-coded here.

Two layers:

  * STEPS  — the deliberate actions a human performs and must learn: build,
             train, warp, morph, research, hallucinate, chrono, send_probe/return_probe,
             set_rally_point, rally_and_transfer_probes, plus economy overrides. Each step
             trigger and fires once, in order.
  * ECONOMY — automatic behaviour the bot runs on its own unless a step
             overrides it: make probes continuously, never leave a worker idle,
             saturate minerals to N per base and then fill gas, return builders
             to mining.

This file is the thin orchestrator: it holds the run state and the on_step loop,
and mixes in the behaviour by concern —

  * steps.py    (StepsMixin)    — the step engine + every do_<action> handler
  * economy.py  (EconomyMixin)  — probes / saturation
  * world.py    (WorldMixin)    — locations, rally point, probe selection, prewalk
  * observe.py  (ObserveMixin)  — run summary, [status]/[complete]/[end] reporting
  * placement.py (Placement)    — building/pylon geometry (held as self.placement)

The YAML spec (triggers, actions) is defined and validated in schema.py.
"""

from __future__ import annotations

import json

from sc2.bot_ai import BotAI
from sc2.data import Result
from sc2.ids.unit_typeid import UnitTypeId
from sc2.ids.upgrade_id import UpgradeId
from sc2.position import Point2

from economy import EconomyMixin
from observe import STATUS_INTERVAL, ObserveMixin
from placement import Placement
from scheduler import Scheduler
from schema import BuildConfig, load_build  # noqa: F401  (load_build re-exported for run.py)
from state import PrewalkState, StepState
from steps import StepsMixin
from world import WorldMixin

# Grace period (game-seconds) between the build finishing and conceding, so the
# last actions land and are watchable rather than cutting out instantly.
CONCEDE_GRACE = 10.0


class BuildOrderBot(StepsMixin, EconomyMixin, WorldMixin, ObserveMixin, BotAI):
    def __init__(self, cfg: BuildConfig, dump_data: bool = False) -> None:
        super().__init__()
        self.cfg = cfg
        self.dump_data = dump_data

        # State related to build progress
        self.steps_done: int = 0  # Steps run in strict order, so progress is just a count.
        self.step_state = StepState()
        self.prewalk_state = PrewalkState()  # Probe rep-walk reservation system state.
        self.placement = Placement(self)
        self.scheduler = Scheduler(self)

        # Info tracking build completion
        self._build_done_at: float | None = None  # game-time the last step finished
        self._conceded = False

        # Economy management
        #
        # Probes are made continuously by default, but this can be toggled in a build order.
        self.continuously_build_workers: bool = True
        # The base a returned/idle worker mines. Moved by `rally_and_transfer_probes`.
        self.populating_base_num: int = 1
        # TODO (wtipton): gas workers system needs some work:
        #   (1) build spec should probably specify what base to populate gas on
        #   (2) need to ensure workers are correctly allocated to assimilators
        self.gas_target: int = 0  # no workers in gas until a `gas_workers` step says so

        # Rally system: all combat unit production buildings get rallied to a single
        # rally point. Defaults to main ramp (set in on_start). `_rallied` is the set of
        # buildings already pointed at the CURRENT value, cleared when it moves so existing
        # ones get re-issued.
        self.rally_point: Point2 | None = None
        self._rallied: set[int] = set()

        # label -> probe tag for probes sent out via `send_probe`. These are held
        # OUT of all worker automation (mining, prewalk, build auto-select) until a
        # `return_probe` step hands them back.
        self.named_probes: dict[str, int] = {}

        # Probe tag -> the base it was mining at when we pulled it off to build. A builder
        # is only away briefly, so it goes back where it came from; dumping it on the
        # populating base instead slowly drains whichever base we keep pulling from.
        self.home_base_by_builder: dict[int, int] = {}

        # Observability system state. Supports:
        # - Printing a [status] line every STATUS_INTERVAL
        # - Tracking when individual buildings, units, and upgrades completed
        self._last_status = -STATUS_INTERVAL
        self._completions: dict[tuple[int, UnitTypeId], float] = {}  # Key is unique tag and unit type
        self._upgrade_completions: dict[UpgradeId, float] = {}  # Values are timestamps

        # Why the current step hasn't completed — set by handlers on a False return,
        # reported every [status] block.
        # TODO: this is frame-scoped, but it's up to a lot of independent handlers to
        # maintain this invariant. Do an audit to ensure it can never be stale and then
        # maybe do something to ensure it stays that way.
        self._status = ""


    async def on_start(self) -> None:
        self.client.game_step = 4
        # Combat units hold our main ramp until a `set_rally_point` step says otherwise.
        self.rally_point = self._resolve_place("main_ramp")
        if self.dump_data:
            import gamedata_dump
            gamedata_dump.dump(self)

    async def on_end(self, result: Result) -> None:
        print(f"[end] t={self.time:.1f}s result={result} supply={self.supply_used} "
              f"workers={self.workers.amount} steps={self.steps_done}/{len(self.cfg.steps)}", flush=True)
        print(f"[end] {self._army_report()}", flush=True)
        # Machine-readable summary
        try:
            summary = self._run_summary(result)
        except Exception as e:
            import traceback
            traceback.print_exc()
            summary = {"name": self.cfg.name, "result": str(result), "error": repr(e)}
        print(f"[summary] {json.dumps(summary)}", flush=True)

    async def on_step(self, iteration: int) -> None:
        if not self.townhalls:
            return
        self.scheduler.new_frame()
        if self.time - self._last_status >= STATUS_INTERVAL:
            self._last_status = self.time
            self._status_report()
        self._note_completions()
        await self.manage_prebuild()
        await self.train_workers()  # probes first: continuous, first claim on minerals each frame
        await self.run_steps()
        await self.manage_economy()
        self.apply_rally()

        # Once every step has fired, keep macroing for a short grace period, then concede.
        if self.all_steps_done and self._build_done_at is None:
            self._build_done_at = self.time
            print(f"[run] {self._clock():>4}  build complete", flush=True)
        if not self._conceded and self._build_done_at is not None and self.time - self._build_done_at >= CONCEDE_GRACE:
            self._conceded = True
            print(f"[run] {self._clock():>4}  conceding ({int(CONCEDE_GRACE)}s after build)", flush=True)
            await self.client.leave()
