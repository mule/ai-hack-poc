class_name MainGame
extends Node2D

const GameState = preload("res://src/game_state.gd")
const DungeonRenderer = preload("res://src/dungeon_renderer.gd")
const GameUI = preload("res://src/game_ui.gd")
const GenerationClient = preload("res://world/generation_client.gd")
const GenerationCoordinator = preload("res://world/generation_coordinator.gd")
const OfflineTransport = preload("res://world/offline_transport.gd")

# Director endpoint. Provider/model ids are optional and stable; the game has
# no provider-specific logic. Environment variables (desktop / CI):
#   DUNGEON_DIRECTOR_URL       base URL, or "offline" to always use local rules
#   DUNGEON_DIRECTOR_PROVIDER  optional provider id      (default: director's)
#   DUNGEON_DIRECTOR_MODEL     optional model id         (default: provider's)
#   DUNGEON_DIRECTOR_TIMEOUT   request timeout in seconds (default 5)
const DEFAULT_DIRECTOR_URL := "http://127.0.0.1:8000"
const DEFAULT_TIMEOUT_SEC := 5.0

@onready var renderer: Node2D = $DungeonRenderer
@onready var ui: Control = $CanvasLayer/UI
@onready var camera: Camera2D = $Camera2D

var state: RefCounted
## Transport for deferred generation (submit/poll/cancel_all). Tests may set
## this before the node enters the tree; otherwise it is built from the
## environment in _ready().
var generation_transport: Variant = null
var coordinator: RefCounted = null
var _seen_revision: int = -1

func _ready() -> void:
	if state == null:
		state = GameState.new()
		state.enable_dynamic_world(1)
	renderer.game_state = state
	_setup_generation()
	
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

func _setup_generation() -> void:
	if not state.dynamic_world:
		return
	if generation_transport == null:
		generation_transport = make_transport()
	coordinator = GenerationCoordinator.new(state, generation_transport)
	coordinator.provider = OS.get_environment("DUNGEON_DIRECTOR_PROVIDER").strip_edges()
	coordinator.model = OS.get_environment("DUNGEON_DIRECTOR_MODEL").strip_edges()
	coordinator.timeout_msec = int(coordinator_timeout_sec() * 1000.0)

func _exit_tree() -> void:
	if coordinator != null:
		coordinator.shutdown()

func make_transport() -> Variant:
	var url := OS.get_environment("DUNGEON_DIRECTOR_URL").strip_edges()
	if url == "":
		url = DEFAULT_DIRECTOR_URL
	if url.to_lower() in ["offline", "off", "none"]:
		return OfflineTransport.new()
	var client := GenerationClient.new()
	client.base_url = url
	client.timeout_sec = coordinator_timeout_sec()
	add_child(client)
	return client

func coordinator_timeout_sec() -> float:
	var raw := OS.get_environment("DUNGEON_DIRECTOR_TIMEOUT")
	return float(raw) if raw != "" else DEFAULT_TIMEOUT_SEC

## Generation is driven from here, once per frame. update() never blocks:
## requests complete later via signals, so the current room stays playable.
func _process(_delta: float) -> void:
	if coordinator == null:
		return
	coordinator.update()
	if state.world != null and state.world.revision != _seen_revision:
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
	if state.world != null:
		_seen_revision = state.world.revision
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
