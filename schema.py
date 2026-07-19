"""Pydantic schema for build-order configs — the authoritative YAML spec.

`load_build()` parses a builds/*.yaml into a validated `BuildConfig`. Unknown
fields are rejected (`extra="forbid"`), so typos and made-up fields fail loudly
instead of being silently ignored. Everything defined here is actually consumed
by bot.py / run.py; there are no decorative fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

_TRIGGER_KEYS = ("supply", "time", "minerals", "vespene", "after")


class Trigger(BaseModel):
    """A firing condition. Exactly one key must be set.

    supply/time/minerals/vespene: fire when that value is >= the given number
    (time in game-seconds). after: fire when a structure of that name is ready.
    """

    model_config = ConfigDict(extra="forbid")
    supply: int | None = None
    time: float | None = None
    minerals: int | None = None
    vespene: int | None = None
    after: str | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "Trigger":
        present = [k for k in _TRIGGER_KEYS if getattr(self, k) is not None]
        if len(present) != 1:
            raise ValueError(f"a trigger needs exactly one of {list(_TRIGGER_KEYS)}, got {present or 'none'}")
        return self


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: Trigger          # when the action fires
    note: str = ""       # human comment, echoed in the [build] log


class BuildStep(_StepBase):
    do: Literal["build"]
    what: str            # structure / expansion (UnitTypeId name, e.g. Pylon, Nexus)
    prewalk: Trigger | None = None  # when to pre-walk the builder into place (defaults to `at`)


class TrainStep(_StepBase):
    do: Literal["train"]
    what: str            # unit (UnitTypeId name, e.g. Adept)


class ChronoStep(_StepBase):
    do: Literal["chrono"]
    target: str          # structure type to chrono-boost (must be producing)


class ScoutStep(_StepBase):
    do: Literal["scout"]


class RallyStep(_StepBase):
    do: Literal["rally"]
    where: Literal["natural", "main"] = "natural"


class GasWorkersStep(_StepBase):
    do: Literal["gas_workers"]
    count: int           # desired total workers in gas


class MineralsCapStep(_StepBase):
    do: Literal["minerals_cap"]
    count: int           # per-base mineral worker cap


class WorkersStep(_StepBase):
    do: Literal["workers"]
    state: Literal["stop", "start"]  # toggle continuous probe production


Step = Annotated[
    Union[
        BuildStep, TrainStep, ChronoStep, ScoutStep,
        RallyStep, GasWorkersStep, MineralsCapStep, WorkersStep,
    ],
    Field(discriminator="do"),
]


class Economy(BaseModel):
    """Defaults for the automatic economy (a step can override at runtime)."""

    model_config = ConfigDict(extra="forbid")
    continuous_workers: bool = True
    minerals_per_base: int = 16
    gas_workers: int = 0


class BuildConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "build"
    race: Literal["Protoss"] = "Protoss"  # bot is Protoss-only for now; anything else fails validation
    economy: Economy = Field(default_factory=Economy)
    steps: list[Step]


def load_build(path: str | Path) -> BuildConfig:
    with open(path) as f:
        data = yaml.safe_load(f)
    return BuildConfig.model_validate(data)
