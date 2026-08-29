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

```
docker run --rm build-order-executor-headless \
  python run.py --build builds/pvz_opening_8worker.yaml --time-limit 300
```

**Interactive shell** (how a terminal agent iterates — edit a build, run it, read
`[summary]`, repeat):

```
docker run --rm -it build-order-executor-headless
```

**Integration tests against 4.10** (pytest is baked into the image; mount the repo so it
runs your current tests/code):

```
docker run --rm -v "$PWD":/app build-order-executor-headless \
  python -m pytest tests/test_integration.py --run-integration
```

## Publishing to GHCR

Publish to GitHub Container Registry (replace `<OWNER>` with your GitHub user/org):

```
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

Notes:
- `SC2_TARGET=linux` is baked in, so `python run.py …` needs no launch flags.
- **Standard eval map: `CatalystLE`** (a 2-player ladder map that ships with 4.10).
  `--map` takes a bare name resolved against the build's map roots; any 4.10 ladder
  map works (`AutomatonLE`, `AbyssalReefLE`, …), but modern ladder maps won't load.
- v4.10 costs/timings differ from current retail, but the bot reads them live from
  the running client, so it stays correct for whatever version is installed.
- `.dockerignore` (repo root) keeps `.venv`, caches, logs, and replays out of the image.
