"""Tier 1 — run_steps' strict hold-the-line ordering: a step that can't fire (its
trigger isn't due, or its handler returns False) blocks everything after it, and
`steps_done` only advances over steps that actually completed. Tested unbound with
a fake `self` and stubbed trigger_met/run_handler."""

from __future__ import annotations

from types import SimpleNamespace

from bot import BuildOrderBot
from schema import Trigger
from state import StepState


def _step(at=None, **kw):
    return SimpleNamespace(at=at or Trigger(time=1), **kw)


class _SeqBot(SimpleNamespace):
    """Fake `self` for run_steps. Borrows the real progress properties rather than
    restating them, so the end-of-build boundary exercised here is the production one."""

    current_step = BuildOrderBot.current_step
    all_steps_done = BuildOrderBot.all_steps_done


def _seq_bot(steps, trigger_met, run_handler, steps_done=0):
    fake = _SeqBot(cfg=SimpleNamespace(steps=steps), steps_done=steps_done,
                   step_state=StepState(), supply_used=0, time=0.0, trigger_met=trigger_met,
                   _clock=lambda: "0:00")
    fake.run_handler = run_handler
    return fake


async def _always(_):  # run_handler stub: every step completes
    return True


async def test_all_steps_fire_when_ready():
    steps = [_step(), _step(), _step()]
    fake = _seq_bot(steps, trigger_met=lambda at: True, run_handler=_always)
    await BuildOrderBot.run_steps(fake)
    assert fake.steps_done == 3
    assert fake.all_steps_done and fake.current_step is None


async def test_untriggered_step_blocks_the_rest():
    # step 1's trigger isn't due -> it and step 2 must not fire even though step 2
    # would be ready.
    steps = [_step(at=Trigger(time=1)), _step(at=Trigger(time=999)), _step(at=Trigger(time=1))]
    fake = _seq_bot(steps, trigger_met=lambda at: at.time <= 1, run_handler=_always)
    await BuildOrderBot.run_steps(fake)
    assert fake.steps_done == 1
    assert fake.current_step is steps[1]


async def test_handler_hold_blocks_the_rest():
    # step 0 is triggered but its handler returns False (can't complete yet) -> the
    # line holds and steps_done stays put (strict order).
    steps = [_step(), _step()]

    async def run_handler(step):
        return False

    fake = _seq_bot(steps, trigger_met=lambda at: True, run_handler=run_handler)
    await BuildOrderBot.run_steps(fake)
    assert fake.steps_done == 0
    assert fake.current_step is steps[0]


async def test_resumes_from_the_current_step():
    # a frame that starts mid-build works from steps_done, not from the top.
    steps = [_step(), _step(), _step()]
    seen = []

    async def run_handler(step):
        seen.append(step)
        return True

    fake = _seq_bot(steps, trigger_met=lambda at: True, run_handler=run_handler, steps_done=1)
    await BuildOrderBot.run_steps(fake)
    assert seen == steps[1:], "re-ran an already-completed step"
    assert fake.steps_done == 3


async def test_empty_build_is_complete_immediately():
    fake = _seq_bot([], trigger_met=lambda at: True, run_handler=_always)
    await BuildOrderBot.run_steps(fake)
    assert fake.all_steps_done and fake.current_step is None


async def test_step_state_is_reset_on_advance():
    # The whole contract of StepState: a finished step's in-flight state must not leak
    # into the next one. Handlers rely on an unset baseline / warping_at_start meaning
    # "my first frame", so a stale one would silently skip a step's capture.
    steps = [_step(), _step()]
    seen = []

    async def run_handler(step):
        seen.append((step, fake.step_state.warp.warped_in, fake.step_state.morph.dest_wanted))
        fake.step_state.warp.warped_in = {7}      # pretend this step went in flight
        fake.step_state.morph.dest_wanted = 3
        return True

    fake = _seq_bot(steps, trigger_met=lambda at: True, run_handler=run_handler)
    await BuildOrderBot.run_steps(fake)

    assert seen == [(steps[0], set(), None), (steps[1], set(), None)], "state leaked between steps"
    assert fake.step_state == StepState(), "StepState not fully cleared after the last step"
    assert fake.step_state.started_at == fake.time, "advance must stamp when the new step began"


async def test_started_step_completes_even_if_trigger_goes_false():
    # Regression for the morph-archon bug: the trigger (count of units the step
    # consumes) is true when the step starts, then goes false — but once started,
    # the step must be driven to completion WITHOUT re-checking the trigger.
    steps = [_step()]
    trig_calls, handler_calls = [], []

    def trigger_met(at):
        trig_calls.append(1)
        return len(trig_calls) == 1  # true only on the first (start) check

    async def run_handler(step):
        handler_calls.append(1)
        return len(handler_calls) >= 2  # holds once (in progress), then confirms

    fake = _seq_bot(steps, trigger_met=trigger_met, run_handler=run_handler)

    await BuildOrderBot.run_steps(fake)          # frame 1: start + run_handler holds
    assert fake.step_state.trigger_fired is True and fake.steps_done == 0

    await BuildOrderBot.run_steps(fake)          # frame 2: trigger now false, but committed
    assert fake.steps_done == 1
    assert fake.step_state.trigger_fired is False, "commit flag must reset for the next step"
    assert len(trig_calls) == 1, "trigger was re-checked after the step started"
