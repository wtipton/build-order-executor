"""Per-frame production scheduling for BuildOrderBot: which structure gets the next order.

We have to track state about chronos and unit production that happens in a single frame.
This is combined with the frame snapshot we get from the library that has info as of the
beginning of the frame to get actual current status.

The policy mirrors what a human does by selecting all producers and pressing the hotkey:
give the work to whichever structure frees up soonest. The game exposes no such chooser
(`ActionRawUnitCommand` applies an ability to EVERY tag you send, and the smart
allocation is UI-layer only), so we model it here.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.unit import Unit

from catalog import (
    CHRONO_CASTER,
    CHRONO_ENERGY,
    FRAMES_PER_SEC,
    MAX_PRODUCTION_QUEUE,
    PRODUCER,
    TRAIN_ABILITY_UNIT,
    WARPGATE_TRAIN_SPEEDUP,
)

if TYPE_CHECKING:
    from bot import BuildOrderBot


class Scheduler:
    def __init__(self, bot: BuildOrderBot) -> None:
        self.bot = bot
        # Commitments made THIS frame, invisible to the observation until it refreshes.
        self._queued: dict[int, list[float]] = {}     # producer tag -> build seconds added
        self._chrono_spent: Counter[int] = Counter()  # Nexus tag -> energy committed

    def new_frame(self) -> None:
        """Called once per on_step, before any handler runs."""
        self._queued.clear()
        self._chrono_spent.clear()

    # ============================================================ training
    def producer_for(self, unit: U) -> Unit | None:
        """The ready producer of `unit` that frees up soonest, or None if every one is at
        the game's queue limit (the caller should then stall rather than lose the order).

        Records the commitment, so a later call in the same frame sees this one.
        """
        producer_type = PRODUCER.get(unit)
        if producer_type is None:
            return None
        pool = (self.bot.townhalls if producer_type == U.NEXUS
                else self.bot.structures(producer_type)).ready
        free = [s for s in pool if self._queue_depth(s) < MAX_PRODUCTION_QUEUE]
        if not free:
            return None
        # Tag breaks ties LAST: the observation's structure order isn't stable between
        # runs, so without it the same build picks a different producer each run.
        chosen = min(free, key=lambda s: (self._busy_seconds(s), s.tag))
        self._queued.setdefault(chosen.tag, []).append(self._unit_seconds(unit, chosen))
        return chosen

    def _queue_depth(self, producer: Unit) -> int:
        return len(producer.orders) + len(self._queued.get(producer.tag, []))

    def _busy_seconds(self, producer: Unit) -> float:
        """Seconds until `producer` is free: what's left of its current order, plus the
        full time of everything queued behind it, plus what we've added this frame.

        Using `progress` is the point of measuring in seconds — a Colossus 90% done makes
        that Robo nearly free, while counting orders scores it the same as one just begun.
        """
        total = sum(self._queued.get(producer.tag, []))
        for i, order in enumerate(producer.orders):
            unit = TRAIN_ABILITY_UNIT.get(order.ability.id)
            if unit is None:
                continue  # not a train/build order (nothing else queues on a producer)
            secs = self._unit_seconds(unit, producer)
            total += secs * (1 - order.progress) if i == 0 else secs
        return total

    def _unit_seconds(self, unit: U, producer: Unit) -> float:
        """Build time, with the runtime modifier game data doesn't carry (see
        catalog.WARPGATE_TRAIN_SPEEDUP)."""
        cost = self.bot.game_data.units[unit.value].cost
        secs = (cost.time or 0) / FRAMES_PER_SEC
        if producer.type_id == U.GATEWAY and UpgradeId.WARPGATERESEARCH in self.bot.state.upgrades:
            secs *= WARPGATE_TRAIN_SPEEDUP
        return secs

    # ============================================================ chrono
    def chrono_caster(self) -> Unit | None:
        """A Nexus with enough energy LEFT THIS FRAME to boost, or None.

        Energy, not a one-cast-per-frame flag: a Nexus at 150 can serve three chronos in
        one frame, and refusing that stalls a step over energy it actually has.

        Fullest first — draining the fullest keeps any one Nexus off the 200 cap, where
        further regen is thrown away. Records the spend.
        """
        best: Unit | None = None
        best_key = ()
        for nexus in self.bot.townhalls(CHRONO_CASTER).ready:
            left = nexus.energy - self._chrono_spent[nexus.tag]
            if left >= CHRONO_ENERGY and (best is None or (left, nexus.tag) > best_key):
                best, best_key = nexus, (left, nexus.tag)
        if best is not None:
            self._chrono_spent[best.tag] += CHRONO_ENERGY
        return best
