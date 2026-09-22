class_name GameState
extends RefCounted

const RoomGenerator = preload("res://generation/room_generator.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")

# Values match GeneratedRoom.TileType so generated tiles drop in unchanged.
const TileType = {
	FLOOR = 0,
	WALL = 1,
	DOOR_CLOSED = 2,
	DOOR_OPEN = 3,
	STAIRS_DOWN = 4,
	STAIRS_UP = 5,
	SECRET_DOOR = 6
}

# Dynamic-world start: one small committed room whose exits lead into unknown space.
const START_PLAN = {
	"room_id": "r-000",
	"depth": 1,
	"room_type": "entrance",
	"size": "small",
	"danger": 1,
	"exits": [
		{"direction": "north", "kind": "door", "locked": false},
		{"direction": "east", "kind": "door", "locked": false},
		{"direction": "west", "kind": "door", "locked": false},
	],
	"enemy_density": 0.0,
	"loot_density": 0.3,
	"secret_probability": 0.0,
}

const EnemyType = {
	GOBLIN = "goblin", # weaker, faster or 1 dmg
	ORC = "orc"        # tougher, 2 dmg
}

const ItemType = {
	HEALTH_POTION = "potion_health",
	SWORD_BONUS = "sword_upgrade"
}

var grid_width: int = 24
var grid_height: int = 16

# Deferred-generation world (null for the legacy static map). `map_tiles` is
# the same dictionary as `world.tiles` while the dynamic world is active.
var dynamic_world: bool = false
var world_seed: int = 1
var world: DungeonWorld = null

# Map data: Dict of Vector2i -> int (TileType)
var map_tiles: Dictionary = {}

# Player state
var player_pos: Vector2i = Vector2i(2, 2)
var player_max_hp: int = 20
var player_hp: int = 20
var player_attack_power: int = 4
var player_score: int = 0
var player_turns: int = 0
var is_player_dead: bool = false
var active_provider: String = ""
var active_model: String = ""
var telemetry_sink: Variant = null:
	set(val):
		telemetry_sink = val
		if world != null:
			world.telemetry_sink = val
var current_room_id: String = ""


# Entities
# Array of dicts: { "id": int, "type": String, "pos": Vector2i, "hp": int, "max_hp": int, "attack": int, "name": String }
var enemies: Array[Dictionary] = []
var next_enemy_id: int = 1

# Items on ground
# Array of dicts: { "id": int, "type": String, "pos": Vector2i, "name": String, "heal_amount": int, "attack_bonus": int }
var items: Array[Dictionary] = []
var next_item_id: int = 1

# Turn & message log
var message_log: Array[String] = []

func _init() -> void:
	reset_game()

## Switch this state to the deferred-generation world and restart the run.
func enable_dynamic_world(seed_value: int = 1) -> void:
	dynamic_world = true
	world_seed = seed_value
	reset_game()

func reset_game() -> void:
	map_tiles = {}
	enemies.clear()
	items.clear()
	message_log.clear()
	next_enemy_id = 1
	next_item_id = 1
	player_max_hp = 20
	player_hp = 20
	player_attack_power = 4
	player_score = 0
	player_turns = 0
	is_player_dead = false
	if dynamic_world:
		_build_start_world()
	else:
		world = null
		_build_default_map()
	log_message("Welcome to the dungeon! Move with Arrow keys/WASD, Numpad, or on-screen D-Pad.")

func log_message(msg: String) -> void:
	message_log.append(msg)
	if message_log.size() > 50:
		message_log.pop_front()

func _build_start_world() -> void:
	world = DungeonWorld.new()
	world.run_id = _new_run_id()
	if telemetry_sink != null:
		world.telemetry_sink = telemetry_sink
	map_tiles = world.tiles
	var room := RoomGenerator.generate(START_PLAN, ("%d|start" % world_seed).hash())
	var res: Dictionary = world.commit_start_room(room, Vector2i.ZERO, 0)
	assert(res.ok, "start room must commit")
	var record: Dictionary = res.room
	record.meta = {"room_type": START_PLAN.room_type, "danger": START_PLAN.danger}
	player_pos = room.player_spawn
	current_room_id = room.room_id
	_spawn_room_entities(record)

func _new_run_id() -> String:
	# A reset starts a distinct playthrough even when it reuses the same world
	# seed. The seed controls generated geometry; this nonce is correlation and
	# stale-response identity only. 128 random bits fit comfortably in the
	# contract's 64-character bounded id.
	return "run-%s" % Crypto.new().generate_random_bytes(16).hex_encode()

## Commit a generated room for frontier `key` through the world (the only
## writer of committed geometry) and spawn its entities. See
## DungeonWorld.commit_generated for the outcome/reason contract.
func commit_generated_room(key: String, request_id: String, room: RefCounted, source: String, meta: Dictionary) -> Dictionary:
	if world == null:
		return {"ok": false, "outcome": "stale", "reason": "no_world"}
	var res: Dictionary = world.commit_generated(key, request_id, room, source, player_turns, meta)
	if res.ok:
		_spawn_room_entities(res.room)
		log_message("A new area opens up ahead.")
	return res

func _spawn_room_entities(record: Dictionary) -> void:
	for enemy in record.enemies:
		spawn_enemy(enemy.type, enemy.pos)
	for item in record.items:
		spawn_item(item.type, item.pos)

func _build_default_map() -> void:
	# Default static map: Two rooms connected by a corridor with a door
	# Room 1: (1,1) to (7,7)
	# Room 2: (12,1) to (18,7)
	# Corridor: (7,3) to (12,3)
	# Door at (8,3)

	for y in range(grid_height):
		for x in range(grid_width):
			map_tiles[Vector2i(x, y)] = TileType.WALL

	# Carve Room 1 (Spawn room)
	for y in range(1, 8):
		for x in range(1, 8):
			map_tiles[Vector2i(x, y)] = TileType.FLOOR

	# Carve Room 2 (East room)
	for y in range(1, 8):
		for x in range(12, 19):
			map_tiles[Vector2i(x, y)] = TileType.FLOOR

	# Corridor connecting them
	for x in range(8, 12):
		map_tiles[Vector2i(x, 3)] = TileType.FLOOR

	# Place a closed door in the corridor at (8,3)
	map_tiles[Vector2i(8, 3)] = TileType.DOOR_CLOSED

	# Room 3 (South room): (4, 9) to (15, 14)
	for y in range(9, 15):
		for x in range(4, 16):
			map_tiles[Vector2i(x, y)] = TileType.FLOOR

	# Corridor down from Room 1: (4,8) to (4,9)
	map_tiles[Vector2i(4, 8)] = TileType.DOOR_CLOSED

	player_pos = Vector2i(2, 2)

	# Spawn enemies
	spawn_enemy(EnemyType.GOBLIN, Vector2i(5, 4))
	spawn_enemy(EnemyType.ORC, Vector2i(15, 4))
	spawn_enemy(EnemyType.GOBLIN, Vector2i(10, 12))

	# Spawn items
	spawn_item(ItemType.HEALTH_POTION, Vector2i(6, 2))
	spawn_item(ItemType.SWORD_BONUS, Vector2i(16, 2))
	spawn_item(ItemType.HEALTH_POTION, Vector2i(14, 13))

func get_tile(pos: Vector2i) -> int:
	return map_tiles.get(pos, TileType.WALL)

func is_walkable(pos: Vector2i) -> bool:
	if not map_tiles.has(pos):
		return false
	var t: int = map_tiles[pos]
	return t == TileType.FLOOR or t == TileType.DOOR_OPEN or t == TileType.STAIRS_DOWN or t == TileType.STAIRS_UP

func get_enemy_at(pos: Vector2i) -> Dictionary:
	for e in enemies:
		if e["pos"] == pos and e["hp"] > 0:
			return e
	return {}

func get_item_at(pos: Vector2i) -> Dictionary:
	for it in items:
		if it["pos"] == pos:
			return it
	return {}

func spawn_enemy(enemy_type: String, pos: Vector2i) -> Dictionary:
	var enemy: Dictionary = {
		"id": next_enemy_id,
		"type": enemy_type,
		"pos": pos,
	}
	next_enemy_id += 1

	if enemy_type == EnemyType.GOBLIN:
		enemy["name"] = "Goblin"
		enemy["hp"] = 6
		enemy["max_hp"] = 6
		enemy["attack"] = 2
	elif enemy_type == EnemyType.ORC:
		enemy["name"] = "Orc"
		enemy["hp"] = 12
		enemy["max_hp"] = 12
		enemy["attack"] = 4
	else:
		enemy["name"] = "Monster"
		enemy["hp"] = 8
		enemy["max_hp"] = 8
		enemy["attack"] = 3

	enemies.append(enemy)
	return enemy

func spawn_item(item_type: String, pos: Vector2i) -> Dictionary:
	var item: Dictionary = {
		"id": next_item_id,
		"type": item_type,
		"pos": pos,
	}
	next_item_id += 1

	if item_type == ItemType.HEALTH_POTION:
		item["name"] = "Health Potion"
		item["heal_amount"] = 8
		item["attack_bonus"] = 0
	elif item_type == ItemType.SWORD_BONUS:
		item["name"] = "Iron Sword"
		item["heal_amount"] = 0
		item["attack_bonus"] = 2
	else:
		item["name"] = "Shiny Bauble"
		item["heal_amount"] = 0
		item["attack_bonus"] = 0

	items.append(item)
	return item

# Main player action: Attempt to step in direction
# Returns true if a turn passed
func player_action_step(dir: Vector2i) -> bool:
	if is_player_dead:
		return false
	if dir == Vector2i.ZERO:
		return false

	var target_pos: Vector2i = player_pos + dir

	# 1. Check if there's an enemy -> Attack!
	var target_enemy: Dictionary = get_enemy_at(target_pos)
	if not target_enemy.is_empty():
		_player_attack_enemy(target_enemy)
		_process_turn()
		return true

	# 2. Check if tile is a closed door -> Open it!
	var tile: int = get_tile(target_pos)
	if tile == TileType.DOOR_CLOSED or tile == TileType.SECRET_DOOR:
		map_tiles[target_pos] = TileType.DOOR_OPEN
		_record_door_revealed(target_pos)
		log_message("You open the door." if tile == TileType.DOOR_CLOSED else "You find a hidden door and open it.")
		_process_turn()
		return true

	# An open frontier door is only a visual promise until its room commits.
	# Keep the player on the committed side so a failed generation cannot seal
	# the tile underneath them and leave them apparently standing in a wall.
	if world != null and not world.open_frontier_at(target_pos).is_empty():
		log_message("The way ahead is still taking shape...")
		return false

	# 3. Check if tile is walkable floor/open door
	if is_walkable(target_pos):
		player_pos = target_pos
		_check_room_transition()
		# Check for items on this tile
		_pickup_item_at(player_pos)
		_process_turn()
		return true

	# Unknown space beyond an exit that is still being generated
	if world != null and not world.open_frontier_leading_to(target_pos).is_empty():
		log_message("The way ahead is still taking shape...")
		return false

	# Bump into wall
	log_message("Ouch! You bump into a wall.")
	return false

# Wait one turn (pass turn)
func player_action_wait() -> bool:
	if is_player_dead:
		return false
	log_message("You wait a turn.")
	_process_turn()
	return true

func _player_attack_enemy(enemy: Dictionary) -> void:
	var dmg: int = player_attack_power
	enemy["hp"] -= dmg
	log_message("You strike %s for %d damage!" % [enemy["name"], dmg])
	if enemy["hp"] <= 0:
		log_message("%s collapses and dies!" % [enemy["name"]])
		player_score += 10 if enemy["type"] == EnemyType.GOBLIN else 25

func _pickup_item_at(pos: Vector2i) -> void:
	var item: Dictionary = get_item_at(pos)
	if item.is_empty():
		return

	if item["heal_amount"] > 0:
		if player_hp >= player_max_hp:
			log_message("You see a %s here, but your health is already full." % [item["name"]])
			return
		var old_hp: int = player_hp
		player_hp = mini(player_max_hp, player_hp + item["heal_amount"])
		var healed: int = player_hp - old_hp
		log_message("Picked up %s! Restored %d HP." % [item["name"], healed])
	elif item["attack_bonus"] > 0:
		player_attack_power += item["attack_bonus"]
		log_message("Found %s! Attack power increased by %d (Total: %d)." % [item["name"], item["attack_bonus"], player_attack_power])
	else:
		log_message("Picked up %s." % [item["name"]])

	player_score += 5
	# Remove item from floor
	for i in range(items.size()):
		if items[i]["id"] == item["id"]:
			items.remove_at(i)
			break

func _process_turn() -> void:
	player_turns += 1
	# Enemies take turn
	_process_enemy_turns()

	# Check if player died (in case of environmental/other damage)
	if player_hp <= 0 and not is_player_dead:
		player_hp = 0
		is_player_dead = true
		log_message("YOU DIED! Press 'R' or Tap 'Restart' to try again.")

func _process_enemy_turns() -> void:
	# Filter dead enemies
	enemies = enemies.filter(func(e): return e["hp"] > 0)

	for enemy in enemies:
		if is_player_dead:
			break
		var epos: Vector2i = enemy["pos"]
		var delta: Vector2i = player_pos - epos
		var dist_x: int = abs(delta.x)
		var dist_y: int = abs(delta.y)
		var manhattan_dist: int = dist_x + dist_y

		# Cardinal only (Manhattan distance == 1), enemies cannot hit diagonally through corners
		if manhattan_dist == 1:
			var dmg: int = enemy["attack"]
			player_hp -= dmg
			if player_hp <= 0:
				player_hp = 0
				is_player_dead = true
				log_message("%s strikes a lethal blow for %d damage! (HP: 0/%d)" % [enemy["name"], dmg, player_max_hp])
				log_message("YOU DIED! Press 'R' or Tap 'Restart' to try again.")
				break
			else:
				log_message("%s attacks you for %d damage! (HP: %d/%d)" % [enemy["name"], dmg, player_hp, player_max_hp])
		elif manhattan_dist <= 6:
			# Simple path step towards player if within aggro range
			var step_dir: Vector2i = Vector2i.ZERO
			if dist_x >= dist_y:
				step_dir.x = 1 if delta.x > 0 else -1
			else:
				step_dir.y = 1 if delta.y > 0 else -1

			var cand_pos: Vector2i = epos + step_dir
			# Don't step into player, closed doors, or walls, or other alive enemies
			if cand_pos != player_pos and is_walkable(cand_pos) and get_enemy_at(cand_pos).is_empty():
				enemy["pos"] = cand_pos
			else:
				# Try alternate axis
				var alt_dir: Vector2i = Vector2i.ZERO
				if step_dir.x != 0 and delta.y != 0:
					alt_dir.y = 1 if delta.y > 0 else -1
				elif step_dir.y != 0 and delta.x != 0:
					alt_dir.x = 1 if delta.x > 0 else -1
				var cand_pos_alt: Vector2i = epos + alt_dir
				if cand_pos_alt != player_pos and is_walkable(cand_pos_alt) and get_enemy_at(cand_pos_alt).is_empty():
					enemy["pos"] = cand_pos_alt


## Observation follows the actual closed/hidden -> open transition, never placement.
func _record_door_revealed(pos: Vector2i) -> void:
	if world == null or telemetry_sink == null:
		return
	var room_id := world.room_id_at(pos)
	var record: Dictionary = world.rooms.get(room_id, {})
	var request_id: String = "req-%s-init" % world.run_id
	var parent_key: String = record.get("parent_frontier", "")
	if world.frontiers.has(parent_key):
		request_id = world.frontiers[parent_key].request_id
	for frontier in world.frontiers.values():
		if frontier.pos == pos and str(frontier.get("request_id", "")) != "":
			request_id = frontier.request_id
			break
	var elapsed := float(maxi(0, Time.get_ticks_msec() - int(record.get("committed_at_msec", Time.get_ticks_msec()))))
	telemetry_sink.enqueue_event("door.revealed", world.run_id, request_id, {
		"room_id": room_id, "time_to_visible_ms": elapsed,
	})


func _check_room_transition() -> void:
	if world == null:
		return
	var new_room_id := world.room_id_at(player_pos)
	if new_room_id == "" or new_room_id == current_room_id:
		return
	current_room_id = new_room_id
	if telemetry_sink != null and telemetry_sink.has_method("enqueue_event"):
		var time_to_entry: float = 0.0
		var r_rec: Dictionary = world.rooms.get(new_room_id, {})
		var committed_at: Variant = r_rec.get("committed_at_msec", null)
		if committed_at != null:
			time_to_entry = float(maxi(0, Time.get_ticks_msec() - int(committed_at)))

		var r_meta: Dictionary = r_rec.get("meta", {})
		var r_source: String = str(r_rec.get("source", ""))
		var prov := str(r_meta.get("provider", active_provider if active_provider != "" else ("rules-baseline" if r_source == "fallback" else "director")))
		var mod := str(r_meta.get("model", active_model if active_model != "" else ("builtin-v1" if r_source == "fallback" else "default")))

		var attrs := {
			"room_id": new_room_id,
			"time_to_entry_ms": time_to_entry,
			"provider": prov,
			"model": mod,
		}
		var req_id: Variant = null
		if not r_rec.is_empty():
			var pf: String = r_rec.get("parent_frontier", "")
			if pf != "" and world.frontiers.has(pf):
				req_id = world.frontiers[pf].get("request_id", null)
		if req_id == null or str(req_id) == "":
			req_id = "req-%s-init" % world.run_id
		telemetry_sink.enqueue_event("room.entered", world.run_id, req_id, attrs)
