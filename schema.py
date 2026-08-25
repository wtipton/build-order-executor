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
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

import catalog

_TRIGGER_KEYS = ("supply", "time", "minerals", "vespene", "count")

# Symbolic locations resolved by the bot at runtime (bot._resolve_place):
#   main..sixth   -> our bases by distance from our start (main, natural, third,
#                    fourth, fifth, sixth) — the Nth-nearest expansion, clamped to
#                    what the map provides.
#   main_ramp     -> the top of our main base's ramp (the choke we defend at, and
#                    the default combat-unit rally point)
#   enemy_main    -> the enemy start location (for scouting)
#   enemy_natural -> the enemy natural expansion
#   proxy         -> out near the enemy but off their doorstep (their ~4th base) —
#                    for proxying a pylon/building and warping units in there.
Place = Literal["main", "natural", "third", "fourth", "fifth", "sixth", "main_ramp",
                "enemy_main", "enemy_natural", "proxy"]


class Trigger(BaseModel):
    """A firing condition. Exactly one key must be set.

    supply/time/minerals/vespene: fire when that value is >= the given number
    (time in game-seconds).
    count:  {UnitType: N} — fire once we have N completed units/structures of that
            type, e.g. {Probe: 18}, {Gateway: 5}, or {CyberneticsCore: 1} for
            "once the Core is done".
    """

    model_config = ConfigDict(extra="forbid")
    supply: int | None = None
    time: float | None = None
    minerals: int | None = None
    vespene: int | None = None
    count: dict[str, int] | None = None

    def __str__(self) -> str:
        """The condition, e.g. `count Pylon=2` or `supply>=19`."""
        if self.count is not None:
            name, n = next(iter(self.count.items()))
            return f"count {name}={n}"
        for key in _TRIGGER_KEYS:
            want = getattr(self, key)
            if want is not None:
                return f"{key}>={want:.0f}"
        return "?"

    @model_validator(mode="after")
    def _exactly_one(self) -> "Trigger":
        present = [k for k in _TRIGGER_KEYS if getattr(self, k) is not None]
        if len(present) != 1:
            raise ValueError(f"a trigger needs exactly one of {list(_TRIGGER_KEYS)}, got {present or 'none'}")
        if self.count is not None:
            if len(self.count) != 1 or any(v < 1 for v in self.count.values()):
                raise ValueError("count trigger must be a single {UnitType: N>=1}, e.g. {Probe: 18}")
            catalog.require_countable(next(iter(self.count)))  # reject typo'd/unknown names at load
        return self


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: Trigger          # when the action fires
    note: str = ""       # human comment, echoed in the [build] log

    def __str__(self) -> str:
        """The action and its arguments, e.g. `build what=Nexus where=natural`. Trigger
        and note are left out — callers that want them have them."""
        args = self.model_dump(exclude={"at", "prewalk", "note", "do"}, exclude_none=True)
        return f"{self.do} {' '.join(f'{k}={v}' for k, v in args.items())}".rstrip()


class BuildStep(_StepBase):
    do: Literal["build"]
    what: str            # structure / expansion (UnitTypeId name, e.g. Pylon, Nexus)
    where: Place | None = None      # where to place it; None = auto (at home). "proxy" builds it near the enemy.
    label: str | None = None        # build with a specific sent probe (see send_probe) instead of auto-selecting
    # When to pull a probe off the line and walk it to the spot. Default: as soon as we
    # can afford the building. Set this to send it EARLIER, e.g. {minerals: 300} on a
    # 400 Nexus so the walk overlaps the saving. Independent of `at`.
    prewalk: Trigger | None = None

    _check = field_validator("what")(staticmethod(catalog.require_structure))


class TrainStep(_StepBase):
    do: Literal["train"]
    what: str            # unit (UnitTypeId name, e.g. Adept)

    _check = field_validator("what")(staticmethod(catalog.require_trainable))


class WarpStep(_StepBase):
    do: Literal["warp"]
    what: str            # unit to warp in (Zealot, Stalker, Sentry, Adept, HighTemplar, DarkTemplar)
    where: Place = "proxy"  # which pylon to warp at (the ready pylon nearest this place)

    _check = field_validator("what")(staticmethod(catalog.require_warpable))


class MorphStep(_StepBase):
    do: Literal["morph"]
    to: str              # warpgate | gateway (convert 1:1) | archon (combine 2 HT/DT). ->warpgate needs Warpgate research.
    count: int | None = None  # how many to make; omit = as many as possible (all sources / all pairs)

    _check = field_validator("to")(staticmethod(catalog.require_morph))


class ResearchStep(_StepBase):
    do: Literal["research"]
    what: str            # upgrade friendly name (Warpgate, Blink, Charge, Storm, GroundWeapons1, ...)

    _check = field_validator("what")(staticmethod(catalog.require_research))


class HallucinateStep(_StepBase):
    do: Literal["hallucinate"]  # Sentry hallucinates a Phoenix (scout) — no options


class ChronoStep(_StepBase):
    do: Literal["chrono"]
    target: str          # structure type to chrono-boost (must be producing/researching)

    _check = field_validator("target")(staticmethod(catalog.require_chrono_target))


class SendProbeStep(_StepBase):
    do: Literal["send_probe"]
    where: Place         # where to send it
    label: str           # label this probe so later steps can move it again, build with it, or return it


class ReturnProbeStep(_StepBase):
    do: Literal["return_probe"]
    label: str           # a probe previously sent via send_probe — hand it back to the mining/build pool


class SetRallyPointStep(_StepBase):
    do: Literal["set_rally_point"]
    where: Place = "natural"


class RallyAndTransferProbesStep(_StepBase):
    do: Literal["rally_and_transfer_probes"]
    base: int = Field(ge=1)  # which of our bases (1 = main, 2 = natural, 3 = third, ...) to saturate
    # Transfers every other base's excess mineral workers to this base, rallies all
    # Nexuses onto its minerals, and makes it the base returned probes mine at.


class GasWorkersStep(_StepBase):
    do: Literal["gas_workers"]
    count: int           # desired total workers in gas


class WaitStep(_StepBase):
    do: Literal["wait"]
    # Does nothing; completes as soon as its trigger fires.


class WorkersStep(_StepBase):
    do: Literal["workers"]
    state: Literal["stop", "start"]  # toggle continuous probe production


Step = Annotated[
    Union[
        BuildStep, TrainStep, WarpStep, MorphStep, ResearchStep, HallucinateStep,
        ChronoStep, SendProbeStep, ReturnProbeStep, SetRallyPointStep, RallyAndTransferProbesStep,
        GasWorkersStep, WorkersStep, WaitStep,
    ],
    Field(discriminator="do"),
]


class BuildConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "build"
    steps: list[Step]


def load_build(path: str | Path) -> BuildConfig:
    p = Path(path)
    with open(p) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        raise ValueError(f"expected a YAML list of steps in {path}, got {type(data).__name__}")
    steps = TypeAdapter(list[Step]).validate_python(data)
    return BuildConfig(name=p.stem, steps=steps)
