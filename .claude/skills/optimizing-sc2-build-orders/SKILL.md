---
name: optimizing-sc2-build-orders
description: Write, optimize, and debug YAML build orders for the build-order-executor python-sc2 bot (~/projects/build-order-executor, builds/*.yaml). Use when tuning a build toward a timing/quantity goal (e.g. "most zealots at X by 5:30"), when a build stalls / underperforms / deadlocks, or when reasoning about the executor's step semantics, placement, or SC2 production mechanics.
---

# Optimizing SC2 Build Orders (build-order-executor)

The bot executes a YAML build (`builds/*.yaml`) validated by `schema.py` and run by
`bot.py`. Steps fire **in list order, strictly**: each step holds the line until it
completes, so a step that can't fire blocks everything after it. Optimizing a build =
sequencing explicit steps against that constraint, verified against the real game.

## Rule 0: MEASURE, never infer or fabricate

The single most important lesson. Do **not** deduce unit counts from supply arithmetic
or guess what happened — run the real game and read the instrumentation (`[status]` and
`[ready]` are always on; `--debug` adds `[step*]`/`[prewalk]` build-issue detail).
`bot.py` prints a `[status]` line every 10 game-seconds, and a final census at `[end]`:

```
[status]   4:32  step=13/34 (38s)  build what=Gateway -- waiting for count Pylon=1 (have 0)  |  supply=45/54 workers=28 min=350 gas=120 bases[b1=16/16 b2=12/16] chrono=2
```

- The `[status]` line prints every 10 game-seconds: the current step with **how long it
  has been current** and what it is waiting on, then an economy snapshot (supply,
  workers, `min/gas`, per-base saturation `bases[b1=16/16 b2=..]`, chrono available).
- A growing step age is a stall — there is no separate `[stall]` tag and nothing is
  hidden behind a threshold. It is build-agnostic.

If a metric matters (e.g. "at the proxy"), add/keep a direct measurement for it. Claims
like "they died in combat" or "≈19 alive" without a log line to back them are how you
mislead yourself and the user. Run to a file and grep; don't trust piped `tail` (it
buffers until the process exits).

```bash
cd ~/projects/build-order-executor
.venv/bin/python run.py --build builds/<x>.yaml --fullscreen --time-limit 330 2>&1 \
  | grep -E "\[end\]|\[ready\]|\[done\]|\[status\]"
```
`--fullscreen` is required (windowed is unusably slow under Wine). `--time-limit N` ends
at N game-seconds; add a trailing step that never fires (or enough production steps) so
the build doesn't `concede` before the deadline. **Runs are ~deterministic** — one run
per change is enough; don't burn time re-running for "stability".

**Verify tight deadlines with `[ready]`, not `[status]`.** `[status]` prints every 10s —
too coarse to tell 4:12 from 4:18 against a 4:15 deadline. `[ready] M:SS <name>` is
logged the frame each unit/structure type or upgrade FIRST finishes; grep `\[ready\]`
and compare to the deadline. It's automatic and build-agnostic — every type you produce is
timestamped, nothing needs configuring. It is first-of-type only, so for "when did I reach
14 Zealots" you still need a direct measurement. The `[end]` line carries a final census,
but "done by end" ≠ "done by the deadline", so use `[ready]` for hard cutoffs.

## The executor's step model (and its traps)

- **`build`** completes when the structure *appears* (starts warping in), not when it
  finishes. Concurrent same-type builds work (drop a batch of gateways at once).
- **`train`** completes the instant it's issued to an *idle, ready* producer; it *holds*
  if none is idle or you can't afford it (cost **or supply**).
- **`warp`** and **`morph`** hold until confirmed (unit appears / N buildings converted).
- **Supply-block deadlock:** a held `train`/`warp` (no supply) blocks a `Pylon` queued
  *after* it → permanent stall. **Front-load supply**; keep pylons ahead of the units
  that need them. Symptom: queue stuck, `min` climbing to thousands, `next=train`.
- **A build that can't place stalls the whole queue.** `where: main`/`natural` give fast
  near-base placement; auto placement + a `prewalk:` trigger is also fast *when there's
  room*. If a base's powered ground fills up, placement returns None and the step holds
  forever — split builds across `main`/`natural`, or cap the count.
- **`morph` with no `count` holds forever if it can't afford/convert all of them.** Use
  `count: N` you can actually afford and that will actually go idle in time.
- **Don't leave the first producer idle:** trigger production on `count: {Core: 1}` /
  `{Gateway: 1}` — not `{Gateway: 3}` — or the opener's gateway sits idle for a minute.
- **Overlap long walks:** `send_probe where: proxy` (or a prewalk) *early*, before the
  home builds, so a cross-map walk finishes while other steps run.
- **`count: {X: N}` triggers count READY units**, not started ones. A structure that
  *starts* at 4:00 isn't ready until +build_time (~4:40 for a Gateway). Gating the back
  half on `{Gateway: 3}` couples everything to that late ready-time and jams it — gate
  supply/research on `time:` or a low count instead.
- **`can_afford` includes GAS.** A "mineral" building (Twilight 150/**100**) silently
  stalls with 800 minerals banked if gas < 100. When a build won't fire and `min` is
  huge, check `gas`. Don't over-pull gas→minerals early: it starves the tech that needs
  gas (Void Ray, Twilight, Charge, morphs). Gas is usually the true limiter.
- **A slow far build poisons same-type home builds.** `already_pending()` is type-wide,
  so while a proxy Pylon crawls across the map (walk + build) every *home* Pylon behind
  it holds (both the prewalk reservation and `do_build` gate on it). Order slow/far
  builds of a type **after** all the near ones of that type.
- **A prewalk/reserved builder that must walk far stalls the queue for the walk.** If a
  build's placement is at an under-saturated base but the free probes are elsewhere,
  the reserved probe walks ~24s and the step holds the whole time. Place builds where
  the free probes actually are (auto near the main), or saturate that base first.
- **`chrono` HOLDS the line when it can't cast** (no 50-energy Nexus, or nothing of that
  type is producing/researching) — a mid-sequence chrono froze production for ~17s
  waiting on energy, and one placed after a research finished blocked the steps behind
  it. There is no best-effort/optional chrono: a build order is precise, so place each
  chrono where it will *actually* have energy and something to boost. Don't put a chrono
  behind steps that might reach it after its target has gone idle.
- **Sustaining a long research needs SPREAD chronos.** The buff wears off (~20s), so N
  chronos on the same frame ≈ 1 boost. To pull Charge (100s) under a deadline, space
  chronos ~15-20s apart on triggers (`time:` / `count:`) that guarantee the structure is
  still researching when the line reaches each one — so none of them stalls waiting.
- **Two proxy pylons for a warp finish.** One pylon's powered tiles fill with warp-ins
  and then warp placement returns None (warps stall, minerals bank) — a second proxy
  pylon (same scout, back-to-back) ~doubles the space so the whole window lands.

## Grounded SC2 mechanics (patch 5.0.16b — see `reference/protoss_data.md`)

All costs/base-build-times live in `reference/protoss_data.md` (dumped from the game
client; regenerate with `run.py --dump-data`). Don't quote numbers from memory. The
key things that table's *static* column can't show are the **runtime modifiers**:

- **Warp Gate research reduces GATEWAY unit train time by EXACTLY 50%** once complete
  (5.0.16b patch notes; was 40%). e.g. Gateway Zealot 27.1s → **13.6s**. Research =
  50/50 gas, 100s. Worth it even if you keep training from gateways. The static
  `build_time` in game data is the *pre-research base* and does NOT reflect this.
- **Warp-in cooldown is longer than a post-research gateway build**: gateways win on raw
  throughput; **warp-ins win on positioning** (unit appears instantly at a pylon).
- **Morph Gateway→Warp Gate: 4s, 25 minerals + 25 gas each.**
- **If the user states a game mechanic, it is authoritative** — do NOT "correct" it from a
  static data table or your own inference, and READ any patch notes / links they give you.
- **Walk time** home→far proxy ≈ 30s. A gateway unit must *finish* by ~5:00 (start
  ~4:45) to reach the proxy by 5:30. Units that can't arrive by the deadline are wasted.
- **Morph fewer, sooner:** `morph count:4` finishes ~4:50 (a long warp window);
  `count:5` waits for the slowest gateway to idle (~5:20) → no warp time. Fewer = better.
- **Chrono only helps when production is the bottleneck.** If you're mineral-capped
  (gateways idle waiting for money), chrono does nothing — build *fewer* producers and
  spend the minerals on units instead.

## Optimization workflow

1. **State the metric precisely** and measure it directly (a `[ready]` timestamp, a
   census count from `[end]`/`[summary]`, not an inference).
2. **Find the binding constraint each phase** from the heartbeat:
   - `min` near 0 → **mineral-limited** (fewer/cheaper structures; more income).
   - `min` banking to hundreds → **producer-limited** (add producers/chrono).
   - `sup_left` 0 with `next=train` stuck → **supply-blocked** (pylons ahead).
   - queue `next=` frozen for many hb's → a step is **deadlocked** (placement/afford).
3. **Change one thing, re-run, re-measure.** Keep the win, revert the loss.
4. **Don't produce what can't reach the goal by the deadline.** Find something else for
   that time/money (e.g. switch from walking gateway units to instant warp-ins).
5. **Cut waste:** don't over-build pylons (each wasted pylon = a lost unit when
   mineral-bound); pull gas to minerals the moment you have enough for your needs.

## Worked example: PvZ proxy-zealot all-in (`builds/pvz_proxy_zealot_allin.yaml`)

Goal: most zealots **at** a proxy pylon by 5:30. Went 5 → ~15 at the proxy by, in order:
fixing placement (off the mineral line); researching Warp Gate; cutting to 4 gateways
(mineral-capped); training gateway zealots only while they can still *walk* in; then
**morphing 4 gates and warping the entire 4:50–5:30 window straight onto the proxy** so
late units arrive instantly instead of walking. ~15 is near the 2-base ceiling for a
3:00 tech start (≈7 walk in + ≈8 warped).

## Worked example: proxy-zealot + Void Ray + Charge (`builds/pvz_proxy_zealot_voidray.yaml`)

Same opener, but with **hard tech deadlines** on top of the proxy warp finish: 2 Adepts +
a Void Ray done by 4:15, Charge by 5:45, then max Zealots at the proxy. Result: Void Ray
~4:08, Charge ~5:36, ~5 at the proxy. The whole build is a fight over a 2-base economy the
tech tax overloads — key moves:
- **The two deadlines directly conflict** (Void Ray 250/150 by 4:15 vs Twilight→Charge by
  5:45 both want the ~3:20–3:50 window). Resolve by *priority*: Void Ray FIRST (tightest)
  gets the gas/minerals; Twilight right after; Charge starts ~4:26 and is dragged toward
  5:45 with **spread chronos** — precisely placed so each fires while Twilight is still
  researching (it lands ~5:47 on front-loaded chronos alone; closing that last ~2s wants
  a chrono guaranteed to hit the Twilight's second half without stalling the warps).
- **Gas is the limiter**, not minerals (minerals banked to 800+ while Twilight stalled on
  its 100 gas). Keep 6 gas workers through the tech; only pull to minerals ~4:35 for the
  100-min warp-ins.
- **Decouple the finish from Gateway-ready** (they aren't ready till ~4:50): gate supply
  pylons on `time:`, morph on `time:`, and use **two** proxy pylons so warps don't stall.
- Fewer Gateways (2 new) ready *sooner* beats more Gateways ready late — the warp *window*
  matters more than gate count once tech eats the early economy.

## Bot-code fixes/features that came out of these builds (already applied)

- `do_build`: baseline `already_pending()` per step so **many same-type structures build
  concurrently** (python-sc2 counts every under-construction building type-wide).
- `placement.building`: anchor **away from the mineral line**, pack tight.
- `observe.py`: the `[status]` line (economy + current step with age/reason) and
  `_note_completions()` (`[ready] M:SS`) observability — keep them; they're how you
  measure. Both are build-agnostic: nothing about a particular build is hard-coded.

If a build behaves impossibly, suspect a bot bug before contorting the YAML — but confirm
it by reading the game data / instrumentation first.
