class_name GeneratedRoom
extends RefCounted
## Output of RoomGenerator containing tile layout, entities, and connectivity info.

const TileType = {
	FLOOR = 0,
	WALL = 1,
	DOOR_CLOSED = 2,
	DOOR_OPEN = 3,
	STAIRS_DOWN = 4,
	STAIRS_UP = 5,
	SECRET_DOOR = 6, # Wall outwardly or hidden door
}

var room_id: String = ""
var width: int = 0
var height: int = 0
var seed_used: int = 0

# Map of Vector2i -> int (TileType)
var tiles: Dictionary = {}

# Exits: Array of Dicts:
# { "direction": String, "kind": String, "locked": bool, "pos": Vector2i }
var exits: Array[Dictionary] = []

# Spawn points
var player_spawn: Vector2i = Vector2i(-1, -1)

# Entities to spawn:
# Array of Dicts: { "type": String, "pos": Vector2i }
var enemies: Array[Dictionary] = []

# Items to spawn:
# Array of Dicts: { "type": String, "pos": Vector2i }
var items: Array[Dictionary] = []

# Secrets:
# Array of Dicts: { "type": String, "pos": Vector2i }
var secrets: Array[Dictionary] = []

# Validation / Diagnostic warnings or normalization notes
var diagnostics: Array[String] = []

func is_valid() -> bool:
	return player_spawn != Vector2i(-1, -1) and not tiles.is_empty()

func get_tile(pos: Vector2i) -> int:
	return tiles.get(pos, TileType.WALL)

func is_traversable(pos: Vector2i) -> bool:
	if not tiles.has(pos):
		return false
	var t: int = tiles[pos]
	return t == TileType.FLOOR or t == TileType.DOOR_OPEN or t == TileType.DOOR_CLOSED or t == TileType.STAIRS_DOWN or t == TileType.STAIRS_UP or t == TileType.SECRET_DOOR

func is_walkable_spawn(pos: Vector2i) -> bool:
	if not tiles.has(pos):
		return false
	var t: int = tiles[pos]
	return t == TileType.FLOOR

func to_dict() -> Dictionary:
	return {
		"room_id": room_id,
		"width": width,
		"height": height,
		"seed_used": seed_used,
		"player_spawn": player_spawn,
		"exits": exits,
		"enemies": enemies,
		"items": items,
		"secrets": secrets,
		"diagnostics": diagnostics
	}
