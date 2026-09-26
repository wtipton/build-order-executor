#!/usr/bin/env python3
"""Ledgered, rate-limited entry point to the engine. Runs as root via run_build.c.

Every game the agent runs passes through here, so this is where the eval gets its
record of what was tried. For each invocation it:

  1. validates the caller's arguments against a small whitelist (see ARGUMENTS),
  2. allocates the next attempt directory and enforces the run cap,
  3. COPIES the submitted YAML into that directory before running it,
  4. runs the engine on the copy, teeing output to the agent and to the log,
  5. appends one line to index.jsonl.

The ledger lives at /var/lib/run-ledger (root:root, 0700) — unreadable and unwritable by
the agent, so the record can't be forged or erased from inside the container. The
verifier, which runs as root after the agent is done, copies it out.

Imports stdlib only: run_build.c invokes python with -I, so there is no sys.path entry
for /opt/executor and nothing from the engine is importable here.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PYTHON = "/opt/venv/bin/python"
RUNNER = "/opt/executor/_runner.pyc"

LEDGER_ROOT = Path("/var/lib/run-ledger")
ATTEMPTS = LEDGER_ROOT / "attempts"
INDEX = ATTEMPTS / "index.jsonl"
LIMITS = LEDGER_ROOT / "limits.json"

# The agent may only submit builds from its own workspace. Without this check, --build
# takes an arbitrary path read as root, and a parse error echoing file contents would
# turn into a read primitive over /opt/executor.
WORKSPACE = Path("/workspace")

# Bounds on --time-limit (game-seconds). The upper bound is well past the engine's own
# ~1320s Tie; it exists only to keep a typo from parking a container for an hour.
TIME_LIMIT_MIN = 1
TIME_LIMIT_MAX = 1800
TIME_LIMIT_DEFAULT = 300

USAGE = """usage: run.py --build PATH [--time-limit SECONDS]

  --build PATH         build order YAML to run; must be under /workspace
  --time-limit N       end the game after N game-seconds (default 300, max 1800)

The map and launch target are fixed by the task and cannot be set here."""


def fail(message: str, *, usage: bool = False) -> None:
    print(f"[run-build] {message}", file=sys.stderr)
    if usage:
        print(USAGE, file=sys.stderr)
    raise SystemExit(2)


def parse_args(argv: list[str]) -> tuple[Path, int]:
    """Whitelist parse of the caller's arguments.

    Deliberately NOT argparse-over-run.py's-own-flags: those include --replay and --map,
    which as root would be an arbitrary-write primitive and a way to silently diverge
    from the map the verifier will score on. Only two flags get through.
    """
    build: str | None = None
    time_limit = TIME_LIMIT_DEFAULT

    args = []
    for arg in argv:  # normalise --flag=value into two tokens
        if arg.startswith("--") and "=" in arg:
            args.extend(arg.split("=", 1))
        else:
            args.append(arg)

    i = 0
    while i < len(args):
        flag = args[i]
        if flag not in ("--build", "--time-limit"):
            fail(f"unsupported argument {flag!r}", usage=True)
        if i + 1 >= len(args):
            fail(f"{flag} needs a value", usage=True)
        value = args[i + 1]
        i += 2

        if flag == "--build":
            build = value
        else:
            try:
                time_limit = int(value)
            except ValueError:
                fail(f"--time-limit must be an integer, got {value!r}")
            if not TIME_LIMIT_MIN <= time_limit <= TIME_LIMIT_MAX:
                fail(f"--time-limit must be {TIME_LIMIT_MIN}..{TIME_LIMIT_MAX}")

    if build is None:
        fail("--build is required", usage=True)

    # realpath first: a symlink in /workspace must not reach outside it.
    path = Path(os.path.realpath(build))
    if not path.is_file():
        fail(f"no such build file: {build}")
    if WORKSPACE not in path.parents:
        fail(f"--build must be a file under {WORKSPACE} (got {path})")
    return path, time_limit


def read_limits() -> dict:
    """Run cap and map, written by the entrypoint as root at container start.

    Read from a root-owned file rather than the environment on purpose: the agent
    controls the environment it invokes run-build with, but cannot touch this file.
    """
    try:
        return json.loads(LIMITS.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        fail(f"malformed {LIMITS}")


def allocate_attempt(cap: int) -> tuple[Path, int]:
    """Claim the next attempt directory, or refuse if the cap is spent.

    mkdir is the allocator: it fails atomically if the name is taken, so concurrent
    invocations can't land on the same number.
    """
    ATTEMPTS.mkdir(parents=True, exist_ok=True)
    used = sum(1 for entry in ATTEMPTS.iterdir() if entry.is_dir())
    if cap and used >= cap:
        fail(f"attempt budget exhausted ({used}/{cap} runs used)")

    n = used
    while True:
        attempt_dir = ATTEMPTS / f"{n:04d}"
        try:
            attempt_dir.mkdir()
            return attempt_dir, n
        except FileExistsError:
            n += 1


def main() -> int:
    os.umask(0o077)  # everything written here stays root-only

    build_path, time_limit = parse_args(sys.argv[1:])
    limits = read_limits()
    cap = int(limits.get("max_attempts", 0))  # 0 = unlimited
    game_map = limits.get("map", "CatalystLE")

    attempt_dir, n = allocate_attempt(cap)
    source = attempt_dir / "build.yaml"
    shutil.copyfile(build_path, source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    budget = f"{n + 1}/{cap}" if cap else f"{n + 1}"
    print(f"[run-build] attempt {budget}  map={game_map} time-limit={time_limit}s "
          f"sha256={digest[:12]}", flush=True)

    # -E -s, not -I: the engine imports its own modules (bot, schema, catalog) from
    # /opt/executor, and it finds them because python prepends the script's directory to
    # sys.path. -I implies -P, which suppresses exactly that, so the runner cannot start
    # under it. -E (ignore PYTHON*) and -s (no user site) give the hardening we actually
    # need here; the environment is already rebuilt from scratch by run_build.c, and this
    # subprocess inherits that clean copy.
    command = [
        PYTHON, "-E", "-s", RUNNER,
        "--target", "linux",
        "--map", game_map,
        "--build", str(source),
        "--time-limit", str(time_limit),
        "--replay", str(attempt_dir / "replay.SC2Replay"),
    ]

    started = time.time()
    with open(attempt_dir / "output.log", "wb") as log:
        process = subprocess.Popen(
            command, cwd="/opt/executor",
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        for line in process.stdout:  # tee: the agent sees output live, we keep a copy
            sys.stdout.buffer.write(line)
            sys.stdout.flush()
            log.write(line)
        returncode = process.wait()
    elapsed = round(time.time() - started, 1)

    record = {
        "attempt": n,
        "argv": sys.argv[1:],
        "build_sha256": digest,
        "submitted_path": str(build_path),
        "map": game_map,
        "time_limit": time_limit,
        "returncode": returncode,
        "started_at": round(started, 1),
        "elapsed_sec": elapsed,
    }
    (attempt_dir / "cmd.json").write_text(json.dumps(record, indent=2) + "\n")
    with open(INDEX, "a") as index:
        index.write(json.dumps(record) + "\n")

    print(f"[run-build] attempt {budget} finished in {elapsed}s (exit {returncode})",
          flush=True)
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
