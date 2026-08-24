"""Tier 1 — the sequencing brain: trigger_met + _prewalk_due, tested unbound
against a fake `self` (no BotAI, no game)."""

from __future__ import annotations

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
    assert BuildOrderBot.trigger_met(fake, Trigger(count={"Gateway": 2})) is True
    assert BuildOrderBot.trigger_met(fake, Trigger(count={"Gateway": 3})) is False


# ------------------------------------------------------------------ _prewalk_due
def test_prewalk_reserves_probe_cost_against_supply_target():
    # at supply 20, currently 18 -> 2 probes still needed = 100 min reserved. So a
    # `prewalk: {minerals: 100}` needs minerals - 100 >= 100, i.e. >= 200 banked.
    step = fake_bot(prewalk=Trigger(minerals=100), at=Trigger(supply=20))
    fake = fake_bot(minerals=199, vespene=0, supply_used=18)
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


def test_prewalk_defaults_to_at_when_non_resource():
    # no explicit prewalk; `at` is a supply trigger -> falls through to trigger_met
    step = fake_bot(prewalk=None, at=Trigger(supply=15))
    fake = fake_bot(supply_used=15)
    fake.trigger_met = lambda trig: BuildOrderBot.trigger_met(fake, trig)
    assert BuildOrderBot._prewalk_due(fake, step) is True
    fake.supply_used = 14
    assert BuildOrderBot._prewalk_due(fake, step) is False
