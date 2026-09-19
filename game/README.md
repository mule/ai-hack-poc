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
  - **Keyboard**: Arrow keys, WASD, or Numpad (8, 2, 4, 6) for movement; Space / Numpad 5 for wait; `R` for restart.
  - **Controller / Gamepad**: D-pad for movement, A / Cross for wait, Y / Triangle for restart.
  - **Touch Controls**: On-screen directional buttons (▲, ▼, ◀, ▶), Wait button, and modal Restart button (`focus_mode=FOCUS_NONE` to avoid stealing keyboard/gamepad input).
- **Independent Architecture**: Pure static game shell with zero external AI provider dependencies.

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
