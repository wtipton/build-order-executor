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

# Bot engine and Python dependencies installed in /opt/executor (compiled to .pyc)
WORKDIR /opt/executor
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt pytest pytest-asyncio
COPY . .
RUN rm -rf docker \
    && mv run.py _runner.py \
    && python -m compileall -b /opt/executor \
    && find /opt/executor -name "*.py" -delete

# Create non-root agent user (UID 1000)
RUN useradd -m -s /bin/bash -u 1000 agent \
    && ln -s /opt/StarCraftII /home/agent/StarCraftII \
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

# Lock /opt/executor: strictly root-only access (non-root agent cannot read/list files)
RUN chown -R root:root /opt/executor \
    && chmod 700 /opt/executor

ENV PYTHONPATH="/opt/executor"
ENV SC2PATH="/opt/StarCraftII"
ENV SC2_TARGET=linux

# Entrypoint script: dynamically creates run.py only when ALLOW_RUN=1 is passed,
# then drops privileges to non-root user 'agent'
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod 755 /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["bash"]
