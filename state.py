"""Small dataclasses grouping the per-step in-flight state BuildOrderBot tracks,
so `__init__` isn't a wall of loose `_foo` vars and each handler's owned state is
explicit.

  * BuildConfirm — do_build: which step is building, the count baseline to confirm
    against, and the probe committed to it (so we wait for a long walk, not re-issue).
  * WarpConfirm  — do_warp: which step is warping + its unit-count baseline.
  * MorphState   — do_morph: which step is morphing, how many to make, dest baseline.
  * Reservation  — the pre-walk builder: the step reserved for, the probe walking,
    and the target (a placement Point2 or a geyser Unit).
"""

from __future__ import annotations

from dataclasses import dataclass

from schema import Step


@dataclass
class BuildConfirm:
    step: Step | None = None
    baseline: int = 0
    builder_tag: int | None = None


@dataclass
class WarpConfirm:
    step: Step | None = None
    baseline: int = 0


@dataclass
class MorphState:
    step: Step | None = None
    target: int = 0
    baseline: int = 0


@dataclass
class Reservation:
    step: Step | None = None
    builder_tag: int | None = None
    target: object = None  # Point2 (placement) or geyser Unit (gas)

    def clear(self) -> None:
        self.step = None
        self.builder_tag = None
        self.target = None
