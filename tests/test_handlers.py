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
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

from fakes import FakeUnits, fake_bot, fake_unit
from state import StepState


# ------------------------------------------------------------------ do_train
# NB: WHICH producer gets the unit is Scheduler's job now — see tests/test_scheduler.py.
# These cover only do_train's own decision: afford, delegate, issue.
def _train_bot(producer):
    return fake_bot(can_afford=lambda u: True,
                    scheduler=SimpleNamespace(producer_for=lambda unit: producer))


async def test_train_holds_when_cant_afford():
    fake = fake_bot(can_afford=lambda u: False)
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is False


async def test_train_holds_when_every_producer_queue_is_full():
    # scheduler returning None means "no producer with queue space" — ordering past the
    # game's limit is silently dropped, so the step must stall instead.
    fake = _train_bot(None)
    fake._status = ""
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is False


async def test_train_issues_to_the_producer_the_scheduler_picked():
    gw = fake_unit(tag=1)
    fake = _train_bot(gw)
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is True
    gw.train.assert_called_once_with(U.ZEALOT)


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
    return fake_bot(all_own_units=all_own_units, step_state=StepState())


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


async def test_morph_done_when_dest_count_reached():
    step = fake_bot(to="archon", count=1)
    fake = _morph_bot([], dest_units=[fake_unit()])  # 1 archon now exists
    # already in progress: target set, baseline captured (so not a first frame)
    fake.step_state.morph.target, fake.step_state.morph.baseline = 1, 0
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
