class_name MainGame
extends Node2D

const GameState = preload("res://src/game_state.gd")
const DungeonRenderer = preload("res://src/dungeon_renderer.gd")
const GameUI = preload("res://src/game_ui.gd")

@onready var renderer: Node2D = $DungeonRenderer
@onready var ui: Control = $CanvasLayer/UI
@onready var camera: Camera2D = $Camera2D

var state: RefCounted

func _ready() -> void:
	if state == null:
		state = GameState.new()
	renderer.game_state = state
	
	if ui:
		if not ui.dpad_pressed.is_connected(_on_dpad_move):
			ui.dpad_pressed.connect(_on_dpad_move)
		if not ui.wait_pressed.is_connected(_on_wait):
			ui.wait_pressed.connect(_on_wait)
		if not ui.restart_pressed.is_connected(_on_restart):
			ui.restart_pressed.connect(_on_restart)
		ui.update_ui(state)
		
	_center_camera()
	queue_redraw_all()

func _unhandled_input(event: InputEvent) -> void:
	if event.is_action_pressed("action_restart"):
		_on_restart()
		if get_viewport():
			get_viewport().set_input_as_handled()
		return
		
	if state.is_player_dead:
		return
		
	if event.is_action_pressed("move_up"):
		_on_dpad_move(Vector2i.UP)
		if get_viewport():
			get_viewport().set_input_as_handled()
	elif event.is_action_pressed("move_down"):
		_on_dpad_move(Vector2i.DOWN)
		if get_viewport():
			get_viewport().set_input_as_handled()
	elif event.is_action_pressed("move_left"):
		_on_dpad_move(Vector2i.LEFT)
		if get_viewport():
			get_viewport().set_input_as_handled()
	elif event.is_action_pressed("move_right"):
		_on_dpad_move(Vector2i.RIGHT)
		if get_viewport():
			get_viewport().set_input_as_handled()
	elif event.is_action_pressed("action_wait"):
		_on_wait()
		if get_viewport():
			get_viewport().set_input_as_handled()

func _on_dpad_move(dir: Vector2i) -> void:
	var moved = state.player_action_step(dir)
	queue_redraw_all()

func _on_wait() -> void:
	if state.player_action_wait():
		queue_redraw_all()

func _on_restart() -> void:
	state.reset_game()
	queue_redraw_all()

func queue_redraw_all() -> void:
	if renderer:
		renderer.queue_redraw()
	if ui:
		ui.update_ui(state)
	_center_camera()

func _center_camera() -> void:
	if camera and state:
		var target_pos = Vector2(
			state.player_pos.x * DungeonRenderer.TILE_SIZE + DungeonRenderer.TILE_SIZE / 2.0,
			state.player_pos.y * DungeonRenderer.TILE_SIZE + DungeonRenderer.TILE_SIZE / 2.0
		)
		camera.position = target_pos
