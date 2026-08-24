"""Tier 1 — the two confirm/baseline state machines: do_build and do_warp, plus the
placement dispatch do_build leans on (_find_build_target).

The state machines share a pattern: issuing an order != it happening, so the handler
captures a baseline count when the step starts, HOLDS (returns False, re-issuing as
needed) until the real count grows past that baseline, and only then returns True.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bot import BuildOrderBot
from catalog import WARP_ABILITY
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from fakes import FakeUnits, fake_bot, fake_unit
from state import BuilderState, StepState


# ============================================================ do_build
def _build_bot(*, struct_amount, afford, builder=None, assigned=True, **over):
    # do_build picks neither probe nor spot — _assign_builder owns both — so `assigned`
    # stubs its verdict and `builder` is whoever builder_state.builder_tag resolves to.
    base = dict(
        step_state=StepState(), builder_state=BuilderState(), _log_builder=MagicMock(),
        structures=lambda t: FakeUnits([object()] * struct_amount),
        can_afford=lambda u: afford,
        _worker_by_tag=lambda tag: builder,
        _assign_builder=AsyncMock(return_value=assigned),
        build=AsyncMock(),
        _status="",  # run_handler blanks it before every handler
    )
    base.update(over)
    return fake_bot(**base)


async def test_build_captures_baseline_on_first_sight_then_holds():
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=False)
    assert await BuildOrderBot.do_build(fake, step) is False
    # baseline captured for this step (None -> 0 means we're now in flight)
    assert fake.builder_state.baseline == 0 and fake.builder_state.issued is False


async def test_build_confirms_when_structure_appears_and_frees_the_probe():
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=1, afford=True)
    fake.builder_state.baseline = 0     # in progress, and one has now appeared (amount 1 > 0)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 7
    assert await BuildOrderBot.do_build(fake, step) is True
    # builder_state spans steps, so the probe must be released here or it never mines again
    assert fake.builder_state.for_step is None and fake.builder_state.builder_tag is None


async def test_build_issues_to_the_assigned_probe():
    probe = fake_unit(tag=99)
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=True, builder=probe)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 99
    fake.builder_state.spot = Point2((50.0, 50.0))
    assert await BuildOrderBot.do_build(fake, step) is False  # issued, holds for confirm
    fake.build.assert_awaited_once()
    assert fake.builder_state.issued is True


async def test_build_holds_without_reissue_while_builder_walks():
    # the assigned probe is still walking to / placing the build (not idle, not
    # gathering) — must NOT re-issue a duplicate.
    walking = fake_unit(is_idle=False, is_gathering=False)
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=True, builder=walking)
    fake.builder_state.baseline, fake.builder_state.issued = 0, True
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 7
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_not_awaited()


async def test_build_reissues_when_the_builder_dropped_the_order():
    # issued, but the probe is back to gathering and no structure appeared: the order
    # was dropped (a probe on the tile blocks placement), so issue it again.
    idle = fake_unit(tag=99, is_idle=False, is_gathering=True)
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=True, builder=idle)
    fake.builder_state.baseline, fake.builder_state.issued = 0, True
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 99
    fake.builder_state.spot = Point2((50.0, 50.0))
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_awaited_once()
    assert fake.builder_state.issued is True


async def test_build_holds_when_no_probe_could_be_assigned():
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=True, assigned=False)
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_not_awaited()


async def test_build_releases_the_assignment_when_the_builder_dies():
    # assigned, but the tag no longer resolves to a live probe -> drop the assignment
    # so manage_builder picks a fresh one, rather than stalling on a ghost forever.
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=True, builder=None)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 7
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_not_awaited()
    assert fake.builder_state.for_step is None
    assert fake._status == "builder for Pylon died"


async def test_build_gas_uses_build_gas_not_build():
    # an Assimilator's spot is the geyser Unit, and it goes through Unit.build_gas
    geyser, probe = fake_unit(tag=1), fake_unit(tag=99)
    step = fake_bot(what="Assimilator", label=None)
    fake = _build_bot(struct_amount=0, afford=True, builder=probe)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 99
    fake.builder_state.spot = geyser
    assert await BuildOrderBot.do_build(fake, step) is False
    probe.build_gas.assert_called_once_with(geyser)
    fake.build.assert_not_awaited()


# ============================================================ _assign_builder
def _assign_bot(*, spot=Point2((50.0, 50.0)), free=None, labelled=None, **over):
    base = dict(
        builder_state=BuilderState(),
        _find_build_target=AsyncMock(return_value=spot),
        _free_probe_near=lambda pos: free,
        _worker_by_label=lambda label: labelled,
        _log_builder=MagicMock(), _status="",
    )
    base.update(over)
    return fake_bot(**base)


async def test_assign_pulls_a_probe_and_walks_it_to_the_spot():
    probe = fake_unit(tag=99)
    step = fake_bot(what="Pylon", label=None, at="supply>=20")
    fake = _assign_bot(free=probe)
    assert await BuildOrderBot._assign_builder(fake, step) is True
    assert fake.builder_state.for_step is step
    assert fake.builder_state.builder_tag == 99
    assert fake.builder_state.spot == Point2((50.0, 50.0))
    probe.move.assert_called_once_with(Point2((50.0, 50.0)))


async def test_assign_is_idempotent_for_the_same_step():
    # do_build calls this every frame; it must not re-issue a move or re-pick a probe
    probe = fake_unit(tag=99)
    step = fake_bot(what="Pylon", label=None, at="supply>=20")
    fake = _assign_bot(free=probe)
    await BuildOrderBot._assign_builder(fake, step)
    probe.move.reset_mock()
    assert await BuildOrderBot._assign_builder(fake, step) is True
    probe.move.assert_not_called()


async def test_assign_prefers_the_labelled_probe():
    labelled, pool = fake_unit(tag=1), fake_unit(tag=2)
    step = fake_bot(what="Pylon", label="scout", at="time>=250")
    fake = _assign_bot(free=pool, labelled=labelled)
    assert await BuildOrderBot._assign_builder(fake, step) is True
    assert fake.builder_state.builder_tag == 1


async def test_assign_falls_back_to_the_pool_when_the_labelled_probe_died():
    # a dead scout must not hang the build order forever
    pool = fake_unit(tag=2)
    step = fake_bot(what="Pylon", label="scout", at="time>=250")
    fake = _assign_bot(free=pool, labelled=None)
    assert await BuildOrderBot._assign_builder(fake, step) is True
    assert fake.builder_state.builder_tag == 2


async def test_assign_fails_without_a_placement_or_a_probe():
    step = fake_bot(what="Pylon", label=None, at="supply>=20")
    no_spot = _assign_bot(spot=None, free=fake_unit(tag=99))
    assert await BuildOrderBot._assign_builder(no_spot, step) is False
    assert no_spot.builder_state.for_step is None

    no_probe = _assign_bot(free=None)
    assert await BuildOrderBot._assign_builder(no_probe, step) is False
    assert no_probe.builder_state.for_step is None
    assert no_probe._status == "no free probe to build with"


# ================================================== _find_build_target (Nexus)
def _nexus_target_bot(*, base_taken: bool, **over):
    base = dict(
        _resolve_place=lambda where: Point2((40.0, 40.0)),
        townhalls=MagicMock(closer_than=lambda d, p: FakeUnits([object()] if base_taken else [])),
        _status="",
    )
    base.update(over)
    return fake_bot(**base)


async def test_nexus_where_aims_at_the_exact_base_location():
    # NOT find_placement near it — an expansion even a tile off-centre from its
    # mineral patches mines slower for the rest of the game.
    fake = _nexus_target_bot(base_taken=False)
    step = fake_bot(what="Nexus", where="natural")
    assert await BuildOrderBot._find_build_target(fake, step) == Point2((40.0, 40.0))


async def test_nexus_where_stalls_when_that_base_is_already_ours():
    # the alternative is handing build() an occupied tile, which wedges the Nexus
    # into whatever spot find_placement can scrape together nearby
    fake = _nexus_target_bot(base_taken=True)
    step = fake_bot(what="Nexus", where="natural")
    assert await BuildOrderBot._find_build_target(fake, step) is None
    assert fake._status == "the natural is already ours"


async def test_nexus_without_where_reports_when_the_map_is_full():
    fake = _nexus_target_bot(base_taken=False, _find_next_expansion=lambda: None)
    step = fake_bot(what="Nexus", where=None)
    assert await BuildOrderBot._find_build_target(fake, step) is None
    assert fake._status == "every expansion is already taken"


# ============================================================ do_warp
def _warp_bot(*, unit_amount, afford, warpgates, ready_abilities, placement, **over):
    ability = WARP_ABILITY[U.ZEALOT]
    base = dict(
        step_state=StepState(),
        units=lambda t: FakeUnits([object()] * unit_amount),
        can_afford=lambda u: afford,
        structures=lambda t: FakeUnits(warpgates),
        get_available_abilities=AsyncMock(return_value=[ready_abilities for _ in warpgates]),
        _warp_pylon=lambda where: fake_unit(position=Point2((10.0, 10.0))),
        find_placement=AsyncMock(return_value=placement),
    )
    base.update(over)
    return fake_bot(**base), ability


def _zealot_step():
    return fake_bot(what="Zealot", where="proxy")


async def test_warp_confirms_when_unit_appears():
    step = _zealot_step()
    fake, _ = _warp_bot(unit_amount=1, afford=True, warpgates=[], ready_abilities=[], placement=None)
    fake.step_state.warp.baseline = 0
    assert await BuildOrderBot.do_warp(fake, step) is True


async def test_warp_holds_when_no_warpgate():
    fake, _ = _warp_bot(unit_amount=0, afford=True, warpgates=[], ready_abilities=[], placement=None)
    assert await BuildOrderBot.do_warp(fake, _zealot_step()) is False


async def test_warp_holds_when_all_on_cooldown():
    wg = fake_unit()
    fake, _ = _warp_bot(unit_amount=0, afford=True, warpgates=[wg], ready_abilities=[], placement=None)
    assert await BuildOrderBot.do_warp(fake, _zealot_step()) is False


async def test_warp_issues_when_gate_ready_and_tile_free():
    wg = fake_unit()
    ability = WARP_ABILITY[U.ZEALOT]
    pos = Point2((11.0, 11.0))
    fake, _ = _warp_bot(unit_amount=0, afford=True, warpgates=[wg],
                        ready_abilities=[ability], placement=pos)
    assert await BuildOrderBot.do_warp(fake, _zealot_step()) is False  # issued, holds for confirm
    wg.warp_in.assert_called_once_with(U.ZEALOT, pos)
