# build_orders

A generic [python-sc2](https://github.com/BurnySc2/python-sc2) bot that executes a
Protoss build order described in a YAML config, for practicing/validating openings
(currently the 8-worker-start opener in `builds/`).

## Run

```bash
cd ~/projects/build_orders && .venv/bin/python run.py --fullscreen
```

`run.py` launches SC2 itself (via Wine/Lutris — paths are baked into `run.py`),
plays the build vs a passive do-nothing opponent (so the build runs undisturbed
and the game doesn't end instantly), saves a replay, then concedes when the build
is done.

All flags, set explicitly (`--fullscreen` is needed — windowed is unusably slow
under Wine; `--debug` adds a 10s economy heartbeat):

```bash
cd ~/projects/build_orders && .venv/bin/python run.py \
  --build builds/pvz_opening_8worker.yaml \
  --map LockdownLE \
  --fullscreen \
  --debug
```

The `[build]` lines (always on) show each step firing with the game clock, e.g.
`1:54  sup19  build what=Nexus`.

## Tests

Fast unit tests, no SC2 required — the executor's methods are tested unbound
against a lightweight fake `self` (`tests/fakes.py`), so schema validation,
catalog invariants, triggers, step ordering, and each handler's gating run in
well under a second.

```bash
.venv/bin/pip install pytest pytest-asyncio   # one-time dev deps
cd ~/projects/build_orders && .venv/bin/python -m pytest
```
