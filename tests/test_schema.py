"""Tier 0 — schema loading + validation (pure, no bot)."""

from __future__ import annotations

import glob

import pytest

from schema import BuildConfig, Step, load_build
from pydantic import TypeAdapter


def test_all_shipped_builds_load():
    files = glob.glob("builds/*.yaml")
    assert files, "no builds found to validate"
    for f in files:
        load_build(f)  # raises if any build is invalid


def _one_step(**step):
    return {"at": {"time": 1}, **step}


# Each case: a step dict that must fail validation, and a substring the error
# should mention (so the message stays useful to a build author).
BAD_STEPS = [
    (dict(do="build", what="Zealott"), "unknown unit type"),
    (dict(do="build", what="Zealot"), "structure to build"),      # a unit, not a structure
    (dict(do="train", what="Pylon"), "unit to train"),            # a structure, not a unit
    (dict(do="warp", what="Immortal"), "unit to warp in"),        # not gateway-warpable
    (dict(do="research", what="Blimk"), "unknown research"),
    (dict(do="morph", to="archn"), "unknown morph target"),
    (dict(do="chrono", target="Zealot"), "chrono target"),        # a unit, not a structure
]


@pytest.mark.parametrize("step,needle", BAD_STEPS)
def test_bad_step_rejected(step, needle):
    with pytest.raises(Exception) as ei:
        TypeAdapter(Step).validate_python(_one_step(**step))
    assert needle in str(ei.value)


def test_extra_field_rejected():
    with pytest.raises(Exception) as ei:
        TypeAdapter(Step).validate_python(_one_step(do="hallucinate", unit="Phoenix"))
    assert "Extra inputs are not permitted" in str(ei.value)
    with pytest.raises(Exception) as ei:
        TypeAdapter(Step).validate_python(_one_step(do="wait", note="some comment"))
    assert "Extra inputs are not permitted" in str(ei.value)


def test_trigger_needs_exactly_one_key():
    with pytest.raises(Exception):
        TypeAdapter(Step).validate_python({"at": {}, "do": "hallucinate"})
    with pytest.raises(Exception):
        TypeAdapter(Step).validate_python({"at": {"time": 1, "supply": 2}, "do": "hallucinate"})


def test_count_trigger_bad_name_rejected():
    with pytest.raises(Exception) as ei:
        TypeAdapter(Step).validate_python({"at": {"count": {"TemplarArchives": 1}}, "do": "hallucinate"})
    assert "unit/structure to count" in str(ei.value) or "unknown unit type" in str(ei.value)


@pytest.mark.parametrize("name", ["Probe", "Gateway", "TemplarArchive", "WarpGate", "Archon", "HighTemplar"])
def test_count_trigger_accepts_ownable_types(name):
    # includes the morph-only results (WarpGate, Archon) a build legitimately gates on
    TypeAdapter(Step).validate_python({"at": {"count": {name: 1}}, "do": "hallucinate"})


def test_non_list_yaml_rejected(tmp_path):
    dict_file = tmp_path / "dict_build.yaml"
    dict_file.write_text("steps:\n  - {at: {time: 1}, do: hallucinate}\n")
    with pytest.raises(ValueError, match="expected a YAML list of steps"):
        load_build(dict_file)

