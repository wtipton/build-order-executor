#!/usr/bin/env python3
"""Launch SC2 and run the opening bot.

Most-natural python-sc2 usage: the library launches its own SC2 instance,
creates a game vs a passive (do-nothing) opponent, and runs our bot. On Linux
with a Wine/Lutris install we just tell the library how to launch:

    SC2PF=WineLinux        -> use the Windows SC2_x64.exe under Wine
    WINE=/path/to/wine     -> the wine binary (Lutris ships its own)
    SC2PATH=/path/to/...   -> the "StarCraft II" folder inside the wine prefix

These are read by sc2.paths at import time, so we set sensible defaults here
*before* importing sc2. Override any of them in your shell.

The game always runs NON-realtime: the simulation advances as fast as the bot
steps it (no render bottleneck), then saves a replay you watch in SC2 at native
speed/quality. (Realtime play lags badly under Wine, so it isn't offered.)

Usage:
    python run.py                              # default map, save replay
    python run.py --map LockdownLE
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime

# --- configure the launcher BEFORE importing sc2 ----------------------------
# These are read by sc2.paths at import time. Defaults match this machine's
# Lutris install; override any of them in your shell.
HOME = os.path.expanduser("~")
DEFAULTS = {
    "SC2PF": "WineLinux",
    "SC2PATH": f"{HOME}/Games/nobak/sc2_bot/drive_c/Program Files (x86)/StarCraft II",
    # Dedicated prefix for the bot (separate from the one you watch replays in),
    # so python-sc2's `wineserver -k` teardown only kills the bot's own game.
    # python-sc2's launcher does NOT set WINEPREFIX itself, so we must.
    "WINEPREFIX": f"{HOME}/Games/nobak/sc2_bot",
    "WINE": f"{HOME}/.local/share/lutris/runners/wine/GE-Proton10-34/files/bin/wine",
}
if os.name != "nt":
    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)

from pathlib import Path  # noqa: E402

from sc2.maps import Map  # noqa: E402
from sc2.bot_ai import BotAI  # noqa: E402
from sc2.data import Race  # noqa: E402
from sc2.main import run_game  # noqa: E402
from sc2.player import Bot  # noqa: E402
from loguru import logger as _loguru  # noqa: E402  (python-sc2's logging backend)

from bot import BuildOrderBot, load_build  # noqa: E402

# One-line legend for the always-on output tags (printed at startup).
OUTPUT_LEGEND = ("[run] output: [step]=step fired  [done]=milestone  "
                 "[stall]=step stalled (with reason)  [end]=final  "
                 "[summary]=machine-readable JSON  "
                 "(--debug adds [hb] heartbeat / [step*] / [prewalk])")


class PassiveBot(BotAI):
    """A do-nothing opponent."""

    async def on_step(self, iteration: int) -> None:
        pass


# Keep replays out of the real SC2 replay folder for now.
REPLAY_DIR = Path.home() / "replays" / "bot"

# Fixed RNG seed for the game engine: a build order is deterministic (no army micro,
# passive opponent), and we want identical runs to reproduce exactly — the same seed
# fixes spawn assignment and any engine RNG. There's no upside to randomizing it.
GAME_SEED = 42


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="builds/pvz_opening_8worker.yaml", help="build-order config")
    ap.add_argument("--map", default="LockdownLE", help="map filename without .SC2Map")
    ap.add_argument("--fullscreen", action="store_true", help="launch SC2 fullscreen (-displayMode 1)")
    ap.add_argument("--replay", default=None, help="replay output path (default: in-game Replays dir)")
    ap.add_argument("--time-limit", type=int, default=300, help="end game after N game-seconds (0 = no limit)")
    ap.add_argument("--debug", action="store_true", help="verbose economy/timing heartbeat each ~10 game-seconds")
    ap.add_argument("--dump-data", action="store_true", help="dump game unit/upgrade data on start (see gamedata_dump.py)")
    args = ap.parse_args()

    # Quiet python-sc2's own (loguru) INFO/boot chatter so our tagged lines are the
    # signal; keep genuine warnings/errors but drop the benign connection-teardown
    # errors our own concede (client.leave) provokes at the end of every run.
    _TEARDOWN_NOISE = ("KILLED", "Connection already closed",
                       "Connection was closed before the game ended")
    _loguru.remove()
    _loguru.add(sys.stderr, level="WARNING",
                filter=lambda r: not any(s in r["message"] for s in _TEARDOWN_NOISE))

    # Pass a bare map filename; SC2 locates it among its own map roots.
    game_map = Map(Path(f"{args.map}.SC2Map"))

    build = load_build(args.build)
    print(f"[run] build loaded: {build.name}")
    print(OUTPUT_LEGEND)

    if args.replay:
        replay_path = Path(args.replay)
    else:
        REPLAY_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", build.name.lower()).strip("-")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        replay_path = REPLAY_DIR / f"{slug}_{stamp}.SC2Replay"

    run_game(
        game_map,
        [
            Bot(Race[build.race],
                BuildOrderBot(build, debug=args.debug, dump_data=args.dump_data),
                name="BuildOrderBot", fullscreen=args.fullscreen),
            Bot(Race.Terran, PassiveBot(), name="PassiveBot"),
        ],
        realtime=False,
        random_seed=GAME_SEED,
        save_replay_as=str(replay_path),
        game_time_limit=args.time_limit or None,
    )
    print(f"[run] replay={replay_path}")


if __name__ == "__main__":
    main()
