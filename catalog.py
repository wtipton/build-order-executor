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
names (RESEARCH), self-cast spells with their energy cost (CAST), and the morph
specs (MORPH). Scope is macro-to-a-timing, so CAST is self-cast only (no targeted
spells) and MORPH covers gateway<->warpgate plus the Archon combine.
"""

from __future__ import annotations

from dataclasses import dataclass

from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId
from sc2.dicts.unit_trained_from import UNIT_TRAINED_FROM
from sc2.dicts.unit_train_build_abilities import TRAIN_INFO
from sc2.dicts.upgrade_researched_from import UPGRADE_RESEARCHED_FROM

# Protoss buildings that produce units (their `trained_from` == one of these).
PRODUCTION_BUILDINGS: frozenset[U] = frozenset(
    {U.NEXUS, U.GATEWAY, U.WARPGATE, U.ROBOTICSFACILITY, U.STARGATE}
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


# ============================================================ curated: casts
@dataclass(frozen=True)
class CastSpec:
    ability: AbilityId
    caster: U
    energy: int


# Friendly spell name -> how to cast it. Self-casts only (no target needed):
# Hallucination spawns the fake unit next to the Sentry. Targeted spells (Storm,
# Feedback, Force Field) are out of macro scope.
CAST: dict[str, CastSpec] = {
    "Hallucination": CastSpec(AbilityId.HALLUCINATION_PHOENIX, U.SENTRY, 75),  # default: Phoenix (scout)
    "HallucinationPhoenix": CastSpec(AbilityId.HALLUCINATION_PHOENIX, U.SENTRY, 75),
    "HallucinationArchon": CastSpec(AbilityId.HALLUCINATION_ARCHON, U.SENTRY, 75),
}


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
# Each returns the value unchanged when valid, else raises ValueError listing the
# valid options — schema.py wires these into pydantic field validators.
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


def _require_key(name: str, mapping: dict[str, object], kind: str) -> str:
    if name not in mapping:
        raise ValueError(f"unknown {kind} {name!r}; valid: {', '.join(mapping)}")
    return name


def require_research(name: str) -> str:
    return _require_key(name, RESEARCH, "research")


def require_cast(name: str) -> str:
    return _require_key(name, CAST, "cast")


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
