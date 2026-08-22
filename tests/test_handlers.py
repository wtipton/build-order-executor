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
    fake = fake_bot(can_afford=lambda u: True,
                    structures=lambda t: FakeUnits(), townhalls=FakeUnits())
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is False


async def test_train_issues_to_idle_gateway():
    gw = fake_unit()
    fake = fake_bot(can_afford=lambda u: True,
                    structures=lambda t: FakeUnits([gw]), townhalls=FakeUnits())
    assert await BuildOrderBot.do_train(fake, fake_bot(what="Zealot")) is True
    gw.train.assert_called_once_with(U.ZEALOT)


async def test_train_probe_uses_a_townhall():
    nx = fake_unit()
    fake = fake_bot(can_afford=lambda u: True,
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
    nexus = fake_unit(energy=nexus_energy)

    def townhalls(t):
        return FakeUnits([nexus])

    fake = fake_bot(townhalls=townhalls,
                    structures=lambda t: FakeUnits(target_structs))
    return fake, nexus


async def test_chrono_holds_without_energized_nexus():
    fake, _ = _chrono_bot(nexus_energy=10, target_structs=[])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is False


async def test_chrono_holds_when_nothing_producing():
    fake, _ = _chrono_bot(nexus_energy=100, target_structs=[])  # no gateway with orders
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is False


async def test_chrono_boosts_a_producing_structure():
    gw = fake_unit(orders=[object()], has_buff=lambda b: False)
    fake, nexus = _chrono_bot(nexus_energy=100, target_structs=[gw])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    nexus.assert_called_once_with(catalog.CHRONO_ABILITY, gw)


async def test_chrono_skips_already_boosted_target():
    gw = fake_unit(orders=[object()], has_buff=lambda b: True)
    fake, nexus = _chrono_bot(nexus_energy=100, target_structs=[gw])
    assert await BuildOrderBot.do_chrono(fake, fake_bot(target="Gateway")) is True
    nexus.assert_not_called()  # already boosted — counted done without re-casting


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
