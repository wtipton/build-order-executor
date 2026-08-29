"""Single source of truth mapping build-order names to game objects.

Imported by BOTH schema.py (load-time validation) and bot.py (execution), so a
build that names a nonexistent or unsupported unit / structure / upgrade / cast /
morph fails at `load_build` with a clear message listing the valid options —
never a mid-game KeyError a third party can't diagnose from the log.

Most tables are DERIVED from python-sc2's generated tech-tree dicts, so they stay
correct across patches with no hand maintenance:

  * PRODUCER / TRAINABLE_UNITS / BUILDABLE_STRUCTURES  <- UNIT_TRAINED_FROM
  * WARP_ABILITY                                        <- TRAIN_INFO[WARPGATE]
  * PROTOSS_UPGRADES (what RESEARCH must cover)         <- UPGRADE_RESEARCHED_FROM

Only the things the tech tree doesn't carry are curated here: friendly upgrade
names (RESEARCH), the morph specs (MORPH), and the two spell steps' caster/energy/
ability constants (CHRONO_*, HALLUCINATION_*). Scope is macro-to-a-timing, so the
only spells are the two a build order names — `chrono` and `hallucinate` — and
MORPH covers gateway<->warpgate plus the Archon combine. No targeted army spells
(Storm, Feedback, Force Field).
"""

from __future__ import annotations

from dataclasses import dataclass

from sc2.ids.ability_id import AbilityId
from sc2.ids.buff_id import BuffId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM

# Game frames per second — the unit for every `cost.time` in game data.
FRAMES_PER_SEC = 22.4

# Warp Gate research cuts GATEWAY unit train time by exactly 50% (5.0.16b patch notes;
# was 40%). The static `cost.time` in game data is the PRE-research base and does NOT
# reflect it, so anything reasoning about production time must apply this itself.
WARPGATE_TRAIN_SPEEDUP = 0.5

# How many units a production structure holds in its queue. Not exposed in game data or
# the protocol, so unlike the tables below this is hand-maintained. Ordering past it is
# silently dropped by the game, which is why the scheduler refuses rather than over-commit.
MAX_PRODUCTION_QUEUE = 5

# Protoss buildings that produce units (their `trained_from` == one of these).
PRODUCTION_BUILDINGS: frozenset[U] = frozenset(
    {U.NEXUS, U.GATEWAY, U.WARPGATE, U.ROBOTICSFACILITY, U.STARGATE}
)
# Producers whose units need somewhere to gather: everything except the Nexus, whose
# probes go to minerals rather than to the army's rally point.
COMBAT_PRODUCTION: frozenset[U] = PRODUCTION_BUILDINGS - {U.NEXUS}

# Producers that can be WALLED IN: their units walk out on the ground, so a building
# packed against the last exit traps them. A Warpgate warps its units in wherever the
# pylon is and a Stargate's fly out, so neither can be blocked; a Nexus can, because
# probes come out on foot.
BLOCKABLE_PRODUCTION: frozenset[U] = frozenset(
    {U.GATEWAY, U.ROBOTICSFACILITY, U.NEXUS}
)

# All Protoss structures that can research upgrades / be chrono'd (for filtering
# "our" upgrades out of the all-race UPGRADE_RESEARCHED_FROM).
PROTOSS_BUILDINGS: frozenset[U] = frozenset(
    {U.NEXUS, U.GATEWAY, U.WARPGATE, U.CYBERNETICSCORE, U.TWILIGHTCOUNCIL, U.FORGE,
     U.ROBOTICSFACILITY, U.ROBOTICSBAY, U.STARGATE, U.FLEETBEACON, U.TEMPLARARCHIVE,
     U.DARKSHRINE}
)


# ============================================================ name -> UnitTypeId
def unit_id(name: str) -> U:
    """Resolve a UnitTypeId by name (case-insensitive). Raises ValueError with a
    clean message rather than the bare KeyError UnitTypeId would throw."""
    try:
        return U[name.upper()]
    except KeyError:
        raise ValueError(f"unknown unit type {name!r}") from None


# ============================================================ derived from data
def _producer_of(u: U) -> U | None:
    """The structure that trains/builds `u`. Gateway units list both GATEWAY and
    WARPGATE — prefer the non-Warpgate one (the real producer / warp is separate)."""
    srcs = UNIT_TRAINED_FROM.get(u, set())
    non_wg = [s for s in srcs if s != U.WARPGATE]
    if non_wg:
        return non_wg[0]
    return next(iter(srcs)) if srcs else None


PRODUCER: dict[U, U] = {u: p for u in UNIT_TRAINED_FROM if (p := _producer_of(u)) is not None}

# Units trained from a production building (Probe..Mothership) — NOT structures
# (those are "trained from" a Probe) and not the Archon (a morph, no producer).
TRAINABLE_UNITS: frozenset[U] = frozenset(
    u for u, p in PRODUCER.items() if p in PRODUCTION_BUILDINGS
)
# Protoss structures a Probe builds (Pylon, Nexus, Assimilator, Gateway, ...).
BUILDABLE_STRUCTURES: frozenset[U] = frozenset(
    u for u, p in PRODUCER.items() if p == U.PROBE
)

# unit -> the warp-in readiness ability (for get_available_abilities checks).
WARP_ABILITY: dict[U, AbilityId] = {
    u: info["ability"] for u, info in TRAIN_INFO[U.WARPGATE].items()
}

# train/build ability -> what it produces. The reverse of TRAIN_INFO, so a structure's
# queued orders can be mapped back to what they're building, and hence how long they take.
# Derived like the tables above — no hand maintenance.
TRAIN_ABILITY_UNIT: dict[AbilityId, U] = {
    info["ability"]: unit
    for units in TRAIN_INFO.values()
    for unit, info in units.items()
}

# Types a `count:` trigger may reference — anything the player can OWN: every
# buildable structure and trainable unit, plus the two morph-only results a build
# refers to (Warp Gate, morphed from a Gateway; Archon, from two templar).
COUNTABLE: frozenset[U] = BUILDABLE_STRUCTURES | TRAINABLE_UNITS | {U.WARPGATE, U.ARCHON}

# Every upgrade researched from a Protoss building — RESEARCH below must cover
# exactly this set (asserted at import, so a patch adding one fails loudly here).
PROTOSS_UPGRADES: frozenset[UpgradeId] = frozenset(
    up for up, b in UPGRADE_RESEARCHED_FROM.items() if b in PROTOSS_BUILDINGS
)


# ============================================================ curated: research
# Friendly name -> UpgradeId, covering ALL Protoss upgrades. Issued generically
# via self.research(UpgradeId), so no ability/building needs recording here.
RESEARCH: dict[str, UpgradeId] = {
    "Warpgate": UpgradeId.WARPGATERESEARCH,
    "Blink": UpgradeId.BLINKTECH,
    "Charge": UpgradeId.CHARGE,
    "Glaives": UpgradeId.ADEPTPIERCINGATTACK,          # Adept: Resonating Glaives
    "ShadowStride": UpgradeId.DARKTEMPLARBLINKUPGRADE,  # Dark Templar blink
    "Storm": UpgradeId.PSISTORMTECH,
    "ThermalLance": UpgradeId.EXTENDEDTHERMALLANCE,     # Colossus range
    "GraviticDrive": UpgradeId.GRAVITICDRIVE,           # Warp Prism speed
    "GraviticBooster": UpgradeId.OBSERVERGRAVITICBOOSTER,  # Observer speed
    "PhoenixRange": UpgradeId.PHOENIXRANGEUPGRADE,      # Anion Pulse-Crystals
    "VoidRaySpeed": UpgradeId.VOIDRAYSPEEDUPGRADE,      # Flux Vanes
    "TempestGroundAttack": UpgradeId.TEMPESTGROUNDATTACKUPGRADE,
    "GroundWeapons1": UpgradeId.PROTOSSGROUNDWEAPONSLEVEL1,
    "GroundWeapons2": UpgradeId.PROTOSSGROUNDWEAPONSLEVEL2,
    "GroundWeapons3": UpgradeId.PROTOSSGROUNDWEAPONSLEVEL3,
    "GroundArmor1": UpgradeId.PROTOSSGROUNDARMORSLEVEL1,
    "GroundArmor2": UpgradeId.PROTOSSGROUNDARMORSLEVEL2,
    "GroundArmor3": UpgradeId.PROTOSSGROUNDARMORSLEVEL3,
    "Shields1": UpgradeId.PROTOSSSHIELDSLEVEL1,
    "Shields2": UpgradeId.PROTOSSSHIELDSLEVEL2,
    "Shields3": UpgradeId.PROTOSSSHIELDSLEVEL3,
    "AirWeapons1": UpgradeId.PROTOSSAIRWEAPONSLEVEL1,
    "AirWeapons2": UpgradeId.PROTOSSAIRWEAPONSLEVEL2,
    "AirWeapons3": UpgradeId.PROTOSSAIRWEAPONSLEVEL3,
    "AirArmor1": UpgradeId.PROTOSSAIRARMORSLEVEL1,
    "AirArmor2": UpgradeId.PROTOSSAIRARMORSLEVEL2,
    "AirArmor3": UpgradeId.PROTOSSAIRARMORSLEVEL3,
}


# ============================================================ curated: spells
# The only two spells a build order names are Chrono Boost and Hallucination, each
# a first-class step (`chrono` / `hallucinate`) rather than a generic "cast": this
# is a build-order executor, not a general playing bot, so there's no army micro /
# targeted spells (Storm, Feedback, Force Field) to justify a generic verb. Each
# spell's caster / energy cost / ability lives here so both are specified the same
# way and bot.py just consumes them.

# Chrono Boost: the Nexus targets one of its own structures that's producing/
# researching. (CHRONO_BUFF tells us a target is already boosted.)
CHRONO_CASTER: U = U.NEXUS
CHRONO_ENERGY: int = 50
CHRONO_ABILITY: AbilityId = AbilityId.EFFECT_CHRONOBOOSTENERGYCOST
CHRONO_BUFF: BuffId = BuffId.CHRONOBOOSTENERGYCOST

# Hallucination: a build order only ever hallucinates a Phoenix (a fast flying
# scout), so it's a single fixed command with no unit to choose.
HALLUCINATION_CASTER: U = U.SENTRY
HALLUCINATION_ENERGY: int = 75
HALLUCINATION_ABILITY: AbilityId = AbilityId.HALLUCINATION_PHOENIX


# ============================================================ curated: morphs
@dataclass(frozen=True)
class MorphSpec:
    sources: tuple[U, ...]  # unit/structure type(s) that can morph into `dest`
    dest: U                 # what they become
    ability: AbilityId
    consumes: int           # source units consumed per dest produced (1: convert; 2: combine)


MORPH: dict[str, MorphSpec] = {
    "warpgate": MorphSpec((U.GATEWAY,), U.WARPGATE, AbilityId.MORPH_WARPGATE, 1),
    "gateway": MorphSpec((U.WARPGATE,), U.GATEWAY, AbilityId.MORPH_GATEWAY, 1),
    "archon": MorphSpec((U.HIGHTEMPLAR, U.DARKTEMPLAR), U.ARCHON, AbilityId.MORPH_ARCHON, 2),
}


# ============================================================ validators (load-time)
# Each returns the CANONICAL spelling of a valid value, else raises ValueError listing
# the valid options — schema.py wires these into pydantic field validators.
#
# Every value the DSL accepts is case-insensitive, and each of these normalizes as it
# validates, so nothing downstream has to think about case. Unit/structure names are the
# one exception to normalizing: `unit_id` resolves them case-insensitively and every
# consumer re-resolves through it, and the game's own display capitalization isn't
# available without a live game (see observe._unit_name) — so the author's spelling is
# passed through rather than mangled into CYBERNETICSCORE.
def _require_in(name: str, allowed: frozenset[U], kind: str) -> str:
    u = unit_id(name)  # raises for a non-unit name first
    if u not in allowed:
        opts = ", ".join(sorted(x.name.title() for x in allowed))
        raise ValueError(f"{name!r} is not a valid {kind}; valid: {opts}")
    return name


def require_structure(name: str) -> str:
    return _require_in(name, BUILDABLE_STRUCTURES, "structure to build")


def require_trainable(name: str) -> str:
    return _require_in(name, TRAINABLE_UNITS, "unit to train")


def require_warpable(name: str) -> str:
    return _require_in(name, frozenset(WARP_ABILITY), "unit to warp in")


def require_chrono_target(name: str) -> str:
    return _require_in(name, BUILDABLE_STRUCTURES, "chrono target structure")


def require_countable(name: str) -> str:
    return _require_in(name, COUNTABLE, "unit/structure to count")


def _require_key(name: str, mapping: dict[str, object], kind: str) -> str:
    """Look `name` up case-insensitively and return the mapping's OWN spelling of it.

    Canonicalizing here isn't cosmetic: `do_research` and `do_morph` index RESEARCH/MORPH
    with the stored value at runtime, so accepting `blink` without normalizing it to
    `Blink` would trade a clean load-time error for a mid-game KeyError."""
    canonical = {k.lower(): k for k in mapping}.get(name.lower()) if isinstance(name, str) else None
    if canonical is None:
        raise ValueError(f"unknown {kind} {name!r}; valid: {', '.join(mapping)}")
    return canonical


def require_research(name: str) -> str:
    return _require_key(name, RESEARCH, "research")


def require_morph(name: str) -> str:
    return _require_key(name, MORPH, "morph target")


# Invariant: friendly RESEARCH names cover exactly the Protoss upgrade set. If a
# patch adds/removes a Protoss upgrade, this trips at import so we notice.
_covered = frozenset(RESEARCH.values())
assert _covered == PROTOSS_UPGRADES, (
    "RESEARCH is out of sync with the game's Protoss upgrades: "
    f"missing={sorted(u.name for u in PROTOSS_UPGRADES - _covered)} "
    f"extra={sorted(u.name for u in _covered - PROTOSS_UPGRADES)}"
)
