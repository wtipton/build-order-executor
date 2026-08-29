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
    """The units this step has warped in, by tag.

    Tags rather than a count of the type, because a type count can't tell a warped unit
    from one a Gateway happened to finish training mid-step, and would count the latter
    toward `count` — warping fewer than asked. Warp-in is distinguishable: it puts the
    unit on the map immediately at build_progress < 1, whereas a trained unit doesn't
    exist until it pops out complete. So anything of this type seen part-built is ours."""
    # Tags mid-warp on the step's first frame, so ordered by an earlier step. Subtracted
    # from every later reading. None means the first frame hasn't happened yet.
    warping_at_start: set[int] | None = None
    # Tags seen mid-warp since, minus the above: the units this step has warped in. Kept
    # after they finish, since a warp that completed still counts toward `count`.
    warped_in: set[int] = field(default_factory=set)


@dataclass
class BuildState:
    """The structure-count baseline for the step as a WHOLE, against which `count` is
    measured (`amount - baseline` is how many we've put up).

    Distinct from BuilderState.baseline, which is re-taken for each structure and only
    says when to move on to the next one. This one can't live there: that field is reset
    every time a structure lands, which is precisely the event being measured."""
    baseline: int | None = None


@dataclass
class TrainState:
    """How many of the step's `count` have been ORDERED so far.

    Counted rather than measured from the units, unlike warp/morph: a trained unit doesn't
    exist until it finishes ~30s later, and `train` completes at the order so the next
    step doesn't wait on it. That makes this count the only record of progress when a step
    can afford 3 of its 7 and has to finish the rest on a later frame."""
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
    
    build: BuildState = field(default_factory=BuildState)
    warp: WarpConfirm = field(default_factory=WarpConfirm)
    train: TrainState = field(default_factory=TrainState)
    morph: MorphState = field(default_factory=MorphState)

    def reset(self, now: float) -> None:
        """Drop everything the finished step owned, so the next one starts clean.
        `now` stamps when the incoming step became current."""
        self.trigger_fired = False
        self.started_at = now
        self.build = BuildState()
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
    build (i.e. at the first train/warp/research step), or when the build completes. On a
    `count:` step the probe is held across the whole group — see
    `reset_for_next_structure`, which keeps it while dropping the finished structure's
    spot and order.

    The invariant, which is also the contract with the build author: AT MOST ONE probe
    is off the mineral line for building at any time. A probe that has to be held
    longer, or for something other than building, is the author's job — send_probe.
    """

    for_step: Step | None = None        # the build step this probe belongs to
    builder_tag: int | None = None      # the probe, once one has been assigned
    spot: Point2 | Unit | None = None   # placement point, or the geyser for gas
    issued: bool = False                # do_build has given it the build order
    baseline: int | None = None         # structure count when THIS structure was started

    def clear(self) -> None:
        self.for_step = None
        self.builder_tag = None
        self.spot = None
        self.issued = False
        self.baseline = None

    def reset_for_next_structure(self) -> None:
        """Move on to the next structure of a `count:` group: forget this structure's
        spot/order/baseline, but KEEP the probe and the step.

        Keeping `builder_tag` is what holds the probe. While it's set, the tag stays in
        `_probes_unavailable_to_automation`, so manage_economy won't reclaim the probe and
        send it back to a mineral patch partway through a group. One probe builds all of
        them, like a player queueing several buildings on one worker."""
        self.spot = None
        self.issued = False
        self.baseline = None
