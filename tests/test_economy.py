"""Tier 1 — the automatic economy: probe production, EconomyFrameState's bookkeeping,
and the manage_economy loops built on it.

Most of the logic is in EconomyFrameState, which exists because the observation is a
start-of-frame snapshot: re-reading it after moving a worker still reports the old
picture. Those tests move workers and assert the state keeps up.
"""

from __future__ import annotations

from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from bot import BuildOrderBot
from economy import EconomyFrameState
from fakes import FakeUnits, fake_bot, fake_unit


NEXUS = 77  # order target of any worker hauling resources home


def _at(x, y=0.0):
    return Point2((float(x), float(y)))


def _gathering(tag, x=0.0, from_gas=None, idle=False):
    """A worker on the GATHER leg of `from_gas` (or on minerals if None). Its order target
    names the geyser, so the state can tell which one it's on."""
    w = fake_unit(tag=tag, position=_at(x), order_target=from_gas,
                  is_carrying_vespene=False, is_idle=idle, is_gathering=not idle)
    w.distance_to = lambda other, w=w: abs(w.position.x - other.position.x)
    return w


def _returning(tag, x=0.0):
    """A worker on the RETURN leg: hauling gas to the Nexus, so its order target is the
    Nexus and the geyser it came from is unknowable."""
    w = fake_unit(tag=tag, position=_at(x), order_target=NEXUS,
                  is_carrying_vespene=True, is_idle=False, is_gathering=True)
    w.distance_to = lambda other, w=w: abs(w.position.x - other.position.x)
    return w


# alias: most tests only care about the gather leg
_worker = _gathering


def _geyser(tag, x=0.0, ideal=3, ready=True, assigned=None, workers=()):
    """`assigned` is the game's own harvester count. Defaults to however many gather-leg
    workers the test handed in, which is the consistent case."""
    if assigned is None:
        assigned = sum(1 for w in workers if getattr(w, "order_target", None) == tag)
    return fake_unit(tag=tag, position=_at(x), ideal_harvesters=ideal, is_ready=ready,
                     assigned_harvesters=assigned)


def _field(tag=99):
    return fake_unit(tag=tag, position=_at(0))


def _state(workers, gasses=()):
    return EconomyFrameState(FakeUnits(workers), FakeUnits(gasses))


# ------------------------------------------------------------------ train_workers
def _train_bot(nexuses, **over):
    base = dict(continuously_build_workers=True, supply_left=10,
                can_afford=lambda u: True, townhalls=FakeUnits(nexuses))
    base.update(over)
    return fake_bot(**base)


async def test_trains_a_probe_at_every_idle_nexus():
    a, b = fake_unit(tag=1), fake_unit(tag=2)
    await BuildOrderBot.train_workers(_train_bot([a, b]))
    a.train.assert_called_once_with(U.PROBE)
    b.train.assert_called_once_with(U.PROBE)


async def test_trains_nothing_when_worker_production_is_stopped():
    """A `workers: stop` step clears the flag — builds cut probes to rush a timing."""
    nx = fake_unit(tag=1)
    await BuildOrderBot.train_workers(_train_bot([nx], continuously_build_workers=False))
    nx.train.assert_not_called()


async def test_trains_nothing_when_supply_blocked():
    # Supply blocking is the ONLY throttle on probe production, so this gate matters.
    nx = fake_unit(tag=1)
    await BuildOrderBot.train_workers(_train_bot([nx], supply_left=0))
    nx.train.assert_not_called()


async def test_trains_nothing_when_broke():
    nx = fake_unit(tag=1)
    await BuildOrderBot.train_workers(_train_bot([nx], can_afford=lambda u: False))
    nx.train.assert_not_called()


# ------------------------------------------------------------------ EconomyFrameState
def test_a_worker_heading_to_a_geyser_already_counts_as_a_gas_worker():
    """Seeding off is_carrying_vespene instead would miss the outbound half of the trip,
    so the worker would read as movable and get re-sent to the geyser it's walking to."""
    gas = _geyser(5, x=10, assigned=1)
    state = _state([_worker(1, 9, from_gas=5), _worker(2, 1)], [gas])
    assert state.num_gas_workers() == 1
    assert state.num_non_gas_workers() == 1


def test_unfinished_geysers_hold_nobody():
    """gas_buildings includes ones still under construction; only ready ones count."""
    building = _geyser(5, x=10, ready=False)
    state = _state([_worker(1, 9, from_gas=5)], [building])
    assert state.num_gas_workers() == 0
    assert state.least_busy_gas() is None


def test_sending_a_worker_to_gas_shows_up_immediately():
    """The whole reason the class exists — the observation won't show this until the
    next frame."""
    gas = _geyser(5, x=10)
    w = _worker(1, 1)
    state = _state([w], [gas])
    assert state.num_gas_workers() == 0
    assert state.send_worker(w, gas) is True
    assert state.num_gas_workers() == 1 and state.num_non_gas_workers() == 0


def test_sending_a_gas_worker_to_minerals_shows_up_immediately():
    gas = _geyser(5, x=10, assigned=1)
    w = _worker(1, 9, from_gas=5)
    state = _state([w], [gas])
    state.send_worker(w, _field())
    assert state.num_gas_workers() == 0 and state.num_non_gas_workers() == 1


def test_send_worker_reports_failure_when_there_is_nowhere_to_send():
    """`_populating_field()` returns None when the populating base has no patches, and
    `least_busy_gas()` returns None when we own no geyser. Callers stop on False rather
    than looping forever."""
    w = _worker(1)
    state = _state([w])
    assert state.send_worker(w, None) is False
    assert state.send_worker(None, _field()) is False
    w.gather.assert_not_called()


def test_least_busy_gas_picks_the_one_with_fewest_workers():
    empty, busy = _geyser(5, x=10), _geyser(6, x=20)
    workers = [_worker(1, 19, from_gas=6), _worker(2, 21, from_gas=6)]
    assert _state(workers, [empty, busy]).least_busy_gas() is empty


def test_least_busy_gas_ignores_ideal_harvesters():
    """ideal_harvesters is where returns stop improving, not a cap — a geyser takes as
    many probes as we send it, so an already-full one is still a candidate."""
    over_full = _geyser(5, x=10, ideal=1)
    state = _state([_worker(1, 9, from_gas=5)], [over_full])
    assert state.least_busy_gas() is over_full


def test_least_busy_gas_counts_workers_sent_this_frame():
    a, b = _geyser(5, x=10), _geyser(6, x=20)
    w = _worker(1, 11)
    state = _state([w], [a, b])
    state.send_worker(w, a)
    assert state.least_busy_gas() is b, "a geyser filled this frame still looked empty"


def test_most_busy_gas_and_the_worker_to_pull_off_it():
    quiet, busy = _geyser(5, x=10, assigned=1), _geyser(6, x=20, assigned=2)
    on_busy = [_worker(1, 19, from_gas=6), _worker(2, 21, from_gas=6)]
    state = _state([*on_busy, _worker(3, 11, from_gas=5)], [quiet, busy])
    assert state.most_busy_gas() is busy
    assert state.worker_mining_from(busy) in on_busy


def test_nothing_to_pull_off_an_unworked_geyser():
    gas = _geyser(5, x=10)
    state = _state([_worker(1, 1)], [gas])
    assert state.most_busy_gas() is None
    assert state.worker_mining_from(gas) is None


def test_non_gas_worker_near_skips_workers_already_on_gas():
    """The nearest worker to a geyser is usually one already mining it."""
    gas = _geyser(5, x=10)
    on_gas, far, near = _worker(1, 10, from_gas=5), _worker(2, 100), _worker(3, 12)
    state = _state([on_gas, far, near], [gas])
    assert state.non_gas_worker_near(gas) is near


def test_counts_come_from_the_game_not_from_order_targets():
    """The whole point of design A. Three probes on a geyser, ALL mid-return: their order
    target is the Nexus and their geyser is unknowable, so reconstructing the count from
    workers gives 0 — which is what made the fill loop shove probes in every frame until
    the geyser held 7 against a target of 3."""
    gas = _geyser(5, x=10, assigned=3)
    state = _state([_returning(i, 9) for i in (1, 2, 3)], [gas])
    assert state.num_gas_workers() == 3


def test_returning_workers_are_not_available_to_send_to_gas():
    """They're already on gas. Offering them up is what produced the re-send-every-frame
    symptom, since a returning probe is standing right next to the geyser."""
    gas = _geyser(5, x=10, assigned=1)
    state = _state([_returning(1, 10)], [gas])
    assert state.num_non_gas_workers() == 0
    assert state.non_gas_worker_near(gas) is None


def test_no_drain_when_every_worker_on_the_busiest_geyser_is_mid_return():
    """We only drain from the busiest geyser. If we can't name a worker on it, we drain
    nobody this frame rather than pulling one off a quieter geyser."""
    busy = _geyser(5, x=10, assigned=3)
    quiet = _geyser(6, x=20, assigned=1)
    state = _state([_returning(1, 9), _returning(2, 9), _returning(3, 9),
                    _worker(4, 19, from_gas=6)], [busy, quiet])
    assert state.most_busy_gas() is busy
    assert state.worker_mining_from(busy) is None


def test_ties_for_busiest_prefer_a_geyser_we_can_actually_drain():
    """Both hold 2. One has only returning workers; picking it would refuse to drain at
    all, so the tie-break favours the drainable one."""
    undrainable = _geyser(5, x=10, assigned=2)
    drainable = _geyser(6, x=20, assigned=2)
    state = _state([_returning(1, 9), _returning(2, 9),
                    _worker(3, 19, from_gas=6), _worker(4, 21, from_gas=6)],
                   [undrainable, drainable])
    assert state.most_busy_gas() is drainable


def test_pulling_a_worker_off_decrements_that_geyser():
    gas = _geyser(5, x=10, assigned=3)
    w = _worker(1, 10, from_gas=5)
    state = _state([w, _returning(2, 9), _returning(3, 9)], [gas])
    state.send_worker(w, _field())
    assert state.num_gas_workers() == 2


# ------------------------------------------------------------------ manage_economy
def _econ_bot(workers, *, gas=(), gas_target=0, populating=None, excluded=()):
    return fake_bot(
        workers=FakeUnits(workers),
        _excluded_tags=lambda: set(excluded),
        gas_buildings=FakeUnits(gas),
        gas_target=gas_target,
        _populating_field=lambda: populating,
    )


async def test_idle_workers_are_sent_to_the_populating_base():
    idle = _worker(1, idle=True)
    field = _field()
    await BuildOrderBot.manage_economy(_econ_bot([idle], populating=field))
    idle.gather.assert_called_once_with(field)


async def test_workers_move_into_gas_up_to_the_target():
    miners = [_worker(i, i * 10) for i in (1, 2, 3)]
    gas = _geyser(5, x=0)
    await BuildOrderBot.manage_economy(
        _econ_bot(miners, gas=[gas], gas_target=2, populating=_field()))
    assert sum(1 for m in miners if m.gather.called) == 2


async def test_gas_fill_stops_when_we_run_out_of_workers():
    miner = _worker(1, 10)
    gas = _geyser(5, x=0)
    await BuildOrderBot.manage_economy(
        _econ_bot([miner], gas=[gas], gas_target=6, populating=_field()))
    miner.gather.assert_called_once_with(gas)


async def test_gas_fill_stops_when_we_own_no_geyser():
    """`gas_workers count: 3` before the Assimilator exists — the loop must notice there
    is nowhere to send anyone rather than spinning."""
    miners = [_worker(i, i * 10) for i in (1, 2)]
    await BuildOrderBot.manage_economy(
        _econ_bot(miners, gas=[], gas_target=3, populating=_field()))
    for m in miners:
        m.gather.assert_not_called()


async def test_surplus_gas_workers_are_sent_back_to_minerals():
    on_gas = [_worker(i, 10, from_gas=5) for i in (1, 2, 3)]
    gas = _geyser(5, x=10, assigned=3)
    field = _field()
    await BuildOrderBot.manage_economy(
        _econ_bot(on_gas, gas=[gas], gas_target=1, populating=field))
    moved = [w for w in on_gas if w.gather.called]
    assert len(moved) == 2, f"drained {len(moved)}, wanted 2"
    for w in moved:
        w.gather.assert_called_once_with(field)


async def test_a_probe_stuck_on_an_unfinished_assimilator_is_sent_back_to_minerals():
    """The builder gets auto-queued onto gas that isn't built yet and reads as
    'gathering', not idle — so nothing else would reclaim it."""
    stuck = _worker(1, 1, from_gas=5)
    building = _geyser(5, x=10, ready=False)
    field = _field()
    await BuildOrderBot.manage_economy(
        _econ_bot([stuck], gas=[building], populating=field))
    stuck.gather.assert_called_once_with(field)


async def test_sent_and_prewalking_probes_are_left_alone():
    """A scout in the enemy main and a probe walking to a build site are excluded, or the
    never-idle branch drags them home to mine."""
    miner, scout = _worker(1, 1, idle=True), _worker(2, 50, idle=True)
    field = _field()
    await BuildOrderBot.manage_economy(
        _econ_bot([miner, scout], populating=field, excluded={2}))
    miner.gather.assert_called_once_with(field)
    scout.gather.assert_not_called()


# ------------------------------------------------------------------ base fields
def _field_bot(fields_by_base, **over):
    base = dict(mineral_field=fake_bot(
        closer_than=lambda d, b: FakeUnits(fields_by_base.get(b.tag, []))))
    base.update(over)
    return fake_bot(**base)


def test_base_field_returns_a_patch_at_that_base():
    base = fake_unit(tag=1)
    patch = _field(7)
    assert BuildOrderBot._base_field(_field_bot({1: [patch]}), base) is patch


def test_base_field_is_none_when_the_base_is_mined_out():
    base = fake_unit(tag=1)
    assert BuildOrderBot._base_field(_field_bot({}), base) is None


def test_populating_field_uses_the_designated_base():
    """`rally_and_transfer_probes base: N` is the only control over where homeless probes
    mine — there is no automatic spill to another base."""
    main, natural = fake_unit(tag=1), fake_unit(tag=2)
    patch = _field(7)
    fake = _field_bot({2: [patch]},
                      _ordered_bases=lambda: FakeUnits([main, natural]),
                      populating_base_num=2)
    fake._base_field = lambda b: BuildOrderBot._base_field(fake, b)
    assert BuildOrderBot._populating_field(fake) is patch
