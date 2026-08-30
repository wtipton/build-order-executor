"""Tier 1 — the two confirm/baseline state machines: do_build and do_warp, plus the
probe assignment do_build leans on (_assign_builder).

The state machines share a pattern: issuing an order != it happening, so the handler
captures a baseline count when the step starts, HOLDS (returns False, re-issuing as
needed) until the real count grows past that baseline, and only then returns True.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from bot import BuildOrderBot
from catalog import PYLON_POWER_RADIUS, WARP_ABILITY
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.position import Point2

from fakes import FakeUnits, fake_bot, fake_unit
from state import BuilderState, StepState


# ============================================================ do_build
def _build_step(*, what="Pylon", count=None, **over):
    """A build step as the schema would hand it over: every field do_build reads is
    present, so a missing one shows up as a test bug rather than an AttributeError."""
    return fake_bot(**{"what": what, "who": None, "count": count, **over})


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
    step = _build_step()
    fake = _build_bot(struct_amount=0, afford=False)
    assert await BuildOrderBot.do_build(fake, step) is False
    # baseline captured for this step (None -> 0 means we're now in flight)
    assert fake.builder_state.baseline == 0 and fake.builder_state.issued is False


def _in_flight(fake, step, *, baseline, tag=7):
    """Mid-build: both baselines were captured on an earlier frame. They're taken together
    on the step's first frame, so setting only one is a state the bot can't be in."""
    fake.step_state.build.baseline = baseline   # the whole step's, fixed
    fake.builder_state.baseline = baseline      # this structure's, re-taken per structure
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, tag


async def test_build_confirms_when_structure_appears_and_frees_the_probe():
    step = _build_step()
    fake = _build_bot(struct_amount=1, afford=True)
    _in_flight(fake, step, baseline=0)  # in progress, and one has now appeared (amount 1 > 0)
    assert await BuildOrderBot.do_build(fake, step) is True
    # builder_state spans steps, so the probe must be released here or it never mines again
    assert fake.builder_state.for_step is None and fake.builder_state.builder_tag is None


async def test_build_issues_to_the_assigned_probe():
    probe = fake_unit(tag=99)
    step = _build_step()
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
    step = _build_step()
    fake = _build_bot(struct_amount=0, afford=True, builder=walking)
    fake.builder_state.baseline, fake.builder_state.issued = 0, True
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 7
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_not_awaited()


async def test_build_reissues_when_the_builder_dropped_the_order():
    # issued, but the probe is back to gathering and no structure appeared: the order
    # was dropped (a probe on the tile blocks placement), so issue it again.
    idle = fake_unit(tag=99, is_idle=False, is_gathering=True)
    step = _build_step()
    fake = _build_bot(struct_amount=0, afford=True, builder=idle)
    fake.builder_state.baseline, fake.builder_state.issued = 0, True
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 99
    fake.builder_state.spot = Point2((50.0, 50.0))
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_awaited_once()
    assert fake.builder_state.issued is True


async def test_build_holds_when_no_probe_could_be_assigned():
    step = _build_step()
    fake = _build_bot(struct_amount=0, afford=True, assigned=False)
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_not_awaited()


async def test_build_releases_the_assignment_when_the_builder_dies():
    # assigned, but the tag no longer resolves to a live probe -> drop the assignment
    # so manage_builder picks a fresh one, rather than stalling on a ghost forever.
    step = _build_step()
    fake = _build_bot(struct_amount=0, afford=True, builder=None)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 7
    assert await BuildOrderBot.do_build(fake, step) is False
    fake.build.assert_not_awaited()
    assert fake.builder_state.for_step is None
    assert fake._status == "builder for Pylon died"


def _amount(fake, n):
    """Set how many of the structure type we own right now."""
    fake.structures = lambda t, n=n: FakeUnits([object()] * n)


async def test_build_count_holds_after_each_one_until_the_last():
    # `count: 3` puts them up one after another; the step only completes on the third.
    step = _build_step(count=3)
    fake = _build_bot(struct_amount=0, afford=True)
    assert await BuildOrderBot.do_build(fake, step) is False  # captures both baselines at 0

    for built in (1, 2):
        _amount(fake, built)
        assert await BuildOrderBot.do_build(fake, step) is False, f"{built}/3 — must hold"
        assert fake.step_state.build.baseline == 0, "the step's baseline must NOT be re-taken"
        await BuildOrderBot.do_build(fake, step)  # next frame re-baselines the builder

    _amount(fake, 3)
    assert await BuildOrderBot.do_build(fake, step) is True


async def test_build_count_keeps_the_same_probe_for_the_whole_group():
    # A `count:` is one errand for one probe. Releasing it between structures would drop it
    # out of _probes_unavailable_to_automation, so manage_economy would walk it back to a
    # patch and the next structure would pull whichever probe happened to be nearest.
    step = _build_step(count=3)
    fake = _build_bot(struct_amount=0, afford=True, builder=fake_unit(tag=77))
    await BuildOrderBot.do_build(fake, step)                     # baselines at 0
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 77
    fake.builder_state.spot, fake.builder_state.issued = Point2((5.0, 5.0)), True

    _amount(fake, 1)  # first structure lands, two still owed
    assert await BuildOrderBot.do_build(fake, step) is False
    assert fake.builder_state.builder_tag == 77, "the probe must be held for the next one"
    assert fake.builder_state.for_step is step
    assert fake.builder_state.spot is None, "but re-targeted: a fresh spot for the next"

    _amount(fake, 3)  # the group finishes
    assert await BuildOrderBot.do_build(fake, step) is True
    assert fake.builder_state.builder_tag is None, "released once the group is done"


async def test_build_count_walks_to_the_next_spot_without_waiting_for_money():
    # The pre-walk gate is a once-per-step decision about taking a probe off minerals. The
    # probe is already off, so once one building is up it heads for the next spot right
    # away and we save up while it walks — it must not stand at the finished building.
    step = _build_step(count=3)
    fake = _build_bot(struct_amount=0, afford=True, builder=fake_unit(tag=77))
    await BuildOrderBot.do_build(fake, step)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 77
    fake.builder_state.spot, fake.builder_state.issued = Point2((5.0, 5.0)), True

    fake.can_afford = lambda u: False   # broke: can't pay for the next one yet
    fake._assign_builder.reset_mock()   # ignore the assignment for the first structure
    _amount(fake, 1)
    assert await BuildOrderBot.do_build(fake, step) is False
    fake._assign_builder.assert_awaited_once()  # re-targeted despite being unable to pay


async def test_build_count_measures_structures_not_confirm_events():
    # The re-issue guard can duplicate a build that had in fact landed, so the count can
    # grow by 2 at once. Counting that as one confirm would leave the step owing one too
    # many and put up `count + 1`. Measured from the step's baseline, 2 counts as 2.
    step = _build_step(count=2)
    fake = _build_bot(struct_amount=0, afford=True)
    assert await BuildOrderBot.do_build(fake, step) is False  # baselines at 0
    _amount(fake, 2)                                          # both appeared at once
    assert await BuildOrderBot.do_build(fake, step) is True, "2 of 2 up — must not order a third"


async def test_build_count_baseline_outlives_each_structure():
    # BuilderState.baseline is re-taken per structure — the very event `count` is measured
    # against — so the step's baseline has to live outside it. Pinned against a refactor.
    step = _build_step(count=2)
    fake = _build_bot(struct_amount=0, afford=True)
    await BuildOrderBot.do_build(fake, step)
    _amount(fake, 1)
    assert await BuildOrderBot.do_build(fake, step) is False
    assert fake.builder_state.baseline is None, "this structure's baseline is reset"
    assert fake.step_state.build.baseline == 0, "the step's baseline survived"


async def test_build_gas_uses_build_gas_not_build():
    # an Assimilator's spot is the geyser Unit, and it goes through Unit.build_gas
    geyser, probe = fake_unit(tag=1), fake_unit(tag=99)
    step = _build_step(what="Assimilator")
    fake = _build_bot(struct_amount=0, afford=True, builder=probe)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 99
    fake.builder_state.spot = geyser
    assert await BuildOrderBot.do_build(fake, step) is False
    probe.build_gas.assert_called_once_with(geyser)
    fake.build.assert_not_awaited()


# ============================================================ _assign_builder
def _assign_bot(*, spot=Point2((50.0, 50.0)), free=None, named=None, held=None, **over):
    base = dict(
        builder_state=BuilderState(),
        placement=fake_bot(find_building_location=AsyncMock(return_value=spot)),
        _free_probe_near=lambda pos: free,
        _worker_by_name=lambda name: named,
        _worker_by_tag=lambda tag: held,   # the probe held across a `count:` group
        _log_builder=MagicMock(), _status="",
    )
    base.update(over)
    return fake_bot(**base)


async def test_assign_pulls_a_probe_and_walks_it_to_the_spot():
    probe = fake_unit(tag=99)
    step = _build_step(at="supply>=20")
    fake = _assign_bot(free=probe)
    assert await BuildOrderBot._assign_builder(fake, step) is True
    assert fake.builder_state.for_step is step
    assert fake.builder_state.builder_tag == 99
    assert fake.builder_state.spot == Point2((50.0, 50.0))
    probe.move.assert_called_once_with(Point2((50.0, 50.0)))


async def test_assign_is_idempotent_for_the_same_step():
    # do_build calls this every frame; it must not re-issue a move or re-pick a probe
    probe = fake_unit(tag=99)
    step = _build_step(at="supply>=20")
    fake = _assign_bot(free=probe)
    await BuildOrderBot._assign_builder(fake, step)
    probe.move.reset_mock()
    assert await BuildOrderBot._assign_builder(fake, step) is True
    probe.move.assert_not_called()


async def test_assign_prefers_the_named_probe():
    named, pool = fake_unit(tag=1), fake_unit(tag=2)
    step = _build_step(who="scout", at="time>=250")
    fake = _assign_bot(free=pool, named=named)
    assert await BuildOrderBot._assign_builder(fake, step) is True
    assert fake.builder_state.builder_tag == 1


async def test_assign_falls_back_to_the_pool_when_the_named_probe_died():
    # a dead scout must not hang the build order forever
    pool = fake_unit(tag=2)
    step = _build_step(who="scout", at="time>=250")
    fake = _assign_bot(free=pool, named=None)
    assert await BuildOrderBot._assign_builder(fake, step) is True
    assert fake.builder_state.builder_tag == 2


async def test_assign_reuses_the_held_probe_mid_group():
    # spot cleared but builder_tag kept = between two structures of a `count:` group. The
    # held probe walks on to the next spot; pulling a fresh one would strand it.
    held, pool = fake_unit(tag=1), fake_unit(tag=2)
    step = _build_step(count=3, at="time>=1")
    fake = _assign_bot(free=pool, held=held)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 1
    assert await BuildOrderBot._assign_builder(fake, step) is True
    assert fake.builder_state.builder_tag == 1
    held.move.assert_called_once_with(Point2((50.0, 50.0)))
    pool.move.assert_not_called()


async def test_assign_re_targets_when_the_spot_was_cleared():
    # the idempotence check must require a SPOT as well as a probe, or a mid-group
    # assignment short-circuits and do_build builds at a stale/None spot
    held = fake_unit(tag=1)
    step = _build_step(count=2, at="time>=1")
    fake = _assign_bot(held=held)
    fake.builder_state.for_step, fake.builder_state.builder_tag = step, 1
    fake.builder_state.spot = None
    assert await BuildOrderBot._assign_builder(fake, step) is True
    assert fake.builder_state.spot == Point2((50.0, 50.0))


async def test_assign_fails_without_a_placement_or_a_probe():
    step = _build_step(at="supply>=20")
    no_spot = _assign_bot(spot=None, free=fake_unit(tag=99))
    assert await BuildOrderBot._assign_builder(no_spot, step) is False
    assert no_spot.builder_state.for_step is None

    no_probe = _assign_bot(free=None)
    assert await BuildOrderBot._assign_builder(no_probe, step) is False
    assert no_probe.builder_state.for_step is None
    assert no_probe._status == "no free probe to build with"


# ============================================================ do_warp
def _warp_bot(*, warping=(), afford=True, warpgates=(), ready_abilities=(), tiles=(), **over):
    """`warping` = tags of units of the step's type that are mid-warp (build_progress < 1).
    A trained unit never looks like this — it appears complete — which is what lets do_warp
    tell its own warp-ins from whatever a Gateway finished."""
    ability = WARP_ABILITY[U.ZEALOT]
    units = [fake_unit(tag=t, build_progress=0.5) for t in warping]
    base = dict(
        step_state=StepState(),
        units=lambda t: FakeUnits(units),
        can_afford=lambda u: afford,
        structures=lambda t: FakeUnits(list(warpgates)),
        get_available_abilities=AsyncMock(return_value=[ready_abilities for _ in warpgates]),
        _warp_pylon=lambda where: fake_unit(position=Point2((10.0, 10.0))),
        # the real one is exercised by the _warp_tiles tests below
        _warp_tiles=AsyncMock(side_effect=lambda ability, pylon, needed: list(tiles)[:needed]),
        _status="",
    )
    base.update(over)
    return fake_bot(**base), ability


def _zealot_step(count=None):
    return fake_bot(what="Zealot", where="proxy", count=count)


def _mid_step(fake, *, already_warped=()):
    """Past the step's first frame: nothing was in flight when it started, and these tags
    have been warped by it so far."""
    fake.step_state.warp.warping_at_start = set()
    fake.step_state.warp.warped_in = set(already_warped)


async def test_warp_confirms_when_unit_appears():
    fake, _ = _warp_bot(warping=[1])
    _mid_step(fake)
    assert await BuildOrderBot.do_warp(fake, _zealot_step()) is True


async def test_warp_holds_when_no_warpgate():
    fake, _ = _warp_bot(warpgates=[])
    assert await BuildOrderBot.do_warp(fake, _zealot_step()) is False


async def test_warp_holds_when_all_on_cooldown():
    wg = fake_unit()
    fake, _ = _warp_bot(warpgates=[wg], ready_abilities=[])
    assert await BuildOrderBot.do_warp(fake, _zealot_step()) is False


async def test_warp_issues_when_gate_ready_and_tile_free():
    wg = fake_unit()
    pos = Point2((11.0, 11.0))
    fake, _ = _warp_bot(warpgates=[wg], ready_abilities=[WARP_ABILITY[U.ZEALOT]], tiles=[pos])
    assert await BuildOrderBot.do_warp(fake, _zealot_step()) is False  # issued, holds for confirm
    wg.warp_in.assert_called_once_with(U.ZEALOT, pos)


async def test_warp_holds_when_every_tile_is_blocked():
    # nothing clear to warp onto: say so, instead of issuing orders the game refuses
    wg = fake_unit()
    fake, _ = _warp_bot(warpgates=[wg], ready_abilities=[WARP_ABILITY[U.ZEALOT]], tiles=[])
    assert await BuildOrderBot.do_warp(fake, _zealot_step()) is False
    wg.warp_in.assert_not_called()
    assert fake._status == "nowhere clear to warp in at proxy"


async def test_warp_does_not_count_a_unit_the_gateway_trained():
    # The bug this design exists to prevent: a Gateway finishing a Zealot mid-step used to
    # count toward `count`, so the step warped fewer than asked. A trained unit appears at
    # build_progress 1, so it must never be picked up.
    fake, _ = _warp_bot(warpgates=[])
    _mid_step(fake)
    fake.units = lambda t: FakeUnits([fake_unit(tag=9, build_progress=1.0)])
    assert await BuildOrderBot.do_warp(fake, _zealot_step(count=1)) is False
    assert fake.step_state.warp.warped_in == set()


async def test_warp_ignores_a_warp_still_in_flight_from_an_earlier_step():
    # first frame of the step, with a previous step's warp not yet landed
    fake, _ = _warp_bot(warping=[1], warpgates=[])
    assert await BuildOrderBot.do_warp(fake, _zealot_step(count=1)) is False
    assert fake.step_state.warp.warped_in == set()
    assert fake.step_state.warp.warping_at_start == {1}


async def test_warp_count_fills_a_round_across_every_ready_gate():
    # `count: 4` is a warp ROUND: one unit per gate, all in this frame — and each onto its
    # OWN tile, since two gates sent to one tile means at most one of them lands.
    gates = [fake_unit(tag=i) for i in range(4)]
    tiles = [Point2((float(i), 11.0)) for i in range(4)]
    fake, _ = _warp_bot(warpgates=gates, ready_abilities=[WARP_ABILITY[U.ZEALOT]], tiles=tiles)
    assert await BuildOrderBot.do_warp(fake, _zealot_step(count=4)) is False
    used = [g.warp_in.call_args.args[1] for g in gates]
    assert sorted(used, key=lambda p: p.x) == tiles, "each gate needs its own tile"


async def test_warp_count_never_exceeds_what_is_still_outstanding():
    # 3 of the 4 already warped, so this frame warps ONE even though 4 gates are free
    gates = [fake_unit(tag=i) for i in range(4)]
    fake, _ = _warp_bot(warpgates=gates, ready_abilities=[WARP_ABILITY[U.ZEALOT]],
                        tiles=[Point2((11.0, 11.0))])
    _mid_step(fake, already_warped=[101, 102, 103])
    assert await BuildOrderBot.do_warp(fake, _zealot_step(count=4)) is False
    assert sum(g.warp_in.call_count for g in gates) == 1


async def test_warp_count_completes_only_once_every_unit_has_appeared():
    # measured from the units, never from the orders we issued — a dropped warp order
    # has to leave the step unfinished so the next frame re-issues it
    fake, _ = _warp_bot(warpgates=[])
    _mid_step(fake, already_warped=[101, 102, 103])
    assert await BuildOrderBot.do_warp(fake, _zealot_step(count=4)) is False, "3 of 4 — not done"
    fake.units = lambda t: FakeUnits([fake_unit(tag=104, build_progress=0.5)])
    assert await BuildOrderBot.do_warp(fake, _zealot_step(count=4)) is True


# ============================================================ _warp_tiles
def _tiles_bot(*, blockers=(), unpowered=(), unplaceable=()):
    """A pylon at (0,0). Candidate tiles are CENTRES, so they read (x+0.5, y+0.5).

    `blockers` are (position, radius) pairs — a unit blocks a tile when its BODY reaches
    it, so the radius is the whole point. `unpowered`/`unplaceable` are tile centres.
    """
    unpowered, unplaceable = set(unpowered), set(unplaceable)
    units = FakeUnits([fake_unit(position=Point2(pos), radius=r) for pos, r in blockers])
    fake = fake_bot(
        state=fake_bot(psionic_matrix=fake_bot(covers=lambda p: (p.x, p.y) not in unpowered)),
        can_place=AsyncMock(side_effect=lambda ability, ps: [(p.x, p.y) not in unplaceable
                                                            for p in ps]),
        all_units=units,
    )
    # the real blocker test, so these exercise the production radius arithmetic
    fake._blocked_for_warp = lambda t: BuildOrderBot._blocked_for_warp(fake, t)
    return fake


async def _tiles(fake, needed=4):
    return await BuildOrderBot._warp_tiles(
        fake, WARP_ABILITY[U.ZEALOT], fake_unit(position=Point2((0.0, 0.0))), needed)


async def test_warp_tiles_are_centres_not_corners():
    # Offsetting by whole tiles from the pylon's position lands on its CORNERS — a 2x2
    # building sits on integer coordinates — so the nearest candidates were points on the
    # Pylon itself, and every warp there was refused.
    got = await _tiles(_tiles_bot(), needed=4)
    assert all(p.x % 1 == 0.5 and p.y % 1 == 0.5 for p in got), got


async def test_warp_tiles_skips_tiles_a_unit_is_standing_on():
    # The placement query calls a tile with a finished unit on it free, because a unit
    # would walk out of the way for a building. It won't for a warp-in.
    zealot = (Point2((0.5, 1.5)), 0.5)
    got = await _tiles(_tiles_bot(blockers=[zealot]))
    assert Point2((0.5, 1.5)) not in got


async def test_warp_tiles_skips_tiles_under_the_pylon_itself():
    # THE live bug. The Pylon that powers the warp has radius 1.12, and the nearest tiles
    # sit inside it. A flat centre-to-centre clearance let those through, we handed the
    # game a target on top of the Pylon, and — list being sorted nearest-first — re-picked
    # the same tile every frame forever.
    pylon = (Point2((0.0, 0.0)), 1.12)
    got = await _tiles(_tiles_bot(blockers=[pylon]), needed=8)
    assert all(p.distance_to(Point2((0.0, 0.0))) >= 1.12 for p in got), got


async def test_warp_tiles_scale_the_exclusion_to_the_blocker():
    # A Nexus (radius 2.75) blocks far more ground than a probe (0.375). One constant
    # cannot express both, which is why the check reads the blocker's own radius.
    nexus = (Point2((0.0, 0.0)), 2.75)
    got = await _tiles(_tiles_bot(blockers=[nexus]), needed=8)
    assert all(p.distance_to(Point2((0.0, 0.0))) >= 2.75 for p in got), got


async def test_warp_tiles_are_distinct_and_nearest_first():
    got = await _tiles(_tiles_bot(), needed=4)
    assert len(set(got)) == 4, "a round of gates must not be sent to the same tile twice"
    dists = [p.distance_to(Point2((0.0, 0.0))) for p in got]
    assert dists == sorted(dists)


async def test_warp_tiles_honours_power_and_terrain():
    # both filters still apply — power from the matrix, terrain from the placement query
    got = await _tiles(_tiles_bot(unpowered=[(0.5, 1.5)], unplaceable=[(1.5, 0.5)]), needed=8)
    flat = {(p.x, p.y) for p in got}
    assert (0.5, 1.5) not in flat and (1.5, 0.5) not in flat


async def test_warp_tiles_empty_when_everything_is_blocked():
    reach = int(PYLON_POWER_RADIUS)
    every = [(Point2((x + 0.5, y + 0.5)), 0.5) for x in range(-reach, reach + 1)
             for y in range(-reach, reach + 1)]
    assert await _tiles(_tiles_bot(blockers=every)) == []
