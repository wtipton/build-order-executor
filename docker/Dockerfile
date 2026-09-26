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
#   docker run --rm -e ALLOW_RUN=1 build-order-executor-headless \
#       python run.py --build builds/pvz_opening_8worker.yaml --time-limit 300
# Interactive shell (the mode a terminal agent iterates in):
#   docker run --rm -it -e ALLOW_RUN=1 build-order-executor-headless
#
# The agent is a non-root user with NO access to the engine, the game, or python-sc2:
# the only route to a game is the setuid `run-build` wrapper, which ledgers every run
# and enforces a cap. See docker/README.md for the boundary in full.
#
# NOTE: the game is version 4.10 (base build shipped in SC2.4.10.zip) — the only
# headless Linux build Blizzard publishes. Costs/timings differ from the current
# retail patch, but the bot reads them live from the running client, so it stays
# correct for whatever version is installed here.
FROM python:3.14-slim

# curl + unzip to fetch/extract the game; ca-certificates for the HTTPS pip index.
RUN apt-get update \
    && apt-get install --assume-yes --no-install-recommends curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Blizzard's headless Linux build at /opt/StarCraftII (placed at top so it is permanently cached)
WORKDIR /opt
RUN curl --retry 5 --retry-delay 5 -C - \
        http://blzdistsc2-a.akamaihd.net/Linux/SC2.4.10.zip -o SC2.4.10.zip \
    && unzip -q -P iagreetotheeula SC2.4.10.zip \
    && rm SC2.4.10.zip \
    && ln -s /opt/StarCraftII/Maps /opt/StarCraftII/maps \
    && chmod -R 755 /opt/StarCraftII

# Build tools & compiler for setuid wrapper
RUN apt-get update \
    && apt-get install --assume-yes --no-install-recommends gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# Engine dependencies (python-sc2 above all) go in a root-only venv, NOT the system
# python: if the agent could `import sc2` it could launch its own games and bypass the
# ledger and the run cap entirely. The system python keeps PyYAML only, so the agent can
# still generate and inspect build files.
RUN /usr/local/bin/python -m venv /opt/venv
WORKDIR /opt/executor
COPY requirements.txt ./
RUN /opt/venv/bin/pip install --no-cache-dir -r requirements.txt pytest pytest-asyncio \
    && /usr/local/bin/pip install --no-cache-dir PyYAML

# Bot engine in /opt/executor, compiled to .pyc (with sources deleted). attempt.py is
# the ledgered entry point run_build.c execs; run.py becomes _runner.pyc so nothing in
# /workspace can be confused for the real engine.
COPY . .
COPY docker/attempt.py ./_attempt.py
RUN rm -rf docker \
    && mv run.py _runner.py \
    && /usr/local/bin/python -m compileall -b /opt/executor \
    && find /opt/executor -name "*.py" -delete

# Create non-root agent user (UID 1000)
RUN useradd -m -s /bin/bash -u 1000 agent \
    && ln -s /opt/StarCraftII /root/StarCraftII

# Compile runner binary inside /opt/executor (locked to root:root, chmod 700)
COPY docker/run_build.c /opt/run_build.c
RUN gcc -O2 /opt/run_build.c -o /opt/executor/run-build \
    && rm /opt/run_build.c \
    && chown root:root /opt/executor/run-build \
    && chmod 700 /opt/executor/run-build

# Clean agent workspace: contains only DSL docs and reference tables by default
WORKDIR /workspace
RUN cp /opt/executor/README.agent.md ./README.md \
    && if [ -d /opt/executor/reference ]; then cp -r /opt/executor/reference ./; fi \
    && chown -R agent:agent /workspace

# The root-only boundary. Everything the agent must not reach is locked here:
#   /opt/executor       engine sources (.pyc) and the setuid wrapper
#   /opt/venv           python-sc2 — otherwise the agent scripts its own games
#   /opt/StarCraftII    the game itself — otherwise it launches SC2 directly
#   /var/lib/run-ledger the attempt ledger; must be unforgeable from inside the container
RUN mkdir -p /var/lib/run-ledger \
    && chown -R root:root /opt/executor /opt/venv /opt/StarCraftII /var/lib/run-ledger \
    && chmod -R go-rwx /opt/executor /opt/venv /opt/StarCraftII /var/lib/run-ledger

# NOTE: no PYTHONPATH. It used to be set here, and because the kernel does not strip
# PYTHONPATH across a setuid exec, an agent could point it at its own module and get
# root. run_build.c now builds the environment from scratch and runs python -I.
ENV SC2PATH="/opt/StarCraftII"
ENV SC2_TARGET=linux

# Entrypoint script: provisions the run gate and attempt ledger as root when ALLOW_RUN=1,
# then drops privileges to non-root user 'agent'
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
