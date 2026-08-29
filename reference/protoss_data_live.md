# Protoss build data (for analytic build-order planning)

Costs and **base** build times are dumped from the running game client — authoritative
for the installed patch (currently **5.0.16b**). Times are in **game-seconds** (the
in-game clock the bot's `self.time` uses): `build_frames / 22.4`.

## ⚠ Runtime modifiers NOT captured by the static `build_time` above

- **Warp Gate research halves gateway training.** Once `WARPGATERESEARCH` completes,
  **Gateway** unit train time is reduced **−50%** (5.0.16b; was −40%). This applies to
  units trained from a (non-morphed) **Gateway** — see the "post-WG" column below.
  Research itself: **50 / 50 gas, 100s** (`--chrono` to speed it).
- **Warp-in ≠ gateway train.** A Warp Gate warps a unit on a per-unit **cooldown**
  (below). With the 5.0.16b −50% gateway bonus that cooldown is now LONGER than the
  post-research gateway build (Zealot: **13.6s gateway vs 22s warp**) — so **gateways
  out-produce warp-ins on throughput**; warp-ins are for *positioning* (unit appears
  instantly at a powered pylon, no walk). ⚠ Liquipedia's Warp_Gate page still claims warp
  gates are ~7s *faster* — stale (based on old gateway train times).

  | gateway unit | warp cooldown (s) | post-WG gateway build (s) |
  |---|---|---|
  | Zealot / Stalker | 22 | 13.6 |
  | Sentry | 22 | 11.6 |
  | Adept | 22 | 16.3 |
  | High Templar / Dark Templar | 35 | 20.0 |

  (Warp cooldowns: Liquipedia LotV; 5.0.16b didn't change them. A freshly morphed Warp
  Gate can warp immediately — the cooldown starts on the first warp.)
- **Gateway↔Warp Gate transformation: 4s** (5.0.16b, was 7s/5s) and **costs 25 minerals +
  25 gas** per transform. (The static unit-type diff shows 0/0; this is an ability cost.)

## Structures

| structure | min | gas | build s | supply |
|---|---|---|---|---|
| Nexus | 400 | 0 | 71.4 | +15 |
| Pylon | 100 | 0 | 17.9 | +8 |
| Assimilator | 75 | 0 | 21.4 | — |
| Gateway | 150 | 0 | 46.4 | — |
| Warp Gate | 25 (morph) | 25 | 4.0 | — |
| Cybernetics Core | 150 | 0 | 35.7 | — |
| Forge | 150 | 0 | 32.1 | — |
| Twilight Council | 150 | 100 | 35.7 | — |
| Robotics Facility | 150 | 100 | 46.4 | — |
| Stargate | 150 | 150 | 42.9 | — |
| Templar Archives | 150 | 200 | 35.7 | — |
| Dark Shrine | 150 | 150 | 71.4 | — |
| Robotics Bay | 150 | 150 | 46.4 | — |
| Fleet Beacon | 300 | 200 | 42.9 | — |
| Photon Cannon | 150 | 0 | 28.6 | — |
| Shield Battery | 100 | 0 | 28.6 | — |

## Units

`build s` is the **base** time. `post-WG` = gateway-trained time after Warp Gate research
(base × 0.5); it applies to Gateway units only (Zealot…Dark Templar).

| unit | min | gas | supply | build s | post-WG (gateway) | producer |
|---|---|---|---|---|---|---|
| Probe | 50 | 0 | 1 | 12.1 | — | Nexus |
| Zealot | 100 | 0 | 2 | 27.1 | 13.6 | Gateway |
| Stalker | 125 | 50 | 2 | 27.1 | 13.6 | Gateway |
| Sentry | 50 | 100 | 2 | 23.3 | 11.6 | Gateway |
| Adept | 100 | 25 | 2 | 32.5 | 16.3 | Gateway |
| High Templar | 50 | 150 | 2 | 40.0 | 20.0 | Gateway |
| Dark Templar | 125 | 125 | 2 | 40.0 | 20.0 | Gateway |
| Archon | 175 | 275 | 4 | 0.0 (morph) | — | 2×HT/DT |
| Immortal | 250 | 100 | 4 | 39.3 | — | Robo |
| Observer | 25 | 75 | 1 | 17.9 | — | Robo |
| Warp Prism | 250 | 0 | 2 | 35.7 | — | Robo |
| Colossus | 300 | 200 | 6 | 53.6 | — | Robo |
| Disruptor | 150 | 150 | 4 | 35.7 | — | Robo |
| Phoenix | 150 | 100 | 2 | 25.0 | — | Stargate |
| Oracle | 150 | 150 | 3 | 37.1 | — | Stargate |
| Void Ray | 250 | 150 | 4 | 43.0 | — | Stargate |
| Tempest | 250 | 175 | 4 | 42.9 | — | Stargate |
| Carrier | 350 | 250 | 6 | 64.3 | — | Stargate |
| Mothership | 400 | 400 | 8 | 89.3 | — | Nexus |

## Upgrades (min / gas / research s)

| upgrade | min | gas | research s |
|---|---|---|---|
| Warp Gate | 50 | 50 | 100.0 |
| Blink | 150 | 150 | 121.4 |
| Charge | 100 | 100 | 100.0 |
| Psionic Storm | 200 | 200 | 78.6 |
| Ground Weapons L1 | 100 | 100 | 121.4 |
| Ground Armor L1 | 100 | 100 | 121.4 |
| Shields L1 | 150 | 150 | 121.4 |
| Air Weapons L1 | 100 | 100 | 128.6 |
| Graviatic Drive (prism speed) | 100 | 100 | 57.1 |
| Extended Thermal Lance (colossus range) | 150 | 150 | 100.0 |

## Open items / to verify
- ~~**Mining rates**~~ — done: measured in-engine, see `economic_data.md`
  (`scripts/economy_measure.py` regenerates them). Headline: **0.95 min/s per worker** up to 16
  on a base, **0.45** for workers 17–24, **2.73 gas/s** for a saturated geyser.
