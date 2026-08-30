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
  python run.py --build builds/pvz_opening_8worker.yaml --time-limit 300
```

**Interactive shell** (how a terminal agent iterates — edit a build, run it, read
`[summary]`, repeat):

```bash
docker run --rm -it -e ALLOW_RUN=1 build-order-executor-headless
```

**Integration tests against 4.10** (pytest is baked into the image; mount the repo so it
runs your current tests/code):

```bash
docker run --rm -v "$PWD":/opt/executor build-order-executor-headless \
  python -m pytest /opt/executor/tests/test_integration.py --run-integration
```

## Publishing to GHCR

Publish to GitHub Container Registry (replace `<OWNER>` with your GitHub user/org):

```bash
# 1. Log in once (gh auth token --scopes write:packages`) and then:
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

`SC2_TARGET=linux` is baked in, so `python run.py …` needs no launch flags.

## Agent Environment Architecture

- **Unprivileged Agent User**: Container runs as non-root user `agent` (`UID 1000`) in `/workspace`.
- **Source Protection**: The engine code in `/opt/executor` is compiled to `.pyc` (with `.py` deleted) and locked to `chmod 700` (`root:root`). The agent has zero read access to engine internals.
- **SetUID Execution**: A compiled C wrapper `/usr/local/bin/run-build` (`chmod 4755`) executes games with elevated permissions while keeping `/opt/executor` unreadable.
- **Evaluation Modes**:
  - **Offline Mode (Default)**: `ALLOW_RUN` not set. `/workspace` contains only `README.md` (the DSL guide) and `reference/`. `run.py` does not exist in `/workspace`.
  - **Interactive Mode**: Pass `-e ALLOW_RUN=1`. `run.py` is created dynamically in `/workspace` at startup for iterative agent testing.

## Installing and Running Claude Code

To run Claude Code inside the container:

   ```bash
   curl -fsSL https://claude.ai/install.sh | bash
   ~/.local/bin/claude
   ```