"""Pydantic schema for build-order configs — the authoritative YAML spec.

`load_build()` parses a builds/*.yaml into a validated `BuildConfig`. Unknown
fields are rejected (`extra="forbid"`), so typos and made-up fields fail loudly
instead of being silently ignored. Everything defined here is actually consumed
by bot.py / run.py; there are no decorative fields.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Union, get_args

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    field_validator,
    model_validator,
)

import catalog

# ============================================================ case-insensitivity
# Every VALUE the DSL accepts is case-insensitive; field names are not (they're the
# syntax, not the data, and a mis-cased key already fails loudly under extra="forbid").
# Each value is normalized to its canonical spelling as it validates, so nothing
# downstream — dict lookups, logs, the [step] echo — has to think about case.
def _lower(v: object) -> object:
    return v.lower() if isinstance(v, str) else v  # non-str falls through to the real error


Lower = BeforeValidator(_lower)


def _Do(tag: str):
    """A `do:` discriminator value. Needs `Lower` of its own: the union's Discriminator
    picks the model from the raw input, but the chosen model then validates this field,
    and a bare Literal would reject the very casing the discriminator just accepted."""
    return Annotated[Literal[tag], Lower]


# Our own bases, in order of distance from our start — the Nth-nearest expansion,
# clamped to what the map provides. Listed IN ORDER: world.BASE_RANK indexes BASE_PLACES
# to turn a name back into that N, so don't reorder it.
_BasePlace = Literal["main", "natural", "third", "fourth", "fifth", "sixth"]

# Every symbolic location resolved by the bot at runtime (bot._resolve_place) — our
# bases plus the strategic spots. Nested Literals flatten, so get_args gives a flat tuple.
#   main_ramp     -> the top of our main base's ramp (the choke we defend at, and
#                    the default combat-unit rally point)
#   enemy_main    -> the enemy start location (for scouting)
#   enemy_natural -> the enemy natural expansion
#   proxy         -> out near the enemy but off their doorstep (their ~4th base) —
#                    for proxying a pylon/building and warping units in there.
_Place = Literal[_BasePlace, "main_ramp", "enemy_main", "enemy_natural", "proxy"]

# The names themselves. Exported for the two consumers that enumerate places
# (world.BASE_RANK, observe._pylons_by_place): they must read the RAW literals, since
# get_args on the annotated aliases below would hand back the validator, not the names.
BASE_PLACES: tuple[str, ...] = get_args(_BasePlace)
PLACES: tuple[str, ...] = get_args(_Place)

BasePlace = Annotated[_BasePlace, Lower]
Place = Annotated[_Place, Lower]

# A probe's `who:` name. Author-chosen rather than drawn from a fixed set, so lowercasing
# IS the canonical form — and it has to happen here, because the name is a key in both
# BuildConfig's held-probe check and the bot's named_probes dict.
Who = Annotated[str, Lower]

# "How many", where omitting it means the step's own default. None rather than a literal
# 1 so `_StepBase.__str__` (which drops None) doesn't tack `count=1` onto every [step]
# line for the single-unit case that is by far the most common.
Count = Annotated[int, Field(ge=1)] | None


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
                return f"{key}>={want:g}"  # :g not :.0f — a fractional `time` must not read as a whole second
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


_TRIGGER_KEYS = tuple(Trigger.model_fields)


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: Trigger          # when the action fires

    def __str__(self) -> str:
        """The action and its arguments, e.g. `build what=Nexus where=natural`. Trigger
        is left out — callers that want it have it."""
        args = self.model_dump(exclude={"at", "prewalk", "do"}, exclude_none=True)
        return f"{self.do} {' '.join(f'{k}={v}' for k, v in args.items())}".rstrip()


class BuildStep(_StepBase):
    do: _Do("build")
    what: str            # structure / expansion (UnitTypeId name, e.g. Pylon, Nexus)
    where: Place | None = None      # where to place it; None = auto (at home). "proxy" builds it near the enemy.
    who: Who | None = None          # build with the named probe (see send_probe) instead of auto-selecting one
    count: Count = None             # how many to put up, one after another; omit = 1
    # When to pull a probe off the line and walk it to the spot. Default: as soon as we
    # can afford the building. Set this to send it EARLIER, e.g. {minerals: 300} on a
    # 400 Nexus so the walk overlaps the saving. Independent of `at`.
    prewalk: Trigger | None = None

    _check = field_validator("what")(staticmethod(catalog.require_structure))


class TrainStep(_StepBase):
    do: _Do("train")
    what: str            # unit (UnitTypeId name, e.g. Adept)
    count: Count = None  # how many to train; omit = 1

    _check = field_validator("what")(staticmethod(catalog.require_trainable))


class WarpStep(_StepBase):
    do: _Do("warp")
    what: str            # unit to warp in (Zealot, Stalker, Sentry, Adept, HighTemplar, DarkTemplar)
    where: Place         # which pylon to warp at (the ready pylon nearest this place)
    count: Count = None  # how many to warp in; omit = 1. A round goes out across every
                         # ready Warpgate at once, as many as `count` and the money allow.

    _check = field_validator("what")(staticmethod(catalog.require_warpable))


class MorphStep(_StepBase):
    do: _Do("morph")
    to: str              # warpgate | gateway (convert 1:1) | archon (combine 2 HT/DT). ->warpgate needs Warpgate research.
    count: Count = None  # how many to make; omit = 1

    _check = field_validator("to")(staticmethod(catalog.require_morph))


class ResearchStep(_StepBase):
    do: _Do("research")
    what: str            # upgrade friendly name (Warpgate, Blink, Charge, Storm, GroundWeapons1, ...)

    _check = field_validator("what")(staticmethod(catalog.require_research))


class HallucinateStep(_StepBase):
    do: _Do("hallucinate")  # Sentry hallucinates a Phoenix (scout) — no options


class ChronoStep(_StepBase):
    do: _Do("chrono")
    target: str          # structure type to chrono-boost (must be producing/researching)

    _check = field_validator("target")(staticmethod(catalog.require_chrono_target))


class SendProbeStep(_StepBase):
    do: _Do("send_probe")
    where: Place         # where to send it
    who: Who             # name this probe, so later steps can move it again, build with it, or return it


class ReturnProbeStep(_StepBase):
    do: _Do("return_probe")
    who: Who             # a probe named by send_probe — hand it back to the mining/build pool


class SetRallyPointStep(_StepBase):
    do: _Do("set_rally_point")
    where: Place         # where newly-produced combat units gather


class RallyAndTransferProbesStep(_StepBase):
    do: _Do("rally_and_transfer_probes")
    where: BasePlace     # which of our bases to saturate
    # Transfers every other base's excess mineral workers to this base, rallies all
    # Nexuses onto its minerals, and makes it the base returned probes mine at.


class SetGasProbesStep(_StepBase):
    do: _Do("set_gas_probes")
    count: int           # desired total probes in gas


class WaitStep(_StepBase):
    do: _Do("wait")
    # Does nothing; completes as soon as its trigger fires.


class CutProbesStep(_StepBase):
    do: _Do("cut_probes")


class ResumeProbesStep(_StepBase):
    do: _Do("resume_probes")


def _do_tag(step: object) -> str | None:
    """Pick the step type from `do`, case-insensitively.

    A plain `discriminator="do"` reads the tag straight off the raw input, so `do: Build`
    would match no member — the union is chosen BEFORE any field validator could lower it.
    Hence a callable, which also has to handle an already-built model (BuildConfig
    re-validates its steps list)."""
    do = step.get("do") if isinstance(step, dict) else getattr(step, "do", None)
    return do.lower() if isinstance(do, str) else None


_STEP_TYPES = (
    BuildStep, TrainStep, WarpStep, MorphStep, ResearchStep, HallucinateStep,
    ChronoStep, SendProbeStep, ReturnProbeStep, SetRallyPointStep, RallyAndTransferProbesStep,
    SetGasProbesStep, CutProbesStep, ResumeProbesStep, WaitStep,
)

# Each member is tagged with its own `do` literal, read off the model so the tag and the
# field can't drift apart.
Step = Annotated[
    Union[tuple(  # noqa: UP007 — a runtime-built union can't use `|` syntax
        Annotated[cls, Tag(get_args(cls.model_fields["do"].annotation)[0])] for cls in _STEP_TYPES
    )],
    Discriminator(_do_tag),
]


class BuildConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = "build"
    steps: list[Step]

    @model_validator(mode="after")
    def _who_is_held_when_used(self) -> "BuildConfig":
        """Every `who:` must name a probe that an EARLIER send_probe bound and that no
        return_probe has since handed back."""
        held: set[str] = set()
        for i, step in enumerate(self.steps):
            who = getattr(step, "who", None)
            if isinstance(step, SendProbeStep):
                held.add(step.who)
            elif who is not None:
                if who not in held:
                    have = ", ".join(sorted(held)) or "none"
                    raise ValueError(
                        f"step {i} ({step.do} who: {who!r}): no probe named {who!r} is held here. "
                        f"An earlier send_probe must name it, with no return_probe since. "
                        f"Held at this point: {have}"
                    )
                if isinstance(step, ReturnProbeStep):
                    held.discard(who)
        return self


def load_build(path: str | Path) -> BuildConfig:
    p = Path(path)
    with open(p) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, list):
        raise ValueError(f"expected a YAML list of steps in {path}, got {type(data).__name__}")
    steps = TypeAdapter(list[Step]).validate_python(data)
    return BuildConfig(name=p.stem, steps=steps)
