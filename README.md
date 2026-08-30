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

### Run via Docker (headless Linux, no Wine/display required)

```bash
docker run --rm \
  -v ~/replays/bot:/root/replays/bot \
  build-order-executor-headless \
  python run.py --build builds/pvz_opening_8worker.yaml
```

Mounting the replays folder this way causes the replay to get saved to the host. To get old maps off the container, e.g.:

```bash
docker run --rm build-order-executor-headless tar -cf - -C /root/StarCraftII/Maps . \
  | tar -xf - -C "$HOME/Games/starcraft-ii/drive_c/Program Files (x86)/StarCraft II/Maps"
```

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

## Reference data

`reference/*.md` are dumped from a live client rather than copied from a wiki, so they are
correct for whatever patch is installed. Regenerate them after a patch.

### Unit, structure and upgrade costs (`reference/protoss_data_live.md`)

Costs and *base* build times, straight from the running game client:
```bash
.venv/bin/python -m scripts.gamedata_dump --fullscreen 2>&1 | grep -E "^DATA|^ABIL|^UPG"
```
It launches its own throwaway game. Add `--target linux` to read the 4.10 client instead,
which is where `reference/protoss_data_4.10.md` comes from.

### Mining rates (`reference/economic_data.md`)

Regenerate (the engine calls the game a Tie at ~1320 game-seconds, so it runs in chunks;
the slices are positional indices into `phases()`, so re-derive them if you edit it):
```bash
for s in 0:12 12:24 24:32 32:40 40:46 46:54 54:58; do
  docker run --rm -v "$PWD":/app build-order-executor-headless \
    python -m scripts.economy_measure --target linux --slice $s 2>&1 | grep -E '^\[econ\]'
done
```

`min`/`gas` phases count resources over a 60s window and can't resolve anything below one
5-mineral trip (~8%), so use the frame-timed `trip` phases for finer effects — that gap is
why the close-vs-far patch difference was missed on the first pass.

