# TODOs — build_orders → polished final state

Goals: (1) schema can completely specify rich Protoss build orders, (2) what's
left unspecified (placement, etc.) executes correctly, (3) clean, well-tested
design, (4) clean/legible output (third parties write builds and reason from it).

Fixed scope: macro-to-a-timing (concede after build, no army control); Protoss
only; pure unit tests (lightweight fake bot, no live SC2); refactor into modules.

Legend: `[ ]` todo · 🎨 = needs genuine design work.

---

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

- [ ] 🎨 `placement.building()`/pylon/expansion still return `None` (and stall)
      if a base's powered ground genuinely fills up — graceful spread/fallback
      across bases not yet done. Document determinism.

## Phase 5 — Clean output + diagnostics  (Goal 4)

- [ ] Integration runs occasionally hit a transient python-sc2 launch crash
      (`run_game` AssertionError before any step) — a Wine/SC2 flake, not our code.
      Consider a one-shot retry in the test's `_run` helper.

## Phase 6 — Docs pass

- [ ] Reconcile README, `schema.py` docstrings, and `SKILL.md` with the final
      shape; regenerate `reference/protoss_data.md` if anything changed.
