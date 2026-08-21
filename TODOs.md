# TODOs — build_orders → polished final state

Goal: a polished, final state where (1) the YAML schema can completely specify
rich Protoss build orders, (2) everything left unspecified (placement, etc.) is
executed correctly by the bot, (3) the design is clean and well-tested, and
(4) program output is clean and legible — third parties will write their own
builds against this schema and reason from the bot's output.

Scope decisions (fixed):
- **Macro to a timing.** Keep the concede-after-build model; no army control /
  combat steps. "Completeness" = every Protoss structure/unit/upgrade/cast/morph
  is producible and name-validated.
- **Pure unit tests**, no live SC2 (a lightweight fake bot; CI-able).
- **Refactor into modules** — real structural cleanup of the 842-line class.
- **Protoss only.**

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · 🎨 = needs genuine design
work (not mechanical).

---

## Phase 1 — Schema completeness + load-time validation  (Goal 1)  ✅ DONE

- [x] 🎨 New `catalog.py` = single source of truth, shared by schema + bot.
      DERIVED from python-sc2 tech-tree dicts (patch-proof, zero maintenance):
      `PRODUCER`, `WARP_ABILITY`, `TRAINABLE_UNITS`, `BUILDABLE_STRUCTURES`,
      `PROTOSS_UPGRADES`. CURATED: `RESEARCH` (friendly names for **all 27**
      Protoss upgrades, with an import-time assert that it matches the game's
      upgrade set), `CAST` (self-cast spells), `MORPH` (gateway<->warpgate **+
      Archon combine**).
- [x] Pydantic `field_validator`s on `build.what`, `train.what`, `warp.what`,
      `research.what`, `cast.what`, `morph.to`, `chrono.target` — all checked at
      `load_build`, error names the exact step + lists valid options.
- [x] A typo (`Zealott`) fails at load (`steps.0.train.what: unknown unit type
      'Zealott'`), never a runtime `KeyError`. All 5 existing builds still load.
- [x] `bot.py` swapped over to import the tables from `catalog.py`; `do_morph`
      generalized to the 2:1 Archon combine; `do_cast` uses `CastSpec`.

Notes / still out of macro scope (by design): targeted spells (Storm/Feedback/
Force Field need target selection); Warp Prism phase morph. Archon morph is IN.
Not yet exercised in a live game — a real run with an Archon/Storm build is worth
doing (belongs with Phase 2 tests or a Phase 5 run).

## Phase 2 — Test scaffold  (Goal 3a)

- [ ] Add `pytest` + a `tests/` dir (+ dev-deps note in README).
- [ ] 🎨 Build a lightweight `FakeBot` exposing only the attributes the pure
      logic reads (`supply_used`, `time`, `minerals`, `vespene`, a `units()`
      stub, …). Design the seam so executor logic is testable without SC2.
- [ ] Tests: every `builds/*.yaml` loads; invalid snippets raise; table
      invariants (`PRODUCER`/`WARP_ABILITY`/`RESEARCH`/`CAST`/`MORPH` internally
      consistent and accepted by the validators); `trigger_met` + `_prewalk_due`
      truth tables.

## Phase 3 — Refactor into modules  (Goal 3b + clean design)

- [ ] 🎨 Extract modules: `steps.py` (handlers), `economy.py`, `observe.py`
      (army report / milestones / heartbeat); keep `placement.py`.
      `BuildOrderBot` becomes a thin orchestrator. Design the module seams.
- [ ] 🎨 Fold the scattered confirm/reservation instance vars into small
      dataclasses (`BuildConfirm`, `WarpConfirm`, `MorphState`, `Reservation`).
- [ ] Preserve the observability `SKILL.md` relies on: `_army_report()`,
      `_note_milestones()`, `[done] M:SS` lines — keep working through the refactor.
- [ ] Expand tests against the now-decoupled pieces.

## Phase 4 — Correctness of the unspecified  (Goal 2)

- [ ] 🎨 `placement.building()` (and pylon/expansion placement) degrade
      gracefully instead of returning `None` and holding the queue forever when a
      base's powered ground fills up (spread / fallback). Document determinism.

## Phase 5 — Clean output + deadlock diagnostics  (Goal 4)

- [ ] 🎨 Stall detector: when the head step hasn't advanced in N seconds, emit
      one clear line naming the step + blocking reason (`can't afford: need 100
      gas, have 40` / `no idle Gateway` / `placement None` / `on cooldown`).
      Emit once, not spammy.
- [ ] Consistent log prefixes + a documented legend; clean split of always-on
      vs `--debug` output.
- [ ] Route Wine / sc2 boot noise off stdout (to a file) so third-party output
      is legible.

## Phase 6 — Docs pass

- [ ] Reconcile README, `schema.py` docstrings, and `SKILL.md` with the final
      shape; regenerate `reference/protoss_data.md` if anything changed.

---

Ordering rationale: 1→2 are the low-risk foundation; 3 is the big structural
change, protected by the Phase-2 tests; 4–5 add the correctness/legibility third
parties depend on. Phase 5's stall detector can be pulled forward if clean
output for external build-writers becomes the priority.
