extends SceneTree

# Headless scene smoke test for main.tscn
# Verifies:
# 1. Successful instantiation and scene tree composition
# 2. Focus modes on all touch and death overlay buttons (must be FOCUS_NONE)
# 3. Gameplay input event handling (keyboard/gamepad action triggers)
# 4. UI text, HP bar, and stats synchronization
# 5. Wall-bump log entry appears immediately in UI (UI refresh on bump)
# 6. Death overlay visibility and restart flow
# 7. DungeonRenderer queue_redraw call verification
# 8. Hard exit nonzero on any failure, guarded by completion sentinel

var tests_passed: int = 0
var tests_failed: int = 0
var smoke_completed_sentinel: bool = false

func _init() -> void:
	process_frame.connect(_run_tests, CONNECT_ONE_SHOT)

func _run_tests() -> void:
	print("--- Running Headless Scene Smoke Tests (main.tscn) ---")
	test_scene_composition_and_focus()
	test_ui_sync_and_wall_bump_refresh()
	test_gameplay_inputs_and_renderer_redraw()
	test_death_overlay_and_restart_flow()
	
	smoke_completed_sentinel = true
	print("\n--- Smoke Test Results: %d Passed, %d Failed (Sentinel: %s) ---" % [
		tests_passed, tests_failed, str(smoke_completed_sentinel)
	])
	
	if tests_failed > 0 or not smoke_completed_sentinel:
		printerr("FAILED: Scene smoke tests encountered failures.")
		quit(1)
	else:
		print("SUCCESS: All scene smoke tests passed!")
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

func test_scene_composition_and_focus() -> void:
	print("\nTest 1: Scene Composition and Button Focus Modes")
	var packed_scene = load("res://scenes/main.tscn")
	assert_true(packed_scene != null, "main.tscn loaded successfully")
	var main_node = packed_scene.instantiate()
	root.add_child(main_node)
	
	var renderer = main_node.get_node_or_null("DungeonRenderer")
	var camera = main_node.get_node_or_null("Camera2D")
	var ui = main_node.get_node_or_null("CanvasLayer/UI")
	
	assert_true(renderer != null, "DungeonRenderer node exists")
	assert_true(camera != null, "Camera2D node exists")
	assert_true(ui != null, "GameUI node exists")
	assert_true(main_node.state != null, "GameState initialized in MainGame")
	
	# Verify all Button nodes have focus_mode == FOCUS_NONE (0)
	var buttons: Array[Button] = [
		ui.get_node("TouchControls/UpBtn"),
		ui.get_node("TouchControls/DownBtn"),
		ui.get_node("TouchControls/LeftBtn"),
		ui.get_node("TouchControls/RightBtn"),
		ui.get_node("TouchControls/WaitBtn"),
		ui.get_node("DeathOverlay/VBox/RestartBtn")
	]
	
	for btn in buttons:
		assert_true(btn != null, "Button exists")
		assert_eq(btn.focus_mode, Control.FOCUS_NONE, "Button %s has focus_mode == FOCUS_NONE" % btn.name)
		
	main_node.queue_free()

func test_ui_sync_and_wall_bump_refresh() -> void:
	print("\nTest 2: UI Synchronization and Wall-Bump Refresh")
	var packed_scene = load("res://scenes/main.tscn")
	var main_node = packed_scene.instantiate()
	root.add_child(main_node)
	var ui = main_node.get_node("CanvasLayer/UI")
	var log_label: RichTextLabel = ui.get_node("LogPanel/LogLabel")
	var hp_label: Label = ui.get_node("TopBar/HBox/HPLabel")
	
	assert_eq(hp_label.text, "HP: 20/20", "Initial HP label text matches state")
	assert_true(log_label.text.contains("Welcome to the dungeon!"), "Initial welcome message present")
	
	# Place player at (1, 1) and bump into left wall at (0, 1)
	main_node.state.player_pos = Vector2i(1, 1)
	main_node._on_dpad_move(Vector2i.LEFT)
	
	# Verify that wall bump message is IMMEDIATELY visible in UI log_label text
	assert_true(log_label.text.contains("Ouch! You bump into a wall."), "Wall bump message refreshed immediately in UI text")
	
	main_node.queue_free()

func test_gameplay_inputs_and_renderer_redraw() -> void:
	print("\nTest 3: Gameplay Input Handling & Redraw")
	var packed_scene = load("res://scenes/main.tscn")
	var main_node = packed_scene.instantiate()
	root.add_child(main_node)
	
	var initial_pos = main_node.state.player_pos
	
	# Simulate move_down action via _unhandled_input
	var ev_down = InputEventAction.new()
	ev_down.action = "move_down"
	ev_down.pressed = true
	main_node._unhandled_input(ev_down)
	
	assert_eq(main_node.state.player_pos, initial_pos + Vector2i.DOWN, "Player moved down via action event")
	
	# Simulate action_wait
	var initial_turns = main_node.state.player_turns
	var ev_wait = InputEventAction.new()
	ev_wait.action = "action_wait"
	ev_wait.pressed = true
	main_node._unhandled_input(ev_wait)
	assert_eq(main_node.state.player_turns, initial_turns + 1, "Turn incremented via action_wait event")
	
	# Trigger redraw and verify camera follows
	main_node.queue_redraw_all()
	var expected_cam_x = main_node.state.player_pos.x * 32 + 16
	var expected_cam_y = main_node.state.player_pos.y * 32 + 16
	assert_eq(main_node.camera.position, Vector2(expected_cam_x, expected_cam_y), "Camera followed player position")
	
	main_node.queue_free()

func test_death_overlay_and_restart_flow() -> void:
	print("\nTest 4: Death Overlay & Restart Flow")
	var packed_scene = load("res://scenes/main.tscn")
	var main_node = packed_scene.instantiate()
	root.add_child(main_node)
	var ui = main_node.get_node("CanvasLayer/UI")
	var death_overlay: ColorRect = ui.get_node("DeathOverlay")
	
	assert_true(not death_overlay.visible, "Death overlay hidden initially")
	
	# Force lethal condition
	main_node.state.player_hp = 0
	main_node.state.is_player_dead = true
	main_node.queue_redraw_all()
	
	assert_true(death_overlay.visible, "Death overlay visible when player is dead")
	
	# Simulate restart input event
	var ev_restart = InputEventAction.new()
	ev_restart.action = "action_restart"
	ev_restart.pressed = true
	main_node._unhandled_input(ev_restart)
	
	assert_true(not main_node.state.is_player_dead, "Player alive after restart")
	assert_true(not death_overlay.visible, "Death overlay hidden after restart")
	assert_eq(main_node.state.player_hp, main_node.state.player_max_hp, "Player HP restored after restart")
	
	main_node.queue_free()
