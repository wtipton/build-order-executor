"""Tier 1 — the two confirm/baseline state machines: do_build and do_warp.

Both share the pattern: issuing an order != it happening, so the handler captures
a baseline count when the step starts, HOLDS (returns False, re-issuing as needed)
until the real count grows past that baseline, and only then returns True.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bot import BuildOrderBot
from catalog import WARP_ABILITY
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from fakes import FakeUnits, fake_bot, fake_unit
from state import PrewalkState, StepState


# ============================================================ do_build
def _build_bot(*, struct_amount, afford, builder=None, **over):
    # builder = the probe already committed to this build (via step_state.build.builder_tag),
    # looked up by _worker_by_tag; None means no build in flight yet.
    base = dict(
        step_state=StepState(), prewalk_state=PrewalkState(), _log_builder=MagicMock(),
        structures=lambda t: FakeUnits([object()] * struct_amount),
        can_afford=lambda u: afford,
        _worker_by_tag=lambda tag: builder,
        _free_probe_near=lambda pos: fake_unit(tag=99),
        build=AsyncMock(),
        _find_build_target=AsyncMock(return_value=Point2((50.0, 50.0))),
    )
    base.update(over)
    return fake_bot(**base)


async def test_build_captures_baseline_on_first_sight_then_holds():
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=False)
    assert await BuildOrderBot.do_build(fake, step) is False
    # baseline captured for this step (None -> 0 means we're now in flight)
    assert fake.step_state.build.baseline == 0 and fake.step_state.build.builder_tag is None


async def test_build_confirms_when_structure_appears():
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=1, afford=True)
    fake.step_state.build.baseline = 0    # in progress, and one has now appeared (amount 1 > 0)
    assert await BuildOrderBot.do_build(fake, step) is True
    assert fake.prewalk_state.for_step is None  # reservation released


async def test_build_holds_without_reissue_while_builder_walks():
    # our committed probe is still walking to / placing the build (not idle, not
    # gathering) — must NOT re-issue a duplicate.
    walking = fake_unit(is_idle=False, is_gathering=False)
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=True, builder=walking)
    fake.step_state.build.baseline = 0
    fake.step_state.build.builder_tag = 7
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_not_awaited()


async def test_build_reissues_when_no_builder_committed():
    step = fake_bot(what="Pylon", label=None)
    fake = _build_bot(struct_amount=0, afford=True)   # step_state.build.builder_tag None
    # prewalk_state.for_step is None, i.e. not this step -> auto-select a builder
    assert await BuildOrderBot.do_build(fake, step) is False  # issued, holds for confirm
    fake.build.assert_awaited_once()
    assert fake.step_state.build.builder_tag == 99  # remembered the committed probe


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
