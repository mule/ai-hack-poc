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
const COLOR_DOOR_OPEN = Color(0.4, 0.3, 0.2)
const COLOR_PLAYER = Color(0.2, 0.85, 0.4) # bright hero green
const COLOR_GOBLIN = Color(0.85, 0.3, 0.2) # red
const COLOR_ORC = Color(0.75, 0.2, 0.75)   # purple/magenta
const COLOR_ITEM_POTION = Color(0.2, 0.6, 1.0) # blue
const COLOR_ITEM_SWORD = Color(1.0, 0.85, 0.2) # gold/yellow

func _draw() -> void:
	if not game_state:
		return
		
	# Draw tiles
	for pos in game_state.map_tiles.keys():
		var tile_type: int = game_state.map_tiles[pos]
		var rect = Rect2(pos.x * TILE_SIZE, pos.y * TILE_SIZE, TILE_SIZE, TILE_SIZE)
		
		match tile_type:
			GameState.TileType.WALL:
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
				# Draw open door frame
				draw_rect(Rect2(pos.x * TILE_SIZE + 2, pos.y * TILE_SIZE + 2, 6, TILE_SIZE - 4), COLOR_DOOR_OPEN)
				draw_rect(Rect2(pos.x * TILE_SIZE + TILE_SIZE - 8, pos.y * TILE_SIZE + 2, 6, TILE_SIZE - 4), COLOR_DOOR_OPEN)
				
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
