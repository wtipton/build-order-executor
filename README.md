# build-order-executor

A generic [python-sc2](https://github.com/BurnySc2/python-sc2) bot that executes a
Protoss build order described in a YAML config, for practicing/validating openings
(currently the 8-worker-start opener in `builds/`).

## Run

```bash
cd ~/projects/build-order-executor && .venv/bin/python run.py --fullscreen
```

`run.py` launches SC2 itself (via Wine/Lutris — paths are baked into `run.py`),
plays the build vs a passive do-nothing opponent (so the build runs undisturbed
and the game doesn't end instantly), saves a replay, then concedes when the build
is done.

All flags, set explicitly (`--fullscreen` is needed — windowed is unusably slow
under Wine):

```bash
cd ~/projects/build-order-executor && .venv/bin/python run.py \
  --build builds/pvz_opening_8worker.yaml \
  --map LockdownLE \
  --fullscreen
```

## Reading the output

Every line is tagged. Always-on (the report a build reads back):

| Tag | Meaning |
|---|---|
| `[run]` | lifecycle: build loaded, output legend, build complete, conceding, replay/summary paths |
| `[step]` | a build step fired, with clock + supply, e.g. `[step] 1:54  sup19  build what=Nexus` |
| `[complete]` | every unit/structure completion with a running per-type count, e.g. `[complete] 4:19  Gateway #3`, plus each upgrade, e.g. `[complete] 5:19  Warpgate`. Exact to the frame — use these for deadlines, not `[status]`. Probes are skipped (worker count is on every `[status]` line) |
| `[builder]` | the lifecycle of the one probe pulled off the mineral line for a build: `assigned` when it's pulled and starts walking (by default as soon as we can afford the building; earlier if the step has a `prewalk:` trigger), then `building` when the order goes in. `dist=` is how far it still is from the spot — near-zero on the `building` line means the walk was overlapped with saving up, a big number means it was on the critical path. At most one probe is assigned this way at a time |
| `[status]` | every 10 game-seconds, two lines: economy snapshot; the current step with how long it's been current and what it's waiting on |
| `[end]` | final state + army report |

There is no `[stall]` tag: `[status]` always reports the current step's age and reason,
so a stall shows up as an age that keeps growing — no threshold to tune.

All of the above are unconditional — there is no verbosity flag, so a run never has to be
repeated just to diagnose it.

The final `[summary]` line is a machine-readable JSON blob (steps done, `completions`
mapping each type to its list of completion times, `upgrades_at`, unit/upgrade census,
where it stalled) — grep `^[summary] ` and `json.loads` the rest.

## Tests

Fast unit tests, no SC2 required — the executor's methods are tested unbound
against a lightweight fake `self` (`tests/fakes.py`), so schema validation,
catalog invariants, triggers, step ordering, and each handler's gating run in
well under a second.

```bash
.venv/bin/pip install pytest pytest-asyncio   # one-time dev deps
cd ~/projects/build-order-executor && .venv/bin/python -m pytest
```

### Integration tests (launch real SC2)

Gated behind `--run-integration` (slow — each launches a game). The same suite is
validated against **both** game versions:

```bash
# Current retail, via local Wine (fullscreen; ~6 min):
cd ~/projects/build-order-executor && .venv/bin/python -m pytest tests/test_integration.py --run-integration

# Game version 4.10, via the headless Docker image (~3 min; see docker/):
docker run --rm -v "$PWD":/app build-order-executor-headless \
  python -m pytest tests/test_integration.py --run-integration
```

The map defaults automatically by target (`CatalystLE` for `linux` / Docker, `LockdownLE` for `wine`),
and can be overridden with `--map` or `SC2_TEST_MAP`/`SC2_MAP`. A couple of upgrades don't exist in
4.10, so the one suite covers only what's valid in both — see the build/test headers.
