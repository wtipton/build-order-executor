"""The automatic economy for BuildOrderBot: make probes, saturate minerals then
gas, and keep no worker idle.

`EconomyMixin` is mixed into `BuildOrderBot` (see bot.py); its methods run on the
live bot via `self`.
"""

from __future__ import annotations

from collections import Counter

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2
from sc2.unit import Unit
from sc2.units import Units

from catalog import FULL_MINERAL_SATURATION
from matching import match_nearest

# How long to keep re-issuing the assignment of probes to patches. This only has to cover
# the initial scramble:
OPENING_SPLIT_UNTIL = 10.0

class EconomyFrameState:
    """Who is mining what, seeded from the frame's observation and kept current as we
    move workers.
    
    Note: annoyingly, there's no way to get all the probes assigned to a gas. We can
    get a count of workers on a gas from g.assigned_harvesters, and we can get the
    probes that are headed to a gas by checking the worker's order_target. But a probe
    who is returning gas to the nexus has order_target == the nexus (as well as
    is_carrying_vespene == True).
    
    So, we can only pull a probe off a specific gas on the visiting leg of its trip.
    This is desirable anyway, since we don't waste gas being carried by a returning
    probe. But it means that we're not always able to pull a probe off a particular
    gas. In this case, we just skip the frame and try again next one.
    """

    def __init__(self, econ_workers: Units, gas_buildings: Units) -> None:
        self._gasses: Units = gas_buildings.ready
        self._gas_tags: set[int] = {g.tag for g in self._gasses}
        self._workers: dict[int, Unit] = {w.tag: w for w in econ_workers}
        self._num_workers_by_gas: Counter[int] = Counter(
            {g.tag: g.assigned_harvesters for g in self._gasses}
        )
        # worker tag -> the geyser it is gathering from. GATHER LEG ONLY — a worker on the
        # return leg is missing from here even though it is on gas.
        self._gas_by_gathering_worker: dict[int, int] = {
            w.tag: w.order_target
            for w in econ_workers
            if w.order_target in self._gas_tags and not w.is_carrying_vespene
        }

    # ---------------------------------------------------------------- mutation
    def send_worker(self, worker: Unit, dest: Unit | None) -> bool:
        """Order `worker` onto `dest` and record it. Returns False (and does nothing) if
        there is nothing to send or nowhere to send it.
        
        Note: it's important that worker is never a worker who is returning vespene,
        because in that case, we wouldn't be able to update self._num_workers_by_gas
        (because we have no way to know exactly which gas the worker is mining from).
        In practice, we never send_worker on such a worker, and we prove that below
        with an assert.
        """
        if worker is None or dest is None:
            return False
        assert worker.is_idle or not worker.is_carrying_vespene, (
            f"probe {worker.tag} is returning vespene, so we can't tell which geyser to "
            f"decrement in _num_workers_by_gas"
        )
        worker.gather(dest)
        gas_worker_was_gathering_from = self._gas_by_gathering_worker.pop(worker.tag, None)
        if gas_worker_was_gathering_from is not None:
            self._num_workers_by_gas[gas_worker_was_gathering_from] -= 1
        if dest.tag in self._gas_tags:
            self._gas_by_gathering_worker[worker.tag] = dest.tag
            self._num_workers_by_gas[dest.tag] += 1
        return True

    # ---------------------------------------------------------------- counts
    def num_gas_workers(self) -> int:
        return sum(self._num_workers_by_gas.values())

    def num_non_gas_workers(self) -> int:
        """Econ workers not on any geyser"""
        return sum(1 for w in self._workers.values() if not self._is_on_gas(w))

    def num_workers_on(self, gas: Unit | None) -> int:
        return self._num_workers_by_gas[gas.tag] if gas else 0

    # ---------------------------------------------------------------- selection
    def least_busy_gas(self) -> Unit | None:
        """The ready geyser with the fewest workers on it, or None if we have none."""
        # tag breaks ties last: observation order isn't stable between runs
        return min(self._gasses, key=lambda g: (self._num_workers_by_gas[g.tag], g.tag),
                   default=None)

    def most_busy_gas(self) -> Unit | None:
        """The geyser with the most workers on it, or None if none has any.

        Among geysers TIED for busiest, prefer one where we can actually identify a worker
        to pull — otherwise we'd refuse to drain while a tied geyser was drainable.
        """
        worked = [g for g in self._gasses if self._num_workers_by_gas[g.tag] > 0]
        if not worked:
            return None
        return max(worked, key=lambda g: (self._num_workers_by_gas[g.tag],
                                          self.worker_mining_from(g) is not None,
                                          -g.tag))

    def non_gas_worker_near(self, gas: Unit | None) -> Unit | None:
        pool = [w for w in self._workers.values() if not self._is_on_gas(w)]
        if gas is None or not pool:
            return None
        return min(pool, key=lambda w: (w.distance_to(gas), w.tag))

    def worker_mining_from(self, gas: Unit | None) -> Unit | None:
        """A worker we can see gathering from `gas`. Best effort: returns None if every
        worker on it happens to be mid-return, since their geyser isn't knowable."""
        if gas is None:
            return None
        pool = [self._workers[tag] for tag, gas_tag in self._gas_by_gathering_worker.items()
                if gas_tag == gas.tag]
        return min(pool, key=lambda w: w.tag) if pool else None

    def _is_on_gas(self, worker: Unit) -> bool:
        """Either leg: gathering from a geyser we know, or hauling gas back from one we
        don't."""
        return worker.tag in self._gas_by_gathering_worker or worker.is_carrying_vespene



class EconomyMixin:
    async def opening_split(self) -> None:
        """Stack the starting probes 2-deep on the NEAR row of patches.

        A probe on the near row mines ~13% faster than one on the far row
        (reference/economic_data.md §2), and a patch takes 2 probes at full rate, so the
        best use of the opening workers is to double up on the near row rather than to
        spread one per patch. Workers past 2 per near patch go round-robin on far patches.

        The engine sometimes undoes a single gather order aimed at a patch it would rather
        not use, so the assignment is re-issued every frame until it takes.
        """
        if self.time > OPENING_SPLIT_UNTIL:
            return
        patches = self._home_patches()
        if not patches:
            return
        econ = self.workers.tags_not_in(self._probes_unavailable_to_automation() or [])
        # A probe a `gas_workers` step has put on a geyser is not ours to move back. With
        # the window as short as it is no Assimilator can exist yet, so this is currently
        # unreachable -- it is here so that raising OPENING_SPLIT_UNTIL stays safe.
        gas_tags = {g.tag for g in self.gas_buildings}
        econ = econ.filter(lambda w: not w.is_carrying_vespene and w.order_target not in gas_tags)
        if not self._split_assignment:
            probes = sorted(econ, key=lambda u: u.tag)
            slots = [self._split_patch(patches, i) for i in range(len(probes))]
            for w, patch in match_nearest(probes, slots):
                self._split_assignment[w.tag] = patch.tag
        by_tag = {m.tag: m for m in patches}
        for w in econ:
            want = self._split_assignment.get(w.tag)
            # Mid-haul is left alone: re-targeting a loaded probe just wastes the trip.
            if want is None or want not in by_tag or w.is_carrying_minerals:
                continue
            if w.order_target == want:
                continue
            w.gather(by_tag[want])

    @staticmethod
    def _split_patch(patches: list[Unit], i: int) -> Unit:
        """Which patch the i-th probe (nearest-first order) should take.

        The near row is taken to be the closer half of the patches. That is a heuristic --
        the rows are not always an even split (CatalystLE's main is 3/5, LockdownLE's is
        4/4) and the real boundary is a step in round-trip time, not a gap in distance --
        but "closer half, 2 deep" captures the gain on a standard base without needing to
        probe the geometry.
        """
        near = len(patches) // 2
        if i < 2 * near:
            return patches[i // 2]                       # 2 deep on the near row
        far = patches[near:] or patches
        return far[(i - 2 * near) % len(far)]            # then spread over the far row

    def _home_patches(self) -> list[Unit]:
        """The main base's patches, nearest first."""
        nexus = self.townhalls.closest_to(self.start_location) if self.townhalls else None
        if nexus is None:
            return []
        fields = self.mineral_field.closer_than(12, nexus)
        return sorted(fields, key=lambda m: (round(m.distance_to(nexus), 3), m.tag))

    async def train_workers(self) -> None:
        # By default, we schedule workers on every idle nexus on every frame.
        if not self.continuously_build_workers or self.supply_left <= 0:
            return
        for nexus in self.townhalls.ready.idle:
            if self.can_afford(U.PROBE):
                nexus.train(U.PROBE)

    async def manage_economy(self) -> None:
        """Saturate minerals up to the per-base cap, then fill gas to gas_target,
        and keep no worker idle."""
        econ_workers = self.workers.tags_not_in(self._probes_unavailable_to_automation() or [])
        state = EconomyFrameState(econ_workers, self.gas_buildings)
        
        # Avoid idle workers. A builder that just finished goes back to the base it
        # came from; anyone else joins the base we're currently populating.
        for w in econ_workers.idle:
            state.send_worker(w, self._builder_home_field(w) or self._populating_field())

        # Make sure probes aren't stuck on assimilators under construction.
        gasses_under_construction = {g.tag for g in self.gas_buildings.not_ready}
        stuck_workers = {w for w in econ_workers.gathering if w.order_target in gasses_under_construction}
        for w in stuck_workers:
            state.send_worker(w, self._populating_field())
            
        # Make sure we have the right number of workers on gasses
        while state.num_gas_workers() < self.gas_target and 0 < state.num_non_gas_workers():
            gas = state.least_busy_gas()
            w = state.non_gas_worker_near(self._populating_field())
            if not state.send_worker(w, gas):
                break  # we have no ready geyser to put them on

        while state.num_gas_workers() > self.gas_target:
            gas = state.most_busy_gas()
            w = state.worker_mining_from(gas)
            if not state.send_worker(w, self._populating_field()):
                break  # nowhere to put them

        # Rebalance workers across ready geysers if unevenly distributed.
        while len(state._gasses) >= 2:
            most_gas = state.most_busy_gas()
            least_gas = state.least_busy_gas()
            if most_gas is None or least_gas is None or most_gas.tag == least_gas.tag:
                break
            if state.num_workers_on(most_gas) - state.num_workers_on(least_gas) <= 1:
                break
            w = state.worker_mining_from(most_gas)
            if not state.send_worker(w, least_gas):
                break  # we have no reassignable worker on the gather leg this frame

    def _builder_home_field(self, worker: Unit) -> Unit | None:
        """A field at the base `worker` was mining before we pulled it off to build, or
        None if it wasn't a builder."""
        base_tag = self.home_base_by_builder.pop(worker.tag, None)
        return self._base_field(self.townhalls.tags_in({base_tag}).first) if base_tag else None

    def _base_field(self, base: Point2 | Unit) -> Unit | None:
        """A mineral patch at `base`, or None if there are none. Takes a position as well
        as a Nexus, so callers can ask about a base we don't own yet."""
        fields = self.mineral_field.closer_than(10, base)
        return fields.closest_to(base) if fields else None

    def _populating_field(self) -> Unit | None:
        """Grab a specific field from the currently-populating base."""
        return self._base_field(self.base_position(self.populating_base_num))
