"""Tier 1 — run_steps' strict hold-the-line ordering: a step that can't fire (its
trigger isn't due, or its handler returns False) blocks everything after it, and
the idx pointer only advances past leading completed steps. Tested unbound with a
fake `self` and stubbed trigger_met/execute."""

from __future__ import annotations

from types import SimpleNamespace

from bot import BuildOrderBot
from schema import Trigger

from fakes import fake_bot


def _step(at=None, **kw):
    return SimpleNamespace(at=at or Trigger(time=1), note="", **kw)


def _seq_bot(steps, done, trigger_met, execute):
    fake = fake_bot(steps=steps, _done=list(done), _started=[False] * len(steps),
                    idx=0, supply_used=0, trigger_met=trigger_met,
                    _clock=lambda: "0:00", _describe=lambda s: "step")
    fake.execute = execute
    return fake


async def _always(_):  # execute stub: every step completes
    return True


async def test_all_steps_fire_when_ready():
    steps = [_step(), _step(), _step()]
    fake = _seq_bot(steps, [False] * 3, trigger_met=lambda at: True, execute=_always)
    await BuildOrderBot.run_steps(fake)
    assert fake._done == [True, True, True]
    assert fake.idx == 3


async def test_untriggered_step_blocks_the_rest():
    # step 1's trigger isn't due -> it and step 2 must not fire even though step 2
    # would be ready.
    steps = [_step(at=Trigger(time=1)), _step(at=Trigger(time=999)), _step(at=Trigger(time=1))]
    fake = _seq_bot(steps, [False] * 3, trigger_met=lambda at: at.time <= 1, execute=_always)
    await BuildOrderBot.run_steps(fake)
    assert fake._done == [True, False, False]
    assert fake.idx == 1


async def test_handler_hold_blocks_the_rest():
    # step 0 is triggered but its handler returns False (can't complete yet) -> the
    # line holds; nothing is marked done and idx stays put (strict order).
    steps = [_step(), _step()]

    async def execute(step):
        return False

    fake = _seq_bot(steps, [False, False], trigger_met=lambda at: True, execute=execute)
    await BuildOrderBot.run_steps(fake)
    assert fake._done == [False, False]
    assert fake.idx == 0


async def test_idx_advances_past_leading_done_steps():
    steps = [_step(), _step(), _step()]
    fake = _seq_bot(steps, [True, False, False], trigger_met=lambda at: True, execute=_always)
    await BuildOrderBot.run_steps(fake)
    assert fake._done == [True, True, True]
    assert fake.idx == 3


async def test_started_step_completes_even_if_trigger_goes_false():
    # Regression for the morph-archon bug: the trigger (count of units the step
    # consumes) is true when the step starts, then goes false — but once started,
    # the step must be driven to completion WITHOUT re-checking the trigger.
    steps = [_step()]
    trig_calls, exec_calls = [], []

    def trigger_met(at):
        trig_calls.append(1)
        return len(trig_calls) == 1  # true only on the first (start) check

    async def execute(step):
        exec_calls.append(1)
        return len(exec_calls) >= 2  # holds once (in progress), then confirms

    fake = _seq_bot(steps, [False], trigger_met=trigger_met, execute=execute)

    await BuildOrderBot.run_steps(fake)          # frame 1: start + execute holds
    assert fake._started == [True] and fake._done == [False]

    await BuildOrderBot.run_steps(fake)          # frame 2: trigger now false, but committed
    assert fake._done == [True] and fake.idx == 1
    assert len(trig_calls) == 1, "trigger was re-checked after the step started"
