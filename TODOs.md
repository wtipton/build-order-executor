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
- [ ] Tier 2 integration (chose: structured JSON summary): bot writes a
      machine-readable run summary at `on_end` (steps done, milestone times, army
      counts, stalls); a kitchen-sink build exercising every step + trigger type
      runs to `idx==len(steps)` with no stall. Mark slow / needs SC2.

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

## Phase 5 — Clean output + deadlock diagnostics  (Goal 4)

- [ ] 🎨 Stall detector: when the head step hasn't advanced in N seconds, emit
      one clear line naming the step + blocking reason (`can't afford: need 100
      gas, have 40` / `no idle Gateway` / `placement None` / `on cooldown`).
      Emit once, not spammy.
- [ ] Consistent log prefixes + a documented legend; clean split of always-on
      vs `--debug` output.
- [ ] Route Wine / sc2 boot noise off stdout (to a file) so third-party output
      is legible.
- [ ] Live-run verify the Phase-1 additions that unit tests can't cover: Archon
      morph actually pops an Archon; newly-enabled upgrades (e.g. Storm) research.

## Phase 6 — Docs pass

- [ ] Reconcile README, `schema.py` docstrings, and `SKILL.md` with the final
      shape; regenerate `reference/protoss_data.md` if anything changed.
