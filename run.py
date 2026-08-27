#!/usr/bin/env python3
"""Launch SC2 and run the opening bot.

Most-natural python-sc2 usage: the library launches its own SC2 instance,
creates a game vs a passive (do-nothing) opponent, and runs our bot. We support
two launch TARGETS (select with --target or SC2_TARGET; default: wine):

    wine   -> local Lutris/Wine install running the CURRENT retail patch, for
              personal build optimization. Uses the Windows SC2_x64.exe under Wine
              (SC2PF=WineLinux + WINE + WINEPREFIX + SC2PATH).
    linux  -> Blizzard's headless Linux build (e.g. game version 4.10) inside
              Docker, for reproducible agent evals. Native SC2_x64 binary, NO Wine
              (SC2PF=Linux); the game version is whatever's installed under SC2PATH.

The library reads these env vars in sc2.paths at import time, so the selected
target's defaults are applied here (via setdefault) *before* importing sc2. Any
single var can still be overridden in the shell / Docker ENV.

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
# sc2.paths reads these env vars at import time, so the launch target must be
# resolved and applied here, ahead of the sc2 imports below. --target is declared
# in argparse too (for --help), but parsed there is too late — so pre-scan argv.
HOME = os.path.expanduser("~")
TARGETS = {
    # Local Lutris/Wine install, CURRENT retail patch. Uses a dedicated wine prefix
    # (separate from the one you watch replays in) so python-sc2's `wineserver -k`
    # teardown only kills the bot's own game; the launcher does NOT set WINEPREFIX
    # itself, so we must.
    "wine": {
        "SC2PF": "WineLinux",
        "SC2PATH": f"{HOME}/Games/nobak/sc2_bot/drive_c/Program Files (x86)/StarCraft II",
        "WINEPREFIX": f"{HOME}/Games/nobak/sc2_bot",
        "WINE": f"{HOME}/.local/share/lutris/runners/wine/GE-Proton10-34/files/bin/wine",
    },
    # Blizzard's headless Linux build inside Docker. SC2PF=Linux selects the native
    # SC2_x64 binary (no Wine, no WINE/WINEPREFIX). SC2PATH falls through to the
    # library default (~/StarCraftII) unless set in the environment; the game
    # version is whatever's installed there (each image ships exactly one).
    "linux": {
        "SC2PF": "Linux",
    },
}


def _select_target() -> str:
    """The launch target from --target (argv) or SC2_TARGET, default 'wine'."""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--target" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--target="):
            return a.split("=", 1)[1]
    return os.environ.get("SC2_TARGET", "wine")


_TARGET = _select_target()
if _TARGET not in TARGETS:
    sys.exit(f"[run] unknown --target {_TARGET!r}; valid: {', '.join(TARGETS)}")
if os.name != "nt":
    for k, v in TARGETS[_TARGET].items():
        os.environ.setdefault(k, v)

from pathlib import Path  # noqa: E402

from sc2 import maps as sc2_maps  # noqa: E402
from sc2.maps import Map  # noqa: E402
from sc2.bot_ai import BotAI  # noqa: E402
from sc2.data import Race  # noqa: E402
from sc2.main import run_game  # noqa: E402
from sc2.player import Bot  # noqa: E402
from loguru import logger as _loguru  # noqa: E402  (python-sc2's logging backend)

from bot import BuildOrderBot, load_build  # noqa: E402

# One-line legend for the always-on output tags (printed at startup).
OUTPUT_LEGEND = ("[run] output: [step]=step fired  [complete]=type first finished  "
                 "[status]=periodic economy + current-step snapshot  [end]=final  "
                 "[summary]=machine-readable JSON  "
                 "[builder]=a probe pulled off the line for a build: assigned (walking there) then building (order in, dist= how far it still had to walk)")


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


def resolve_map(name: str) -> Map:
    """Locate the map named `name` for the current target. The native (headless)
    client opens the exact path we hand it, so we resolve to an absolute path with
    maps.get (its maps sit in per-season subfolders); the Windows client under Wine
    resolves a bare relative name itself and can't open an absolute Linux path."""
    if os.environ.get("SC2PF") != "WineLinux":
        try:
            return sc2_maps.get(name)
        except KeyError:
            pass
    return Map(Path(f"{name}.SC2Map"))


def main() -> None:
    default_map = os.environ.get("SC2_MAP", "CatalystLE" if _TARGET == "linux" else "LockdownLE")
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=_TARGET, choices=sorted(TARGETS),
                    help="launch target (also via SC2_TARGET env); resolved before sc2 import")
    ap.add_argument("--build", default="builds/pvz_opening_8worker.yaml", help="build-order config")
    ap.add_argument("--map", default=default_map,
                    help="map filename without .SC2Map (default: CatalystLE for linux, LockdownLE for wine; also via SC2_MAP env)")
    ap.add_argument("--fullscreen", action="store_true", help="launch SC2 fullscreen (-displayMode 1)")
    ap.add_argument("--replay", default=None, help="replay output path (default: in-game Replays dir)")
    ap.add_argument("--time-limit", type=int, default=300, help="end game after N game-seconds (0 = no limit)")
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

    game_map = resolve_map(args.map)

    build = load_build(args.build)
    print(f"[run] target={_TARGET} SC2PF={os.environ.get('SC2PF')} "
          f"SC2PATH={os.environ.get('SC2PATH', '(library default)')}")
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
            Bot(Race.Protoss,
                BuildOrderBot(build, dump_data=args.dump_data),
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
