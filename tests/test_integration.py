"""Tier 2 — integration: run a build against real SC2 and assert on the structured
[summary] line. Slow (launches the game); skipped unless `pytest --run-integration`.

This is where executor-vs-real-game bugs surface: the coverage builds exercise
every step + trigger type, every unit/structure, and every upgrade — so a wrong
API call, a bad placement, a jammed producer, or a broken confirm state machine
shows up as a stall (completed=false) or a missing artifact in the census/upgrades.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

import catalog

# Map to run these on. Defaults target-aware (LockdownLE for wine, CatalystLE for linux);
# override with SC2_TEST_MAP / SC2_MAP to run against another map.
_DEFAULT_MAP = "CatalystLE" if os.environ.get("SC2_TARGET") == "linux" else "LockdownLE"
TEST_MAP = os.environ.get("SC2_TEST_MAP", os.environ.get("SC2_MAP", _DEFAULT_MAP))


RUN_TIMEOUT = 1800  # seconds of wall-clock per game before we give up on the child


def _tail(text: str | None, n: int = 20) -> str:
    return "\n".join((text or "").splitlines()[-n:]) or "(empty)"


def _fail(build: str, why: str, proc_stdout: str | None, proc_stderr: str | None) -> None:
    """Fail with the child's own output. `pytrace=False` because the useful context is
    the subprocess's traceback, not this helper's call stack."""
    pytest.fail(
        f"{why} — {build}\n"
        f"--- child stderr (last 20 lines) ---\n{_tail(proc_stderr)}\n"
        f"--- child stdout (last 20 lines) ---\n{_tail(proc_stdout)}",
        pytrace=False,
    )


def _census(data: dict) -> dict:
    """The summary's census keyed by UnitTypeId name.

    `[summary]` reports the game's own display names (CyberneticsCore, WarpGate) so it
    matches the `[complete]` lines; those upper-case back to the enum names the catalog
    tables use, which is what these assertions are written against.
    """
    return {name.upper(): n for name, n in data["census"].items()}


def _run(build: str, time_limit: int) -> dict:
    """Run a build to completion and return its parsed [summary] JSON.

    Surfaces the child's output on every failure path. `capture_output` otherwise
    swallows it and a crashed run reports as a bare exit code — which hides the
    difference that matters here: a bug in the bot vs. the SC2 client dying on
    launch (python-sc2 raises `assert all(isinstance(r, Result) ...)`, and stdout
    has no [step]/[summary] lines at all).
    """
    cmd = [sys.executable, "run.py", "--build", build, "--fullscreen",
           "--map", TEST_MAP, "--time-limit", str(time_limit)]
    try:
        proc = subprocess.run(cmd, timeout=RUN_TIMEOUT, capture_output=True, text=True)
    except subprocess.TimeoutExpired as e:
        _fail(build, f"run.py exceeded {RUN_TIMEOUT}s", e.stdout, e.stderr)

    if proc.returncode != 0:
        _fail(build, f"run.py exited {proc.returncode}", proc.stdout, proc.stderr)

    line = next((l for l in proc.stdout.splitlines() if l.startswith("[summary] ")), None)
    if line is None:
        _fail(build, "run.py exited 0 but printed no [summary] line", proc.stdout, proc.stderr)
    return json.loads(line[len("[summary] "):])


@pytest.mark.integration
def test_all_schema_features_runs_to_completion():
    """(Nearly) every step type + every trigger type, run to the end. Train-based so
    the one build is valid on both current retail and 4.10 (see the build's header:
    warp / morph-warpgate diverge between versions and are omitted here)."""
    data = _run("builds/test_all_schema_features.yaml", time_limit=750)
    assert data["completed"], (
        f"stalled on {data['next_step']!r} at {data['final_time']}s "
        f"({data['steps_done']}/{data['steps_total']})"
    )
    assert _census(data).get("ARCHON", 0) >= 1, f"no Archon (census={data['census']})"
    # No Warpgate assertion: the build doesn't research Warpgate at all (it auto-morphs
    # Gateways in 4.10 but not modern, so it's version-specific — see the build header).


@pytest.mark.integration
def test_warp_in():
    """Warp a unit in from a Warpgate — one build valid on both versions (the morph step
    starts before Warpgate research finishes, so it completes via the manual morph on
    modern or the auto-morph on 4.10; see the build header)."""
    data = _run("builds/test_warp_in.yaml", time_limit=400)
    assert data["completed"], (
        f"stalled on {data['next_step']!r} at {data['final_time']}s "
        f"({data['steps_done']}/{data['steps_total']})"
    )
    assert _census(data).get("ZEALOT", 0) >= 1, f"no warped-in Zealot (census={data['census']})"


@pytest.mark.integration
def test_all_units_and_structures_produced():
    """Every buildable structure and every trainable unit (+ Archon) is present."""
    data = _run("builds/test_all_units_and_structures.yaml", time_limit=900)
    assert data["completed"], (
        f"stalled on {data['next_step']!r} at {data['final_time']}s "
        f"({data['steps_done']}/{data['steps_total']})"
    )
    expected = ({s.name for s in catalog.BUILDABLE_STRUCTURES}
                | {u.name for u in catalog.TRAINABLE_UNITS}
                | {"ARCHON"})
    census = _census(data)
    missing = sorted(name for name in expected if census.get(name, 0) < 1)
    assert not missing, f"missing from census: {missing} (census={data['census']})"


@pytest.mark.integration
def test_all_base_locations():
    """Expand to 6 bases, populate them all, and drop a Pylon at every named
    location — our bases (main..sixth) and the enemy/proxy spots."""
    data = _run("builds/test_all_base_locations.yaml", time_limit=750)
    assert data["completed"], (
        f"stalled on {data['next_step']!r} at {data['final_time']}s "
        f"({data['steps_done']}/{data['steps_total']}) — a named location may not resolve/place"
    )
    assert _census(data).get("NEXUS", 0) >= 6, f"expected 6 bases (census={data['census']})"
    bases = data.get("bases", [])
    assert len(bases) >= 6 and all(w > 0 for w in bases[:6]), f"a base is unpopulated: {bases}"
    # a Pylon landed at each distinct named location (guards the re-issue/placement
    # bug where enemy pylons ended up clustered at the proxy instead)
    by_place = data.get("pylons_by_place", {})
    for place in ["main", "natural", "third", "fourth", "fifth", "sixth",
                  "enemy_main", "enemy_natural", "proxy"]:
        assert by_place.get(place, 0) >= 1, f"no pylon at {place}: {by_place}"
    # the Nth Nexus and `where: <Nth base>` must agree — a Nexus sits at every
    # named base (guards the get_next_expansion-vs-_expansion_near mismatch)
    nexus_at = data.get("nexus_by_place", {})
    for place in ["main", "natural", "third", "fourth", "fifth", "sixth"]:
        assert nexus_at.get(place, 0) >= 1, f"no Nexus at {place}: {nexus_at}"


@pytest.mark.integration
def test_probe_labelling():
    """Labelled probes end-to-end: a labelled tour runs to completion, `label:` builds
    use the labelled probe, and return_probe hands them all back."""
    data = _run("builds/test_probe_labelling.yaml", time_limit=400)
    assert data["completed"], (
        f"stalled on {data['next_step']!r} at {data['final_time']}s "
        f"({data['steps_done']}/{data['steps_total']})"
    )
    # NB: that each re-send reuses the SAME probe is pinned by the unit tests
    # (tests/test_probe_rally.py::test_resending_a_label_moves_the_same_probe) — it has
    # no in-game symptom, so there is nothing to assert on here.
    assert data.get("labelled_probes_held") == [], f"probes not returned to mining: {data.get('labelled_probes_held')}"
    # the builder probe actually built its home structures
    for s in ("PYLON", "GATEWAY", "CYBERNETICSCORE"):
        assert _census(data).get(s, 0) >= 1, f"builder didn't build {s}: {data['census']}"


@pytest.mark.integration
def test_all_upgrades_researched():
    """Every Protoss upgrade researchable in BOTH game versions completes."""
    data = _run("builds/test_all_upgrades.yaml", time_limit=1400)
    done = set(data["upgrades"])
    # These two aren't researchable in the 4.10 headless build (TempestGroundAttack's id
    # is absent; VoidRaySpeed's id exists but has no research ability), so they're commented
    # out of the build and not required — keeps this one build valid on both versions.
    expected = set(catalog.RESEARCH) - {"TempestGroundAttack", "VoidRaySpeed"}
    missing = sorted(expected - done)
    assert not missing, (
        f"upgrades not completed: {missing} "
        f"(still researching: {data.get('researching')}, completed: {sorted(done)})"
    )
