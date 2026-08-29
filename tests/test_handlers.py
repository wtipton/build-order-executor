"""Tier 1 — step-handler gating: each handler's hold-vs-proceed decision and the
call it issues, tested unbound against a fake `self` with stubbed SC2 surface.

This is the "also mock handler gating" tier: it pins the DECISION logic (when a
step holds the line vs. fires, and that it issues the right order), not whether
the SC2 API then behaves as expected — that's for integration.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import catalog
from bot import BuildOrderBot
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

from fakes import FakeUnits, fake_bot, fake_unit
from state import StepState


# ------------------------------------------------------------------ do_train
# NB: WHICH producer gets the unit is Scheduler's job now — see tests/test_scheduler.py.
# These cover only do_train's own decision: afford, delegate, issue.
def _train_bot(producer, afford=True):
    return fake_bot(can_afford=lambda u: afford, step_state=StepState(), _status="",
                    scheduler=SimpleNamespace(producer_for=lambda unit: producer))


def _zealot(count=None):
    return fake_bot(what="Zealot", count=count)


async def test_train_holds_when_cant_afford():
    fake = _train_bot(fake_unit(tag=1), afford=False)
    assert await BuildOrderBot.do_train(fake, _zealot()) is False


async def test_train_holds_when_every_producer_queue_is_full():
    # scheduler returning None means "no producer with queue space" — ordering past the
    # game's limit is silently dropped, so the step must stall instead.
    fake = _train_bot(None)
    assert await BuildOrderBot.do_train(fake, _zealot()) is False


async def test_train_issues_to_the_producer_the_scheduler_picked():
    gw = fake_unit(tag=1)
    fake = _train_bot(gw)
    assert await BuildOrderBot.do_train(fake, _zealot()) is True
    gw.train.assert_called_once_with(U.ZEALOT)


async def test_train_count_orders_them_all_in_one_frame():
    # what a human does holding the hotkey: the whole batch goes out this frame if the
    # money and the queues allow. Unit.train subtracts cost as it goes, and the Scheduler
    # records each producer it hands out, so neither check goes stale mid-loop.
    gw = fake_unit(tag=1)
    fake = _train_bot(gw)
    assert await BuildOrderBot.do_train(fake, _zealot(count=5)) is True
    assert gw.train.call_count == 5
    assert fake.step_state.train.issued == 5


async def test_train_count_resumes_next_frame_after_a_partial_batch():
    # afford 2 of 5, then the money arrives: the step must order the REMAINING 3, not
    # start over — the tally is the only record that a partial batch went out.
    gw = fake_unit(tag=1)
    budget = [2]
    fake = _train_bot(gw)
    fake.can_afford = lambda u: budget[0] > 0 and (budget.__setitem__(0, budget[0] - 1) or True)
    step = _zealot(count=5)
    assert await BuildOrderBot.do_train(fake, step) is False, "2 of 5 ordered — must hold"
    assert gw.train.call_count == 2

    budget[0] = 99
    assert await BuildOrderBot.do_train(fake, step) is True
    assert gw.train.call_count == 5, "must top up to 5, not re-order all 5"


# ------------------------------------------------------------------ do_research
def _research_bot(**over):
    base = dict(already_pending_upgrade=lambda up: 0,
                state=fake_bot(upgrades=set()),
                can_afford=lambda up: True,
                research=MagicMock())
    base.update(over)
    return fake_bot(**base)


async def test_research_done_when_already_pending():
    fake = _research_bot(already_pending_upgrade=lambda up: 1)
    assert await BuildOrderBot.do_research(fake, fake_bot(what="Charge")) is True
    fake.research.assert_not_called()


async def test_research_holds_when_cant_afford():
    fake = _research_bot(can_afford=lambda up: False)
    assert await BuildOrderBot.do_research(fake, fake_bot(what="Charge")) is False
    fake.research.assert_not_called()


async def test_research_issues_then_holds_for_confirm():
    fake = _research_bot()
    assert await BuildOrderBot.do_research(fake, fake_bot(what="Charge")) is False
    fake.research.assert_called_once_with(UpgradeId.CHARGE)


# ------------------------------------------------------------------ do_hallucinate
async def test_hallucinate_holds_without_energized_sentry():
    fake = fake_bot(units=lambda t: FakeUnits([fake_unit(energy=50)]))
    assert await BuildOrderBot.do_hallucinate(fake, fake_bot()) is False


async def test_hallucinate_casts_when_sentry_has_energy():
    sentry = fake_unit(energy=100)
    fake = fake_bot(units=lambda t: FakeUnits([sentry]))
    assert await BuildOrderBot.do_hallucinate(fake, fake_bot()) is True
    sentry.assert_called_once_with(catalog.HALLUCINATION_ABILITY)


# ------------------------------------------------------------------ do_morph
def _morph_bot(sources, dest_units):
    """all_own_units answers both a set (the morph sources) and a single type
    (the dest, for the baseline/confirm count)."""
    def all_own_units(sel):
        return FakeUnits(sources) if isinstance(sel, (set, frozenset)) else FakeUnits(dest_units)
    morphing = set()
    fake = fake_bot(all_own_units=all_own_units, step_state=StepState(),
                    already_pending=lambda u: 0.0, _status="",
                    scheduler=SimpleNamespace(
                        morph_sources=lambda srcs: [s for s in srcs.idle if s.tag not in morphing],
                        commit_morph=morphing.add,
                        # Scheduler.new_frame() clears this; tests stepping frames call it.
                        new_frame=morphing.clear))
    fake._order_morph = lambda spec, group: BuildOrderBot._order_morph(fake, spec, group)
    return fake


async def test_morph_converts_each_idle_source_1to1():
    gw1, gw2 = fake_unit(), fake_unit()
    fake = _morph_bot([gw1, gw2], dest_units=[])  # 2 gateways, 0 warpgates
    fake._morph_sources = lambda spec: FakeUnits([gw1, gw2])
    assert await BuildOrderBot.do_morph(fake, fake_bot(to="warpgate", count=2)) is False
    ability = catalog.MORPH["warpgate"].ability
    gw1.assert_called_once_with(ability)
    gw2.assert_called_once_with(ability)


async def test_morph_archon_combines_two_templar_per_archon():
    ht1, ht2 = fake_unit(), fake_unit()
    fake = _morph_bot([ht1, ht2], dest_units=[])  # 2 HT, 0 archons
    fake._morph_sources = lambda spec: FakeUnits([ht1, ht2])
    assert await BuildOrderBot.do_morph(fake, fake_bot(to="archon", count=1)) is False
    ability = catalog.MORPH["archon"].ability
    ht1.assert_called_once_with(ability)  # both templar of the pair get MORPH_ARCHON
    ht2.assert_called_once_with(ability)


async def test_morph_done_when_the_committed_sources_are_consumed():
    # The other way a group ends: merged templar stop existing, and a converted Gateway
    # stops being a Gateway. Either way its tags leave the source list, which is what tells
    # us the order took.
    step = fake_bot(to="archon", count=1)
    fake = _morph_bot([], dest_units=[fake_unit()])  # the pair merged; 1 archon now exists
    fake._morph_sources = lambda spec: FakeUnits([])
    fake.step_state.morph.dest_wanted = 1
    fake.step_state.morph.committed = {1, 2}
    assert await BuildOrderBot.do_morph(fake, step) is True


# ------------------------------------------------------------------ do_chrono
def _chrono_bot(nexus_energy, target_structs):
    """WHICH Nexus casts (and its energy accounting) is Scheduler's job — see
    tests/test_scheduler.py. Here the scheduler just hands back a caster, or None when
    nothing can pay, so these cover do_chrono's own job: picking the TARGET."""
    nexus = fake_unit(energy=nexus_energy, tag=99)
    fake = fake_bot(townhalls=lambda t: FakeUnits([nexus]),
                    structures=lambda t: FakeUnits(target_structs),
                    scheduler=SimpleNamespace(
                        chrono_caster=lambda: nexus if nexus_energy >= catalog.CHRONO_ENERGY else None))
    fake._status = ""
    return fake, nexus


async def test_chrono_holds_without_energized_nexus():
    fake, _ = _chrono_bot(nexus_energy=10, target_structs=[])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is False


async def test_chrono_holds_when_target_type_absent():
    fake, _ = _chrono_bot(nexus_energy=100, target_structs=[])  # no Gateway exists at all
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is False


async def test_chrono_falls_back_to_an_idle_structure():
    """The real game lets you boost an idle building. Requiring a *producing* target
    deadlocked the queue: a chrono waiting on energy blocks the trains behind it, the
    Gateways go idle, and the 'is producing' precondition can then never be met again."""
    idle_gw = fake_unit(orders=[], has_buff=lambda b: False, tag=1)
    fake, nexus = _chrono_bot(nexus_energy=100, target_structs=[idle_gw])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    nexus.assert_called_once_with(catalog.CHRONO_ABILITY, idle_gw)


async def test_chrono_prefers_a_producing_structure_over_an_idle_one():
    idle_gw = fake_unit(orders=[], has_buff=lambda b: False, tag=1)
    busy_gw = fake_unit(orders=[object()], has_buff=lambda b: False, tag=2)
    fake, nexus = _chrono_bot(nexus_energy=100, target_structs=[idle_gw, busy_gw])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    nexus.assert_called_once_with(catalog.CHRONO_ABILITY, busy_gw)


async def test_chrono_prefers_unboosted_even_over_a_producing_boosted_one():
    """Buff status ranks ABOVE producing: re-boosting an already-boosted building is
    wasted, so an un-boosted idle Gateway beats a boosted busy one."""
    boosted_busy = fake_unit(orders=[object()], has_buff=lambda b: True, tag=1)
    unboosted_idle = fake_unit(orders=[], has_buff=lambda b: False, tag=2)
    fake, nexus = _chrono_bot(nexus_energy=100,
                              target_structs=[boosted_busy, unboosted_idle])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    nexus.assert_called_once_with(catalog.CHRONO_ABILITY, unboosted_idle)


async def test_chrono_always_spends_even_when_every_target_is_boosted():
    """A chrono step always spends a chrono. Previously an already-boosted target let
    the step complete for free, and which Gateway got picked came from an unstable
    observation order — so the same build spent a different number of chronos per run."""
    gw = fake_unit(orders=[object()], has_buff=lambda b: True, tag=1)
    fake, nexus = _chrono_bot(nexus_energy=100, target_structs=[gw])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    nexus.assert_called_once_with(catalog.CHRONO_ABILITY, gw)


async def test_chrono_target_choice_ignores_observation_order():
    """Same two Gateways, opposite list order — the tag tie-break must pick the same one."""
    def pick(order):
        a = fake_unit(orders=[], has_buff=lambda b: False, tag=1)
        b = fake_unit(orders=[], has_buff=lambda b: False, tag=2)
        structs = [a, b] if order else [b, a]
        fake, nexus = _chrono_bot(nexus_energy=100, target_structs=structs)
        return fake, nexus, a

    for order in (True, False):
        fake, nexus, expected = pick(order)
        assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
        nexus.assert_called_once_with(catalog.CHRONO_ABILITY, expected)


async def test_chrono_boosts_a_producing_structure():
    gw = fake_unit(orders=[object()], has_buff=lambda b: False)
    fake, nexus = _chrono_bot(nexus_energy=100, target_structs=[gw])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    nexus.assert_called_once_with(catalog.CHRONO_ABILITY, gw)


# ------------------------------------------------------------------ state setters
async def test_set_gas_probes_sets_target():
    fake = fake_bot(gas_target=0)
    assert await BuildOrderBot.do_set_gas_probes(fake, fake_bot(count=6)) is True
    assert fake.gas_target == 6


async def test_wait_completes_immediately():
    # `wait` is a pure trigger: run_steps gates it on `at`, so the handler itself
    # just reports done. Nothing else may change.
    fake = fake_bot()
    assert await BuildOrderBot.do_wait(fake, fake_bot()) is True
    assert vars(fake) == {}, "do_wait must not touch bot state"


async def test_cut_and_resume_probes_toggle_production():
    fake = fake_bot(continuously_build_workers=True)
    assert await BuildOrderBot.do_cut_probes(fake, fake_bot()) is True
    assert fake.continuously_build_workers is False
    assert await BuildOrderBot.do_resume_probes(fake, fake_bot()) is True
    assert fake.continuously_build_workers is True


async def test_morph_commits_the_same_sources_when_the_observation_reorders():
    # The bug this design exists for. Our order is invisible for a frame, and the
    # observation lists sources in a DIFFERENT order each frame (seen live), so re-deriving
    # candidates from "how many are still missing" picks a fresh Gateway and over-morphs.
    # Naming the committed tags is what pins it: 4 groups means 4 distinct sources, ever.
    gws = [fake_unit(tag=i, orders=[], is_idle=True) for i in range(1, 6)]
    order = list(gws)
    fake = _morph_bot(order, dest_units=[])
    fake._morph_sources = lambda spec: FakeUnits(order)
    step = fake_bot(to="warpgate", count=4)

    assert await BuildOrderBot.do_morph(fake, step) is False
    assert sorted(g.tag for g in gws if g.call_count) == [1, 2, 3, 4]

    # next frame: orders still not visible, and the observation hands them back reordered
    fake.scheduler.new_frame()
    order[:] = [gws[4], gws[0], gws[1], gws[2], gws[3]]
    assert await BuildOrderBot.do_morph(fake, step) is False
    assert sorted(g.tag for g in gws if g.call_count) == [1, 2, 3, 4], "committed a 5th source"
    gws[4].assert_not_called()


async def test_morph_retries_the_same_source_until_the_order_takes():
    # Measured live: a morph ordered before Warpgate research finished was re-issued for
    # 488 frames before the game accepted it. Retrying is the normal path, not an edge case.
    gw, spare = fake_unit(tag=1, orders=[], is_idle=True), fake_unit(tag=2, orders=[], is_idle=True)
    fake = _morph_bot([gw, spare], dest_units=[])
    fake._morph_sources = lambda spec: FakeUnits([gw, spare])
    step = fake_bot(to="warpgate", count=1)

    for expected in (1, 2, 3):
        fake.scheduler.new_frame()
        assert await BuildOrderBot.do_morph(fake, step) is False
        assert gw.call_count == expected, "must re-issue to the same source"
        spare.assert_not_called()
    assert fake.step_state.morph.committed == {1}


def _morph_order(name):
    return SimpleNamespace(ability=SimpleNamespace(id=catalog.MORPH[name].ability))


async def test_morph_is_done_once_every_group_is_under_way():
    # A source carrying the morph ability has taken the order and will finish, so the step
    # ends there rather than waiting out the conversion.
    gw = fake_unit(tag=1, orders=[], is_idle=True)
    fake = _morph_bot([gw], dest_units=[])
    fake._morph_sources = lambda spec: FakeUnits([gw])
    step = fake_bot(to="warpgate", count=1)
    assert await BuildOrderBot.do_morph(fake, step) is False

    fake.scheduler.new_frame()
    gw.orders, gw.is_idle = [_morph_order("warpgate")], False   # the order registered
    assert await BuildOrderBot.do_morph(fake, step) is True


async def test_morph_archon_under_way_is_the_merge_ability_not_the_one_we_issue():
    # A merging templar reports ARCHON_WARP_TARGET, never the MORPH_ARCHON we issued.
    # Matching only what we issue would leave the step unable to ever see it start.
    ht1, ht2 = fake_unit(tag=1, orders=[], is_idle=True), fake_unit(tag=2, orders=[], is_idle=True)
    fake = _morph_bot([ht1, ht2], dest_units=[])
    fake._morph_sources = lambda spec: FakeUnits([ht1, ht2])
    step = fake_bot(to="archon", count=1)
    assert await BuildOrderBot.do_morph(fake, step) is False

    fake.scheduler.new_frame()
    walking = SimpleNamespace(ability=SimpleNamespace(id=AbilityId.ARCHON_WARP_TARGET))
    for t in (ht1, ht2):
        t.orders, t.is_idle = [walking], False
    assert await BuildOrderBot.do_morph(fake, step) is True

    fake.scheduler.new_frame()
    fake.already_pending = lambda u: 1.0          # the order registered; it's converting
    assert await BuildOrderBot.do_morph(fake, step) is True


async def test_morph_skips_a_source_the_scheduler_just_gave_work():
    # A structure handed a unit to train this frame still reads idle — its order isn't sent
    # until the frame ends. Morphing it means the train order wins and the morph is lost,
    # which is how a Gateway ended up training a Zealot instead of converting.
    busy, free = fake_unit(tag=1, orders=[], is_idle=True), fake_unit(tag=2, orders=[], is_idle=True)
    fake = _morph_bot([busy, free], dest_units=[])
    fake._morph_sources = lambda spec: FakeUnits([busy, free])
    fake.scheduler.morph_sources = lambda srcs: [s for s in srcs.idle if s.tag != 1]
    assert await BuildOrderBot.do_morph(fake, fake_bot(to="warpgate", count=1)) is False
    busy.assert_not_called()
    free.assert_called_once_with(catalog.MORPH["warpgate"].ability)


async def test_morph_without_count_fixes_the_target_on_the_first_frame():
    # `morph to: warpgate` with no count means "every Gateway we have NOW" — and a
    # shipped build uses it (proxy_warp_zealot). Recomputing the target each frame shrinks
    # it as sources convert and leave the source list, so the step converts fewer than
    # asked and can finish having done almost none of them.
    gws = [fake_unit(tag=i, orders=[], is_idle=True) for i in range(1, 4)]
    fake = _morph_bot(gws, dest_units=[])
    fake._morph_sources = lambda spec: FakeUnits(gws)
    step = fake_bot(to="warpgate", count=None)

    assert await BuildOrderBot.do_morph(fake, step) is False
    assert fake.step_state.morph.dest_wanted == 3

    # one finished converting, so it is a WarpGate now and no longer a source
    fake.scheduler.new_frame()
    fake._morph_sources = lambda spec: FakeUnits(gws[1:])
    await BuildOrderBot.do_morph(fake, step)
    assert fake.step_state.morph.dest_wanted == 3, "target must not shrink as sources convert"


async def test_morph_finishes_the_stragglers_when_the_game_pairs_across_our_intent():
    # Measured live: 6 templar ordered in one frame came back as one Archon from the pair
    # we meant, one from two templar of two DIFFERENT intended pairs, and 2 left over.
    # Tracking pairs strands those 2 forever; a flat committed set re-issues to them.
    hts = [fake_unit(tag=i, orders=[], is_idle=True) for i in range(1, 7)]
    alive = list(hts)
    fake = _morph_bot(alive, dest_units=[])
    fake._morph_sources = lambda spec: FakeUnits(alive)
    step = fake_bot(to="archon", count=3)

    assert await BuildOrderBot.do_morph(fake, step) is False
    assert fake.step_state.morph.committed == {1, 2, 3, 4, 5, 6}, "all 6 committed at once"

    # the game merged 1+2 and 3+5, leaving 4 and 6 idle — not the pairing we'd have chosen
    fake.scheduler.new_frame()
    alive[:] = [hts[3], hts[5]]                      # tags 4 and 6
    for h in alive:
        h.reset_mock()
    assert await BuildOrderBot.do_morph(fake, step) is False, "2 of 3 made — must not finish"
    for h in alive:
        h.assert_called_once_with(catalog.MORPH["archon"].ability)

    # they merge on the retry; every committed source is now gone
    fake.scheduler.new_frame()
    alive[:] = []
    assert await BuildOrderBot.do_morph(fake, step) is True
