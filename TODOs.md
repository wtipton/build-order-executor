# TODOs — build_orders → polished final state

Goals: (1) schema can completely specify rich Protoss build orders, (2) what's
left unspecified (placement, etc.) executes correctly, (3) clean, well-tested
design, (4) clean/legible output (third parties write builds and reason from it).

Fixed scope: macro-to-a-timing (concede after build, no army control); Protoss
only; pure unit tests (lightweight fake bot, no live SC2); refactor into modules.

Legend: `[ ]` todo · 🎨 = needs genuine design work.

---

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
