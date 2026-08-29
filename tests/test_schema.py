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


def _config(*steps):
    return BuildConfig(steps=TypeAdapter(list[Step]).validate_python(list(steps)))


_SEND = {"at": {"time": 1}, "do": "send_probe", "where": "proxy", "who": "scout"}
_RETURN = {"at": {"time": 2}, "do": "return_probe", "who": "scout"}
_BUILD_WITH = {"at": {"time": 3}, "do": "build", "what": "Pylon", "who": "scout"}


# A `who:` that names no held probe has NO runtime symptom — return_probe no-ops and a
# `build who:` falls back to the mining pool — so the build completes while quietly not
# doing what it says. These must all fail at load instead.
BAD_WHO = [
    ("typo", [_SEND, {**_BUILD_WITH, "who": "scot"}]),
    ("never sent", [_BUILD_WITH]),
    ("returned before use", [_SEND, _RETURN, _BUILD_WITH]),
    ("returned twice", [_SEND, _RETURN, {**_RETURN, "at": {"time": 3}}]),
]


@pytest.mark.parametrize("case,steps", BAD_WHO, ids=[c for c, _ in BAD_WHO])
def test_unheld_who_rejected(case, steps):
    with pytest.raises(Exception) as ei:
        _config(*steps)
    assert "is held here" in str(ei.value)


def test_a_name_can_be_re_sent_after_being_returned():
    # the binding is per-send, not permanent: returning scout and sending it again is a
    # legitimate second tour, and must not be confused with using a returned name
    _config(_SEND, _RETURN, {**_SEND, "at": {"time": 3}}, _BUILD_WITH)


def test_non_list_yaml_rejected(tmp_path):
    dict_file = tmp_path / "dict_build.yaml"
    dict_file.write_text("steps:\n  - {at: {time: 1}, do: hallucinate}\n")
    with pytest.raises(ValueError, match="expected a YAML list of steps"):
        load_build(dict_file)

