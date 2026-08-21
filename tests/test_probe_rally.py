"""Tier 1 — probe dispatch and rally handlers: send_probe / return_probe / rally /
rally_and_transfer. These are about which worker gets picked and where it's told
to go, plus the rally-target state; tested unbound with stubbed selection helpers.
"""

from __future__ import annotations

from bot import BuildOrderBot
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U

from fakes import FakeUnits, fake_bot, fake_unit

DEST = object()  # sentinel returned by a stubbed _resolve_place


# ------------------------------------------------------------------ send_probe
async def test_send_probe_reuses_named_probe():
    existing = fake_unit(tag=1)
    fake = fake_bot(_resolve_place=lambda w: DEST, named_probes={"scout": 1},
                    _named_worker=lambda label: existing, _free_probe_near=lambda p: None)
    assert await BuildOrderBot.do_send_probe(fake, fake_bot(where="proxy", label="scout")) is True
    existing.move.assert_called_once_with(DEST)
    assert fake.named_probes == {"scout": 1}  # unchanged — same probe reused


async def test_send_probe_pulls_and_registers_fresh_probe():
    fresh = fake_unit(tag=7)
    fake = fake_bot(_resolve_place=lambda w: DEST, named_probes={},
                    _named_worker=lambda label: None, _free_probe_near=lambda p: fresh)
    assert await BuildOrderBot.do_send_probe(fake, fake_bot(where="proxy", label="scout")) is True
    assert fake.named_probes == {"scout": 7}  # registered so it's held out of automation
    fresh.move.assert_called_once_with(DEST)


async def test_send_probe_holds_when_no_probe_available():
    fake = fake_bot(_resolve_place=lambda w: DEST, named_probes={},
                    _named_worker=lambda label: None, _free_probe_near=lambda p: None)
    assert await BuildOrderBot.do_send_probe(fake, fake_bot(where="proxy", label="scout")) is False


# ------------------------------------------------------------------ return_probe
async def test_return_probe_sends_it_back_to_mining():
    field = object()
    worker = fake_unit()
    fake = fake_bot(named_probes={"scout": 7},
                    _worker_by_tag=lambda t: worker if t == 7 else None,
                    _populating_field=lambda: field)
    assert await BuildOrderBot.do_return_probe(fake, fake_bot(label="scout")) is True
    assert "scout" not in fake.named_probes  # handed back to automation
    worker.gather.assert_called_once_with(field)


async def test_return_probe_noop_for_unknown_label():
    fake = fake_bot(named_probes={}, _worker_by_tag=lambda t: None, _populating_field=lambda: None)
    assert await BuildOrderBot.do_return_probe(fake, fake_bot(label="ghost")) is True


# ------------------------------------------------------------------ rally
async def test_rally_sets_target():
    fake = fake_bot(_resolve_place=lambda w: DEST, rally_target=None)
    assert await BuildOrderBot.do_rally(fake, fake_bot(where="natural")) is True
    assert fake.rally_target is DEST


# ------------------------------------------------------------------ rally_and_transfer
async def test_rally_and_transfer_holds_when_base_not_up():
    fake = fake_bot(_ordered_bases=lambda: [fake_unit()], populating_base_num=1)
    assert await BuildOrderBot.do_rally_and_transfer(fake, fake_bot(base=2)) is False
    assert fake.populating_base_num == 1  # unchanged — didn't switch to a base that isn't up


async def test_rally_and_transfer_sets_base_and_rallies_nexuses():
    field = object()
    b1 = fake_unit(tag=1, assigned_harvesters=16)  # at cap -> no excess to transfer
    b2 = fake_unit(tag=2, assigned_harvesters=0)
    nexus = fake_unit()
    fake = fake_bot(
        _ordered_bases=lambda: [b1, b2],
        _base_field=lambda base: field,
        minerals_per_base=16,
        populating_base_num=1,
        townhalls=lambda t: FakeUnits([nexus]),
        workers=FakeUnits([]),
        _excluded_tags=lambda: set(),
    )
    assert await BuildOrderBot.do_rally_and_transfer(fake, fake_bot(base=2)) is True
    assert fake.populating_base_num == 2
    nexus.assert_called_once_with(AbilityId.RALLY_WORKERS, field)
