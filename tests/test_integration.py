"""Tier 2 — integration: run a build against real SC2 and assert on the structured
[summary] line. Slow (launches the game); skipped unless `pytest --run-integration`.

This is where executor-vs-real-game bugs surface: the coverage builds exercise
every step + trigger type, every unit/structure, and every upgrade — so a wrong
API call, a bad placement, a jammed producer, or a broken confirm state machine
shows up as a stall (completed=false) or a missing artifact in the census/upgrades.
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import catalog


def _run(build: str, time_limit: int) -> dict:
    """Run a build to completion and return its parsed [summary] JSON."""
    proc = subprocess.run(
        [sys.executable, "run.py", "--build", build, "--fullscreen", "--time-limit", str(time_limit)],
        check=True, timeout=1800, capture_output=True, text=True,
    )
    line = next(l for l in proc.stdout.splitlines() if l.startswith("[summary] "))
    return json.loads(line[len("[summary] "):])


@pytest.mark.integration
def test_all_schema_features_runs_to_completion():
    """Every step type + every trigger type, run to the end."""
    data = _run("builds/test_all_schema_features.yaml", time_limit=750)
    assert data["completed"], (
        f"stalled on {data['next_step']!r} at {data['final_time']}s "
        f"({data['steps_done']}/{data['steps_total']})"
    )
    assert data["census"].get("ARCHON", 0) >= 1, f"no Archon (census={data['census']})"
    assert data["census"].get("WARPGATE", 0) >= 1, f"no Warpgate (census={data['census']})"
    assert "Warpgate" in data["upgrades"], f"Warpgate not researched (upgrades={data['upgrades']})"


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
    missing = sorted(name for name in expected if data["census"].get(name, 0) < 1)
    assert not missing, f"missing from census: {missing} (census={data['census']})"


@pytest.mark.integration
def test_all_upgrades_researched():
    """Every Protoss upgrade completes."""
    data = _run("builds/test_all_upgrades.yaml", time_limit=1100)
    done = set(data["upgrades"])
    missing = sorted(set(catalog.RESEARCH) - done)
    assert not missing, (
        f"upgrades not completed: {missing} "
        f"(still researching: {data.get('researching')}, completed: {sorted(done)})"
    )
