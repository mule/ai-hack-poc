# Godot 4 Roguelike Game Shell

A 2D grid-based roguelike client built with Godot 4.
This shell serves as the minimal playable game workload for the dungeon generation POC.

## Features

- **Grid/Tile Movement**: 4-directional grid movement with collision detection.
- **Rooms, Corridors & Doors**: Static dungeon layout with navigable corridors and interactive doors (closed doors open on interaction, open doors are walkable).
- **Combat & Damage**: Turn-based combat with 2 enemy archetypes:
  - **Goblin**: Lower health (6 HP), 2 attack damage.
  - **Orc**: Tougher, higher health (12 HP), 4 attack damage.
- **Items & Loot**:
  - **Health Potion**: Restores +8 HP (not consumed when player HP is already full).
  - **Iron Sword**: Increases player attack damage (+2 ATK).
- **Death & Restart**: Death triggers a game over overlay and blocks further movement; pressing `R` or tapping **Restart** resets the state.
- **Responsive Layout**: Designed for both desktop and mobile/Android viewports (`canvas_items` stretch mode, `expand` aspect ratio) with top stats bar, turn log panel, and on-screen touch controls.
- **Controls**:
  - **Keyboard**: Arrow keys, WASD, or Numpad (8, 2, 4, 6) for movement; Space / Numpad 5 for wait; `R` for restart; `P` for provider/model selection; backquote or `F3` for the debug HUD.
  - **Controller / Gamepad**: D-pad for movement, A / Cross for wait, Y / Triangle for restart.
  - **Touch Controls**: On-screen directional buttons (▲, ▼, ◀, ▶), Wait, **CFG** for provider/model selection, **HUD** for generation diagnostics, and the modal Restart button (`focus_mode=FOCUS_NONE` to avoid stealing keyboard/gamepad input).
- **Deferred generation** (issue #7): the main scene starts with one small committed room; exits lead into unknown space and new rooms are generated as you approach (see below). The legacy static map remains available to the mechanics tests via `GameState.new()`; the main scene opts into the dynamic world with `enable_dynamic_world()`.
- **Provider-neutral**: the game only speaks the canonical `POST /v1/generate` contract; there is no provider-specific logic.
- **Runtime experiments** (issue #12): the selector reads `GET /v1/config`, marks unavailable providers, and applies a provider/model pair to a fresh run. The toggleable HUD shows generation status, end-to-end latency, recent `RoomPlan` decisions, fallbacks, and opaque provider metadata such as Jev probabilities without using it for gameplay.

## Launching the Game

### Desktop GUI Launch

Launch using the Godot 4 executable:
```bash
godot --path game
```

### Running Deterministic Headless Mechanics Checks

Run the automated deterministic test suite:
```bash
godot --headless --path game -s res://tests/test_mechanics.gd --log-file /tmp/godot_mechanics_test.log
```

### Running Scene Smoke Tests

Run the scene smoke test verifying node composition, input handlers, focus modes, UI updates, renderer redraws, and wall-bump refreshes:
```bash
godot --headless --path game -s res://tests/test_scene_smoke.gd --log-file /tmp/godot_smoke_test.log
```

### Running Room Generator Tests

Run the headless deterministic room generator test suite:
```bash
godot --headless --path game -s res://tests/test_room_generator.gd --log-file /tmp/godot_room_generator_test.log
```

The test suite validates:
1. **Deterministic reproducibility**: Same RoomPlan + seed produces bit-for-bit identical tile layout, entity placements, and secret positions.
2. **Contract fixture compatibility**: Validates against canonical RoomPlan contract schemas and fixtures.
3. **Room sizes & archetypes**: Supports all contract sizes (`tiny`, `small`, `medium`, `large`, `huge`) and room types (`corridor`, `cavern`, `vault`, `shrine`, `shop`, `treasure`, `entrance`, `stairs_down`, `stairs_up`, etc.).
4. **Collision & spawn safety**: Player, enemies, loot, and secrets never spawn inside walls or doors; enemies never spawn on top of the player.
5. **Connectivity & traversability**: All floor tiles and exits connect in a single traversable component via BFS validation.
6. **Plan normalization**: Odd, out-of-bound, or malformed plan parameters are safely clamped/normalized with explicit diagnostic logs.

### Running Deferred-Generation Tests

```bash
godot --headless --path game -s res://tests/test_deferred_world.gd        # frontier/commit state, placement
godot --headless --path game -s res://tests/test_deferred_generation.gd   # coordinator, HTTP client, fallback, main scene
godot --headless --path game -s res://tests/test_deferred_simulation.gd   # 100+ room expansion invariants
```

No live server is needed: transports are injected fakes plus a loopback mini HTTP server (`tests/support/`).

### Running Provider Selector and Debug HUD Tests

```bash
godot --headless --path game -s res://tests/test_provider_selector.gd
```

The suite covers live config parsing, unavailable and offline providers,
provider/model changes across run resets, keyboard and touch controls, latency,
recent decisions, generic provider metadata, and success-to-fallback HUD state.

### Running the Simulation Harness (issue #16)

```bash
make simulate                                    # from the repo root: 5 runs x 100 steps, offline
godot --headless --path game -s res://tests/test_simulation_harness.gd   # its test suite
```

`game/simulation/` drives the real `GameState`, `DungeonWorld`, `GenerationCoordinator`
and `RoomGenerator` with no player and audits topology, reachability, overlap, placement
conflicts and stalls. It is offline by default; real provider calls need an explicit
`--remote`. Flags, dataset format and limitations: [benchmarks/simulation/README.md](../benchmarks/simulation/README.md).

### Running Headless Contract Tests

Run the contract fixture validation test suite:
```bash
godot --headless --path game -s res://tests/test_contracts.gd
```

## Platform Exports

Preset configurations are managed in [`export_presets.cfg`](export_presets.cfg).

### Exporting Linux Desktop
```bash
make -C .. export-linux GODOT=/path/to/godot
# Or:
mkdir -p builds/linux
godot --headless --path . --export-debug "Linux Desktop" builds/linux/ai-hack-poc.x86_64
tar -czf builds/linux/ai-hack-poc-linux-x86_64.tar.gz -C builds/linux ai-hack-poc.x86_64 ai-hack-poc.pck ai-hack-poc.sh
```

### Exporting Android Debug APK
Requires Godot Android export templates, OpenJDK 17, Android SDK (build-tools 35+, platform 35+), and debug keystore configured in editor settings or passed via environment variables (`GODOT_ANDROID_KEYSTORE_DEBUG_PATH`, `GODOT_ANDROID_KEYSTORE_DEBUG_USER`, `GODOT_ANDROID_KEYSTORE_DEBUG_PASSWORD`).
```bash
make -C .. export-android GODOT=/path/to/godot
# Or:
mkdir -p builds/android
godot --headless --path . --export-debug "Android Debug" builds/android/ai-hack-poc-debug.apk
```

> [!NOTE]
> CI builds use an ephemeral debug key. To install a CI-built APK on a device that already has a local build or a prior CI APK installed, uninstall the previous package first (`adb uninstall com.mule.aihackpoc`).

## Deferred Dungeon Generation

Code lives in `world/`; `GameState` owns a `DungeonWorld` and `MainGame` drives a `GenerationCoordinator` once per frame.

- **State** (`dungeon_world.gd`): world-space tiles, committed rooms (bounds, tiles, entities, source, parent frontier) and one *frontier* per exit with a one-way status `unresolved -> pending -> committed | sealed`. `begin_generation` moves a frontier to pending once; `commit_generated` commits it once. Duplicate, stale (wrong request id / not pending / unknown) and contradictory completions are refused and counted without mutating anything. Placement is evaluated side-effect-free, then committed atomically.
- **Placement**: the new room's backlink door lands on the target exit's door tile (the two rooms share that tile). Rooms may only overlap wall-on-wall; a room whose exits would face committed space is re-planned with those exits pruned, then with smaller footprints. Every commit keeps all traversable tiles in one connected component. If no exit is open, a wall of a committed room is breached (only where the smallest room provably fits), so the dungeon can always continue.
- **Triggering** (`generation_coordinator.gd`): unresolved exits within `trigger_radius` (3 tiles, Manhattan) of the player are prefetched, at most `max_in_flight` at a time. One adjacent player-facing frontier may use an urgent reserve beyond that limit, so speculative requests cannot block the next passage. `update()` never blocks; results arrive later and the current room stays playable. Approaching a still-pending exit shows "The way ahead is still taking shape...".
- **Fallback**: on timeout, transport failure, invalid or failed response, mismatched ids, or placement rejection, a deterministic local plan (`rules_baseline.gd`, a function of run id + frontier only) is committed instead; if even the smallest room cannot fit, the exit is sealed. Fallbacks are observable in the on-screen log, the `[dungeon-gen]` console line, HUD (`Fallbacks: n`) and `world.generation_log` / `world.counters`.
- **Rendering**: unresolved exits are drawn with a coloured door frame, arrow and a hatched "unknown" cell (violet = unresolved, amber = pending).
- **Runtime selection and diagnostics**: press `P` or tap **CFG** to fetch the director configuration. Applying a provider/model resets the dungeon and sends the selection with future generation requests. Press backquote or `F3`, or tap **HUD**, to inspect the latest outcome and a bounded history of decisions. If the config endpoint is unreachable, the selector offers `rules-baseline` / `builtin-v1` as an explicit offline fallback.

### Configuration (environment)

| Variable | Meaning |
|----------|---------|
| `DUNGEON_DIRECTOR_URL` | Director base URL (default `http://127.0.0.1:8000`); `offline` skips the network and always uses local rules |
| `DUNGEON_DIRECTOR_PROVIDER` / `DUNGEON_DIRECTOR_MODEL` | Optional stable ids sent as `?provider=&model=` |
| `DUNGEON_DIRECTOR_TIMEOUT` | Request timeout in seconds (default 5) |
| `DUNGEON_TELEMETRY_URL` | Optional telemetry collector base URL (defaults to `DUNGEON_DIRECTOR_URL` or `http://127.0.0.1:8000`); `offline` disables telemetry |
| `DUNGEON_TELEMETRY_ENABLED` | Set to `false`/`0` to disable the telemetry sink |

## Game Lifecycle Telemetry (Issue #25)

The game runtime includes an asynchronous, bounded telemetry sink (`game/world/game_telemetry_sink.gd`) emitting batch `POST /v1/telemetry/game` events conforming to the canonical OpenTelemetry lifecycle schema.

- **Opt-in**: Set `DUNGEON_TELEMETRY_ENABLED=1`; the sink is disabled by default. It uses `DUNGEON_DIRECTOR_URL` (or `DUNGEON_TELEMETRY_URL`) and keeps collector credentials server-side.
- **Non-blocking Execution**: Gameplay never stalls on telemetry queues, serialization, or network latency.
- **Queue Bounds & Safe Backpressure**: Bounded queue (`max_queue_size=256`). When full, older events are safely dropped with explicit drop counters (`dropped_backpressure`, `dropped_invalid`, `dropped_payload_limit`, `dropped_total`).
- **Batching**: Automatic flushing at configurable intervals (`flush_interval_sec=0.2`) or batch sizes (`batch_size=20`, up to 100 events per batch, capped at 64 KiB).
- **Attribute Allowlisting & Redaction**: Strict per-event attribute allowlists (`room_type`, `room_size`, `danger`, `exit_direction`, `queue_ms`, `network_ms`, `materialization_ms`, `time_to_entry_ms`, etc.). Scalar bounds and enum validation are enforced. Sensitive tokens (`Bearer`, `sk-`, `password`, `prompt`, `raw_payload`) are automatically redacted.
- **Correlated Events**:
  - `frontier.discovered`: Emitted when room exits or breaches are discovered.
  - `door.revealed`: Emitted only when a closed or hidden door is actually opened. `time_to_visible_ms` measures time since its owning room committed.
  - `generation.queued`: Emitted when an unresolved frontier enters prefetch / generation.
  - `generation.sent` / `generation.response_received`: Client monotonic queue/network timings, separate from provider latency.
  - `generation.accepted`: Emitted upon successful generation response acceptance.
  - `generation.normalized`: Emitted instead of accepted when the director plan was adjusted to fit geometry; the decision event precedes room.committed.
  - `generation.rejected`: Emitted only when a returned plan is rejected by the game. Transport/provider failures remain fallback reasons.
  - `generation.fallback_applied`: Emitted when fallback placement succeeds; provider/model identify the failed request. Committed/entered room events identify rules-baseline.
  - `room.committed`: Emitted when a room is permanently placed and committed into the world.
  - `room.entered`: Emitted when the player enters a new room, tracking `time_to_entry_ms`.

### Running Game Telemetry Tests

```bash
godot --headless --path game -s res://tests/test_game_telemetry.gd
# Actual raw serialized batches are validated against the Python contract:
GODOT=godot PYTHONPATH=director director/.venv/bin/pytest director/tests/test_godot_telemetry_contract.py
```

### Limitations

- Single level: vertical exits (`up`/`down`) and stairs room types are not expanded; requests forbid stairs room types and any vertical exit in a plan is ignored.
- No fog of war or persistence: committed rooms live for the run; restarting rebuilds the same start room for the same seed.
- Env-var configuration is desktop-oriented; on Android the default `127.0.0.1` targets the device itself, so the game plays on local rules until a URL setting is added.
