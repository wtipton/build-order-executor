"""Observability for BuildOrderBot: the machine-readable run summary, the human
`[status]`/`[complete]`/`[end]` reporting, and step/trigger formatting.

`ObserveMixin` is mixed into `BuildOrderBot` (see bot.py) — its methods run on the
live bot via `self`, so they read game state (`self.time`, `self.structures`, …)
and other mixins' helpers directly.
"""

from __future__ import annotations

from collections import Counter
from typing import get_args

from sc2.data import Result
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

from catalog import (
    BUILDABLE_STRUCTURES,
    CHRONO_ENERGY,
    FULL_MINERAL_SATURATION,
    PRODUCTION_BUILDINGS,
    RESEARCH,
    TRAINABLE_UNITS,
    unit_id as _unit,
)
from schema import Place, Step, Trigger

# How often the periodic [status] block prints (game-seconds).
STATUS_INTERVAL = 10.0

# UpgradeId -> the friendly name a build order uses (reverse of catalog.RESEARCH),
# so reports say "Charge" rather than "CHARGE".
UPGRADE_NAMES = {v: k for k, v in RESEARCH.items()}


class ObserveMixin:
    def _run_summary(self, result: Result) -> dict:
        """Machine-readable end-of-run summary (the `[summary]` stdout line) so a
        caller asserts on what ACTUALLY happened (structures/units/upgrades produced,
        where the queue stalled) rather than parsing the human log."""
        completed = self.all_steps_done
        census = {}
        for t in (BUILDABLE_STRUCTURES | TRAINABLE_UNITS | {U.WARPGATE, U.ARCHON}):
            n = self.all_own_units(t).ready.amount
            if n:
                census[t.name] = n
        upgrades = sorted(UPGRADE_NAMES.get(u, u.name) for u in self.state.upgrades)
        # Upgrades that STARTED but haven't finished — so "researched but unfinished"
        # is distinguishable from "never started" (already_pending_upgrade is the
        # research progress in [0,1)). Skip any upgrade the running client doesn't
        # define: an older game (e.g. the 4.10 headless build) lacks upgrade IDs the
        # library knows, and already_pending_upgrade would KeyError on game_data.
        researching = sorted(
            name for name, u in RESEARCH.items()
            if u.value in self.game_data.upgrades and 0 < self.already_pending_upgrade(u) < 1
        )
        return {
            "name": self.cfg.name,
            "result": str(result),
            "completed": completed,
            "steps_done": self.steps_done,
            "steps_total": len(self.cfg.steps),
            "next_step": None if completed else self._describe(self.current_step),
            "final_time": round(self.time, 1),
            "completions": self._completions_by_type(),
            "upgrade_completions": {UPGRADE_NAMES.get(u, u.name): round(t, 1)
                            for u, t in self._upgrade_completions.items()},
            "census": census,
            "upgrades": upgrades,
            "researching": researching,
            "bases": [int(t.assigned_harvesters) for t in self._ordered_bases()],
            "pylons_by_place": self._pylons_by_place(),
            "nexus_by_place": {p: 1 for p in self.BASE_RANK
                               if self.townhalls.closer_than(6, self._resolve_place(p)).exists},
            "named_probes_held": sorted(self.named_probes),
            "workers": self.workers.amount,
            "supply_used": self.supply_used,
        }

    def _pylons_by_place(self) -> dict:
        """Count our Pylons near each named location — so a build placing pylons at
        `enemy_main`/`proxy`/etc. can be verified to have put one at each distinct
        spot (not all clustered in one place)."""
        out = {}
        for place in get_args(Place):
            n = self.structures(U.PYLON).closer_than(12, self._resolve_place(place)).amount
            if n:
                out[place] = n
        return out

    def _note_completions(self) -> None:
        """Timestamp EVERY unit/structure completion and every upgrade, as
        `[complete] M:SS <Name> #<n>` where n is the running count of that type.

        Derived purely from game state — no knowledge of what the build asked for — so it
        works for any build with no configuration. [status] only prints every
        STATUS_INTERVAL seconds, far too coarse to check a tight deadline (4:12 vs 4:18
        against a 4:15 target); these are exact to the frame.

        `_completions` is keyed on (tag, type) and holds the completion time, so the
        record IS the once-fired latch — there's no separate `seen` set to fall out of
        sync with it. (tag, type) rather than tag alone because a Gateway morphing into a
        Warpgate KEEPS its tag, and a tag-only key would never report the Warpgate; and
        rather than a per-type count, which re-reports after a death (3 -> 2 -> 3) as if
        a new one had finished. Tags are unique and never reused, so pairs are exact.

        Probes are skipped: they'd be most of the output (~45 a game) and worker count is
        already on every [status] line.
        """
        live = {(u.tag, u.type_id) for u in self.all_own_units.ready if u.type_id != U.PROBE}
        # sorted so several completions in one frame log in a stable order (set
        # iteration order is not reproducible across runs)
        for key in sorted(live - self._completions.keys(), key=lambda p: (p[1].name, p[0])):
            self._completions[key] = self.time
            type_id = key[1]
            nth = sum(1 for _, t in self._completions if t == type_id)
            print(f"[complete] {self._clock():>4}  {type_id.name.title()} #{nth}", flush=True)

        new_upgrades = set(self.state.upgrades) - self._upgrade_completions.keys()
        for upgrade in sorted(new_upgrades, key=lambda u: u.name):
            self._upgrade_completions[upgrade] = self.time
            print(f"[complete] {self._clock():>4}  {UPGRADE_NAMES.get(upgrade, upgrade.name)}", flush=True)

    def _completions_by_type(self) -> dict:
        """Completion times grouped by type name, oldest first — the summary's view of
        `_completions`, which is stored keyed by unit so it can double as the latch."""
        out: dict[str, list[float]] = {}
        for (_, type_id), when in sorted(self._completions.items(), key=lambda kv: kv[1]):
            out.setdefault(type_id.name.title(), []).append(round(when, 1))
        return out

    def _status_report(self) -> None:
        """The periodic `[status]` block: an economy snapshot, then what we're doing and
        how long we've been doing it.

        The second line replaces the old [stall] machinery. Rather than classifying a
        stall against a threshold, it always shows the current step, its age, and why it
        hasn't completed — an observer sees the age growing and draws their own
        conclusion, with no thresholds to tune and nothing hidden for the first 60s.

        Army/production counts deliberately aren't here: [complete] already timestamps
        every type as it appears, and [end] carries the final census."""
        print(f"[status] {self._clock():>5}  sup={self.supply_used}/{self.supply_cap} "
              f"workers={self.workers.amount} min={self.minerals} gas={self.vespene} "
              f"bases[{self._base_saturation()}] chrono={self._chrono_available()}", flush=True)
        print(f"[status] {self._clock():>5}  {self._step_status()}", flush=True)

    def _step_status(self) -> str:
        """Which step we're on, how long it's been current, and what it's waiting for."""
        step = self.current_step
        if step is None:
            return f"step {self.steps_done}/{len(self.cfg.steps)}  BUILD COMPLETE"
        age = int(self.time - self.step_state.started_at)
        if not self.step_state.trigger_fired:
            why = f"waiting on trigger {self._trigger_status(step.at)}"
        else:
            why = self._status or "issued; waiting to confirm"
        return (f"step {self.steps_done}/{len(self.cfg.steps)}  {self._describe(step)}"
                f"  ({age}s)  {why}")

    def _base_saturation(self) -> str:
        return " ".join(f"b{i+1}={t.assigned_harvesters}/{FULL_MINERAL_SATURATION}"
                        for i, t in enumerate(self._ordered_bases()))

    def _chrono_available(self) -> int:
        return int(sum(n.energy for n in self.townhalls.ready) // CHRONO_ENERGY)

    def _army_report(self) -> str:
        """Observed army and production, by type — generic, so it reads the same for any
        build. Idle producers are called out because an idle production building is the
        clearest "you are wasting production" signal when optimizing a build."""
        army = Counter(u.type_id.name.title() for u in self.units if u.type_id != U.PROBE)
        army_str = " ".join(f"{name}={n}" for name, n in sorted(army.items())) or "none"
        prod = []
        for t in sorted(PRODUCTION_BUILDINGS, key=lambda t: t.name):
            ready = self.structures(t).ready
            if ready:
                idle = ready.idle.amount
                prod.append(f"{t.name.title()}={ready.amount}" + (f"({idle} idle)" if idle else ""))
        return f"army {army_str}  |  prod {' '.join(prod) or 'none'}"

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
