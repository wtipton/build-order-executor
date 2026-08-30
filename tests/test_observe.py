"""Tests for observe.py (ObserveMixin) — status reporting, step formatting, single-line format."""

from __future__ import annotations

from types import SimpleNamespace

from observe import ObserveMixin
from schema import BuildStep, Trigger
from state import StepState


class _ObserveBot(ObserveMixin, SimpleNamespace):
    pass


def test_status_report_waiting_for_trigger(capsys):
    step = BuildStep(at=Trigger(count={"Pylon": 1}), do="build", what="Gateway")
    bot = _ObserveBot(
        time=272.0,
        supply_used=45,
        supply_cap=54,
        supply_workers=28.0,
        minerals=350,
        vespene=120,
        ordered_bases=lambda: [
            SimpleNamespace(assigned_harvesters=16, ideal_harvesters=16),
            SimpleNamespace(assigned_harvesters=12, ideal_harvesters=16),
        ],
        townhalls=SimpleNamespace(ready=[SimpleNamespace(energy=100)]),
        current_step=step,
        steps_done=12,
        cfg=SimpleNamespace(steps=[None] * 34),
        step_state=StepState(started_at=234.0, trigger_fired=False),
        count_of=lambda name: 0,
        _status="",
    )

    bot._status_report()

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0] == (
        "[status]   4:32  step=13/34 (38s)  build what=Gateway -- waiting for count Pylon=1 (have 0)  |  "
        "supply=45/54 workers=28 min=350 gas=120 bases[b1=16/16 b2=12/16] chrono=2"
    )


def test_status_report_issued_waiting_to_confirm(capsys):
    step = BuildStep(at=Trigger(time=0), do="build", what="Pylon")
    bot = _ObserveBot(
        time=45.0,
        supply_used=14,
        supply_cap=15,
        supply_workers=14.0,
        minerals=100,
        vespene=0,
        ordered_bases=lambda: [
            SimpleNamespace(assigned_harvesters=14, ideal_harvesters=16),
        ],
        townhalls=SimpleNamespace(ready=[SimpleNamespace(energy=50)]),
        current_step=step,
        steps_done=1,
        cfg=SimpleNamespace(steps=[None] * 20),
        step_state=StepState(started_at=40.0, trigger_fired=True),
        _status="",
    )

    bot._status_report()

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0] == (
        "[status]   0:45  step=2/20 (5s)  build what=Pylon -- issued; waiting to confirm  |  "
        "supply=14/15 workers=14 min=100 gas=0 bases[b1=14/16] chrono=1"
    )


def test_status_report_custom_status_reason(capsys):
    step = BuildStep(at=Trigger(time=0), do="build", what="Gateway")
    bot = _ObserveBot(
        time=120.0,
        supply_used=20,
        supply_cap=23,
        supply_workers=18.0,
        minerals=150,
        vespene=0,
        ordered_bases=lambda: [
            SimpleNamespace(assigned_harvesters=18, ideal_harvesters=16),
        ],
        townhalls=SimpleNamespace(ready=[]),
        current_step=step,
        steps_done=5,
        cfg=SimpleNamespace(steps=[None] * 20),
        step_state=StepState(started_at=100.0, trigger_fired=True),
        _status="no free probe to build with",
    )

    bot._status_report()

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0] == (
        "[status]   2:00  step=6/20 (20s)  build what=Gateway -- no free probe to build with  |  "
        "supply=20/23 workers=18 min=150 gas=0 bases[b1=18/16] chrono=0"
    )


def test_status_report_when_build_complete(capsys):
    bot = _ObserveBot(
        time=300.0,
        supply_used=60,
        supply_cap=66,
        supply_workers=35.0,
        minerals=100,
        vespene=50,
        ordered_bases=lambda: [
            SimpleNamespace(assigned_harvesters=16, ideal_harvesters=16),
        ],
        townhalls=SimpleNamespace(ready=[]),
        current_step=None,
        steps_done=20,
        cfg=SimpleNamespace(steps=[None] * 20),
        step_state=StepState(),
        _status="",
    )

    bot._status_report()

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 1
    assert lines[0] == (
        "[status]   5:00  step=20/20  BUILD COMPLETE  |  "
        "supply=60/66 workers=35 min=100 gas=50 bases[b1=16/16] chrono=0"
    )


def test_note_completions_emits_ready_tag(capsys):
    from sc2.ids.unit_typeid import UnitTypeId as U
    from sc2.ids.upgrade_id import UpgradeId

    bot = _ObserveBot(
        time=124.0,
        all_own_units=SimpleNamespace(
            ready=[
                SimpleNamespace(tag=101, type_id=U.GATEWAY, is_hallucination=False),
                SimpleNamespace(tag=102, type_id=U.PROBE, is_hallucination=False),
            ]
        ),
        state=SimpleNamespace(upgrades={UpgradeId.WARPGATERESEARCH}),
        _completions={},
        _upgrade_completions={},
        game_data=SimpleNamespace(units={U.GATEWAY.value: SimpleNamespace(name="Gateway")}),
    )

    bot._note_completions()

    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0] == "[ready]    2:04  Gateway #1"
    assert lines[1] == "[ready]    2:04  Warpgate"

