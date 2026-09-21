class_name DungeonRenderer
extends Node2D

const GameState = preload("res://src/game_state.gd")

const TILE_SIZE: int = 32

var game_state: RefCounted

# Visual styling
const COLOR_WALL = Color(0.2, 0.22, 0.25)
const COLOR_WALL_OUTLINE = Color(0.12, 0.13, 0.15)
const COLOR_FLOOR = Color(0.08, 0.09, 0.11)
const COLOR_FLOOR_GRID = Color(0.13, 0.14, 0.16)
const COLOR_DOOR_CLOSED = Color(0.72, 0.45, 0.2)
const COLOR_DOOR_OPEN = Color(0.68, 0.42, 0.18)
const COLOR_PLAYER = Color(0.2, 0.85, 0.4) # bright hero green
const COLOR_GOBLIN = Color(0.85, 0.3, 0.2) # red
const COLOR_ORC = Color(0.75, 0.2, 0.75)   # purple/magenta
const COLOR_ITEM_POTION = Color(0.2, 0.6, 1.0) # blue
const COLOR_ITEM_SWORD = Color(1.0, 0.85, 0.2) # gold/yellow
const COLOR_STAIRS = Color(0.55, 0.6, 0.7)

# Unresolved exits (deferred generation): a highlighted door plus a hatched
# "unknown" cell beyond it. Pending exits use a warmer colour.
const COLOR_FRONTIER_UNRESOLVED = Color(0.62, 0.4, 1.0) # violet
const COLOR_FRONTIER_PENDING = Color(1.0, 0.72, 0.2)    # amber
const COLOR_UNKNOWN_FILL = Color(0.10, 0.06, 0.18)

func frontier_color(status: String) -> Color:
	return COLOR_FRONTIER_PENDING if status == "pending" else COLOR_FRONTIER_UNRESOLVED

## One entry per open frontier: {door, unknown, status, direction}. `door` is
## the exit tile, `unknown` the not-yet-generated cell it leads into. _draw()
## renders exactly this list.
func frontier_markers() -> Array[Dictionary]:
	var markers: Array[Dictionary] = []
	if game_state == null or game_state.world == null:
		return markers
	for f in game_state.world.open_frontiers():
		if game_state.get_tile(f.pos) == GameState.TileType.WALL:
			continue
		markers.append({"door": f.pos, "unknown": f.outward, "status": f.status, "direction": f.direction})
	return markers

func _draw() -> void:
	if not game_state:
		return

	# Draw tiles
	for pos in game_state.map_tiles.keys():
		var tile_type: int = game_state.map_tiles[pos]
		var rect = Rect2(pos.x * TILE_SIZE, pos.y * TILE_SIZE, TILE_SIZE, TILE_SIZE)

		match tile_type:
			GameState.TileType.WALL, GameState.TileType.SECRET_DOOR:
				draw_rect(rect, COLOR_WALL)
				draw_rect(rect, COLOR_WALL_OUTLINE, false, 2.0)
			GameState.TileType.FLOOR:
				draw_rect(rect, COLOR_FLOOR)
				draw_rect(rect, COLOR_FLOOR_GRID, false, 1.0)
			GameState.TileType.DOOR_CLOSED:
				draw_rect(rect, COLOR_FLOOR)
				var door_rect = Rect2(pos.x * TILE_SIZE + 4, pos.y * TILE_SIZE + 4, TILE_SIZE - 8, TILE_SIZE - 8)
				draw_rect(door_rect, COLOR_DOOR_CLOSED)
				draw_rect(door_rect, Color.WHITE, false, 1.5)
				# Draw door lock symbol (+)
				draw_line(
					Vector2(pos.x * TILE_SIZE + 16, pos.y * TILE_SIZE + 10),
					Vector2(pos.x * TILE_SIZE + 16, pos.y * TILE_SIZE + 22),
					Color.WHITE, 2.0
				)
			GameState.TileType.DOOR_OPEN:
				draw_rect(rect, COLOR_FLOOR)
				_draw_open_door(pos, rect)
			GameState.TileType.STAIRS_DOWN, GameState.TileType.STAIRS_UP:
				draw_rect(rect, COLOR_FLOOR)
				for step in range(4):
					var y: int = pos.y * TILE_SIZE + 6 + step * 6
					draw_line(Vector2(pos.x * TILE_SIZE + 5, y), Vector2(pos.x * TILE_SIZE + TILE_SIZE - 5, y), COLOR_STAIRS, 2.0)

	_draw_frontiers()

	# Draw items
	for item in game_state.items:
		var ipos = item["pos"]
		var center = Vector2(ipos.x * TILE_SIZE + TILE_SIZE / 2.0, ipos.y * TILE_SIZE + TILE_SIZE / 2.0)
		if item["type"] == GameState.ItemType.HEALTH_POTION:
			# Draw potion (circle or diamond)
			draw_circle(center, 7.0, COLOR_ITEM_POTION)
			draw_circle(center, 7.0, Color.WHITE, false, 1.5)
			# Small cross inside
			draw_line(center - Vector2(3, 0), center + Vector2(3, 0), Color.WHITE, 1.5)
			draw_line(center - Vector2(0, 3), center + Vector2(0, 3), Color.WHITE, 1.5)
		elif item["type"] == GameState.ItemType.SWORD_BONUS:
			# Draw sword / blade symbol
			draw_circle(center, 7.0, COLOR_ITEM_SWORD)
			draw_circle(center, 7.0, Color.WHITE, false, 1.5)
			draw_line(center - Vector2(4, 4), center + Vector2(4, 4), Color.BLACK, 2.0)
		else:
			draw_circle(center, 6.0, Color.GOLD)

	# Draw enemies
	for enemy in game_state.enemies:
		if enemy["hp"] <= 0:
			continue
		var epos = enemy["pos"]
		var rect = Rect2(epos.x * TILE_SIZE + 4, epos.y * TILE_SIZE + 4, TILE_SIZE - 8, TILE_SIZE - 8)
		var color = COLOR_ORC if enemy["type"] == GameState.EnemyType.ORC else COLOR_GOBLIN
		draw_rect(rect, color)
		draw_rect(rect, Color.WHITE, false, 1.5)

		# Draw HP bar above enemy
		var hp_pct = float(enemy["hp"]) / float(enemy["max_hp"])
		var bar_bg = Rect2(epos.x * TILE_SIZE + 2, epos.y * TILE_SIZE - 3, TILE_SIZE - 4, 3)
		var bar_fg = Rect2(epos.x * TILE_SIZE + 2, epos.y * TILE_SIZE - 3, (TILE_SIZE - 4) * hp_pct, 3)
		draw_rect(bar_bg, Color(0.2, 0.2, 0.2))
		draw_rect(bar_fg, Color.RED)

	# Draw player
	var ppos = game_state.player_pos
	var prect = Rect2(ppos.x * TILE_SIZE + 3, ppos.y * TILE_SIZE + 3, TILE_SIZE - 6, TILE_SIZE - 6)
	if game_state.is_player_dead:
		# Draw tombstone or gray X
		draw_rect(prect, Color(0.5, 0.5, 0.5))
		draw_line(Vector2(ppos.x * TILE_SIZE + 4, ppos.y * TILE_SIZE + 4), Vector2((ppos.x + 1) * TILE_SIZE - 4, (ppos.y + 1) * TILE_SIZE - 4), Color.RED, 3.0)
		draw_line(Vector2((ppos.x + 1) * TILE_SIZE - 4, ppos.y * TILE_SIZE + 4), Vector2(ppos.x * TILE_SIZE + 4, (ppos.y + 1) * TILE_SIZE - 4), Color.RED, 3.0)
	else:
		draw_rect(prect, COLOR_PLAYER)
		draw_rect(prect, Color.WHITE, false, 2.0)
		# Eyes/indicator
		draw_circle(Vector2(ppos.x * TILE_SIZE + 11, ppos.y * TILE_SIZE + 12), 2.5, Color.BLACK)
		draw_circle(Vector2(ppos.x * TILE_SIZE + 21, ppos.y * TILE_SIZE + 12), 2.5, Color.BLACK)


## Keep an opened door legible after its frontier has committed. Fast hosted
## providers can resolve the room between the opening action and the player's
## next step, at which point the unknown-frontier highlight correctly goes
## away. The brighter frame and swung leaf make the remaining DOOR_OPEN tile
## visibly distinct from ordinary floor.
func _draw_open_door(pos: Vector2i, rect: Rect2) -> void:
	var direction := _door_direction(pos)
	draw_rect(rect.grow(-3.0), COLOR_DOOR_OPEN, false, 2.5)
	if direction in ["east", "west"]:
		# East/west travel crosses a vertical wall; show horizontal jambs and
		# a leaf swung along the room side of the doorway.
		draw_rect(Rect2(rect.position + Vector2(2, 2), Vector2(TILE_SIZE - 4, 5)), COLOR_DOOR_OPEN)
		draw_rect(Rect2(rect.position + Vector2(2, TILE_SIZE - 7), Vector2(TILE_SIZE - 4, 5)), COLOR_DOOR_OPEN)
		var leaf_x := 4.0 if direction == "east" else TILE_SIZE / 2.0
		draw_rect(Rect2(rect.position + Vector2(leaf_x, 5), Vector2(TILE_SIZE / 2.0 - 4, 4)), COLOR_DOOR_OPEN)
	else:
		# North/south travel crosses a horizontal wall.
		draw_rect(Rect2(rect.position + Vector2(2, 2), Vector2(5, TILE_SIZE - 4)), COLOR_DOOR_OPEN)
		draw_rect(Rect2(rect.position + Vector2(TILE_SIZE - 7, 2), Vector2(5, TILE_SIZE - 4)), COLOR_DOOR_OPEN)
		var leaf_y := 4.0 if direction == "south" else TILE_SIZE / 2.0
		draw_rect(Rect2(rect.position + Vector2(5, leaf_y), Vector2(4, TILE_SIZE / 2.0 - 4)), COLOR_DOOR_OPEN)


func _door_direction(pos: Vector2i) -> String:
	if game_state.world != null:
		for frontier in game_state.world.frontiers.values():
			if frontier.pos == pos:
				return str(frontier.direction)
	# Legacy/static maps have no frontier records. Infer the doorway axis from
	# the wall pair around it so their open doors receive the same treatment.
	var above: bool = game_state.get_tile(pos + Vector2i.UP) == GameState.TileType.WALL
	var below: bool = game_state.get_tile(pos + Vector2i.DOWN) == GameState.TileType.WALL
	return "east" if above and below else "north"


func _draw_frontiers() -> void:
	for marker in frontier_markers():
		var color := frontier_color(marker.status)
		var door: Vector2i = marker.door
		var unknown: Vector2i = marker.unknown
		# Door tile: coloured frame so the exit reads as "not yet explored".
		draw_rect(Rect2(door.x * TILE_SIZE, door.y * TILE_SIZE, TILE_SIZE, TILE_SIZE), color, false, 3.0)
		# Unknown cell: dark fill with diagonal hatching and a coloured border.
		var cell := Rect2(unknown.x * TILE_SIZE, unknown.y * TILE_SIZE, TILE_SIZE, TILE_SIZE)
		draw_rect(cell, COLOR_UNKNOWN_FILL)
		for i in range(1, 4):
			var offset := float(i * TILE_SIZE) / 4.0
			draw_line(cell.position + Vector2(offset, 0), cell.position + Vector2(0, offset), color * Color(1, 1, 1, 0.6), 1.5)
			draw_line(cell.position + Vector2(TILE_SIZE, offset), cell.position + Vector2(offset, TILE_SIZE), color * Color(1, 1, 1, 0.6), 1.5)
		draw_rect(cell, color, false, 2.0)
		# Arrow on the door pointing into the unknown.
		var dir_vec: Vector2 = Vector2(unknown - door)
		var center := Vector2(door.x * TILE_SIZE + TILE_SIZE / 2.0, door.y * TILE_SIZE + TILE_SIZE / 2.0)
		var side := Vector2(-dir_vec.y, dir_vec.x)
		draw_colored_polygon(PackedVector2Array([
			center + dir_vec * 9.0,
			center - dir_vec * 3.0 + side * 6.0,
			center - dir_vec * 3.0 - side * 6.0,
		]), color)
