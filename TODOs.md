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

- [ ] 🎨 `do_train` should issue ONE action carrying every ready producer's tag and
      let the game pick the building, the way a human selects all Gateways and hits
      the hotkey. python-sc2 documents exactly this in `sc2/action.py`
      (`combine_actions`): grouping several producers' tags into a single raw
      `ActionRawUnitCommand` for a train ability yields **one** unit total, with the
      game choosing which building — *"Select 3 hatcheries, build a queen with each
      hatch — the grouping function would group these unit tags and only issue one
      train command once to all 3 unit tags — resulting in one total train command"*.
      python-sc2 deliberately splits train commands per-unit to avoid that (train is
      not in `COMBINEABLE_ABILITIES`), because most bots want one unit per building —
      but our model is one `train` step = one unit, so the grouped form is what we
      actually want.
      This would delete the `_issued_this_frame` bookkeeping entirely. That set exists
      only because the observation cache doesn't refresh mid-frame, so a producer we
      just ordered still reads as idle and a burst of train steps all stack onto
      `havers.first` (measured: 5 Gateways at queue depths `[1,1,2,2,4]`). The current
      fix prefers an unused producer and falls back to stacking — note it must NOT
      stall when all producers are used, since that starves the step queue and made
      `test_all_units_and_structures_produced` time out at 58/60 steps.
      Implementation: bypass `Unit.train()` and send a raw action built from
      `self.game_data.units[unit.value].creation_ability.id` plus the tags of all ready
      producers. Keep the existing "wait for an idle producer" gate so a `train` step
      still holds the line rather than silently queueing.

## Phase 5 — Clean output + diagnostics  (Goal 4)

- [ ] Integration runs occasionally hit a transient python-sc2 launch crash
      (`run_game` AssertionError before any step) — a Wine/SC2 flake, not our code.
      Consider a one-shot retry in the test's `_run` helper.

## Phase 6 — Docs pass

- [ ] Reconcile README, `schema.py` docstrings, and `SKILL.md` with the final
      shape; regenerate `reference/protoss_data.md` if anything changed.
