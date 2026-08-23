"""The automatic economy for BuildOrderBot: make probes, saturate minerals then
gas, and keep no worker idle.

`EconomyMixin` is mixed into `BuildOrderBot` (see bot.py); its methods run on the
live bot via `self`.
"""

from __future__ import annotations

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.unit import Unit
from sc2.units import Units

from catalog import FULL_MINERAL_SATURATION


class EconomyMixin:
    async def make_workers(self) -> None:
        # Make probes continuously, full stop. If a build needs to pause worker
        # production it says so with a `workers: stop` step (which clears
        # continuously_build_workers). No target/cap maths — supply blocking from the
        # build's own pylons is the only thing that throttles it.
        if not self.continuously_build_workers or self.supply_left <= 0:
            return
        for nexus in self.townhalls.ready.idle:
            if self.can_afford(U.PROBE):
                nexus.train(U.PROBE)

    async def manage_economy(self) -> None:
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

    def _minerals_under_cap(self) -> Units | None:
        """Mineral fields of the first (in base order: main, natural, third, ...)
        base below the per-base cap — so bases fill up in order."""
        for th in self._ordered_bases():
            if th.assigned_harvesters < FULL_MINERAL_SATURATION:
                fields = self.mineral_field.closer_than(10, th)
                if fields:
                    return fields
        return None

    def _ordered_bases(self) -> Units:
        """Our ready bases in expansion order (nearest our start first = base 1)."""
        return self.townhalls.ready.sorted(key=lambda t: t.distance_to(self.start_location))

    def _populating_base(self) -> Unit | None:
        bases = self._ordered_bases()
        if not bases:
            return None
        return bases[min(self.populating_base_num, len(bases)) - 1]

    def _base_field(self, base: Unit | None) -> Unit | None:
        """A mineral field at `base` (closest patch to it), or None."""
        if base is None:
            return None
        fields = self.mineral_field.closer_than(10, base)
        return fields.closest_to(base) if fields else None

    def _populating_field(self) -> Unit | None:
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

    def _econ_workers(self) -> Units:
        """Workers available to the economy — excludes the prewalk builder and any
        probes sent out via send_probe, so they aren't yanked back to mining."""
        ex = self._excluded_tags()
        return self.workers.tags_not_in(ex) if ex else self.workers
