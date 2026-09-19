extends SceneTree

# Deterministic headless test runner for roguelike game shell mechanics
# Tests:
# 1. Collision (walls block player, position unchanged)
# 2. Doors (closed door blocks step and is opened; closed doors block enemies; subsequent step enters)
# 3. Combat/Damage (player attacks enemy, enemy retaliates cardinally, diagonal hit blocked)
# 4. Enemy Movement & Aggro (enemies path towards player within aggro range, ignore beyond range)
# 5. Multiple-Enemy Lethal Stop (first lethal hit clamps HP to 0 and stops subsequent enemy actions, single death msg)
# 6. Wait (wait action advances turn and triggers enemy processing)
# 7. Loot/Pickup (stepping on item collects it, restores HP / increases ATK; full-HP potion is left on floor)
# 8. Death/Restart (taking fatal damage marks player dead and resets state on restart)
# 9. Static Map Reachability (verifies all floor tiles are reachable via flood-fill when doors are open)

const GameStateScript = preload("res://src/game_state.gd")

var tests_passed: int = 0
var tests_failed: int = 0
var execution_completed: bool = false

func _init() -> void:
	print("--- Running Deterministic Roguelike Headless Mechanics Checks ---")
	test_collision_mechanics()
	test_door_mechanics()
	test_enemy_movement_and_aggro()
	test_loot_pickup_and_full_hp()
	test_combat_and_cardinal_melee()
	test_multiple_enemy_lethal_stop()
	test_wait_action()
	test_death_and_restart_mechanics()
	test_static_map_reachability()
	
	execution_completed = true
	print("\n--- Test Results: %d Passed, %d Failed (Completed: %s) ---" % [tests_passed, tests_failed, str(execution_completed)])
	if tests_failed > 0 or not execution_completed:
		print("FAILED: Mechanics checks did not pass.")
		quit(1)
	else:
		print("SUCCESS: All deterministic mechanics checks passed!")
		quit(0)

func assert_true(condition: bool, message: String) -> void:
	if condition:
		tests_passed += 1
		print("  [PASS] %s" % message)
	else:
		tests_failed += 1
		printerr("  [FAIL] %s" % message)

func assert_eq(actual, expected, message: String) -> void:
	if actual == expected:
		tests_passed += 1
		print("  [PASS] %s" % message)
	else:
		tests_failed += 1
		printerr("  [FAIL] %s (Expected: %s, Got: %s)" % [message, str(expected), str(actual)])

func test_collision_mechanics() -> void:
	print("\nTest 1: Collision Mechanics")
	var state = GameStateScript.new()
	var ok1 = state.player_action_step(Vector2i.UP) # (2, 1)
	assert_true(ok1, "Stepping onto floor succeeds")
	assert_eq(state.player_pos, Vector2i(2, 1), "Player position updated to (2,1)")
	
	var ok2 = state.player_action_step(Vector2i.UP) # into wall at (2, 0)
	assert_true(not ok2, "Stepping into wall returns false")
	assert_eq(state.player_pos, Vector2i(2, 1), "Player position remains unchanged at (2,1)")
	
	state.player_pos = Vector2i(1, 1)
	var ok_wall_left = state.player_action_step(Vector2i.LEFT)
	assert_true(not ok_wall_left, "Stepping left into wall at (0,1) fails")
	assert_eq(state.player_pos, Vector2i(1, 1), "Player position stays at (1,1)")

func test_door_mechanics() -> void:
	print("\nTest 2: Door Mechanics")
	var state = GameStateScript.new()
	state.enemies.clear()
	state.player_pos = Vector2i(7, 3)
	
	assert_eq(state.get_tile(Vector2i(8, 3)), GameStateScript.TileType.DOOR_CLOSED, "Door at (8,3) is initially closed")
	
	# Closed door blocks enemy movement as well
	var enemy = state.spawn_enemy(GameStateScript.EnemyType.GOBLIN, Vector2i(9, 3))
	state.player_action_wait()
	assert_eq(enemy["pos"], Vector2i(9, 3), "Enemy cannot walk through closed door at (8,3)")
	
	# First step towards door opens it but does not enter
	var opened = state.player_action_step(Vector2i.RIGHT)
	assert_true(opened, "Action towards closed door succeeds (opens door)")
	assert_eq(state.get_tile(Vector2i(8, 3)), GameStateScript.TileType.DOOR_OPEN, "Door tile changed to DOOR_OPEN")
	assert_eq(state.player_pos, Vector2i(7, 3), "Player stayed at (7,3) while opening door")
	
	# After door is opened, player can step onto the door tile
	# (first defeat or remove enemy on 9,3 so it doesn't block step)
	state.enemies.clear()
	var stepped = state.player_action_step(Vector2i.RIGHT)
	assert_true(stepped, "Stepping into open door succeeds")
	assert_eq(state.player_pos, Vector2i(8, 3), "Player moved to (8,3)")

func test_enemy_movement_and_aggro() -> void:
	print("\nTest 3: Enemy Movement & Aggro")
	var state = GameStateScript.new()
	state.enemies.clear()
	state.items.clear()
	state.player_pos = Vector2i(2, 2)
	
	# Spawn enemy within aggro range (Manhattan distance <= 6, e.g. at (2, 5) dist = 3)
	var goblin = state.spawn_enemy(GameStateScript.EnemyType.GOBLIN, Vector2i(2, 5))
	state.player_action_wait()
	assert_eq(goblin["pos"], Vector2i(2, 4), "Goblin within aggro steps towards player")
	
	# Spawn enemy beyond aggro range (e.g. at (15, 14) dist > 6)
	var far_orc = state.spawn_enemy(GameStateScript.EnemyType.ORC, Vector2i(15, 14))
	state.player_action_wait()
	assert_eq(far_orc["pos"], Vector2i(15, 14), "Enemy beyond aggro range does not move")

func test_loot_pickup_and_full_hp() -> void:
	print("\nTest 4: Loot Pickup & Full-HP Handling")
	var state = GameStateScript.new()
	state.enemies.clear()
	
	# When player HP is full (20/20), potion must NOT be consumed
	state.player_pos = Vector2i(6, 1)
	assert_eq(state.player_hp, 20, "Player HP is full")
	var moved_full = state.player_action_step(Vector2i.DOWN) # (6, 2) has potion
	assert_true(moved_full, "Player stepped onto potion tile")
	assert_eq(state.player_pos, Vector2i(6, 2), "Player at (6,2)")
	assert_eq(state.player_hp, 20, "Player HP remains 20")
	assert_true(not state.get_item_at(Vector2i(6, 2)).is_empty(), "Potion is still on floor when HP is full")
	
	# Now injure player and step away, then step back onto potion
	state.player_hp = 10
	state.player_action_step(Vector2i.UP) # (6, 1)
	var moved_injured = state.player_action_step(Vector2i.DOWN) # (6, 2)
	assert_true(moved_injured, "Player stepped back onto potion while injured")
	assert_eq(state.player_hp, 18, "Potion consumed: restored 8 HP (10 -> 18)")
	assert_true(state.get_item_at(Vector2i(6, 2)).is_empty(), "Potion removed from floor")
	
	# Sword pickup increases attack
	var old_atk = state.player_attack_power
	var sword_pos = Vector2i(16, 2)
	state.player_pos = Vector2i(16, 1)
	state.player_action_step(Vector2i.DOWN)
	assert_eq(state.player_pos, sword_pos, "Player at sword pos")
	assert_eq(state.player_attack_power, old_atk + 2, "Attack power increased by 2")
	assert_true(state.get_item_at(sword_pos).is_empty(), "Sword removed from floor")

func test_combat_and_cardinal_melee() -> void:
	print("\nTest 5: Combat and Cardinal-Only Melee")
	var state = GameStateScript.new()
	state.enemies.clear()
	state.items.clear()
	state.player_pos = Vector2i(2, 2)
	
	# Test diagonal enemy cannot attack cardinally (Manhattan dist = 2, diagonal)
	var diag_orc = state.spawn_enemy(GameStateScript.EnemyType.ORC, Vector2i(3, 3))
	var hp_before = state.player_hp
	state.player_action_wait()
	# Enemy was diagonal; either it takes a step to cardinally align or did not melee through diagonal
	# If it stepped towards player (e.g. to (2,3) or (3,2)), it couldn't also attack on same turn
	assert_eq(state.player_hp, hp_before, "Diagonal enemy did not damage player across corner")
	
	# Now test direct combat
	state.enemies.clear()
	var goblin = state.spawn_enemy(GameStateScript.EnemyType.GOBLIN, Vector2i(3, 2))
	var turn1 = state.player_action_step(Vector2i.RIGHT)
	assert_true(turn1, "Attack action taken")
	assert_eq(goblin["hp"], 2, "Goblin took 4 damage (6 -> 2)")
	assert_eq(state.player_hp, hp_before - goblin["attack"], "Goblin counterattacked cardinally")
	
	var turn2 = state.player_action_step(Vector2i.RIGHT)
	assert_true(turn2, "Second attack action taken")
	assert_true(goblin["hp"] <= 0, "Goblin killed")
	assert_true(state.get_enemy_at(Vector2i(3, 2)).is_empty(), "Goblin removed from active enemies")

func test_multiple_enemy_lethal_stop() -> void:
	print("\nTest 6: Multiple Enemy Lethal Stop")
	var state = GameStateScript.new()
	state.enemies.clear()
	state.items.clear()
	state.player_pos = Vector2i(3, 3)
	state.player_hp = 3 # lethal to 4 dmg from Orc
	
	# Surround player with two Orcs (each has 4 dmg)
	var orc1 = state.spawn_enemy(GameStateScript.EnemyType.ORC, Vector2i(2, 3)) # West
	var orc2 = state.spawn_enemy(GameStateScript.EnemyType.ORC, Vector2i(4, 3)) # East
	
	state.player_action_wait()
	assert_eq(state.player_hp, 0, "Player HP clamped to 0 on lethal blow")
	assert_true(state.is_player_dead, "Player marked dead immediately")
	
	# Count how many times death was logged
	var death_log_count = 0
	for msg in state.message_log:
		if msg.contains("YOU DIED"):
			death_log_count += 1
	assert_eq(death_log_count, 1, "Exactly one YOU DIED message logged when multiple enemies surround player")

func test_wait_action() -> void:
	print("\nTest 7: Wait Action")
	var state = GameStateScript.new()
	var initial_turns = state.player_turns
	var ok = state.player_action_wait()
	assert_true(ok, "Wait action returned true")
	assert_eq(state.player_turns, initial_turns + 1, "Turn counter incremented by 1 on wait")

func test_death_and_restart_mechanics() -> void:
	print("\nTest 8: Death and Restart Mechanics")
	var state = GameStateScript.new()
	state.enemies.clear()
	state.items.clear()
	
	var orc = state.spawn_enemy(GameStateScript.EnemyType.ORC, Vector2i(2, 3))
	state.player_hp = 2
	state.player_action_wait()
	assert_eq(state.player_hp, 0, "Player died")
	assert_true(state.is_player_dead, "is_player_dead is true")
	
	var step_blocked = state.player_action_step(Vector2i.UP)
	assert_true(not step_blocked, "Actions while dead are blocked")
	
	state.reset_game()
	assert_true(not state.is_player_dead, "Player alive after reset")
	assert_eq(state.player_hp, state.player_max_hp, "HP restored to max after reset")
	assert_eq(state.player_pos, Vector2i(2, 2), "Player pos reset to spawn")
	assert_true(state.enemies.size() > 0, "Enemies respawned after reset")

func test_static_map_reachability() -> void:
	print("\nTest 9: Static Map Reachability")
	var state = GameStateScript.new()
	# Open all doors for reachability test
	for pos in state.map_tiles.keys():
		if state.map_tiles[pos] == GameStateScript.TileType.DOOR_CLOSED:
			state.map_tiles[pos] = GameStateScript.TileType.DOOR_OPEN
			
	var reachable: Dictionary = {}
	var queue: Array[Vector2i] = [state.player_pos]
	reachable[state.player_pos] = true
	
	while not queue.is_empty():
		var curr = queue.pop_front()
		for offset in [Vector2i.UP, Vector2i.DOWN, Vector2i.LEFT, Vector2i.RIGHT]:
			var neighbor = curr + offset
			if state.is_walkable(neighbor) and not reachable.has(neighbor):
				reachable[neighbor] = true
				queue.append(neighbor)
				
	# Count total walkable floor/door tiles in map
	var total_walkable = 0
	for pos in state.map_tiles.keys():
		if state.is_walkable(pos):
			total_walkable += 1
			assert_true(reachable.has(pos), "Tile %s is reachable from spawn" % [str(pos)])
			
	assert_true(total_walkable > 50, "Static map has substantial walkable floor area (%d tiles)" % total_walkable)
	assert_eq(reachable.size(), total_walkable, "All walkable tiles are 100% reachable via connected doors/corridors")
