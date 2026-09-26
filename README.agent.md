# Starcraft 2

Starcraft 2 is a real time strategy game. A player starts with a single town hall building (e.g. a nexus) and some workers. She must then manage an economy, build tech and production buildings, and produce an army. The end goal is mostly to defeat an opponents' army. There are 3 different factions (races) in the game. We will focus on the Protoss.

There are two resources that workers can be told to collect: minerals and vespene gas.

- Minerals: Minerals are collected from mineral patches, and workers can begin collecting from these immediately. Workers mine fairly efficiently from these up to 2 workers per patch, after which there are severe diminishing returns.
- Gas: Gas is mined from gas geysers, and an assimilator must be constructed on the geyser before it can be productive. Workers generally mine efficiently from assimilators with up to 3 workers per geyser. The last worker mines a bit less efficiently than the first two, and there's no benefit to a fourth worker on a single geyser. Generally high tech items cost gas.

There are many trade-offs to consider. E.g.:

- You may set your workers to mine either gas or minerals.
- Certain abilities must be allocated, e.g. chrono boost, which speeds up a single production or upgrade building.
- You can forego army units early to build more workers. This may grow your economy, allowing for more army units later.
- Similarly, you can invest in upgrades for units. This may mean less money to spend on army units now but a stronger army later.

All actions in the game take some amount of time. Workers take time to mine resources. They also take time to walk places in order to perform various tasks. Buildings take time to build, units take time to train, upgrades take time to complete, etc. And there are various other mechanics that must be accounted for, e.g. resource fields eventually run out.

Additionally, there are many dependencies are baked into the rules of the game and must be accounted for. Units are always produced from buildings, and the buildings must first be built. There is a "tech tree", such that certain buildings are pre-requisites for other buildings, units, or upgrades. Also, both workers and army units require some amount of "supply" (which is provided by pylons and nexuses) to be available before they can be produced, so you must construct supply-providing buildings in advance to avoid getting supply-capped.

# Build orders

A step-by-step sequence of building construction, upgrades, unit production, etc. is a "build order". Generally the build order is constructed to grow the economy and army as efficiently as possible. The end goal is to win the game, which usually means defeating an opponent's army. So, a common build order might lead up to a planned attack at some time. In that case, a build order will often be designed to have as large an army as possible (along with certain upgrades and auxillary units) at that particular time. A build order might also have some other constraints along the way, e.g. some number of army units might be necessary to defend oneself early on before the final attack.

Designing a build order is something that human players will often do offline, outside of a competitive game. This gives time to experiment with different approaches, consider trade-offs, etc. Build order design is a continuous time planning problem, under multiple constraints and trade-offs, that (some) humans find challenging and interesting in its own right. Occasionally, new build orders are discovered, with routes to their objectives that are strictly more efficient than those known previously.

# Build order executor

The build order executor (entrypoint `run.py`) takes a build order specified as a yaml file, it executes the build against the real game engine in a game with no active opponent, and it prints the results. This allows an agent or human to specify a build as a simple text file and then see how well it works.

The schema for the yaml files amounts to a domain specific language for build orders. There is some leeway as far as how high or low level this DSL should be. Generally, we tried to make the things that need to be specified the same things that a human of intermediate skill would need to learn or specify when describing a new build. The bot is generally handling under the covers only those things that a human would take for granted.

The build order config is a list of steps. Each step has:

- an `at` field which specifies a triggering criterion (exactly one of supply, time, minerals, vespene, or count). Count is (e.g., {Pylon: 2}, {CyberneticsCore: 1}). Write `at: asap` (a bare word, not a map) for a step that should fire the moment the previous one completes, with no wait of its own.
- and a `do` field which specifies the action to takke in the build

Various other fields are specific to the type of action. Values, including building and unit names, are case-insensitive, but field names are not. Several steps take a `count`. The step isn't complete until all of them have been made, so the build order waits for the whole group before moving on.

Steps are executed one at a time, i.e. a step's trigger is checked and its action taken only after the previous step is complete. (There is a single exception to this, the prewalk system for builders, described below, which involves some limit lookahead past the current step.) For each step, we wait until the triggering condition becomes true, and then execute it.

So, if one step's triggering condition never becomes true, we never move past the step, and the whole build stalls. Deadlocks like this can definitely happen in pratice -- e.g. suppose a step's trigger criterion is for a unit to appear, but we are currently supply-capped, so that no units can be produced. In this case, the whole build will stall.

The rest of this document is the complete specification of the syntax — the schema itself
is not readable from here, so if something isn't described below, it isn't supported. A
build order `example.yaml` might begin:

```yaml
- {at: {supply: 14},  do: build,  what: Pylon,   prewalk: {minerals: 75}}
- {at: {count: {Pylon: 1}}, do: chrono, target: Nexus}
- {at: {supply: 16},  do: build,  what: Gateway, prewalk: {minerals: 100}}
- {at: {supply: 16},  do: send_probe, where: enemy_main, who: scout}
- {at: {supply: 17},  do: build,  what: Assimilator}
```

and it can be run with e.g.:

```
python run.py --build build.yaml --time-limit 600
```

Those two flags are the entire interface. `--build` must be a file in this directory;
`--time-limit N` ends the game after N game-seconds (default 300, maximum 1800). The map
is fixed — CatalystLE, which we use pervasively — and cannot be changed, so every run is
measured on the same terrain.

Runs are recorded, and the number of runs available to you may be capped.

## Step Completion

A step completes (and prints `[done]`) once its action has been issued, or (for actions where issuing a command is not the same as it happening) once the executor can confirm it took. Either way this is distinct from when the resulting unit, structure, or upgrade finishes and becomes usable (which prints `[ready]`).

The **Confirmed?** column is the one to read when writing a build. A *confirmed* step stalls the line until game state agrees, so anything after it can assume the thing exists. An *issued* step completes on the same frame it fires, so the next step's trigger must not assume the effect has landed.

| Step Action (`do:`) | Confirmed? | When Step Completes (`[done]`) |
|---|---|---|
| `build` | confirmed | The structure appears in game state (construction started, not finished) and the probe is released back to mining. With `count: N`, after all N |
| `warp` | confirmed | The warping units are on the map (warp animation under way), counted by tag. With `count: N`, after all N |
| `morph` | confirmed | Every source committed is consumed or visibly carrying the morph ability. Covers `warpgate`/`gateway` (1:1) and `archon` (2 HT/DT combined) |
| `research` | confirmed | The upgrade reads as pending on a later frame. Completes instantly, spending nothing, if it's already researching or done |
| `train` | issued | All `count` units queued into production buildings. Note that units will be queued-but-not-actively-training in case we are supply blocked |
| `chrono` | issued | 50 energy spent, boost issued to the chosen structure |
| `hallucinate` | issued | 75 energy spent and the ability issued by a Sentry |
| `send_probe` | issued | Move order given. Stalls first if no probe is free to send |
| `return_probe` | issued | Gather order given at the populating base. Completes even if the probe died or there's no field to send it to |
| `set_rally_point` | issued | Rally point recorded; the rally commands go out to producers next frame |
| `rally_and_transfer_probes` | issued | Populating base set, Nexuses rallied, excess probes reassigned. Completes having done only the first if that base has no mineral patches |
| `set_gas_probes` | issued | The gas target is recorded so that the economy manager reallocates probes over following frames |
| `cut_probes` / `resume_probes` | issued | Worker production toggled |
| `wait` | issued | Trigger criterion met; does nothing else |

## Systems

### Locations

There are named locations. Our bases, in order, are called:

- main
- natural
- third
- fourth
- fifth
- sixth

and some additional strategic locations are:

- enemy_main
- enemy_natural
- proxy: a location out on the map, relatively near the opponent's starting location
- main_ramp: the entrypoint to our own starting base location

These locations can be used with the `where` parameter in various steps, e.g. set_rally_point.

### Economy 

By default, probes are produced continuously from all ready nexuses. You can pause and resume that with the `cut_probes` and `resume_probes` steps, e.g.:

```
- {at: {time: 10},  do: cut_probes}
- {at: {time: 20},  do: resume_probes}
```

Note: probe production is the highest-priority use of money and is performed before any other tasks, including e.g. performing build steps. Therefore, if you want to cut probe production at a particular number of probes, a `cut_probes` step with trigger `{count: {Probe: ...}}` is not the way to do it! The executor will blow right past that count before checking the trigger condition.

More probes mine more minerals, allowing production of more probes, etc. Exponential growth of the economy is important, so generally we won't want to cut probes early unless there's a pretty good reason for it. It's more common to cut later to divert resources to army production during the build-up to an attack.

Nexuses are always constructed adjacent at a base adjacent to resources. A base will always have 8 mineral patches and 2 gas geysers. When a nexus is first produced, probes will be rallied to the adjacent minerals, and that workers will automatically begin collecting these minerals.

The `set_gas_probes` step allows specific number of probes to be allocated to mine gas. When that number changes, probes are pulled to/from minerals to mine gas. Gas probes will be constantly balanced between available assimilators. E.g.:

```
- {at: {supply: 18},  do: set_gas_probes, count: 6}  # fully saturate two assimilators
```

The `rally_and_transfer_probes` command takes a particular base location as input and does two things:

- rallies all nexuses to that base's minerals
- transfers all other bases mineral workers > optimal saturation to the target base

So, one plausible approach to expansion would be to constantly produce workers and then build new nexuses so that they'll finish when the previously-constructed nexus becomes fully saturated. Then, just rally_and_transfer_probes to the newst nexus.

There's a notion of the currently populating base. This starts out as the main but changes to the indicated base upon rally_and_transfer_probes. Any idle workers (not explicitly allocated for some other purpose) are sent to mine minerals at the currently populating base. Also, workers pulled to/from gas come from this base.

### Building

All protoss buildings must be placed near a pylon, except for nexuses and pylons themselves. Building placement is largely automatic. By default:

- Nexuses are placed at the next available base
- Assimilators are placed on a gas near any ready nexus
- Pylons are placed preferentially near a nexus that doesn't have one nearby, and otherwise spread around the main base
- All other buildings are packed tightly around pylons closest to the main, with some allowance to ensure we dont block exits of production buildings

Generally these heuristics should be sufficient to exercise a build order. An explicit `where` may be provided to override, however. This may be useful e.g. for proxy builds.

To build a building, a nearby worker is pulled from mineral mining to the location where we want to build. The probe has to walk to the location, but it doesn't have to wait for completion. After building all buildings for the step, it returns to the same minerals from whence it came.

By default, a worker is pulled once we're on the build step *and* we have sufficient resources to construct a building. In this case, the building will definitely start later than necessary, by however much time it takes the worker to walk to the building location.

To be as efficient as possible, a build will usually want to perform actions as soon as possible. To execute a tighter build, you can use the `prewalk` field on build steps, to start the probe walking to the building location a bit earlier. This step takes a `trigger` field whose value is of the same type as `at`. This trigger criterion is then used to decide when to send the worker. A common pattern will be to pre-walk a probe a little while before resources for the building are available.

If a build step specifies a `count`, the probe is pulled by default when we have resources to build a single building. Regardless, the prewalk condition is only checked once per build step, and the same pulled probe is used to build all buildings in the step.

A couple other important notes about prewalk:

- This is the only case where a step can have any effect before previous steps are complete.
- In general, at any particular point in time, we look ahead as far as the next resource-spending step. If it's a build step, we check the prewalk criterion. And if the criterion is true, we walk the probe.
- Effectively, this means that we partition steps into minimal groups ending with a resource-spending step. If that last step is a build step with a prewalk, then we check the prewalk criterion whenever any steps in the partition are active. And e.g., a prewalk criterion of `asap` will send a probe when the first step in the partition becomes active.
- A consequence of the partitioning approach is that only one prewalking probe can be active at a time. I.e. we'll have at most one builder probe off of mining due to this system.

A final detail about probe walking: in case the build step has a `supply` trigger which is higher than our current supply, we need to build probes (at a cost of 50 minerals apiece) before we can build the building. We deduct these probe costs from our current mineral count before checking the probe-sending criterion, whether it's explicit via `prewalk: {minerals: ...}` or implicit via the default building cost check. A `count: {Probe: N}` trigger reserves the same way as well.

### Named workers

A `send_probe` step can be used to send a worker to a particular `where` on the map:

```
- {at: {supply: 15},  do: send_probe, where: proxy, who: scout}
```

This removes the worker from the rest of the worker management system so that you can otherwise use it without interference from the automation. The name you give it in `who` is how you refer to that same probe later, e.g. to use it to build a particular building, rather than pulling a probe from mining: 

```
- {at: {supply: 18},  do: build, what: Pylon, where: proxy, who: scout}  # scout builds the proxy Pylon
```

A name is select-or-create, and which one you get depends only on whether that name is already in use. A `send_probe` with a new name pulls a fresh probe off the line and binds it to that name; a `send_probe` with a name that's already in use moves the probe already bound to it.

Once it's done its job, we can release the probe back to mining via `return_probe`:

```
- {at: {supply: 18},  do: return_probe, who: scout}
```

### Chrono boost

The `chrono` step spends 50 Nexus energy to accelerate production or research on a target structure type:

```yaml
- {at: {count: {CyberneticsCore: 1}}, do: chrono, target: CyberneticsCore}
```

A chrono step will wait until a Nexus has 50 energy and at least one structure of the target type exists. When multiple structures of the same type exist, it automatically targets unboosted structures first and chooses the one with the most production or research time remaining. If none are actively producing, it will boost an idle structure as a fallback so the build order queue doesn't stall.

## Observability

The bot's output contains semi-structured data to help understand what happened during the game. Every line of output starts with a tag:

| Tag | Meaning |
|---|---|
| `[run]` | lifecycle: build loaded, output legend, build complete, conceding, replay/summary paths |
| `[done]` | a build step completed, with clock and step progress, e.g. `[done] 1:54  step=3/20  build what=Nexus` |
| `[ready]` | a unit, structure, or upgrade became ready, with a running per-type count, e.g. `[ready] 4:19  Gateway #3`. Probes are skipped. |
| `[builder]` | the lifecycle of the one probe pulled off the mineral line for a build: `assigned` when it's pulled and starts walking, then `building` when the order goes in. `dist=` is how far it still is from the spot — near-zero on the `building` line means the walk was overlapped with saving up, a big number means it was on the critical path. A `count` step logs `assigned` and `building` for each structure; after the first, `assigned` says `held`, meaning it reused the probe already building for this step. |
| `[status]` | every 10 game-seconds: current step with age and status, then economy snapshot |
| `[end]` | final state + army report |

All of the above are unconditional — there is no verbosity flag, so a run never has to be
repeated just to diagnose it.

The final `[summary]` line is a machine-readable JSON blob (steps done, `completions`
mapping each type to its list of completion times, `upgrades_at`, unit/upgrade census,
where it stalled) — grep `^[summary] ` and `json.loads` the rest.
