# Dungeon simulation harness

Issue [#16](https://github.com/mule/ai-hack-poc/issues/16). A headless harness
that grows dungeons with **the real game generation and placement code**
(`GameState`, `DungeonWorld`, `GenerationCoordinator`, `RoomGenerator`,
`RulesBaseline`) and no player, then writes machine-readable datasets about
what happened. Use it to catch generation regressions and to compare providers
on identical dungeons.

```sh
make simulate                      # 5 runs x 100 steps, offline rules baseline
```

Nothing leaves your machine and nothing costs money unless you pass `--remote`
(see [Remote provider mode](#remote-provider-mode-costs-money)).

Output goes to `simulation-output/<UTC timestamp>/` (git-ignored). The exit code
is `0` when no invariant was violated, `1` when failures were detected (the
dataset is still written), `2` for a usage or output-directory error.

## What it does

Each **run** builds a fresh dynamic world from a seed (`base seed + run index`).
Each **step** the simulated player picks an unresolved exit, walks a checked
shortest path to it (which also marks every room on the path *explored*), and
the coordinator requests, validates, places and commits a room exactly as in
play. After each step the harness audits the world. It reuses the game's time
handling: offline runs use a virtual 100 ms tick (no sleeping, fully
reproducible); remote runs use the real clock.

A step is one frontier resolution (committed or sealed). `--steps` is a lower
bound: exits prefetched near the player can resolve in the same iteration and
are counted too.

## Modes

| Mode | Flags | What answers requests | Cost |
|------|-------|-----------------------|------|
| **Rules baseline** (default) | none | `RulesTransport`: the in-game rules baseline, wrapped as a valid director response (`rules-baseline` / `builtin-v1`) | none, offline |
| Offline fallback | `--transport offline` | the game's `OfflineTransport`: every request fails, so every room comes from the local fallback | none, offline |
| Faulty rules baseline | `--faults flaky` or `--faults hang` | rules baseline with deterministic injected failures (hangs, invalid JSON, failure envelopes, duplicate and mismatched responses, missing backlinks) | none, offline |
| **Remote** | `--remote ...` | a real director over HTTP (`POST /v1/generate`) | **may bill you** |

The rules baseline here is the *in-game GDScript* baseline, not the Python
director's `rules-baseline` provider: it needs no process or network, but the
two generate different plans.

### Flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--runs N` | 5 | independent dungeon runs (max 1000) |
| `--steps N` | 100 | generation steps per run (max 10000) |
| `--seed N` | 1 | base seed; run *i* uses seed `N+i`, run id `sim-<seed>` |
| `--policy P` | `random` | exit choice: `random` (seeded) or `nearest` |
| `--transport T` | `rules` | `rules` or `offline` |
| `--faults F` | `none` | `none`, `flaky`, `hang` (rules transport only) |
| `--audit-every N` | 1 | audit invariants every N steps (always at run end) |
| `--out DIR` | `simulation-output/<stamp>` via `make` | must not already hold a dataset |
| `--remote` | off | explicit opt-in to real provider calls |
| `--endpoint URL` | `$DUNGEON_DIRECTOR_URL`, else `http://127.0.0.1:8000` | director base URL (`http`/`https`, no credentials, no query) |
| `--provider ID` | `$DUNGEON_DIRECTOR_PROVIDER`, else the director's default | provider id sent as `?provider=` |
| `--model ID` | `$DUNGEON_DIRECTOR_MODEL`, else the provider's default | model id sent as `?model=` |
| `--max-requests N` | 300 | hard cap on requests actually sent |
| `--timeout SEC` | 5 | per-request timeout (0.1 to 300) |

Pass flags through make: `make simulate SIM_ARGS='--runs 20 --steps 200 --seed 7'`
(`SIM_OUT=dir` overrides the output directory). List the flags with
`godot --headless --path game -s res://simulation/run_simulation.gd -- --help`.

## Remote provider mode (costs money)

Real providers can bill per request or token and can rate-limit you. The harness
therefore never contacts one by accident:

- The default is offline. `--endpoint`, `--provider`, `--model`,
  `--max-requests` and `--timeout` **without `--remote` are a usage error**,
  not silently ignored.
- The `DUNGEON_DIRECTOR_*` variables the game reads only supply defaults
  **after** `--remote` is given; an ambient variable cannot enable remote calls.
- `--remote` prints a conspicuous warning to stderr before the first request,
  naming the endpoint, provider, model and request cap:

  ```
  !!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
  !!  COST WARNING: REMOTE PROVIDER MODE                                 !!
  ...
  ```

- A finite request budget always applies (`--max-requests`, default 300). Once
  it is spent no further request is sent: the rest are answered by the local
  fallback and recorded as `budget_exhausted`, later runs are not started, and
  the summary says `truncated: "request_budget_exhausted"`. The cap counts
  requests, not tokens or dollars; check your provider's pricing.
- `--remote` cannot be combined with `--faults` or `--transport`.

```sh
# Explicit, capped run against a local director configured for a paid provider
make simulate SIM_ARGS='--remote --endpoint http://127.0.0.1:8000 \
  --provider cloudflare-jev --model typesafe/jev --max-requests 100 --runs 2 --steps 40'
```

The director holds provider credentials; the harness never sees them. The
manifest records the endpoint, provider, model, budget and any cost the
responses report. Review remote datasets before sharing them: `fallback_detail`
holds short validation messages (at most 200 characters), not raw provider
output.

## Dataset format

Five UTF-8 files, all plain JSON, with sorted keys so identical runs produce
identical bytes. `*.jsonl` files hold one JSON object per line and end with a
newline (an empty file means no rows). The manifest is written last, so its
presence marks a complete dataset. JSON Schemas (draft 2020-12) live in
[`schemas/`](schemas/); the fixture in [`fixtures/sample/`](fixtures/sample/)
is a real, tiny dataset and is validated against them in CI.

The dataset format is versioned separately from the director contract:
`schema_version` (currently `1.0.0`) follows semver; new optional fields bump
the minor version, breaking changes the major.

### `manifest.json` ([schema](schemas/manifest.schema.json))

What was run: `schema_version`, `kind` (`dungeon-simulation`),
`contract_version`, optional `engine` (Godot version), `config` (runs, steps,
base_seed, policy, faults, audit_every, trigger_radius, max_in_flight,
timeout_sec, stall_after_msec, tick_msec), `provider` (`mode`
`rules-baseline`/`offline-fallback`/`remote`, `provider`, `model`, `endpoint`),
`remote` (`opt_in`, `request_budget`), `runs` (`run`, `seed`, `run_id`) and
`files` (the dataset's file names: `manifest`, `steps`, `rooms`, `failures`,
`summary`).

### `steps.jsonl` ([schema](schemas/step.schema.json))

One row per resolved frontier:

| Field | Meaning |
|-------|---------|
| `run`, `step` | run index and 1-based step number within the run |
| `frontier`, `request_id` | the exit and the request that resolved it |
| `outcome` | `committed` or `sealed` (no plan fit; the exit became a wall) |
| `source` | `director`, `fallback`, or `none` (sealed) |
| `fallback` | the local fallback was used for this frontier, whatever the outcome |
| `fallback_reason`, `fallback_detail` | reason category (`timeout`, `offline`, `invalid_response`, `provider_failure`, `response_mismatch`, `plan_rejected`, `budget_exhausted`, ...) and the full reason text |
| `rejections` | placement rejection reasons seen (`overlap`, `blocked_exit`, `missing_backlink`, ...) |
| `room_id`, `room_type`, `size`, `danger` | the committed room (null when sealed) |
| `repositioned`, `pruned_exits` | the coordinator shrank the room or dropped exits to fit |
| `wait_ticks` | ticks between request and resolution |
| `latency_ms`, `http_status`, `error_kind` | transport telemetry (`latency_ms` only for real-clock remote runs) |
| `reported_provider`, `reported_model`, `cost_usd` | what the response's metadata said |
| `player_turn`, `rooms_total` | simulated turn counter and committed rooms after the step |

### `rooms.jsonl` ([schema](schemas/room.schema.json))

One row per committed room in commit order (`index`, start room first):
`room_id`, `source` (`start`/`director`/`fallback`), `room_type`, `size`,
`width`, `height`, `danger` (1 to 5), `parent_frontier`,
`distance_from_start` (room-graph depth), `origin`, `floor_tiles`,
`exit_count`, `exits`, `enemy_count`, `item_count`, `enemies` and `items`
(type to count), `explored`, `repositioned`, `pruned_exits`, `fallback_reason`
and `signature` (`type|WxH|exits|dDanger`, the unit of repetition).

### `failures.jsonl` ([schema](schemas/failure.schema.json))

Each row has `kind`, `severity`, `run`, `step`, `frontier` (or null),
`message` and `detail`.

| `kind` | Detected by | Severity |
|--------|-------------|----------|
| `overlap` | audit: rooms claim a tile other than wall-on-wall, or a room record disagrees with the world map | `error` |
| `topology` | audit: traversable space not one connected component, dangling or inconsistent frontier links, room graph not a tree from the start, orphan tiles, plus the world's own integrity check | `error` |
| `reachability` | audit: a room, open exit, or spawned enemy/item cannot be reached from the start; or no walkable path to the chosen exit | `error` |
| `stall` | a step did not resolve within the timeout plus grace, no exit was left, or an iteration cap was hit | `error` |
| `placement_conflict` | the world rejected a plan | `info` when recovered by re-planning or the fallback, `warning` when the exit had to be sealed |
| `budget` | remote request budget spent | `warning` |

Only `error` failures fail the run (`passed: false`, exit code 1). Duplicate
audit findings within a run are reported once.

### `summary.json` ([schema](schemas/summary.schema.json))

`overall` (verdict `passed`, `truncated`, totals, pooled metrics), `runs`
(one object per run, with `status` `completed`/`stalled`/`aborted`/
`budget_exhausted`) and `telemetry` (requests submitted and refused, latency
percentiles, reported providers/models, HTTP statuses, cost when reported).

| Metric | Where | Definition |
|--------|-------|------------|
| Rooms explored | `rooms_explored`, `rooms.explored` | rooms the simulated player stood in or walked through |
| Danger progression | `danger` | min/max/mean, least-squares `slope` per room, first/last-third means, histogram, per-10-room buckets |
| Resource distribution | `resources` | enemy/item totals by type, per-room mean/stdev/min/max, per-room histograms, rooms with none |
| Repetition | `repetition` | `signature_repeat_rate` (1 - unique/total), consecutive-repeat rate, longest run of one room type, type and size histograms |
| Fallback frequency | `fallback_frequency`, `fallback_reasons` | steps that used the local fallback / steps, grouped by reason |
| Provider/model config | manifest `provider`, telemetry `reported_*` | what was requested and what responses reported |

## Determinism and fixtures

Offline runs (any transport, faults included) are byte-for-byte reproducible:
same flags, same dataset. `game/tests/test_simulation_harness.gd` runs a tiny
faulty-baseline simulation and compares it with the committed fixture
`fixtures/sample/`. After an intentional change to generation or the format,
regenerate and review the diff:

```sh
SIM_UPDATE_FIXTURES=1 godot --headless --path game -s res://tests/test_simulation_harness.gd
```

`make check` validates the fixture against the schemas and cross-checks the
files against each other (`director/tests/test_simulation_dataset.py`). The
fixture contains no timestamps, paths or credentials. **Never commit real run
output**: `simulation-output/` is git-ignored.

## Limitations

- **Danger does not progress in the baseline.** The world's depth is fixed at 1
  today, so the rules baseline always plans danger 1 and `danger.slope` is 0.
  The metric only becomes informative for providers that scale danger with
  pacing.
- **The player is abstract.** It follows BFS paths and does not fight, take
  damage, eat or trigger enemies. `player_turn` counts path steps only.
- **Explored is close to committed.** Every chosen exit is reached by walking
  through the dungeon, so almost every room ends up visited; only rooms
  prefetched at the very end stay unexplored.
- **The offline default is not a perfect run.** With the defaults roughly 15% of
  steps end in a local fallback, always `plan_rejected`: the baseline's
  rooms sometimes cannot be placed without overlap and the exit is sealed.
  Those show as `placement_conflict` warnings. It is real behaviour of the
  current generator, not noise.
- **Remote timing is approximate.** Latency is measured on the engine clock
  between submit and the frame that delivers the response, so it includes
  harness work (auditing) done in between; use `--audit-every` to reduce it.
  Remote runs are not reproducible.
- **Costs are only what the provider reports.** Missing usage metadata leaves
  `cost_usd` null.
- **Pinned to Godot 4.7.x.** The fixture depends on the engine's seeded RNG;
  regenerate it if you change Godot versions.
