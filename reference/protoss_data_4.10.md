# Protoss build data (for SC2 version 4.10 / Headless Docker)

Costs and base build times dumped directly from the running **4.10** headless client.
Times are in **game-seconds** (`build_frames / 22.4`).

Regenerate:
```bash
docker run --rm build-order-executor-headless \
  python run.py --dump-data --time-limit 5 2>&1 | grep -E "^DATA|^ABIL|^UPG"
```

## ⚠ Version 4.10 Mechanics & Differences from Modern Retail (5.0.16b)

- **Auto-morph on Warp Gate research**: In 4.10, finishing `WARPGATERESEARCH` **automatically morphs every completed Gateway into a Warp Gate**. (Modern retail requires manual morphing).
- **No Gateway train-time reduction**: In 4.10, Warp Gate research does **not** halve Gateway training time. Gateways train units at their standard base time.
- **Morph cost & time**: Manual Gateway↔Warp Gate morph is **0 minerals / 0 gas** and takes **7.1s** (vs 25m/25g and 4.0s in 5.0.16b).
- **Immortal cost**: **275 min / 100 gas** in 4.10 (vs 250/100 in 5.0.16b).
- **Warp Prism cost**: **200 min / 0 gas** in 4.10 (vs 250/0 in 5.0.16b).
- **Tempest supply**: **5 supply** in 4.10 (vs 4 in 5.0.16b).
- **Upgrades absent/inactive**:
  - `TempestGroundAttack` (*Tectonic Reinforcements*): Absent in 4.10.
  - `VoidRaySpeed` (*Flux Vanes*): Upgrade ID exists but has no research ability in 4.10.

---

## Structures

| structure | min | gas | build s | supply |
|---|---|---|---|---|
| Nexus | 400 | 0 | 71.4 | +15 |
| Pylon | 100 | 0 | 17.9 | +8 |
| Assimilator | 75 | 0 | 21.4 | — |
| Gateway | 150 | 0 | 46.4 | — |
| Warp Gate | 0 (morph) | 0 | 7.1 | — |
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

---

## Units

| unit | min | gas | supply | build s | producer |
|---|---|---|---|---|---|
| Probe | 50 | 0 | 1 | 12.1 | Nexus |
| Zealot | 100 | 0 | 2 | 27.1 | Gateway |
| Stalker | 125 | 50 | 2 | 30.0 | Gateway |
| Sentry | 50 | 100 | 2 | 26.4 | Gateway |
| Adept | 100 | 25 | 2 | 30.0 | Gateway |
| High Templar | 50 | 150 | 2 | 39.3 | Gateway |
| Dark Templar | 125 | 125 | 2 | 39.3 | Gateway |
| Archon | 175 | 275 | 4 | 0.0 (morph) | 2×HT/DT |
| Immortal | 275 | 100 | 4 | 39.3 | Robo |
| Observer | 25 | 75 | 1 | 21.4 | Robo |
| Warp Prism | 200 | 0 | 2 | 35.7 | Robo |
| Colossus | 300 | 200 | 6 | 53.6 | Robo |
| Disruptor | 150 | 150 | 3 | 35.7 | Robo |
| Phoenix | 150 | 100 | 2 | 25.0 | Stargate |
| Oracle | 150 | 150 | 3 | 37.1 | Stargate |
| Void Ray | 250 | 150 | 4 | 42.9 | Stargate |
| Tempest | 250 | 175 | 5 | 42.9 | Stargate |
| Carrier | 350 | 250 | 6 | 64.3 | Stargate |
| Mothership | 400 | 400 | 8 | 114.3 | Nexus |

---

## Upgrades (min / gas / research s)

| upgrade | min | gas | research s |
|---|---|---|---|
| Warp Gate | 50 | 50 | 100.0 |
| Blink | 150 | 150 | 121.4 |
| Charge | 100 | 100 | 100.0 |
| Psionic Storm | 200 | 200 | 78.6 |
| Ground Weapons L1 | 100 | 100 | 128.6 |
| Ground Armor L1 | 100 | 100 | 128.6 |
| Shields L1 | 150 | 150 | 128.6 |
| Air Weapons L1 | 100 | 100 | 128.6 |
| Gravitic Drive (prism speed) | 100 | 100 | 57.1 |
| Extended Thermal Lance (colossus range) | 150 | 150 | 100.0 |
