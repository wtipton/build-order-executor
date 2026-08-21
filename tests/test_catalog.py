"""Tier 0 — catalog table invariants (pure, no bot).

These pin the derived-from-game-data tables and the curated ones together, so a
python-sc2 / patch change that renames an enum or drops a unit fails here loudly
instead of at runtime mid-build.
"""

from __future__ import annotations

import catalog
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId


def test_research_covers_exactly_protoss_upgrades():
    assert set(catalog.RESEARCH.values()) == set(catalog.PROTOSS_UPGRADES)


def test_every_trainable_unit_has_a_producer():
    for u in catalog.TRAINABLE_UNITS:
        assert u in catalog.PRODUCER
        assert catalog.PRODUCER[u] in catalog.PRODUCTION_BUILDINGS


def test_warp_units_are_trainable_and_map_to_abilities():
    for u, ability in catalog.WARP_ABILITY.items():
        assert u in catalog.TRAINABLE_UNITS
        assert isinstance(ability, AbilityId)


def test_derived_sets_nonempty():
    assert catalog.TRAINABLE_UNITS
    assert catalog.BUILDABLE_STRUCTURES
    assert catalog.PROTOSS_UPGRADES


def test_morph_specs_reference_real_units():
    for name, spec in catalog.MORPH.items():
        assert spec.sources and all(isinstance(s, U) for s in spec.sources)
        assert isinstance(spec.dest, U)
        assert isinstance(spec.ability, AbilityId)
        assert spec.consumes >= 1


def test_spell_constants_well_formed():
    assert isinstance(catalog.CHRONO_ABILITY, AbilityId)
    assert isinstance(catalog.HALLUCINATION_ABILITY, AbilityId)
    assert catalog.CHRONO_ENERGY > 0 and catalog.HALLUCINATION_ENERGY > 0


# Every derived/curated name a build can use must pass its own validator (and the
# validators must reject a clearly-wrong value).
def test_validators_accept_all_valid_names():
    for u in catalog.BUILDABLE_STRUCTURES:
        assert catalog.require_structure(u.name) == u.name
    for u in catalog.TRAINABLE_UNITS:
        assert catalog.require_trainable(u.name) == u.name
    for u in catalog.WARP_ABILITY:
        assert catalog.require_warpable(u.name) == u.name
    for name in catalog.RESEARCH:
        assert catalog.require_research(name) == name
    for name in catalog.MORPH:
        assert catalog.require_morph(name) == name
