# Runtime image: the build-order bot + Blizzard's headless StarCraft II Linux build,
# runnable with `run.py --target linux` (native SC2_x64, no Wine, no display).
#
# This image is deliberately harbor-agnostic: it just containerizes the bot so it
# can run headless (for CI, local repro, or as the base an eval benchmark builds
# FROM). Nothing about any eval framework lives here.
#
# Build (context is the REPO ROOT so the whole repo is available to COPY):
#   docker build -f docker/Dockerfile -t build-order-executor-headless .
# Run a build once (agent/CI style; prints the machine-readable [summary] line):
#   docker run --rm build-order-executor-headless \
#       python run.py --build builds/pvz_opening_8worker.yaml --map <MapName> --time-limit 300
# Interactive shell (the mode a terminal agent iterates in):
#   docker run --rm -it build-order-executor-headless
#
# NOTE: the game is version 4.10 (base build shipped in SC2.4.10.zip) — the only
# headless Linux build Blizzard publishes. Costs/timings differ from the current
# retail patch, but the bot reads them live from the running client, so it stays
# correct for whatever version is installed here.
FROM python:3.14-slim

# curl + unzip to fetch/extract the game; ca-certificates for the HTTPS pip index.
# (Deps install from wheels, so no compiler toolchain is needed.)
RUN apt-get update \
    && apt-get install --assume-yes --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Blizzard's headless Linux build. Extracts to /root/StarCraftII (== ~/StarCraftII,
# the python-sc2 default for SC2PF=Linux), so the `linux` target needs no SC2PATH.
# The zip is EULA-locked with the well-known password 'iagreetotheeula'. We keep the
# maps that ship with the build (the runner passes a valid --map); we only add a
# lowercase `maps` symlink because python-sc2 probes that name first.
WORKDIR /root
RUN curl --retry 5 --retry-delay 5 -C - \
        http://blzdistsc2-a.akamaihd.net/Linux/SC2.4.10.zip -o SC2.4.10.zip \
    && unzip -q -P iagreetotheeula SC2.4.10.zip \
    && rm SC2.4.10.zip \
    && ln -s /root/StarCraftII/Maps /root/StarCraftII/maps

# Bot code + Python deps. pytest is included so the integration suite can run inside
# the image against 4.10 (see docker/README.md); the eval runtime itself doesn't use it.
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt pytest pytest-asyncio
COPY . .

# Default to the headless launch target so `python run.py ...` inside the container
# needs no extra flags; still overridable per-invocation with --target / SC2_TARGET.
ENV SC2_TARGET=linux

# A terminal agent iterates here (edit a build, run it, read [summary], repeat);
# a one-shot run overrides this with an explicit `python run.py ...` command.
CMD ["bash"]
