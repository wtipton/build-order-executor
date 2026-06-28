# build_orders

A generic [python-sc2](https://github.com/BurnySc2/python-sc2) bot that executes a
Protoss build order described in a YAML config, for practicing/validating openings
(currently the 8-worker-start opener in `builds/`).

## Run

```bash
cd ~/projects/build_orders && .venv/bin/python run.py --fullscreen
```

`run.py` launches SC2 itself (via Wine/Lutris — paths are baked into `run.py`),
plays the build vs the built-in AI, saves a replay, then concedes when the build
is done.

All flags, set explicitly (`--fullscreen` is needed — windowed is unusably slow
under Wine; `--realtime` watches live; `--debug` adds a 10s economy heartbeat):

```bash
cd ~/projects/build_orders && .venv/bin/python run.py \
  --build builds/pvz_pvt_opening_8worker.yaml \
  --map LockdownLE \
  --opponent zerg \
  --difficulty easy \
  --fullscreen \
  --no-realtime \
  --debug
```

The `[build]` lines (always on) show each step firing with the game clock, e.g.
`1:54  sup19  build what=Nexus`.

## Files

- `bot.py` — generic `BuildOrderBot`: interprets a build config; runs the economy
  (continuous probes, no idle workers, minerals-then-gas) automatically.
- `builds/*.yaml` — build orders. `steps:` are the deliberate actions; `economy:`
  sets the auto-economy defaults.
- `run.py` — launcher.
