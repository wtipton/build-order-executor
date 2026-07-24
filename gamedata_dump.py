"""One-shot dump of authoritative unit/structure/upgrade data from the live game.

Costs and BASE build times come straight from the running SC2 client, so they're correct
for the installed patch (unlike Liquipedia, which lags balance changes). Build times are
in game-seconds (frames / 22.4). Runtime modifiers (e.g. the Warp Gate research gateway
train-time reduction) are NOT reflected in these static values — see reference/protoss_data.md.

Usage (needs a live game, hence run through the bot):
    DUMP_DATA=1 .venv/bin/python run.py --build builds/pvz_opening_8worker.yaml \
        --fullscreen --time-limit 5 2>&1 | grep -E "^DATA|^ABIL|^UPG"

bot.on_start calls dump(self) when $DUMP_DATA is set.
"""

from __future__ import annotations

from sc2.ids.ability_id import AbilityId
from sc2.ids.unit_typeid import UnitTypeId as U
from sc2.ids.upgrade_id import UpgradeId

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


def _secs(cost) -> float:
    return (cost.time or 0) / _FRAMES_PER_SEC


def dump(bot) -> None:
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
