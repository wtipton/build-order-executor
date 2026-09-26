# Docker: headless SC2 runtime image

Containerizes the bot with Blizzard's **headless StarCraft II Linux build (v4.10)**
so builds run without Wine or a display (`run.py --target linux`). Harbor-agnostic —
this is just "run the bot in a box"; eval/benchmark definitions live elsewhere.

**Build** (from the repo root — the build context must be the repo root):

```
docker build -f docker/Dockerfile -t build-order-executor-headless .
```

First build downloads the ~4.1 GB game zip.

**Run one build** (prints the machine-readable `[summary]` JSON line):

```bash
docker run --rm -e ALLOW_RUN=1 \
  build-order-executor-headless \
  python run.py --build /workspace/my_build.yaml --time-limit 300
```

**Interactive shell** (how a terminal agent iterates — edit a build, run it, read
`[summary]`, repeat):

```bash
docker run --rm -it -e ALLOW_RUN=1 build-order-executor-headless
```

## Agent environment architecture

The container is built so that an unprivileged agent can play games but can learn
nothing about the engine and cannot run a game off the record.

- **Unprivileged agent user.** Everything runs as `agent` (UID 1000) in `/workspace`,
  which holds only `README.md` (the DSL guide) and `reference/`.
- **The root-only boundary.** Four trees are `root:root` with no group/other access:
  | Path | Why |
  |---|---|
  | `/opt/executor` | engine, compiled to `.pyc` with sources deleted |
  | `/opt/venv` | python-sc2 — otherwise the agent scripts its own games |
  | `/opt/StarCraftII` | the game — otherwise it launches SC2 directly |
  | `/var/lib/run-ledger` | the attempt ledger, which must be unforgeable from inside |
- **One way in.** `/usr/local/bin/run-build` is a setuid-root C wrapper
  (`docker/run_build.c`) and is the only route across that boundary.
  `/workspace/run.py` is a two-line shim that hands its argv to it.

### The setuid boundary

`run_build.c` stays tiny on purpose — it runs as root, so every line is attack surface.
It does two things beyond `exec`:

- **Rebuilds the environment** (`execve` with a fixed `envp`, not `execv`). The kernel
  strips `LD_PRELOAD` across a setuid exec but does **not** strip `PYTHONPATH`; the image
  used to set `PYTHONPATH=/opt/executor`, so inheriting the caller's environment into a
  root python was an arbitrary-code-execution hole. There is no `PYTHONPATH` in the image
  now, and nothing is inherited.
- **Runs `python -I`** (isolated: ignores `PYTHON*`, skips user site-packages, keeps the
  script directory off `sys.path`). Belt and braces with the above.

All policy lives in `_attempt.py` (`docker/attempt.py`), which the wrapper execs.

### Argument whitelist

`_attempt.py` does **not** forward argv to `run.py`'s own parser. It accepts exactly:

```
--build PATH         build order YAML; must resolve (after symlinks) under /workspace
--time-limit N       game-seconds, 1..1800, default 300
```

`--replay` is not exposed: as root it would be an arbitrary-write primitive. `--map` is
not exposed either, so an agent cannot quietly measure on a different map from the one it
will be scored on; the map is fixed by `RUN_MAP`.

### The attempt ledger

Every run through the wrapper is recorded under `/var/lib/run-ledger/attempts/`:

```
attempts/
├── index.jsonl        one line per attempt — the score-vs-attempts curve, directly
└── 0007/
    ├── build.yaml     the exact bytes that were run
    ├── cmd.json       argv, sha256, map, time limit, exit code, elapsed
    ├── output.log     full stdout+stderr (teed; the agent still sees it live)
    └── replay.SC2Replay
```

The YAML is **copied before the run**, and the copy is what gets executed. Recording a
path instead would let the agent edit the file afterwards, leaving a record that
describes a build which never ran.

`RUN_MAX_ATTEMPTS` caps the number of runs. The cap is read from root-owned
`/var/lib/run-ledger/limits.json`, written by the entrypoint at container start — **not**
from the environment, because the agent controls the environment it invokes `run-build`
with.

### Modes and settings

| Variable | Default | Effect |
|---|---|---|
| `ALLOW_RUN` | unset | `1` installs `run-build` and creates `/workspace/run.py`. Unset = offline mode: no route to the engine at all, so the agent must write a build from the docs alone. |
| `RUN_MAX_ATTEMPTS` | `50` | Runs allowed before `run-build` refuses. `0` = unlimited. |
| `RUN_MAP` | `CatalystLE` | The map every run uses. |

```bash
docker run --rm -it -e ALLOW_RUN=1 -e RUN_MAX_ATTEMPTS=25 -e RUN_MAP=CatalystLE \
  build-order-executor-headless
```

## Development flows

The entrypoint always drops to `agent`, and the engine is root-only, so dev work that
needs the real modules bypasses both with `--user root --entrypoint`.

**Integration tests against 4.10** (mount the repo so it runs your current code; pytest
lives in the engine venv):

```bash
docker run --rm --user root --entrypoint /opt/venv/bin/python \
  -v "$PWD":/opt/executor -w /opt/executor build-order-executor-headless \
  -m pytest tests/test_integration.py --run-integration
```

**Run a build as yourself, replay straight to the host.** This is the personal/CI flow —
it skips `run-build`, so there is no ledger, no cap, and the engine's full flag surface
(`--map`, `--replay`, `--time-limit`) is available again:

```bash
docker run --rm --user root --entrypoint /opt/venv/bin/python \
  -v "$PWD/builds":/builds -v ~/replays/bot:/replays \
  build-order-executor-headless \
  /opt/executor/_runner.pyc --build /builds/pvz_opening_8worker.yaml \
    --time-limit 300 --replay /replays/pvz.SC2Replay
```

Mounting `builds/` is not optional — it's `.dockerignore`d, so the image has no build
files of its own. The replay lands on the host owned by root but mode 644, so it reads
and deletes normally.

**Getting replays out of an eval run.** There, replays are in the root-only ledger by
design; copy the whole thing out rather than bind-mounting over it (the entrypoint
`chmod`s `/var/lib/run-ledger` recursively, which you do not want applied to a host
directory):

```bash
docker cp <container>:/var/lib/run-ledger/attempts ./attempts
```

**Getting maps off the container:**

```bash
docker run --rm --user root --entrypoint tar build-order-executor-headless \
  -cf - -C /opt/StarCraftII/Maps . \
  | tar -xf - -C "$HOME/Games/starcraft-ii/drive_c/Program Files (x86)/StarCraft II/Maps"
```

## Publishing to GHCR

Publish to GitHub Container Registry (replace `<OWNER>` with your GitHub user/org):

```bash
# 1. Log in once (gh auth token --scopes write:packages) and then:
echo "$GHCR_TOKEN" | docker login ghcr.io -u <OWNER> --password-stdin

# 2. Tag the local image:
docker build -f docker/Dockerfile -t build-order-executor-headless .        # if not already built
docker tag build-order-executor-headless ghcr.io/<OWNER>/build-order-executor-headless:sc2-4.10

# 3. Push:
docker push ghcr.io/<OWNER>/build-order-executor-headless:sc2-4.10
```

Then reference it downstream as `FROM ghcr.io/<OWNER>/build-order-executor-headless:sc2-4.10`. Make the
package public in its GitHub package settings if pullers shouldn't need auth. Bump the tag
(`sc2-4.10-v2`, …) whenever the image changes.

## Running agents in the container

```bash
curl -fsSL https://claude.ai/install.sh | bash
~/.local/bin/claude
```

```bash
curl -fsSL https://antigravity.google/cli/install.sh | bash
~/.local/bin/agy
```
