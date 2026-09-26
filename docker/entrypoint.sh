#!/bin/bash
# Container entrypoint. Runs as root, sets up the run gate, then drops to `agent`.
#
# ALLOW_RUN=1 selects INTERACTIVE mode: the agent gets /workspace/run.py and can play
# games. Unset (the default) is OFFLINE mode: no run.py, no way to reach the engine, so
# the agent must write a build from the docs alone.
#
# Everything privileged happens here, before the unprivileged agent exists — in
# particular the run cap, which is written to a root-owned file rather than left in the
# environment. The agent controls the environment it invokes run-build with, so an
# env-var cap would be trivially bypassed.
set -e

LEDGER_ROOT=/var/lib/run-ledger

if [ "$ALLOW_RUN" = "1" ]; then
    max_attempts="${RUN_MAX_ATTEMPTS:-50}"
    if ! [[ "$max_attempts" =~ ^[0-9]+$ ]]; then
        echo "entrypoint: RUN_MAX_ATTEMPTS must be a non-negative integer (got '$max_attempts')" >&2
        exit 1
    fi
    game_map="${RUN_MAP:-CatalystLE}"
    if ! [[ "$game_map" =~ ^[A-Za-z0-9_-]+$ ]]; then
        echo "entrypoint: RUN_MAP must be alphanumeric (got '$game_map')" >&2
        exit 1
    fi

    # 0 means unlimited; the eval always sets a real number.
    mkdir -p "$LEDGER_ROOT/attempts"
    printf '{"max_attempts": %s, "map": "%s"}\n' "$max_attempts" "$game_map" \
        > "$LEDGER_ROOT/limits.json"
    chown -R root:root "$LEDGER_ROOT"
    chmod -R go-rwx "$LEDGER_ROOT"

    install -o root -g root -m 4755 /opt/executor/run-build /usr/local/bin/run-build

    # run.py is a shim, not the engine: it just hands argv to the setuid wrapper, which
    # is the only thing that can read /opt/executor.
    cat << 'RUN_EOF' > /workspace/run.py
#!/usr/bin/env python3
import os
import sys

os.execv("/usr/local/bin/run-build", ["run-build"] + sys.argv[1:])
RUN_EOF
    chmod 755 /workspace/run.py
    chown agent:agent /workspace/run.py
else
    rm -f /usr/local/bin/run-build /workspace/run.py
fi

exec runuser -u agent -- "$@"
