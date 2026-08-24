"""Tier 1 — the sequencing brain: trigger_met + _prewalk_due, tested unbound
against a fake `self` (no BotAI, no game)."""

from __future__ import annotations

from types import SimpleNamespace

from bot import BuildOrderBot
from schema import Trigger

from fakes import FakeUnits, fake_bot


# ------------------------------------------------------------------ trigger_met
def test_scalar_triggers_fire_at_or_above_threshold():
    fake = fake_bot(supply_used=20, time=100.0, minerals=500, vespene=88)
    assert BuildOrderBot.trigger_met(fake, Trigger(supply=20)) is True
    assert BuildOrderBot.trigger_met(fake, Trigger(supply=21)) is False
    assert BuildOrderBot.trigger_met(fake, Trigger(time=100)) is True
    assert BuildOrderBot.trigger_met(fake, Trigger(time=101)) is False
    assert BuildOrderBot.trigger_met(fake, Trigger(minerals=500)) is True
    assert BuildOrderBot.trigger_met(fake, Trigger(minerals=501)) is False
    assert BuildOrderBot.trigger_met(fake, Trigger(vespene=88)) is True
    assert BuildOrderBot.trigger_met(fake, Trigger(vespene=89)) is False


def test_count_trigger_counts_ready_units():
    fake = fake_bot(all_own_units=lambda t: FakeUnits([object(), object()]))
    fake.count_of = lambda name: BuildOrderBot.count_of(fake, name)
    assert BuildOrderBot.trigger_met(fake, Trigger(count={"Gateway": 2})) is True
    assert BuildOrderBot.trigger_met(fake, Trigger(count={"Gateway": 3})) is False


# ------------------------------------------------------------------ count_of
def test_count_of_probes_uses_supply_workers_not_the_unit_list():
    # A probe inside an Assimilator vanishes from the observation for ~1.4s a trip, so
    # the unit list reads low at random; food_workers doesn't have that hole. Measured
    # on a real run: `count: {Probe: 17}` fired 1.4s late because of this.
    fake = fake_bot(supply_workers=19, workers=FakeUnits([object()] * 18),
                    all_own_units=lambda t: FakeUnits([object()] * 18))
    fake.count_of = lambda name: BuildOrderBot.count_of(fake, name)
    assert BuildOrderBot.count_of(fake, "Probe") == 19
    assert fake.workers.amount == 18, "the unit list is the count we're deliberately NOT using"
    assert BuildOrderBot.trigger_met(fake, Trigger(count={"Probe": 19})) is True


def test_count_of_uses_the_unit_list_for_everything_else():
    # only workers can be hidden inside a building, so nothing else needs the detour
    fake = fake_bot(supply_workers=19, all_own_units=lambda t: FakeUnits([object(), object()]))
    assert BuildOrderBot.count_of(fake, "Gateway") == 2


# ------------------------------------------------------------------ _prewalk_due
def test_prewalk_reserves_probe_cost_against_supply_target():
    # at supply 20, currently 18 -> 2 probes still needed = 100 min reserved. So a
    # `prewalk: {minerals: 100}` needs minerals - 100 >= 100, i.e. >= 200 banked.
    step = fake_bot(prewalk=Trigger(minerals=100), at=Trigger(supply=20))
    fake = fake_bot(minerals=199, vespene=0, supply_used=18)
    fake._probe_reserve = lambda s: BuildOrderBot._probe_reserve(fake, s)
    assert BuildOrderBot._prewalk_due(fake, step) is False
    fake.minerals = 200
    assert BuildOrderBot._prewalk_due(fake, step) is True


def test_prewalk_vespene_threshold():
    # gas gets no probe reservation (probes cost no gas), so it defers to trigger_met
    step = fake_bot(prewalk=Trigger(vespene=100), at=Trigger(time=1))
    fake = fake_bot(minerals=0, vespene=99, supply_used=0)
    fake.trigger_met = lambda trig: BuildOrderBot.trigger_met(fake, trig)
    assert BuildOrderBot._prewalk_due(fake, step) is False
    fake.vespene = 100
    assert BuildOrderBot._prewalk_due(fake, step) is True


def _cost_bot(minerals, vespene, supply_used=0, cost=(150, 100)):
    fake = fake_bot(minerals=minerals, vespene=vespene, supply_used=supply_used)
    fake.calculate_cost = lambda unit: SimpleNamespace(minerals=cost[0], vespene=cost[1])
    fake._probe_reserve = lambda s: BuildOrderBot._probe_reserve(fake, s)
    return fake


def test_prewalk_defaults_to_affording_the_building():
    # No explicit prewalk: pull the probe exactly when we could pay for the building.
    # Independent of `at`, so reordering steps doesn't move when the builder leaves.
    step = fake_bot(prewalk=None, at=Trigger(time=1), what="TwilightCouncil")
    assert BuildOrderBot._prewalk_due(_cost_bot(149, 100), step) is False
    assert BuildOrderBot._prewalk_due(_cost_bot(150, 100), step) is True


def test_prewalk_default_waits_for_the_gas_too():
    # a gas-costing building must not pull a probe on minerals alone
    step = fake_bot(prewalk=None, at=Trigger(time=1), what="TwilightCouncil")
    assert BuildOrderBot._prewalk_due(_cost_bot(500, 99), step) is False
    assert BuildOrderBot._prewalk_due(_cost_bot(500, 100), step) is True


def test_prewalk_default_still_reserves_probe_cost():
    # the default is "as if prewalk: {cost}", so it goes through the same reservation:
    # at supply 20 from 18 = 2 probes = 100 reserved, so a 400 Nexus needs 500 banked
    step = fake_bot(prewalk=None, at=Trigger(supply=20), what="Nexus")
    assert BuildOrderBot._prewalk_due(_cost_bot(499, 0, supply_used=18, cost=(400, 0)), step) is False
    assert BuildOrderBot._prewalk_due(_cost_bot(500, 0, supply_used=18, cost=(400, 0)), step) is True


def test_explicit_prewalk_overrides_the_cost_default():
    # the whole point of `prewalk:` — pull EARLIER than affording it (300 < 400)
    step = fake_bot(prewalk=Trigger(minerals=300), at=Trigger(time=1), what="Nexus")
    fake = _cost_bot(300, 0, cost=(400, 0))
    assert BuildOrderBot._prewalk_due(fake, step) is True, "should not wait for the full cost"
