"""Tier 2 — integration: run a build against real SC2 and assert on the structured
run summary. Slow (launches the game); skipped unless `pytest --run-integration`.

This is where executor-vs-real-game bugs surface: the all-schema-features build
exercises every step + trigger type, so a wrong API call, a bad placement, or a
broken confirm state machine shows up as either a stall (completed=false) or a
missing artifact (no Archon, no Warpgate, un-researched upgrade).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


@pytest.mark.integration
def test_all_schema_features_runs_to_completion():
    proc = subprocess.run(
        [sys.executable, "run.py",
         "--build", "builds/test_all_schema_features.yaml",
         "--fullscreen", "--time-limit", "750"],
        check=True, timeout=1200, capture_output=True, text=True,
    )
    # the bot prints the machine-readable summary as one `[summary] <json>` line
    line = next(l for l in proc.stdout.splitlines() if l.startswith("[summary] "))
    data = json.loads(line[len("[summary] "):])

    assert data["completed"], (
        f"build stalled on {data['next_step']!r} at {data['final_time']}s "
        f"({data['steps_done']}/{data['steps_total']} steps)"
    )
    # the confirm state machines actually produced the hard artifacts:
    assert data["census"].get("ARCHON", 0) >= 1, f"no Archon (census={data['census']})"
    assert data["census"].get("WARPGATE", 0) >= 1, f"no Warpgate (census={data['census']})"
    # Warpgate research must have COMPLETED — the morph->warpgate step can't finish
    # until it does — which also proves the research path end-to-end.
    assert "Warpgate" in data["upgrades"], f"Warpgate not researched (upgrades={data['upgrades']})"
    # (We don't assert Storm *completes*: `research Storm` finishing only means the
    # upgrade STARTED, and the bot concedes ~10s after the build, before a 79s
    # research would finish. completed==True already proves Storm research started.)
