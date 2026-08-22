"""Observability for BuildOrderBot: the machine-readable run summary, the human
`[end]`/`[done]`/`[stall]` reporting, and step/trigger formatting.

`ObserveMixin` is mixed into `BuildOrderBot` (see bot.py) — its methods run on the
live bot via `self`, so they read game state (`self.time`, `self.structures`, …)
and other mixins' helpers directly.
"""

from __future__ import annotations

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

from catalog import (
    BUILDABLE_STRUCTURES,
    CHRONO_ENERGY,
    RESEARCH,
    TRAINABLE_UNITS,
    unit_id as _unit,
)

# Stall diagnostics: how long the head step may stall before we report why, and
# how often to re-report while it's still stuck. Set high so legitimate long
# waits (saving for a Nexus, a slow count trigger) don't cry wolf — a `[stall]`
# line means "this has been sitting a while; here's what it's waiting on".
STALL_FIRST = 60.0
STALL_REPEAT = 30.0


class ObserveMixin:
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

    def _heartbeat(self) -> None:
        """The periodic `[hb]` line under --debug: a one-glance snapshot of supply,
        workers, resources, per-base saturation, and the next step, plus the army
        report — so a live run is watchable without reading every [step] line."""
        nxt = self._describe(self.steps[self.idx]) if self.idx < len(self.steps) else "DONE"
        bases = " ".join(f"b{i+1}={t.assigned_harvesters}/{self.minerals_per_base}"
                         for i, t in enumerate(self._ordered_bases()))
        print(f"[hb] t={self.time:5.0f}s sup={self.supply_used}/{self.supply_cap} "
              f"w={self.workers.amount} pend_probe={self.already_pending(U.PROBE)} "
              f"min={self.minerals} gas={self.vespene} sup_left={self.supply_left} "
              f"mins[{bases}] next={nxt}", flush=True)
        print(f"[hb] {self._army_report()}", flush=True)

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
    def _trig_str(t) -> str:
        if t.count is not None:
            name, n = next(iter(t.count.items()))
            return f"count:{name}={n}"
        for k in ("supply", "time", "minerals", "vespene"):
            v = getattr(t, k)
            if v is not None:
                return f"{k}:{v}"
        return "?"

    def _describe(self, step) -> str:
        args = step.model_dump(exclude={"at", "prewalk", "note", "do"}, exclude_none=True)
        argstr = " ".join(f"{k}={v}" for k, v in args.items())
        return f"{step.do} {argstr}".rstrip()

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

    def _trigger_status(self, at) -> str:
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
