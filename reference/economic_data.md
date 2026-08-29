# StarCraft II Economic Reference Data (Protoss)

Mining rates, engine constants, and economic formulas for build-order planning.

Every number here was measured in a live game. They were measured in the 4.10 headless client
on the map CatalystLE. All times are in seconds and all rates are per-second.

---

## 1. Engine constants

| Metric | Value |
|---|---|
| **Tick rate** | 22.4 frames / game-second |
| **Starting workers** | **12** probes, 50 minerals |
| **Minerals per trip** | **5** |
| **Vespene per trip** | **4** |
| **Mineral harvest duration** | **37 frames = 1.65s** (time attached to the patch, distance-independent) |
| **Probe cost** | 50 minerals, 1 supply |
| **Probe build time** | 272 frames = **12.14s** |
| **Chrono'd probe** | 181 frames = **8.09s** |
| **Chrono Boost** | **×1.50 production speed** |
| **Probe movement speed** | **3.93 tiles / game-second** |

### Standard base layout (measured on CatalystLE main)

| | Count | Contents | Distance to Nexus |
|---|---|---|---|
| Mineral patches | **8** (4 × 1800 + 4 × 900) | **10,800** total | 6.19 – 7.63 tiles |
| Vespene geysers | **2** | **2,250** each (4,500) | 7.62 tiles |

The 4-large/4-small split is the LotV standard, so a base's mineral line is only worth
10,800 — not 8 × 1800. The half-patches run out first.

---

## 2. Mineral mining

Mineral patches are staggered in their distance from the nexus: close or far. CatalystLE's
main bases have 3 close and 5 far patches (though most maps have 4 of each). A worker on a
near patch mines ~13% faster than one on a far patch** (62 vs ~54 min/min).

The build order executor takes care to preferentially assign workers to near patches in the
main base at the beginning of the game.

Thus, at the beginning of the game, the first (2 * number of close patch) workers mine
slightly more efficiently than the rest, up to 16 workers. Then, there are much more severe
diminishing returns up to 24 workers. And then there is no gain from exceeding 24 workers on
minerals per base.

Specifically:

- **Workers 1 to 2×(close patches)**: ≈1.03 min/s each (62 min/min).
- **Up to 16 workers: ≈0.91 min/s each** (54 min/min). The close patches are full, so
  these are on the far row — the ~13% penalty above.
- **Workers 17–24: ≈0.43 min/s each** (26 min/min), i.e. **half again**. These are third
  probes on an already-busy patch, which is why the drop is so steep.
- **Beyond 24: nothing.** A base caps at **18.79 min/s** (1,127 min/min).

For whole-base planning with good saturation past the opening, just use 0.95 min/s: the
blend of the first two tiers.

---

## 3. Vespene gas

Per geyser (7.62 tiles from the Nexus):

| Workers | Geyser rate (gas/s) | (gas/min) | Marginal gain |
|---|---|---|---|
| 1 | 1.00 | 60 | — |
| 2 | 2.06 | 124 | +1.06 |
| **3** *(saturated)* | **2.73** | **164** | +0.67 |
| 4 | 2.73 | 164 | **+0.00** |

Notes:

- The third probe on a geyser is worse than the first two.
- 3 workers saturate a geyser. No advantage to 4+.
- Both geysers of a base, (6 workers total) give: 5.47 gas/s (328 gas/min).
  Exactly double one geyser, so the two don't interfere.
- A saturated geyser turns over one 4-gas trip every **~1.46s**.

---

## 4. Planning formulas

### Probe payback
- **Cost**: 50 minerals + 1 supply.
- **Below 16 workers on the base**: +0.95 min/s ⇒ pays for itself after **~53s of
  mining** (~65s from the moment you start training it).
- **As the 17th–24th on a base**: +0.43 min/s ⇒ **~116s of mining**. Over a 5-minute
  build a probe added here returns roughly half what an earlier one does.

### Chrono Boost on probes
Saves **4.02s** of training, so that probe (and every probe behind it in the queue)
starts mining 4.02s sooner ⇒ **~3.8 extra minerals** per boost, plus the compounding from
reaching saturation earlier.

### Builder-probe opportunity cost
A probe pulled off the line costs **0.95 min/s** for its whole round trip.
*Example*: a 20-second walk out to a proxy costs 20 × 0.95 = **~19 minerals**.
