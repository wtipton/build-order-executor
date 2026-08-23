"""The mutable state BuildOrderBot carries between frames.

Two lifetimes:

  * StepState — everything scoped to the ONE step currently being executed. The
    engine clears it as a unit the moment it advances (StepsMixin.run_steps), so a
    handler never sees a previous step's state.

    In all of these, a `baseline` of None means "not captured yet this step". That
    how a handler knows it's on its first frame and should initialize the state.

  * PrewalkState — deliberately NOT per-step: the pre-walk reserves a probe for a
    FUTURE build step and walks it there while other steps fire, so it must survive
    step transitions. It has its own lifecycle (WorldMixin.manage_prebuild clears it
    when the build it was reserved for changes).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from sc2.position import Point2
from sc2.unit import Unit
from schema import Step


@dataclass
class BuildConfirm:
    """The count baseline to confirm a newly-built structure and the probe committed
       to it (so a long walk isn't re-issued as a duplicate build."""
    baseline: int | None = None      # structure count when the step started; None = not yet captured
    builder_tag: int | None = None   # the probe committed to this build


@dataclass
class WarpConfirm:
    """The unit-count baseline to confirm the warp landed"""
    baseline: int | None = None      # unit count when the step started


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
    
    build: BuildConfirm = field(default_factory=BuildConfirm)
    warp: WarpConfirm = field(default_factory=WarpConfirm)
    morph: MorphState = field(default_factory=MorphState)

    def reset(self, now: float) -> None:
        """Drop everything the finished step owned, so the next one starts clean.
        `now` stamps when the incoming step became current."""
        self.trigger_fired = False
        self.started_at = now
        self.build = BuildConfirm()
        self.warp = WarpConfirm()
        self.morph = MorphState()


@dataclass
class PrewalkState:
    """A probe pre-walked to a future build's spot, so construction can start the
    instant we can afford it. Spans steps by design."""

    for_step: Step | None = None     # the (future) build step this probe is reserved for
    builder_tag: int | None = None
    target: Point2 | Unit | None = None  # placement point, or the geyser for gas

    def clear(self) -> None:
        self.for_step = None
        self.builder_tag = None
        self.target = None
