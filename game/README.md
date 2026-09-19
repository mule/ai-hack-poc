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

The test suite validates:
1. **Collision mechanics**: Walls block movement, player coordinates remain unchanged.
2. **Door mechanics**: Closed doors block step and open on interaction; subsequent step navigates through open door.
3. **Loot pickup mechanics**: Stepping onto items collects them, restores player HP or buffs attack power, and removes items from the floor.
4. **Combat and damage mechanics**: Player attacks enemies, enemies retaliate on player turn, enemy dies upon reaching 0 HP, and defeated enemy tile becomes passable.
5. **Death and restart mechanics**: Taking fatal damage marks player dead and halts actions; restarting resets HP, position, and spawns.
