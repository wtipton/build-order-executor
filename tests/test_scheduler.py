"""Tier 1 — Scheduler: which structure gets the next order.

Two things it has to get right that the observation alone can't tell it: load measured in
build SECONDS (not order count, which scores a Colossus the same as an Observer), and
commitments made earlier in the SAME frame (the snapshot doesn't refresh as we issue).
"""

from __future__ import annotations

from types import SimpleNamespace

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

import catalog
from fakes import FakeUnits, fake_unit
from scheduler import Scheduler

# Build times in frames, as game data reports them (cost.time). Real values from
# reference/protoss_data.md: Zealot 27.1s, Observer 17.9s, Colossus 53.6s.
FRAMES = {U.ZEALOT: 607.0, U.OBSERVER: 401.0, U.COLOSSUS: 1200.0, U.PROBE: 272.0}


def _order(unit: U, progress: float = 0.0):
    ability = catalog.TRAIN_ABILITY_UNIT and next(
        a for a, u in catalog.TRAIN_ABILITY_UNIT.items() if u == unit)
    return SimpleNamespace(ability=SimpleNamespace(id=ability), progress=progress)


def _bot(structures=(), townhalls=(), upgrades=frozenset()):
    game_data = SimpleNamespace(units={u.value: SimpleNamespace(cost=SimpleNamespace(time=t))
                                        for u, t in FRAMES.items()})
    return SimpleNamespace(
        structures=lambda t: FakeUnits(structures),
        townhalls=FakeUnits(townhalls),
        game_data=game_data,
        state=SimpleNamespace(upgrades=set(upgrades)),
    )


def _gw(tag, orders=()):
    return fake_unit(tag=tag, type_id=U.GATEWAY, orders=list(orders))


def _robo(tag, orders=()):
    return fake_unit(tag=tag, type_id=U.ROBOTICSFACILITY, orders=list(orders))


# ------------------------------------------------------------------ producer_for
def test_round_robins_across_idle_producers():
    gws = [_gw(1), _gw(2), _gw(3)]
    s = Scheduler(_bot(structures=gws))
    picked = [s.producer_for(U.ZEALOT).tag for _ in range(6)]
    assert picked == [1, 2, 3, 1, 2, 3], f"not round-robinning: {picked}"


def test_overflow_spreads_evenly_rather_than_stacking_on_one():
    # The old set-based tracker degraded to [6,1,1] here: once every producer had been
    # used once it always fell back to the lowest tag.
    gws = [_gw(1), _gw(2), _gw(3)]
    s = Scheduler(_bot(structures=gws))
    picked = [s.producer_for(U.ZEALOT).tag for _ in range(8)]
    depths = [picked.count(t) for t in (1, 2, 3)]
    assert sorted(depths, reverse=True) == [3, 3, 2], f"uneven: {depths}"


def test_load_is_measured_in_seconds_not_order_count():
    """A Robo holding two Observers (36s) is freer than one holding a Colossus (54s),
    even though counting orders makes the Colossus Robo look HALF as loaded."""
    two_observers = _robo(1, [_order(U.OBSERVER), _order(U.OBSERVER)])
    one_colossus = _robo(2, [_order(U.COLOSSUS)])
    s = Scheduler(_bot(structures=[two_observers, one_colossus]))
    assert s.producer_for(U.OBSERVER).tag == 1


def test_progress_on_the_current_order_counts():
    """A Colossus 95% done leaves that Robo nearly free; order-counting can't see it."""
    almost_done = _robo(1, [_order(U.COLOSSUS, progress=0.95)])
    just_started = _robo(2, [_order(U.OBSERVER, progress=0.0)])
    s = Scheduler(_bot(structures=[almost_done, just_started]))
    assert s.producer_for(U.OBSERVER).tag == 1


def test_refuses_past_the_games_queue_limit():
    """Ordering past the limit is silently dropped by the game, so the caller must be
    able to stall instead of losing the unit."""
    full = _gw(1, [_order(U.ZEALOT)] * catalog.MAX_PRODUCTION_QUEUE)
    s = Scheduler(_bot(structures=[full]))
    assert s.producer_for(U.ZEALOT) is None


def test_queue_limit_counts_this_frames_commitments_too():
    s = Scheduler(_bot(structures=[_gw(1)]))
    for _ in range(catalog.MAX_PRODUCTION_QUEUE):
        assert s.producer_for(U.ZEALOT) is not None
    assert s.producer_for(U.ZEALOT) is None, "queue limit ignored this frame's own orders"


def test_warpgate_research_halves_gateway_train_time():
    """Game data's cost.time is the PRE-research base (see catalog.WARPGATE_TRAIN_SPEEDUP),
    so a post-research Gateway is half as loaded as the raw table implies."""
    busy = _gw(1, [_order(U.ZEALOT)])
    idle = _gw(2)
    plain = Scheduler(_bot(structures=[busy, idle]))
    researched = Scheduler(_bot(structures=[busy, idle], upgrades={UpgradeId.WARPGATERESEARCH}))
    assert plain._busy_seconds(busy) == 2 * researched._busy_seconds(busy)


def test_new_frame_clears_commitments():
    s = Scheduler(_bot(structures=[_gw(1), _gw(2)]))
    assert s.producer_for(U.ZEALOT).tag == 1
    s.new_frame()
    assert s.producer_for(U.ZEALOT).tag == 1, "ledger survived the frame boundary"


def test_probe_trains_from_a_townhall():
    nx = fake_unit(tag=1, type_id=U.NEXUS, orders=[])
    s = Scheduler(_bot(townhalls=[nx]))
    assert s.producer_for(U.PROBE) is nx


def test_producer_choice_ignores_observation_order():
    """Structure order isn't stable between runs; the tag tie-break must pin the choice."""
    for reverse in (False, True):
        a, b = _gw(1), _gw(2)
        s = Scheduler(_bot(structures=[b, a] if reverse else [a, b]))
        assert s.producer_for(U.ZEALOT).tag == 1


# ------------------------------------------------------------------ chrono_caster
def test_one_nexus_serves_several_chronos_if_it_has_the_energy():
    """150 energy is three casts. The old one-cast-per-frame flag stalled the 2nd and 3rd
    steps over energy the Nexus actually had."""
    nexus = fake_unit(tag=1, energy=150)
    s = Scheduler(_bot(townhalls=[nexus]))
    assert [s.chrono_caster() for _ in range(3)] == [nexus] * 3
    assert s.chrono_caster() is None, "spent 200 energy from a 150-energy Nexus"


def test_a_single_charge_nexus_serves_exactly_one():
    nexus = fake_unit(tag=1, energy=50)
    s = Scheduler(_bot(townhalls=[nexus]))
    assert s.chrono_caster() is nexus
    assert s.chrono_caster() is None


def test_spends_from_the_fullest_nexus():
    """Draining the fullest keeps a Nexus off the 200 cap, where regen is thrown away."""
    low, high = fake_unit(tag=1, energy=60), fake_unit(tag=2, energy=190)
    s = Scheduler(_bot(townhalls=[low, high]))
    assert s.chrono_caster() is high


def test_no_caster_when_nobody_can_pay():
    s = Scheduler(_bot(townhalls=[fake_unit(tag=1, energy=10)]))
    assert s.chrono_caster() is None
