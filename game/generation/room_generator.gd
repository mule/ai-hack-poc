class_name RoomGenerator
extends RefCounted
## Deterministic RoomPlan-to-tile dungeon room generator (Issue #6).
## Converts semantic RoomPlan decisions into mechanically valid, connected geometry.

const DungeonContracts = preload("res://contracts/dungeon_contracts.gd")
const GeneratedRoom = preload("res://generation/generated_room.gd")

# Constants for room dimensions per size
const SIZE_DIMENSIONS = {
	"tiny": Vector2i(7, 7),
	"small": Vector2i(9, 9),
	"medium": Vector2i(13, 11),
	"large": Vector2i(17, 13),
	"huge": Vector2i(21, 15)
}

# Supported room types
const ROOM_TYPES = DungeonContracts.ROOM_TYPES
const ROOM_SIZES = DungeonContracts.ROOM_SIZES
const EXIT_DIRECTIONS = DungeonContracts.EXIT_DIRECTIONS
const EXIT_KINDS = DungeonContracts.EXIT_KINDS

## Main entry point: generate a GeneratedRoom from a RoomPlan dictionary and an optional integer seed.
## Accepts raw RoomPlan Dict or parsed DungeonContracts.RoomPlanData.
static func generate(room_plan_input: Variant, seed_value: int = 1337) -> GeneratedRoom:
	var room: GeneratedRoom = GeneratedRoom.new()
	room.seed_used = seed_value

	# Normalization & Validation of input
	var plan_dict: Dictionary = {}
	if room_plan_input is DungeonContracts.RoomPlanData:
		plan_dict = {
			"room_id": room_plan_input.room_id,
			"depth": room_plan_input.depth,
			"room_type": room_plan_input.room_type,
			"size": room_plan_input.size,
			"danger": room_plan_input.danger,
			"exits": room_plan_input.exits,
			"enemy_density": room_plan_input.enemy_density,
			"loot_density": room_plan_input.loot_density,
			"secret_probability": room_plan_input.secret_probability,
			"has_secret": room_plan_input.has_secret,
			"environmental_tags": room_plan_input.environmental_tags,
			"description": room_plan_input.description,
		}
	elif room_plan_input is Dictionary:
		plan_dict = room_plan_input.duplicate(true)
	else:
		room.diagnostics.append("Invalid room_plan_input type: expected Dictionary or RoomPlanData")
		return _generate_fallback_room(room, "invalid_plan_type", seed_value)

	var validation := DungeonContracts.validate_room_plan(plan_dict)
	if not validation.ok:
		room.diagnostics.append("Plan failed contract validation: %s. Normalizing safely." % validation.error)
		_normalize_plan_dict(plan_dict, room.diagnostics)
		var reval := DungeonContracts.validate_room_plan(plan_dict)
		if not reval.ok:
			room.diagnostics.append("Normalized plan failed contract revalidation: %s. Returning fallback room." % reval.error)
			return _generate_fallback_room(room, "revalidation_fallback", seed_value)

	var room_id: String = String(plan_dict.get("room_id", "r-fallback"))
	if room_id.is_empty():
		room_id = "r-fallback"
	room.room_id = room_id

	var room_type: String = String(plan_dict.get("room_type", "room"))
	if not (room_type in ROOM_TYPES):
		room.diagnostics.append("Unknown room_type '%s', falling back to 'room'" % room_type)
		room_type = "room"

	var size_str: String = String(plan_dict.get("size", "medium"))
	if not SIZE_DIMENSIONS.has(size_str):
		room.diagnostics.append("Unknown size '%s', falling back to 'medium'" % size_str)
		size_str = "medium"

	var dims: Vector2i = SIZE_DIMENSIONS[size_str]
	room.width = dims.x
	room.height = dims.y

	# Setup deterministic RNG
	var rng := RandomNumberGenerator.new()
	rng.seed = seed_value

	# Step 1: Carve basic layout based on archetype
	_carve_archetype(room, room_type, rng)

	# Step 2: Carve exits & guarantee connectivity to room interior
	var raw_exits: Array = plan_dict.get("exits", [])
	_place_exits(room, raw_exits, rng)

	# Step 3: Ensure complete connectivity between all floor tiles and exits
	_ensure_connectivity(room)

	# Step 4: Determine player spawn
	_place_player_spawn(room, raw_exits, rng)

	# Step 5: Place enemies, loot, and secrets based on densities and archetypes
	_place_features(room, plan_dict, rng)

	return room


## Safely normalizes an invalid, wrong-typed, or extreme plan dictionary in place.
## Guarantees that every field consumed subsequently has a strictly valid type and bounded value.
static func _normalize_plan_dict(dict: Dictionary, diagnostics: Array[String]) -> void:
	# 1. room_id
	if not dict.has("room_id") or not (dict["room_id"] is String) or dict["room_id"].is_empty():
		dict["room_id"] = "normalized-room"
		diagnostics.append("Normalized room_id to 'normalized-room'")
	else:
		var raw_id: String = String(dict["room_id"])
		var clean_id := ""
		for i in range(raw_id.length()):
			var c := raw_id[i]
			var is_alnum := (c >= "a" and c <= "z") or (c >= "A" and c <= "Z") or (c >= "0" and c <= "9")
			if i == 0:
				clean_id += c if is_alnum else "r"
			else:
				clean_id += c if (is_alnum or c == "_" or c == "." or c == "-") else "-"
		if clean_id.length() > DungeonContracts.ID_MAX_LENGTH:
			clean_id = clean_id.substr(0, DungeonContracts.ID_MAX_LENGTH)
		if clean_id.is_empty():
			clean_id = "normalized-room"
		dict["room_id"] = clean_id

	# 2. depth
	if not dict.has("depth") or not ((dict["depth"] is int) or (dict["depth"] is float)):
		dict["depth"] = 1
		diagnostics.append("Normalized missing/invalid depth to 1")
	else:
		dict["depth"] = clampi(int(dict["depth"]), DungeonContracts.DEPTH_MIN, DungeonContracts.DEPTH_MAX)

	# 3. room_type
	if not dict.has("room_type") or not (dict["room_type"] is String) or not (dict["room_type"] in ROOM_TYPES):
		dict["room_type"] = "room"
		diagnostics.append("Normalized room_type to 'room'")

	# 4. size
	if not dict.has("size") or not (dict["size"] is String) or not (dict["size"] in ROOM_SIZES):
		dict["size"] = "medium"
		diagnostics.append("Normalized size to 'medium'")

	# 5. danger
	if not dict.has("danger") or not ((dict["danger"] is int) or (dict["danger"] is float)):
		dict["danger"] = 1
	else:
		dict["danger"] = clampi(int(dict["danger"]), DungeonContracts.DANGER_MIN, DungeonContracts.DANGER_MAX)

	# 6. densities & probabilities
	for field in ["enemy_density", "loot_density", "secret_probability"]:
		if not dict.has(field) or not ((dict[field] is int) or (dict[field] is float)):
			dict[field] = 0.0
		else:
			dict[field] = clampf(float(dict[field]), 0.0, 1.0)

	# 7. has_secret & secret_probability contract relationship
	if dict.has("has_secret"):
		if not (dict["has_secret"] is bool):
			dict.erase("has_secret")
		elif dict["has_secret"] == true:
			if float(dict.get("secret_probability", 0.0)) <= 0.0:
				dict["secret_probability"] = 0.5

	# 8. exits (defense against non-Array, wrong-type elements, duplicate directions, max 8)
	if not dict.has("exits") or not (dict["exits"] is Array):
		dict["exits"] = []
		diagnostics.append("Normalized non-array exits to empty array")
	else:
		var raw_list: Array = dict["exits"]
		var valid_exits: Array = []
		var seen_dirs: Dictionary = {}
		for item in raw_list:
			if not (item is Dictionary):
				diagnostics.append("Dropped non-dict exit item")
				continue
			var dir_val = item.get("direction", null)
			if not (dir_val is String) or not (dir_val in EXIT_DIRECTIONS):
				diagnostics.append("Dropped exit with invalid direction: %s" % str(dir_val))
				continue
			var dir_str: String = String(dir_val)
			if seen_dirs.has(dir_str):
				diagnostics.append("Dropped duplicate exit direction: %s" % dir_str)
				continue
			seen_dirs[dir_str] = true

			var kind_val = item.get("kind", "door")
			var kind_str: String = String(kind_val) if (kind_val is String and kind_val in EXIT_KINDS) else "door"
			var locked_val = item.get("locked", false)
			var locked_bool: bool = bool(locked_val) if (locked_val is bool) else false

			valid_exits.append({
				"direction": dir_str,
				"kind": kind_str,
				"locked": locked_bool
			})
			if valid_exits.size() >= DungeonContracts.MAX_EXITS:
				break
		dict["exits"] = valid_exits

	# 9. environmental_tags (must be Array of valid enum strings, max 8)
	if dict.has("environmental_tags"):
		if not (dict["environmental_tags"] is Array or dict["environmental_tags"] is PackedStringArray):
			dict.erase("environmental_tags")
		else:
			var valid_tags: Array = []
			for tag in dict["environmental_tags"]:
				if (tag is String) and (tag in DungeonContracts.ENVIRONMENTAL_TAGS):
					valid_tags.append(tag)
					if valid_tags.size() >= DungeonContracts.MAX_ENVIRONMENTAL_TAGS:
						break
			dict["environmental_tags"] = valid_tags

	# 10. description (must be String 1..200 chars)
	if dict.has("description"):
		if not (dict["description"] is String) or dict["description"].is_empty():
			dict.erase("description")
		elif dict["description"].length() > DungeonContracts.MAX_DESCRIPTION_LENGTH:
			dict["description"] = dict["description"].substr(0, DungeonContracts.MAX_DESCRIPTION_LENGTH)

	# 11. Remove any unknown keys that would violate contract validation
	var allowed_keys := ["room_id", "depth", "room_type", "size", "danger", "exits", "enemy_density", "loot_density", "secret_probability", "has_secret", "environmental_tags", "description"]
	var current_keys := dict.keys()
	for k in current_keys:
		if not (k in allowed_keys):
			dict.erase(k)
			diagnostics.append("Removed unknown plan key '%s'" % str(k))


static func _generate_fallback_room(room: GeneratedRoom, fallback_id: String, seed_val: int) -> GeneratedRoom:
	var dims = SIZE_DIMENSIONS["small"]
	room.room_id = fallback_id
	room.width = dims.x
	room.height = dims.y
	room.seed_used = seed_val
	for y in range(room.height):
		for x in range(room.width):
			if x == 0 or x == room.width - 1 or y == 0 or y == room.height - 1:
				room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.WALL
			else:
				room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.FLOOR
	room.player_spawn = Vector2i(room.width / 2, room.height / 2)
	return room


static func _carve_archetype(room: GeneratedRoom, room_type: String, rng: RandomNumberGenerator) -> void:
	# Initialize all with WALL
	for y in range(room.height):
		for x in range(room.width):
			room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.WALL

	match room_type:
		"room":
			_carve_standard_room(room, rng)
		"corridor":
			_carve_corridor_archetype(room, rng)
		"cavern":
			_carve_cavern_archetype(room, rng)
		"chamber":
			_carve_chamber_archetype(room)
		"vault":
			_carve_vault_archetype(room)
		"shrine":
			_carve_shrine_archetype(room)
		"treasure":
			_carve_treasure_archetype(room)
		"shop":
			_carve_shop_archetype(room, rng)
		"entrance":
			_carve_entrance_archetype(room)
		"stairs_down", "stairs_up":
			_carve_stairs_archetype(room, room_type)
		_:
			_carve_standard_room(room, rng)


static func _carve_standard_room(room: GeneratedRoom, rng: RandomNumberGenerator) -> void:
	# Plain rooms retain a broad usable interior, but clipped corners keep even
	# tiny/small rooms from presenting as the same featureless rectangle.
	_carve_chamfered_interior(room, 1 if room.width <= 9 else 2)
	var mid_x := room.width / 2
	var mid_y := room.height / 2
	match rng.randi_range(0, 2):
		1:
			# Opposed wall niches make one seed read differently from the next.
			room.tiles[Vector2i(2, mid_y - 1)] = GeneratedRoom.TileType.WALL
			room.tiles[Vector2i(room.width - 3, mid_y + 1)] = GeneratedRoom.TileType.WALL
		2:
			# A diagonal pillar pair leaves every side connected through the center.
			room.tiles[Vector2i(mid_x - 1, mid_y - 1)] = GeneratedRoom.TileType.WALL
			room.tiles[Vector2i(mid_x + 1, mid_y + 1)] = GeneratedRoom.TileType.WALL


static func _fill_interior(room: GeneratedRoom) -> void:
	for y in range(1, room.height - 1):
		for x in range(1, room.width - 1):
			room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.FLOOR


static func _carve_chamfered_interior(room: GeneratedRoom, depth: int) -> void:
	_fill_interior(room)
	for y in range(1, depth + 1):
		for x in range(1, depth + 1):
			if x + y > depth + 1:
				continue
			room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.WALL
			room.tiles[Vector2i(room.width - 1 - x, y)] = GeneratedRoom.TileType.WALL
			room.tiles[Vector2i(x, room.height - 1 - y)] = GeneratedRoom.TileType.WALL
			room.tiles[Vector2i(room.width - 1 - x, room.height - 1 - y)] = GeneratedRoom.TileType.WALL


static func _carve_chamber_archetype(room: GeneratedRoom) -> void:
	# Worked chambers are cruciform halls with broad central sight lines.
	var mid_x := room.width / 2
	var mid_y := room.height / 2
	var half_band := 1 if mini(room.width, room.height) <= 9 else 2
	for y in range(1, room.height - 1):
		for x in range(1, room.width - 1):
			if absi(x - mid_x) <= half_band or absi(y - mid_y) <= half_band:
				room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.FLOOR


static func _carve_corridor_archetype(room: GeneratedRoom, rng: RandomNumberGenerator) -> void:
	# Narrow corridor with possible bend or central channel
	var mid_y := room.height / 2
	var mid_x := room.width / 2

	# Main horizontal spine
	for x in range(1, room.width - 1):
		room.tiles[Vector2i(x, mid_y)] = GeneratedRoom.TileType.FLOOR
		# Corridor width of 2 for ease of traversal if height >= 7
		if room.height >= 7:
			room.tiles[Vector2i(x, mid_y - 1)] = GeneratedRoom.TileType.FLOOR

	# Vertical spine connecting across
	for y in range(1, room.height - 1):
		room.tiles[Vector2i(mid_x, y)] = GeneratedRoom.TileType.FLOOR
		if room.width >= 9:
			room.tiles[Vector2i(mid_x + 1, y)] = GeneratedRoom.TileType.FLOOR


static func _carve_cavern_archetype(room: GeneratedRoom, rng: RandomNumberGenerator) -> void:
	# Organic cellular/rounded shape: carve oval and perturb edges
	var rx := float(room.width - 2) / 2.0
	var ry := float(room.height - 2) / 2.0
	var cx := float(room.width - 1) / 2.0
	var cy := float(room.height - 1) / 2.0

	for y in range(1, room.height - 1):
		for x in range(1, room.width - 1):
			var dx := (float(x) - cx) / rx
			var dy := (float(y) - cy) / ry
			var dist_sq := dx * dx + dy * dy
			# deterministic noise perturbation
			var noise_val := (rng.randf() - 0.5) * 0.35
			if dist_sq + noise_val <= 0.85:
				room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.FLOOR

	# Always ensure center cross is open so cavern is not empty or disconnected
	var mid_x := room.width / 2
	var mid_y := room.height / 2
	for x in range(2, room.width - 2):
		room.tiles[Vector2i(x, mid_y)] = GeneratedRoom.TileType.FLOOR
	for y in range(2, room.height - 2):
		room.tiles[Vector2i(mid_x, y)] = GeneratedRoom.TileType.FLOOR


static func _carve_vault_archetype(room: GeneratedRoom) -> void:
	# A protected inner rectangle reached through four narrow approaches.
	var mid_x := room.width / 2
	var mid_y := room.height / 2
	for y in range(2, room.height - 2):
		for x in range(2, room.width - 2):
			room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.FLOOR
	for x in range(1, room.width - 1):
		room.tiles[Vector2i(x, mid_y)] = GeneratedRoom.TileType.FLOOR
	for y in range(1, room.height - 1):
		room.tiles[Vector2i(mid_x, y)] = GeneratedRoom.TileType.FLOOR


static func _carve_shrine_archetype(room: GeneratedRoom) -> void:
	# A diamond sanctuary with four stones framing its open altar.
	var mid_x := room.width / 2
	var mid_y := room.height / 2
	var radius_x := maxf(1.0, float(room.width - 3) / 2.0)
	var radius_y := maxf(1.0, float(room.height - 3) / 2.0)
	for y in range(1, room.height - 1):
		for x in range(1, room.width - 1):
			var diamond_distance := absf(float(x - mid_x)) / radius_x + absf(float(y - mid_y)) / radius_y
			if diamond_distance <= 1.05:
				room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.FLOOR
	for dx in [-1, 1]:
		for dy in [-1, 1]:
			room.tiles[Vector2i(mid_x + dx, mid_y + dy)] = GeneratedRoom.TileType.WALL


static func _carve_treasure_archetype(room: GeneratedRoom) -> void:
	# An octagonal trove with staggered inner guards around the centre.
	_carve_chamfered_interior(room, 1 if room.width <= 9 else 2)
	var mid_x := room.width / 2
	var mid_y := room.height / 2
	var pillar_offsets: Array[Vector2i] = [
		Vector2i(-2, -1), Vector2i(2, -1), Vector2i(-2, 1), Vector2i(2, 1)
	]
	for offset: Vector2i in pillar_offsets:
		var pos: Vector2i = Vector2i(mid_x, mid_y) + offset
		if pos.x > 1 and pos.x < room.width - 2 and pos.y > 1 and pos.y < room.height - 2:
			room.tiles[pos] = GeneratedRoom.TileType.WALL


static func _carve_shop_archetype(room: GeneratedRoom, rng: RandomNumberGenerator) -> void:
	# Shops use an L-shaped footprint. Mirroring the long aisle by seed gives
	# repeat visits variation while keeping the counter area obvious.
	var mid_y := room.height / 2
	var aisle_on_left := rng.randi_range(0, 1) == 0
	for y in range(1, room.height - 1):
		for x in range(1, room.width - 1):
			var in_top_room := y <= mid_y
			var in_aisle := x <= room.width / 2 if aisle_on_left else x >= room.width / 2
			if in_top_room or in_aisle:
				room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.FLOOR


static func _carve_entrance_archetype(room: GeneratedRoom) -> void:
	# Entrance room is spacious, with clipped corners marking it as a hall.
	_carve_chamfered_interior(room, 2 if room.width >= 9 else 1)


static func _carve_stairs_archetype(room: GeneratedRoom, room_type: String) -> void:
	# Round stair landings contrast with both the diamond shrine and box rooms.
	var mid_x := room.width / 2
	var mid_y := room.height / 2
	var rx := maxf(1.0, float(room.width - 3) / 2.0)
	var ry := maxf(1.0, float(room.height - 3) / 2.0)
	for y in range(1, room.height - 1):
		for x in range(1, room.width - 1):
			var dx := float(x - mid_x) / rx
			var dy := float(y - mid_y) / ry
			if dx * dx + dy * dy <= 1.0:
				room.tiles[Vector2i(x, y)] = GeneratedRoom.TileType.FLOOR
	var stair_tile := GeneratedRoom.TileType.STAIRS_DOWN if room_type == "stairs_down" else GeneratedRoom.TileType.STAIRS_UP
	room.tiles[Vector2i(mid_x, mid_y)] = stair_tile


static func _place_exits(room: GeneratedRoom, exit_defs: Array, rng: RandomNumberGenerator) -> void:
	var mid_x := room.width / 2
	var mid_y := room.height / 2

	# Check if both "up" and "down" exits are requested so they can be given distinct positions
	var has_up := false
	var has_down := false
	for exit_dict in exit_defs:
		if exit_dict is Dictionary:
			var dir_str: String = String(exit_dict.get("direction", ""))
			if dir_str == "up":
				has_up = true
			elif dir_str == "down":
				has_down = true

	var both_vertical: bool = has_up and has_down

	for exit_dict in exit_defs:
		if not (exit_dict is Dictionary):
			continue
		var direction: String = String(exit_dict.get("direction", ""))
		var kind: String = String(exit_dict.get("kind", "door"))
		var locked: bool = bool(exit_dict.get("locked", false))

		var exit_pos := Vector2i(-1, -1)
		var lead_in := Vector2i(-1, -1)

		match direction:
			"north":
				exit_pos = Vector2i(mid_x, 0)
				lead_in = Vector2i(mid_x, 1)
			"south":
				exit_pos = Vector2i(mid_x, room.height - 1)
				lead_in = Vector2i(mid_x, room.height - 2)
			"east":
				exit_pos = Vector2i(room.width - 1, mid_y)
				lead_in = Vector2i(room.width - 2, mid_y)
			"west":
				exit_pos = Vector2i(0, mid_y)
				lead_in = Vector2i(1, mid_y)
			"up":
				# If both up and down exits exist, offset up to west of center
				if both_vertical:
					exit_pos = Vector2i(maxi(1, mid_x - 1), mid_y)
				else:
					exit_pos = Vector2i(mid_x, mid_y)
				lead_in = exit_pos
				room.tiles[exit_pos] = GeneratedRoom.TileType.STAIRS_UP
				# Ensure tunnel to center
				_tunnel(room, exit_pos, Vector2i(mid_x, mid_y))
				room.exits.append({
					"direction": direction,
					"kind": "stairs",
					"locked": locked,
					"pos": exit_pos
				})
				continue
			"down":
				# If both up and down exits exist, offset down to east of center
				if both_vertical:
					exit_pos = Vector2i(mini(room.width - 2, mid_x + 1), mid_y)
				else:
					exit_pos = Vector2i(mid_x, mid_y)
				lead_in = exit_pos
				room.tiles[exit_pos] = GeneratedRoom.TileType.STAIRS_DOWN
				# Ensure tunnel to center
				_tunnel(room, exit_pos, Vector2i(mid_x, mid_y))
				room.exits.append({
					"direction": direction,
					"kind": "stairs",
					"locked": locked,
					"pos": exit_pos
				})
				continue
			_:
				continue

		# Place tile at wall boundary
		var tile_val: int = GeneratedRoom.TileType.DOOR_CLOSED
		if kind == "passage":
			tile_val = GeneratedRoom.TileType.FLOOR
		elif kind == "stairs":
			tile_val = GeneratedRoom.TileType.STAIRS_DOWN
		elif kind == "secret":
			tile_val = GeneratedRoom.TileType.SECRET_DOOR
		else: # "door"
			tile_val = GeneratedRoom.TileType.DOOR_CLOSED

		room.tiles[exit_pos] = tile_val

		# Ensure the tile right inside the exit is walkable floor
		room.tiles[lead_in] = GeneratedRoom.TileType.FLOOR

		# Tunnel corridor from lead_in to room center (mid_x, mid_y) to guarantee pathing
		_tunnel(room, lead_in, Vector2i(mid_x, mid_y))

		room.exits.append({
			"direction": direction,
			"kind": kind,
			"locked": locked,
			"pos": exit_pos
		})


static func _tunnel(room: GeneratedRoom, from_pos: Vector2i, to_pos: Vector2i) -> void:
	var curr := from_pos
	while curr.x != to_pos.x:
		# Do not overwrite stairs or doors when tunneling
		if room.tiles.get(curr, GeneratedRoom.TileType.WALL) == GeneratedRoom.TileType.WALL:
			room.tiles[curr] = GeneratedRoom.TileType.FLOOR
		curr.x += 1 if to_pos.x > curr.x else -1
	while curr.y != to_pos.y:
		if room.tiles.get(curr, GeneratedRoom.TileType.WALL) == GeneratedRoom.TileType.WALL:
			room.tiles[curr] = GeneratedRoom.TileType.FLOOR
		curr.y += 1 if to_pos.y > curr.y else -1
	if room.tiles.get(curr, GeneratedRoom.TileType.WALL) == GeneratedRoom.TileType.WALL:
		room.tiles[curr] = GeneratedRoom.TileType.FLOOR


## Ensures all walkable/floor tiles are in a single connected component.
## Fills small isolated pockets and links exits to main component.
static func _ensure_connectivity(room: GeneratedRoom) -> void:
	var mid := Vector2i(room.width / 2, room.height / 2)
	# Find a floor seed tile near center
	var seed_tile := Vector2i(-1, -1)
	if room.tiles.get(mid, GeneratedRoom.TileType.WALL) == GeneratedRoom.TileType.FLOOR:
		seed_tile = mid
	else:
		# Search nearest floor tile
		var best_dist := 999999.0
		for pos: Vector2i in room.tiles.keys():
			if room.tiles[pos] == GeneratedRoom.TileType.FLOOR:
				var d := mid.distance_squared_to(pos)
				if d < best_dist:
					best_dist = d
					seed_tile = pos

	if seed_tile == Vector2i(-1, -1):
		# No floor tiles at all?! Carve center
		room.tiles[mid] = GeneratedRoom.TileType.FLOOR
		seed_tile = mid

	# BFS from seed_tile to find main component
	var visited := {}
	var queue: Array[Vector2i] = [seed_tile]
	visited[seed_tile] = true

	var dirs = [Vector2i.UP, Vector2i.DOWN, Vector2i.LEFT, Vector2i.RIGHT]

	while not queue.is_empty():
		var curr: Vector2i = queue.pop_front()
		for d: Vector2i in dirs:
			var n: Vector2i = curr + d
			if room.tiles.has(n) and not visited.has(n):
				var t: int = room.tiles[n]
				# Traversable check: floor, door, stairs, secret
				if t != GeneratedRoom.TileType.WALL:
					visited[n] = true
					queue.append(n)

	# Any floor tiles not reached are disconnected pockets; either tunnel them or fill them
	for pos: Vector2i in room.tiles.keys():
		var t: int = room.tiles[pos]
		if t == GeneratedRoom.TileType.FLOOR and not visited.has(pos):
			# Tunnel from disconnected tile to nearest visited tile
			var nearest_visited := seed_tile
			var min_d := 999999.0
			for vpos: Vector2i in visited.keys():
				var d := (pos - vpos).length_squared()
				if d < min_d:
					min_d = d
					nearest_visited = vpos
			_tunnel(room, pos, nearest_visited)


static func _place_player_spawn(room: GeneratedRoom, exit_defs: Array, rng: RandomNumberGenerator) -> void:
	# Priority 1: If there is an entrance, stairs_up, or south exit, spawn near it
	# Priority 2: Room center or nearest floor to center
	var candidate_pos := Vector2i(room.width / 2, room.height / 2)

	# If south exit exists, spawn 1-2 tiles above south door
	for ex in room.exits:
		if ex.direction == "south":
			candidate_pos = ex.pos + Vector2i.UP
			break
		elif ex.direction == "up":
			# Spawn adjacent to up stairs
			candidate_pos = ex.pos + Vector2i.RIGHT
			if not room.is_walkable_spawn(candidate_pos):
				candidate_pos = ex.pos + Vector2i.LEFT
			if not room.is_walkable_spawn(candidate_pos):
				candidate_pos = ex.pos + Vector2i.UP
			break

	# Check if candidate_pos is floor
	if room.is_walkable_spawn(candidate_pos):
		room.player_spawn = candidate_pos
		return

	# Otherwise find closest floor tile to candidate_pos
	var best_tile := Vector2i(-1, -1)
	var best_dist := 999999.0
	for pos: Vector2i in room.tiles.keys():
		if room.is_walkable_spawn(pos):
			var d := candidate_pos.distance_squared_to(pos)
			if d < best_dist:
				best_dist = d
				best_tile = pos

	room.player_spawn = best_tile


static func _place_features(room: GeneratedRoom, plan_dict: Dictionary, rng: RandomNumberGenerator) -> void:
	# Collect all candidate spawn floor tiles (excluding player_spawn and immediate exit tiles)
	var reserved_tiles: Dictionary = {
		room.player_spawn: true
	}
	for ex in room.exits:
		reserved_tiles[ex.pos] = true
		# Reserve tile adjacent to exit so doors aren't immediately blocked
		for d in [Vector2i.UP, Vector2i.DOWN, Vector2i.LEFT, Vector2i.RIGHT]:
			reserved_tiles[ex.pos + d] = true

	var floor_tiles: Array[Vector2i] = []
	for pos: Vector2i in room.tiles.keys():
		if room.tiles[pos] == GeneratedRoom.TileType.FLOOR and not reserved_tiles.has(pos):
			floor_tiles.append(pos)

	# Shuffle candidate floor tiles deterministically
	_shuffle_array(floor_tiles, rng)

	var total_floors := floor_tiles.size()
	if total_floors == 0:
		return

	# 1. Enemies
	var enemy_density: float = float(plan_dict.get("enemy_density", 0.0))
	var danger: int = int(plan_dict.get("danger", 1))
	var room_type: String = String(plan_dict.get("room_type", "room"))

	# Peaceful room types have 0 enemies unless forced
	var peaceful_types := ["entrance", "shop", "stairs_up", "shrine"]
	var max_enemies := 0
	if not (room_type in peaceful_types):
		# Scale count by density: e.g. 0.1 on 40 tiles = 4 enemies max
		max_enemies = int(round(enemy_density * float(total_floors) * 0.4))
		# Bound enemies sensibly (0..8)
		max_enemies = clampi(max_enemies, 0, 8)
		if enemy_density > 0.0 and max_enemies == 0 and total_floors > 4:
			max_enemies = 1

	for i in range(max_enemies):
		if floor_tiles.is_empty():
			break
		var pos: Vector2i = floor_tiles.pop_back()
		# Determine enemy archetype based on danger and rng
		# danger >= 3 or rng roll spawns orc, otherwise goblin
		var enemy_type := "goblin"
		if danger >= 3 and rng.randf() < (float(danger) * 0.25):
			enemy_type = "orc"
		elif danger >= 2 and rng.randf() < 0.3:
			enemy_type = "orc"
		room.enemies.append({
			"type": enemy_type,
			"pos": pos
		})

	# 2. Loot / Items
	var loot_density: float = float(plan_dict.get("loot_density", 0.0))
	var max_items := int(round(loot_density * float(total_floors) * 0.3))
	max_items = clampi(max_items, 0, 6)
	if loot_density > 0.0 and max_items == 0 and total_floors > 4:
		max_items = 1

	# Treasure and shop rooms boost loot
	if room_type == "treasure":
		max_items = clampi(max_items + 2, 2, 8)

	for i in range(max_items):
		if floor_tiles.is_empty():
			break
		var pos: Vector2i = floor_tiles.pop_back()
		# Archetypes: potion_health or sword_upgrade
		var item_type := "potion_health"
		if rng.randf() < 0.35 or room_type == "treasure":
			item_type = "sword_upgrade"
		room.items.append({
			"type": item_type,
			"pos": pos
		})

	# 3. Secrets
	var secret_prob: float = float(plan_dict.get("secret_probability", 0.0))
	var has_secret: Variant = plan_dict.get("has_secret", null)
	var spawn_secret := false
	if has_secret == true:
		spawn_secret = true
	elif has_secret == null and secret_prob > 0.0:
		spawn_secret = rng.randf() < secret_prob

	if spawn_secret and not floor_tiles.is_empty():
		var pos: Vector2i = floor_tiles.pop_back()
		room.secrets.append({
			"type": "hidden_cache",
			"pos": pos
		})
		# A secret hidden cache can also place a bonus item
		# Place bonus item on secret cache position; note: in secret cache, this represents the cache loot
		room.items.append({
			"type": "sword_upgrade" if rng.randf() < 0.5 else "potion_health",
			"pos": pos
		})


static func _shuffle_array(arr: Array, rng: RandomNumberGenerator) -> void:
	for i in range(arr.size() - 1, 0, -1):
		var j := rng.randi_range(0, i)
		var tmp = arr[i]
		arr[i] = arr[j]
		arr[j] = tmp
