"""One-shot dump of authoritative unit/structure/upgrade data from the live game.

Costs and BASE build times come straight from the running SC2 client, so they're correct
for the installed patch (unlike Liquipedia, which lags balance changes). Build times are
in game-seconds (frames / 22.4). Runtime modifiers (e.g. the Warp Gate research gateway
train-time reduction) are NOT reflected in these static values — see
reference/protoss_data_live.md.

The values only exist on a running client, so this launches its own throwaway game,
dumps, and quits. It deliberately does NOT go through the bot: nothing outside scripts/
may depend on this package, so that scripts/ can be left out of the Docker image (see
scripts/__init__.py).

    python -m scripts.gamedata_dump --target linux 2>&1 | grep -E "^DATA|^ABIL|^UPG"
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

import run  # noqa: F401  (sets SC2PF/SC2PATH before sc2 is imported; must come first)

from sc2.bot_ai import BotAI
from sc2.data import Race, Result
from sc2.game_data import Cost
from sc2.main import run_game
from sc2.player import Bot
from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

if TYPE_CHECKING:
    from sc2.bot_ai import BotAI as _Bot

STRUCTURES = ["NEXUS", "PYLON", "ASSIMILATOR", "GATEWAY", "WARPGATE", "CYBERNETICSCORE",
              "FORGE", "TWILIGHTCOUNCIL", "ROBOTICSFACILITY", "STARGATE", "TEMPLARARCHIVE",
              "DARKSHRINE", "ROBOTICSBAY", "FLEETBEACON", "PHOTONCANNON", "SHIELDBATTERY"]
UNITS = ["PROBE", "ZEALOT", "STALKER", "SENTRY", "ADEPT", "HIGHTEMPLAR", "DARKTEMPLAR",
         "ARCHON", "IMMORTAL", "OBSERVER", "WARPPRISM", "COLOSSUS", "DISRUPTOR",
         "PHOENIX", "ORACLE", "VOIDRAY", "TEMPEST", "CARRIER", "MOTHERSHIP"]
ABILITIES = ["MORPH_WARPGATE", "MORPH_GATEWAY", "WARPGATETRAIN_ZEALOT",
             "WARPGATETRAIN_STALKER", "TRAINWARP_ADEPT", "GATEWAYTRAIN_ZEALOT"]
UPGRADES = ["WARPGATERESEARCH", "BLINKTECH", "CHARGE", "PROTOSSGROUNDWEAPONSLEVEL1",
            "PROTOSSGROUNDARMORSLEVEL1", "PROTOSSSHIELDSLEVEL1", "PSISTORMTECH",
            "PROTOSSAIRWEAPONSLEVEL1", "GRAVITICDRIVE", "EXTENDEDTHERMALLANCE"]

_FRAMES_PER_SEC = 22.4


def _secs(cost: Cost) -> float:
    return (cost.time or 0) / _FRAMES_PER_SEC


def dump(bot: _Bot) -> None:
    """Print the data tables (tab-separated) using the bot's live game_data."""
    gd = bot.game_data
    print("DATA\tNAME\tmin\tgas\tbuild_s\tfood_req\tfood_prov\tmorph(min/gas/s)", flush=True)
    for name in STRUCTURES + UNITS:
        try:
            d = gd.units[U[name].value]
            c, p = d.cost, d._proto
            try:
                mc = d.morph_cost
            except Exception:
                mc = None
            morph = "" if mc is None else f"{mc.minerals}/{mc.vespene}/{_secs(mc):.1f}"
            print(f"DATA\t{name}\t{c.minerals}\t{c.vespene}\t{_secs(c):.1f}"
                  f"\t{p.food_required}\t{p.food_provided}\t{morph}", flush=True)
        except Exception as e:
            print(f"DATA\t{name}\tERR\t{e}", flush=True)
    for ab in ABILITIES:
        try:
            c = gd.abilities[AbilityId[ab].value].cost
            print(f"ABIL\t{ab}\t{c.minerals}\t{c.vespene}\t{_secs(c):.1f}", flush=True)
        except Exception as e:
            print(f"ABIL\t{ab}\tERR\t{e}", flush=True)
    for up in UPGRADES:
        try:
            c = gd.upgrades[UpgradeId[up].value].cost
            print(f"UPG\t{up}\t{c.minerals}\t{c.vespene}\t{_secs(c):.1f}", flush=True)
        except Exception as e:
            print(f"UPG\t{up}\tERR\t{e}", flush=True)
    # The FULL set of upgrade ids this client defines (id + library name if known),
    # so two game versions can be diffed to find ids present in one but not the other.
    for k in sorted(gd.upgrades):
        try:
            nm = UpgradeId(k).name
        except ValueError:
            nm = "?"
        print(f"UPGID\t{k}\t{nm}", flush=True)
    # Which of OUR named RESEARCH upgrades are actually researchable in THIS client:
    # ABSENT = id not in game data (e.g. TempestGroundAttack in 4.10); NO_ABILITY = id
    # exists but has no research ability (e.g. VoidRaySpeed in 4.10 — can't be started).
    from catalog import RESEARCH
    for name, up in RESEARCH.items():
        v = up.value
        if v not in gd.upgrades:
            status = "ABSENT"
        elif gd.upgrades[v].research_ability is None:
            status = "NO_ABILITY"
        else:
            status = "ok"
        print(f"RESEARCHABLE\t{name}\t{status}", flush=True)


class _DumpBot(BotAI):
    """Exists only to hold a live `game_data`, dump it, and concede."""

    async def on_start(self) -> None:
        dump(self)

    async def on_step(self, iteration: int) -> None:
        await self.client.leave()

    async def on_end(self, result: Result) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", default=None, help="see run.py (wine | linux)")
    ap.add_argument("--map", default=None)
    ap.add_argument("--fullscreen", action="store_true")
    args = ap.parse_args()

    from loguru import logger as _loguru
    _loguru.remove()
    _loguru.add(sys.stderr, level="WARNING")

    map_name = args.map or ("CatalystLE" if run._TARGET == "linux" else "LockdownLE")
    run_game(
        run.resolve_map(map_name),
        [Bot(Race.Protoss, _DumpBot(), name="Dump", fullscreen=args.fullscreen),
         Bot(Race.Terran, run.PassiveBot(), name="Passive")],
        realtime=False,
        random_seed=run.GAME_SEED,
    )


if __name__ == "__main__":
    main()
