"""Ad-hoc: what IS ability id N, according to the running client?

`GameData` drops any ability whose id isn't in python-sc2's `AbilityId` enum
(game_data.py:26-29), so an id the library predates is invisible through the normal
API — you can only see it in the raw ResponseData. This asks the client directly.

    python -m scripts.ability_lookup --target wine 4135
"""

from __future__ import annotations

import argparse
import sys

import run  # noqa: F401  (sets SC2PF/SC2PATH before sc2 is imported; must come first)

from s2clientprotocol import sc2api_pb2 as sc_pb
from sc2.bot_ai import BotAI
from sc2.data import Race, Result
from sc2.ids.ability_id import AbilityId
from sc2.main import run_game
from sc2.player import Bot

WANTED: list[int] = []


class Lookup(BotAI):
    async def on_start(self) -> None:
        result = await self._client._execute(
            data=sc_pb.RequestData(ability_id=True, unit_type_id=True,
                                   upgrade_id=True, buff_id=True, effect_id=True)
        )
        raw = {a.ability_id: a for a in result.data.abilities}
        known = {a.value for a in AbilityId if a.value != 0}
        print(f"ABIL total={len(raw)} known_to_python_sc2={len(raw.keys() & known)} "
              f"unknown={len(raw.keys() - known)}", flush=True)
        for want in WANTED:
            a = raw.get(want)
            if a is None:
                print(f"ABIL {want}: NOT PRESENT in this client's data", flush=True)
                continue
            print(f"ABIL {want}: link={a.link_name!r} idx={a.link_index} "
                  f"button={a.button_name!r} friendly={a.friendly_name!r} "
                  f"remaps_to={a.remaps_to_ability_id} available={a.available} "
                  f"target={a.target} in_python_sc2={want in known}", flush=True)
            if a.remaps_to_ability_id:
                r = raw.get(a.remaps_to_ability_id)
                print(f"ABIL   remap {a.remaps_to_ability_id}: link={r.link_name!r} "
                      f"button={r.button_name!r} in_python_sc2="
                      f"{a.remaps_to_ability_id in known}", flush=True)
        await self._client.leave()

    async def on_step(self, iteration: int) -> None:
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="+", type=int, help="ability ids to look up")
    ap.add_argument("--target", default=None, help="see run.py (wine | linux)")
    ap.add_argument("--map", default=None)
    args = ap.parse_args()
    WANTED.extend(args.ids)

    from sc2 import maps
    game_map = maps.get(args.map or ("CatalystLE" if run._TARGET == "linux" else "LockdownLE"))
    run_game(game_map, [Bot(Race.Protoss, Lookup())], realtime=False)


if __name__ == "__main__":
    sys.exit(main() or 0)
