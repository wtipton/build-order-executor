"""Tier 1 — step-handler gating: each handler's hold-vs-proceed decision and the
call it issues, tested unbound against a fake `self` with stubbed SC2 surface.

This is the "also mock handler gating" tier: it pins the DECISION logic (when a
step holds the line vs. fires, and that it issues the right order), not whether
the SC2 API then behaves as expected — that's for integration.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import catalog
from bot import BuildOrderBot
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

from fakes import FakeUnits, fake_bot, fake_unit
from state import MorphState


# ------------------------------------------------------------------ do_train
async def test_train_holds_when_cant_afford():
    fake = fake_bot(can_afford=lambda u: False)
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is False


async def test_train_holds_when_no_idle_producer():
    fake = fake_bot(can_afford=lambda u: True, _issued_this_frame=set(),
                    structures=lambda t: FakeUnits(), townhalls=FakeUnits())
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is False


async def test_train_issues_to_idle_gateway():
    gw = fake_unit(tag=1)
    fake = fake_bot(can_afford=lambda u: True, _issued_this_frame=set(),
                    structures=lambda t: FakeUnits([gw]), townhalls=FakeUnits())
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is True
    gw.train.assert_called_once_with(U.ZEALOT)


async def test_train_spreads_across_producers_within_a_frame():
    """The observation cache doesn't refresh mid-frame, so a Gateway we just ordered
    still reads as idle. Consecutive train steps in one frame must therefore walk to
    the NEXT producer rather than all stacking onto the first."""
    gws = [fake_unit(tag=1), fake_unit(tag=2), fake_unit(tag=3)]
    fake = fake_bot(can_afford=lambda u: True, _issued_this_frame=set(),
                    structures=lambda t: FakeUnits(gws), townhalls=FakeUnits())
    for _ in gws:
        assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is True
    for gw in gws:
        gw.train.assert_called_once_with(U.ZEALOT)


async def test_train_producer_choice_ignores_observation_order():
    """Same producers, opposite list order — the tag tie-break must pick the same one.
    `.first` here made which Gateway got the unit a coin flip between runs."""
    for reverse in (False, True):
        a, b = fake_unit(tag=1), fake_unit(tag=2)
        gws = [b, a] if reverse else [a, b]
        fake = fake_bot(can_afford=lambda u: True, _issued_this_frame=set(),
                        structures=lambda t: FakeUnits(gws), townhalls=FakeUnits())
        assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is True
        a.train.assert_called_once_with(U.ZEALOT)
        b.train.assert_not_called()


async def test_train_stacks_rather_than_stalling_once_all_producers_used():
    """Spreading is a PREFERENCE, not a gate. Once every producer has been used this
    frame the step must still issue (queuing on one) — stalling here starves the step
    queue, so everything ordered behind a run of train steps crawls."""
    gw = fake_unit(tag=1)
    fake = fake_bot(can_afford=lambda u: True, _issued_this_frame=set(),
                    structures=lambda t: FakeUnits([gw]), townhalls=FakeUnits())
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is True
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is True
    assert gw.train.call_count == 2


async def test_train_probe_uses_a_townhall():
    nx = fake_unit(tag=1)
    fake = fake_bot(can_afford=lambda u: True, _issued_this_frame=set(),
                    townhalls=FakeUnits([nx]), structures=lambda t: FakeUnits())
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Probe")) is True
    nx.train.assert_called_once_with(U.PROBE)


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
    return fake_bot(all_own_units=all_own_units, _morph=MorphState())


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
    fake._morph.step = step  # already in progress
    fake._morph.target, fake._morph.baseline = 1, 0
    assert await BuildOrderBot.do_morph(fake, step) is True
    assert fake._morph.step is None


# ------------------------------------------------------------------ do_chrono
def _chrono_bot(nexus_energy, target_structs):
    nexus = fake_unit(energy=nexus_energy, tag=99)

    def townhalls(t):
        return FakeUnits([nexus])

    fake = fake_bot(townhalls=townhalls,
                    structures=lambda t: FakeUnits(target_structs),
                    _chrono_cast_this_frame=set())
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


async def test_chrono_spends_from_the_fullest_nexus():
    """The observation's townhall ordering isn't stable between runs, so casting from
    `.first` drained a different Nexus each run. Always spend from the fullest — which
    also keeps a Nexus off the 200 cap, where further regen is thrown away."""
    low = fake_unit(energy=60, tag=1)
    high = fake_unit(energy=190, tag=2)
    gw = fake_unit(orders=[object()], has_buff=lambda b: False)
    fake = fake_bot(townhalls=lambda t: FakeUnits([low, high]),
                    structures=lambda t: FakeUnits([gw]),
                    _chrono_cast_this_frame=set())
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    high.assert_called_once_with(catalog.CHRONO_ABILITY, gw)
    low.assert_not_called()


async def test_chrono_does_not_double_spend_one_nexus_in_a_frame():
    """Energy reads pre-spend until the next frame, so two chrono steps firing in the
    same frame would both cast from a Nexus that can only afford one — the second
    silently no-ops while its step is marked done. The second must hold instead."""
    nexus = fake_unit(energy=50, tag=1)
    gw = fake_unit(orders=[object()], has_buff=lambda b: False)
    fake = fake_bot(townhalls=lambda t: FakeUnits([nexus]),
                    structures=lambda t: FakeUnits([gw]),
                    _chrono_cast_this_frame=set())
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is False
    nexus.assert_called_once_with(catalog.CHRONO_ABILITY, gw)


async def test_chrono_boosts_a_producing_structure():
    gw = fake_unit(orders=[object()], has_buff=lambda b: False)
    fake, nexus = _chrono_bot(nexus_energy=100, target_structs=[gw])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    nexus.assert_called_once_with(catalog.CHRONO_ABILITY, gw)


# ------------------------------------------------------------------ state setters
async def test_gas_workers_sets_target():
    fake = fake_bot(gas_target=0)
    assert await BuildOrderBot.do_gas_workers(fake, fake_bot(count=6)) is True
    assert fake.gas_target == 6


async def test_minerals_cap_sets_value():
    fake = fake_bot(minerals_per_base=16)
    assert await BuildOrderBot.do_minerals_cap(fake, fake_bot(count=20)) is True
    assert fake.minerals_per_base == 20


async def test_workers_toggle_start_stop():
    fake = fake_bot(continuous_workers=True)
    assert await BuildOrderBot.do_workers(fake, fake_bot(state="stop")) is True
    assert fake.continuous_workers is False
    assert await BuildOrderBot.do_workers(fake, fake_bot(state="start")) is True
    assert fake.continuous_workers is True
