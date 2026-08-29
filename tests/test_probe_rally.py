"""Tier 1 — probe dispatch and rally handlers: send_probe / return_probe / rally /
rally_and_transfer_probes. These are about which worker gets picked and where it's told
to go, plus the rally-target state; tested unbound with stubbed selection helpers.
"""

from __future__ import annotations

from bot import BuildOrderBot
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from fakes import FakeUnits, fake_bot, fake_unit

DEST = object()  # sentinel returned by a stubbed _resolve_place


# ------------------------------------------------------------------ _free_probe_near
def test_free_probe_near_reuses_idle_probe_at_target():
    # A probe that just gave up / finished right AT the target should be reused
    # instead of pulling a gathering probe from home (which sent a second probe
    # across the map to re-build an enemy/proxy pylon).
    target = Point2((100.0, 100.0))
    idle_at_target = fake_unit(tag=1, position=Point2((101.0, 100.0)),
                               is_idle=True, is_gathering=False, is_carrying_minerals=False,
                               is_carrying_vespene=False, order_target=None)
    gathering_home = fake_unit(tag=2, position=Point2((10.0, 10.0)),
                               is_idle=False, is_gathering=True, is_carrying_minerals=False,
                               is_carrying_vespene=False, order_target=None)
    fake = fake_bot(workers=FakeUnits([idle_at_target, gathering_home]),
                    _probes_unavailable_to_automation=lambda: set(), townhalls=FakeUnits(),
                    gas_buildings=FakeUnits(), home_base_by_builder={})
    assert BuildOrderBot._free_probe_near(fake, target).tag == 1


def _probe(tag, x, *, idle=False, gathering=True, minerals=False, vespene=False, target=None):
    return fake_unit(tag=tag, position=Point2((float(x), 0.0)), is_idle=idle,
                     is_gathering=gathering, is_carrying_minerals=minerals,
                     is_carrying_vespene=vespene, order_target=target)


def _pool_bot(workers, gasses=()):
    return fake_bot(workers=FakeUnits(workers), _probes_unavailable_to_automation=lambda: set(),
                    townhalls=FakeUnits(), gas_buildings=FakeUnits(gasses),
                    home_base_by_builder={})


def test_wont_pull_a_probe_carrying_vespene():
    """Pulling a loaded probe throws the load away — same reason we skip mineral carriers.
    It also used to leave the probe holding vespene for the whole build, which then read
    as a live gas worker long after it had stopped being one."""
    loaded = _probe(1, 1, gathering=False, idle=True, vespene=True)
    empty = _probe(2, 50)
    assert BuildOrderBot._free_probe_near(_pool_bot([loaded, empty]), Point2((0.0, 0.0))).tag == 2


def test_wont_pull_a_probe_that_is_mining_gas():
    """Gas assignment is deliberate. Stealing a gas probe just makes manage_economy refill
    the geyser from minerals next frame — a mineral probe would have been pulled anyway,
    plus a wasted round trip."""
    geyser = fake_unit(tag=5, is_ready=True)
    on_gas = _probe(1, 1, target=5)
    on_minerals = _probe(2, 50)
    bot = _pool_bot([on_gas, on_minerals], gasses=[geyser])
    assert BuildOrderBot._free_probe_near(bot, Point2((0.0, 0.0))).tag == 2


def test_no_probe_rather_than_a_bad_one():
    """Single tier: if nobody qualifies we return None and the caller retries. The old
    'else anyone at all' fallback is what let a mid-return probe get picked."""
    bot = _pool_bot([_probe(1, 1, gathering=False, idle=False, vespene=True)])
    assert BuildOrderBot._free_probe_near(bot, Point2((0.0, 0.0))) is None


# ------------------------------------------------------------------ send_probe
async def test_send_probe_reuses_named_probe():
    existing = fake_unit(tag=1)
    fake = fake_bot(_resolve_place=lambda w: DEST, named_probes={"scout": 1}, home_base_by_builder={},
                    _worker_by_name=lambda name: existing, _free_probe_near=lambda p: None)
    assert await BuildOrderBot.do_send_probe(fake, fake_bot(where="proxy", who="scout")) is True
    existing.move.assert_called_once_with(DEST)
    assert fake.named_probes == {"scout": 1}  # unchanged — same probe reused


async def test_send_probe_pulls_and_registers_fresh_probe():
    fresh = fake_unit(tag=7)
    fake = fake_bot(_resolve_place=lambda w: DEST, named_probes={}, home_base_by_builder={},
                    _worker_by_name=lambda name: None, _free_probe_near=lambda p: fresh)
    assert await BuildOrderBot.do_send_probe(fake, fake_bot(where="proxy", who="scout")) is True
    assert fake.named_probes == {"scout": 7}  # registered so it's held out of automation
    fresh.move.assert_called_once_with(DEST)


async def test_send_probe_holds_when_no_probe_available():
    fake = fake_bot(_resolve_place=lambda w: DEST, named_probes={}, home_base_by_builder={},
                    _worker_by_name=lambda name: None, _free_probe_near=lambda p: None)
    assert await BuildOrderBot.do_send_probe(fake, fake_bot(where="proxy", who="scout")) is False


def _named_probe_bot(pool):
    """Fake bot that drives the REAL _worker_by_name / _worker_by_tag lookups (rather
    than stubbing them), so the name -> live-probe reuse path is genuinely exercised."""
    fake = fake_bot(_resolve_place=lambda w: DEST, named_probes={}, home_base_by_builder={}, workers=FakeUnits(pool))
    fake._worker_by_tag = lambda tag: BuildOrderBot._worker_by_tag(fake, tag)
    fake._worker_by_name = lambda name: BuildOrderBot._worker_by_name(fake, name)
    return fake


async def test_resending_a_name_moves_the_same_probe():
    """The tour case (test_probe_naming.yaml re-sends `scout1` eight times): every
    re-send must MOVE the probe already bound to the name. Silently pulling a fresh
    one each time still looks correct from outside — the probe tours, the build
    finishes — while stripping workers off minerals one leg at a time."""
    scout, spare = fake_unit(tag=1), fake_unit(tag=2)
    pulls = []
    fake = _named_probe_bot([scout, spare])
    fake._free_probe_near = lambda pos: (pulls.append(pos), scout)[1]

    step = fake_bot(where="proxy", who="scout")
    for _ in range(3):
        assert await BuildOrderBot.do_send_probe(fake, step) is True

    assert len(pulls) == 1, f"re-send pulled a fresh probe instead of reusing: {len(pulls)} pulls"
    assert fake.named_probes == {"scout": 1}
    assert scout.move.call_count == 3
    spare.move.assert_not_called()


async def test_resend_rebinds_when_the_named_probe_died():
    # tag 1 is bound but no longer among our workers -> fall back to a fresh probe
    replacement = fake_unit(tag=2)
    fake = _named_probe_bot([replacement])
    fake.named_probes = {"scout": 1}
    fake._free_probe_near = lambda pos: replacement
    assert await BuildOrderBot.do_send_probe(fake, fake_bot(where="proxy", who="scout")) is True
    assert fake.named_probes == {"scout": 2}
    replacement.move.assert_called_once_with(DEST)


# ------------------------------------------------------------------ return_probe
async def test_return_probe_sends_it_back_to_mining():
    field = object()
    worker = fake_unit()
    fake = fake_bot(named_probes={"scout": 7},
                    _worker_by_tag=lambda t: worker if t == 7 else None,
                    _populating_field=lambda: field)
    assert await BuildOrderBot.do_return_probe(fake, fake_bot(who="scout")) is True
    assert "scout" not in fake.named_probes  # handed back to automation
    worker.gather.assert_called_once_with(field)


async def test_return_probe_noop_for_unknown_name():
    fake = fake_bot(named_probes={}, _worker_by_tag=lambda t: None, _populating_field=lambda: None)
    assert await BuildOrderBot.do_return_probe(fake, fake_bot(who="ghost")) is True


# ------------------------------------------------------------------ rally
async def test_rally_sets_target():
    fake = fake_bot(_resolve_place=lambda w: DEST, rally_point=None, _rallied={1, 2, 3})
    assert await BuildOrderBot.do_set_rally_point(fake, fake_bot(where="natural")) is True
    assert fake.rally_point is DEST
    assert fake._rallied == set(), "must forget who's set, so existing producers get re-issued"


# ------------------------------------------------------------------ rally_and_transfer_probes
async def test_rally_and_transfer_probes_does_not_wait_for_the_nexus():
    """The build says populate base N, so we do. Probes sent to that base's minerals will
    be mining by the time the Nexus finishes — no reason to stall on owning it.
    Also ensures unready/in-construction Nexuses receive the rally command."""
    field = object()
    nexus = fake_unit(is_ready=False)
    fake = fake_bot(
        base_position=lambda n: Point2((100.0, 100.0)),
        _base_field=lambda pos: field,
        ordered_bases=lambda: FakeUnits(),        # we own nothing at base 3 yet
        populating_base_num=1,
        townhalls=lambda t: FakeUnits([nexus]),
        workers=FakeUnits([]),
        _probes_unavailable_to_automation=lambda: set(),
    )
    assert await BuildOrderBot.do_rally_and_transfer_probes(fake, fake_bot(base=3)) is True
    assert fake.populating_base_num == 3
    nexus.assert_called_once_with(AbilityId.RALLY_WORKERS, field)


async def test_rally_and_transfer_probes_skips_the_target_base_when_transferring():
    """Excess comes from the OTHER bases — the target is identified by position now, so
    a Nexus sitting on it must not be treated as a donor."""
    field = object()
    at_target = fake_unit(tag=1, position=Point2((100.0, 100.0)),
                          assigned_harvesters=99, ideal_harvesters=16)
    donor = fake_unit(tag=2, position=Point2((10.0, 10.0)),
                      assigned_harvesters=0, ideal_harvesters=16)
    nexus = fake_unit()
    fake = fake_bot(
        base_position=lambda n: Point2((100.0, 100.0)),
        _base_field=lambda pos: field,
        ordered_bases=lambda: FakeUnits([at_target, donor]),
        populating_base_num=1,
        townhalls=lambda t: FakeUnits([nexus]),
        workers=FakeUnits([]),
        _probes_unavailable_to_automation=lambda: set(),
    )
    assert await BuildOrderBot.do_rally_and_transfer_probes(fake, fake_bot(base=2)) is True
    # at_target is way over the cap; if it weren't skipped we'd try to drain it
    assert fake.populating_base_num == 2


async def test_transfer_caps_each_base_at_its_remaining_patches_not_a_constant():
    """ideal_harvesters is 2 per mineral patch STILL STANDING, so a base that has partly
    mined out sheds probes down to what it can still use. A fixed 16 would leave them
    sitting on a base with nothing left to mine."""
    field = object()
    target = fake_unit(tag=1, position=Point2((100.0, 100.0)),
                       assigned_harvesters=0, ideal_harvesters=16)
    # 5 patches left -> ideal 10, and 13 probes on it: 3 too many
    mined_out = fake_unit(tag=2, position=Point2((10.0, 10.0)),
                          assigned_harvesters=13, ideal_harvesters=10)
    movers = [fake_unit(tag=10 + i, position=Point2((10.0, 10.0)), is_gathering=True,
                        is_carrying_minerals=False) for i in range(5)]
    fake = fake_bot(
        base_position=lambda n: Point2((100.0, 100.0)),
        _base_field=lambda pos: field,
        ordered_bases=lambda: FakeUnits([target, mined_out]),
        populating_base_num=1,
        townhalls=lambda t: FakeUnits([fake_unit()]),
        workers=FakeUnits(movers),
        _probes_unavailable_to_automation=lambda: set(),
    )
    assert await BuildOrderBot.do_rally_and_transfer_probes(fake, fake_bot(base=1)) is True
    moved = [w for w in movers if w.gather.called]
    assert len(moved) == 3, f"expected 13-10=3 moved, got {len(moved)}"
