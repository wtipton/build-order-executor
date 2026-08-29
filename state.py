"""The mutable state BuildOrderBot carries between frames.

Two lifetimes:

  * StepState — everything scoped to the ONE step currently being executed. The
    engine clears it as a unit the moment it advances (StepsMixin.run_steps), so a
    handler never sees a previous step's state.

    In all of these, a `baseline` of None means "not captured yet this step". That
    how a handler knows it's on its first frame and should initialize the state.

  * BuilderState — deliberately NOT per-step: it assigns a probe to a build step that
    may not be current yet and walks it there while other steps fire, so it must
    survive step transitions. WorldMixin.manage_builder owns its lifecycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from sc2.position import Point2
from sc2.unit import Unit
from schema import Step


@dataclass
class WarpConfirm:
    """The unit-count baseline to confirm the warps landed. No `issued` counter: how many
    of `count` are done is measured from the units themselves, so a warp order the game
    dropped is simply re-issued rather than counted as delivered."""
    baseline: int | None = None      # unit count when the step started


@dataclass
class TrainState:
    """How many of the step's `count` have been ORDERED so far.

    Counted rather than measured, unlike warp/morph: a trained unit doesn't exist until
    it pops ~30s later, and `train` deliberately completes at the order (the next step
    shouldn't wait on the Zealot). So the tally is the only record that survives a step
    that could afford 3 of its 7 and has to resume next frame."""
    issued: int = 0


@dataclass
class MorphState:
    """How many `dest` (i.e. archon) to make + the dest-count baseline"""
    target: int = 0                  # how many `dest` this step should produce
    baseline: int | None = None      # dest count when the step started


@dataclass
class StepState:
    """State owned by the step currently being executed. Cleared as one unit on
    advance — see `reset`. Anything that must outlive a step does NOT belong here."""
    
    # A step's trigger gates starting it, and once started, we don't want to re-check
    # the trigger. This is necessary for correctness, e.g. if we're gating on two
    # HighTemplar available to do an archon morph, which falsifies the trigger
    # condition by consuming the templar.
    trigger_fired: bool = False

    # Game-time this became the current step.
    started_at: float = 0.0
    
    warp: WarpConfirm = field(default_factory=WarpConfirm)
    train: TrainState = field(default_factory=TrainState)
    morph: MorphState = field(default_factory=MorphState)

    def reset(self, now: float) -> None:
        """Drop everything the finished step owned, so the next one starts clean.
        `now` stamps when the incoming step became current."""
        self.trigger_fired = False
        self.started_at = now
        self.warp = WarpConfirm()
        self.train = TrainState()
        self.morph = MorphState()


@dataclass
class BuilderState:
    """The one probe this system has pulled off the mineral line, and the build step it
    was pulled for.

    Spans frames AND steps by design: a probe is assigned to a build step that may not
    be current yet, walks to the spot while intervening steps fire, is handed to
    do_build, and is held until the structure appears. `manage_builder` owns the
    lifecycle; the reservation dies when the step it was made for is no longer the next
    build (i.e. at the first train/warp/research step), or when the build completes.

    The invariant, which is also the contract with the build author: AT MOST ONE probe
    is off the mineral line for building at any time. A probe that has to be held
    longer, or for something other than building, is the author's job — send_probe.
    """

    for_step: Step | None = None        # the build step this probe belongs to
    builder_tag: int | None = None      # the probe, once one has been assigned
    spot: Point2 | Unit | None = None   # placement point, or the geyser for gas
    issued: bool = False                # do_build has given it the build order
    baseline: int | None = None         # structure count when the step started

    def clear(self) -> None:
        self.for_step = None
        self.builder_tag = None
        self.spot = None
        self.issued = False
        self.baseline = None
