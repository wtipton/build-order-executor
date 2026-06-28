#!/usr/bin/env python3
"""Launch SC2 and run the opening bot.

Most-natural python-sc2 usage: the library launches its own SC2 instance,
creates a game vs the built-in AI, and runs our bot. On Linux with a
Wine/Lutris install we just tell the library how to launch:

    SC2PF=WineLinux        -> use the Windows SC2_x64.exe under Wine
    WINE=/path/to/wine     -> the wine binary (Lutris ships its own)
    SC2PATH=/path/to/...   -> the "StarCraft II" folder inside the wine prefix

These are read by sc2.paths at import time, so we set sensible defaults here
*before* importing sc2. Override any of them in your shell.

By default the game runs NON-realtime: the simulation advances as fast as the
bot steps it (no render bottleneck), then saves a replay you watch in SC2 at
native speed/quality. Realtime windowed play tends to lag badly under Wine.

Usage:
    python run.py                              # default: vs Easy Zerg, save replay
    python run.py --map LockdownLE --opponent terran --difficulty hard
    python run.py --realtime                   # watch live (may lag under Wine)
"""

from __future__ import annotations

import argparse
import os
import re
from datetime import datetime

# --- configure the launcher BEFORE importing sc2 ----------------------------
# These are read by sc2.paths at import time. Defaults match this machine's
# Lutris install; override any of them in your shell.
HOME = os.path.expanduser("~")
DEFAULTS = {
    "SC2PF": "WineLinux",
    "SC2PATH": f"{HOME}/Games/starcraft-ii-2/drive_c/Program Files (x86)/StarCraft II",
    # python-sc2's launcher does NOT set WINEPREFIX itself, so we must.
    "WINEPREFIX": f"{HOME}/Games/starcraft-ii-2",
    "WINE": f"{HOME}/.local/share/lutris/runners/wine/GE-Proton10-34/files/bin/wine",
}
if os.name != "nt":
    for k, v in DEFAULTS.items():
        os.environ.setdefault(k, v)

from pathlib import Path  # noqa: E402

from sc2.maps import Map  # noqa: E402
from sc2.data import Difficulty, Race  # noqa: E402
from sc2.main import run_game  # noqa: E402
from sc2.player import Bot, Computer  # noqa: E402

from bot import BuildOrderBot, load_build  # noqa: E402

RACES = {"zerg": Race.Zerg, "terran": Race.Terran, "protoss": Race.Protoss, "random": Race.Random}
DIFFS = {
    "easy": Difficulty.Easy,
    "medium": Difficulty.Medium,
    "hard": Difficulty.Hard,
    "harder": Difficulty.Harder,
}


# Keep replays out of the real SC2 replay folder for now.
REPLAY_DIR = Path.home() / "replays" / "bot"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", default="builds/pvz_pvt_opening_8worker.yaml", help="build-order config")
    ap.add_argument("--map", default="LockdownLE", help="map filename without .SC2Map")
    ap.add_argument("--opponent", default="zerg", choices=RACES, help="built-in AI race")
    ap.add_argument("--difficulty", default="easy", choices=DIFFS)
    ap.add_argument("--realtime", action=argparse.BooleanOptionalAction, default=False,
                    help="--realtime watches live (may lag under Wine); --no-realtime (default) runs as fast as possible")
    ap.add_argument("--fullscreen", action="store_true", help="launch SC2 fullscreen (-displayMode 1)")
    ap.add_argument("--replay", default=None, help="replay output path (default: in-game Replays dir)")
    ap.add_argument("--time-limit", type=int, default=300, help="end game after N game-seconds (0 = no limit)")
    ap.add_argument("--debug", action="store_true", help="verbose economy/timing heartbeat each ~10 game-seconds")
    args = ap.parse_args()

    # Pass a bare map filename; SC2 locates it among its own map roots.
    game_map = Map(Path(f"{args.map}.SC2Map"))

    build = load_build(args.build)
    print(f"Build: {build.get('name', args.build)}")

    if args.replay:
        replay_path = Path(args.replay)
    else:
        REPLAY_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9]+", "-", build.get("name", "build").lower()).strip("-")
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        replay_path = REPLAY_DIR / f"{slug}_{stamp}.SC2Replay"

    run_game(
        game_map,
        [
            # fullscreen on the Bot player -> SC2Process launches with -displayMode 1
            Bot(Race.Protoss, BuildOrderBot(build, debug=args.debug), name="OpeningBot", fullscreen=args.fullscreen),
            Computer(RACES[args.opponent], DIFFS[args.difficulty]),
        ],
        realtime=args.realtime,
        save_replay_as=str(replay_path),
        game_time_limit=args.time_limit or None,
    )
    print(f"\nReplay saved: {replay_path}\n(open it in SC2 to watch the build at full speed)")


if __name__ == "__main__":
    main()
