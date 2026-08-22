# TODOs — build_orders → polished final state

Goals: (1) schema can completely specify rich Protoss build orders, (2) what's
left unspecified (placement, etc.) executes correctly, (3) clean, well-tested
design, (4) clean/legible output (third parties write builds and reason from it).

Fixed scope: macro-to-a-timing (concede after build, no army control); Protoss
only; pure unit tests (lightweight fake bot, no live SC2); refactor into modules.

Legend: `[ ]` todo · `[~]` in progress · 🎨 = needs genuine design work.

---

## Phase 2 — Test scaffold  (Goal 3a)

Approach: methods tested UNBOUND against a fake `self` (`tests/fakes.py`) — no
BotAI/game. Tiers: (0) pure schema+catalog, (1) sequencing + handler gating,
(2) integration vs real SC2 asserting on a structured run summary [later].

- [x] `pytest` + `pytest-asyncio` + `tests/` (`asyncio_mode=auto`); `FakeUnits`
      + fake-`self` helpers in `tests/fakes.py`. 45 tests, ~0.4s.
- [x] Tier 0: every `builds/*.yaml` loads; bad names/extra-fields/trigger-arity
      rejected with the right message; catalog table invariants.
- [x] Tier 1: `trigger_met`, `_prewalk_due`, `run_steps` strict ordering;
      handler gating for `train`/`research`/`hallucinate`/`morph` (incl. Archon
      combine)/`chrono`/`gas_workers`/`minerals_cap`/`workers`.
- [x] Tier 1 remaining handlers: `do_build` + `do_warp` confirm/baseline state
      machines, `do_send_probe`/`return_probe`, `do_rally`/`rally_and_transfer`.
- [x] Tier 2 integration built: `on_end` prints an unconditional `[summary]` JSON
      line to stdout; `builds/test_all_schema_features.yaml` exercises every step +
      trigger type; `tests/test_integration.py` (gated on `--run-integration`)
      asserts it runs to completion + produced Archon/Warpgate. Found a real bug
      (fixed below) — now passes.
- [x] Fixed `run_steps` re-gating in-flight steps: a step's trigger now gates only
      STARTING it (`_started` latch); once fired it's driven to completion without
      re-checking. Fixes non-monotonic `count:` triggers on consumed units (morph
      archon gated on `HighTemplar:2`, morph warpgate on `Gateway:N`). Regression
      test in `test_sequencing.py`; integration test now green.

## Phase 3 — Refactor into modules  (Goal 3b + clean design)

- [ ] 🎨 Extract modules: `steps.py` (handlers), `economy.py`, `observe.py`
      (army report / milestones / heartbeat); keep `placement.py`.
      `BuildOrderBot` becomes a thin orchestrator. Design the module seams.
- [ ] 🎨 Fold the scattered confirm/reservation instance vars into small
      dataclasses (`BuildConfirm`, `WarpConfirm`, `MorphState`, `Reservation`).
- [ ] Preserve the observability `SKILL.md` relies on: `_army_report()`,
      `_note_milestones()`, `[done] M:SS` lines — keep working through the refactor.
- [ ] Expand tests against the now-decoupled pieces.
- [ ] Consider lifting `optional:` (best-effort skip) from chrono-only to a
      general step attribute.

## Phase 4 — Correctness of the unspecified  (Goal 2)

- [ ] 🎨 `placement.building()` (and pylon/expansion placement) degrade
      gracefully instead of returning `None` and holding the queue forever when a
      base's powered ground fills up (spread / fallback). Document determinism.

## Phase 5 — Clean output + deadlock diagnostics  (Goal 4)  ✅ mostly done

- [x] Stall detector: `[stall]` names the stalled step + reason (trigger status
      with current value, or a handler `_stall_reason` like `can't afford X` /
      `no idle Gateway` / `no placement` / `all Warpgates on cooldown`). Fires at
      60s, re-emits every 30s. Verified live — it cleanly diagnosed the archon bug.
- [x] Tag rename + legend: `[run]`/`[step]`/`[done]`/`[stall]`/`[end]` always-on,
      `[hb]`/`[step*]`/`[prewalk]` under `--debug`; one-line legend at startup;
      "Reading the output" table in README.
- [x] Noise: python-sc2 loguru dropped to WARNING + a filter for the benign
      connection-teardown errors our concede provokes. stdout is now just our tags.
- [ ] Live-run verify the Phase-1 additions that unit tests can't cover: Archon
      morph actually pops an Archon; newly-enabled upgrades (e.g. Storm) research.

## Phase 6 — Docs pass

- [ ] Reconcile README, `schema.py` docstrings, and `SKILL.md` with the final
      shape; regenerate `reference/protoss_data.md` if anything changed.
