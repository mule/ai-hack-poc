extends SceneTree
## Comprehensive headless test suite for Issue #12:
## - Runtime provider & model selector fetching GET /v1/config
## - Dynamic population of available providers and models
## - Handling unavailable providers with clear user feedback
## - Selection application to new runs and restarts
## - Offline transport fallback when config cannot be reached
## - Toggleable desktop and Android Debug HUD (keyboard shortcut ` / F3, touch button)
## - HUD display: current provider/model, generation status, latency, recent RoomPlan decisions,
##   and generic provider metadata including Jev probabilities and scores.

const DungeonContracts = preload("res://contracts/dungeon_contracts.gd")
const GameState = preload("res://src/game_state.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")
const GenerationCoordinator = preload("res://world/generation_coordinator.gd")
const ScriptedTransport = preload("res://tests/support/scripted_transport.gd")
const StubDirector = preload("res://tests/support/stub_director.gd")
const MiniHttpServer = preload("res://tests/support/mini_http_server.gd")
const ProviderSelector = preload("res://src/provider_selector.gd")
const DebugHUD = preload("res://src/debug_hud.gd")
const MainGame = preload("res://src/main_game.gd")
const OfflineTransport = preload("res://world/offline_transport.gd")

var _checks := 0
var _failures := PackedStringArray()
var _completed := false
var _reached_end := false


func _initialize() -> void:
	OS.set_environment("DUNGEON_DIRECTOR_URL", "offline")
	create_timer(60.0).timeout.connect(_on_watchdog)
	process_frame.connect(_run, CONNECT_ONE_SHOT)


func _on_watchdog() -> void:
	printerr("FAILED: test_provider_selector suite exceeded 60s watchdog")
	quit(2)


func _run() -> void:
	print("=== Running test_provider_selector.gd ===")
	await _step("test_selector_population_and_switching", _test_selector_population_and_switching)
	await _step("test_selector_handles_unavailable_provider", _test_selector_handles_unavailable_provider)
	await _step("test_selector_handles_offline_fallback", _test_selector_handles_offline_fallback)
	await _step("test_debug_hud_toggle_and_display", _test_debug_hud_toggle_and_display)
	await _step("test_debug_hud_metadata_and_jev_probabilities", _test_debug_hud_metadata_and_jev_probabilities)
	await _step("test_debug_hud_clears_stale_metadata_on_fallback", _test_debug_hud_clears_stale_metadata_on_fallback)
	await _step("test_main_scene_selector_and_hud_interaction", _test_main_scene_selector_and_hud_interaction)
	_completed = true
	_finish()


func _step(test_name: String, fn: Callable) -> void:
	_reached_end = false
	await fn.call()
	if not _reached_end:
		_failures.append("%s aborted before reaching its end" % test_name)
		printerr("  [FAIL] %s aborted before reaching its end" % test_name)


func _end() -> void:
	_reached_end = true


func _check(condition: bool, message: String) -> void:
	_checks += 1
	if condition:
		print("  [PASS] %s" % message)
	else:
		_failures.append(message)
		printerr("  [FAIL] %s" % message)


func _check_eq(actual: Variant, expected: Variant, message: String) -> void:
	_check(actual == expected, "%s (expected %s, got %s)" % [message, str(expected), str(actual)])


func _finish() -> void:
	print("\n--- Provider Selector Results: %d Passed, %d Failed (completed=%s) ---" % [_checks - _failures.size(), _failures.size(), str(_completed)])
	if _failures.is_empty() and _completed:
		print("SUCCESS: All provider selector and debug HUD checks passed!")
		quit(0)
	else:
		printerr("FAILED: provider selector checks did not pass.")
		quit(1)


# --- Tests -------------------------------------------------------------------


func _test_selector_population_and_switching() -> void:
	print("\nTest: ProviderSelector populates from config and changes selection")
	var transport := ScriptedTransport.new()
	var packed := load("res://scenes/main.tscn")
	var main: Node = packed.instantiate()
	root.add_child(main)

	var selector: ProviderSelector = main.provider_selector
	_check(selector != null, "ProviderSelector exists in scene")

	# Prepare sample director config
	var config_dict := {
		"default_provider": "rules-baseline",
		"default_model": "builtin-v1",
		"providers": [
			{
				"id": "rules-baseline",
				"available": true,
				"default_model": "builtin-v1",
				"models": ["builtin-v1"]
			},
			{
				"id": "groq",
				"available": true,
				"default_model": "openai/gpt-oss-20b",
				"models": ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]
			}
		]
	}
	transport.auto_config = {"transport_ok": true, "http_status": 200, "body": JSON.stringify(config_dict)}

	selector.open_selector(transport, "rules-baseline", "builtin-v1")
	transport.poll()

	_check(selector.is_selector_visible, "selector is visible when opened")
	_check_eq(selector.provider_option.item_count, 2, "2 providers populated")
	_check_eq(selector.provider_option.get_item_text(0), "rules-baseline", "provider 0 label")
	_check_eq(selector.provider_option.get_item_text(1), "groq", "provider 1 label")

	# Select groq
	selector._on_provider_selected(1)
	_check_eq(selector.model_option.item_count, 2, "groq models populated")
	_check_eq(selector.model_option.get_item_text(0), "openai/gpt-oss-20b", "default model selected")

	# Change model to 120b
	selector._on_model_selected(1)
	_check_eq(selector.pending_model, "openai/gpt-oss-120b", "pending model updated")

	# Apply selection
	var applied_signals := []
	selector.selection_applied.connect(func(p, m): applied_signals.append([p, m]))
	selector._on_apply_pressed()

	_check(not selector.is_selector_visible, "selector closed after apply")
	_check_eq(applied_signals.size(), 1, "selection_applied signal emitted")
	_check_eq(applied_signals[0], ["groq", "openai/gpt-oss-120b"], "correct provider and model emitted")

	main.queue_free()
	_end()


func _test_selector_handles_unavailable_provider() -> void:
	print("\nTest: ProviderSelector marks unavailable providers, disables Apply, and re-enables on available provider")
	var transport := ScriptedTransport.new()
	var packed := load("res://scenes/main.tscn")
	var main: Node = packed.instantiate()
	root.add_child(main)

	var selector: ProviderSelector = main.provider_selector
	var config_dict := {
		"default_provider": "rules-baseline",
		"default_model": "builtin-v1",
		"providers": [
			{
				"id": "rules-baseline",
				"available": true,
				"default_model": "builtin-v1",
				"models": ["builtin-v1"]
			},
			{
				"id": "cerebras",
				"available": false,
				"default_model": "qwen-3.8-27b",
				"models": ["qwen-3.8-27b"]
			}
		]
	}
	transport.auto_config = {"transport_ok": true, "http_status": 200, "body": JSON.stringify(config_dict)}
	selector.open_selector(transport, "rules-baseline", "builtin-v1")
	transport.poll()

	_check(selector.provider_option.get_item_text(1).contains("(unavailable)"), "unavailable marked in label")
	_check(not selector.apply_btn.disabled, "apply button initially enabled on available default provider")

	# Selecting unavailable provider updates warning status and disables Apply
	selector._on_provider_selected(1)
	_check(selector.status_label.text.contains("UNAVAILABLE"), "explicitly indicates UNAVAILABLE")
	_check(selector.status_label.text.contains("cannot be selected"), "explains cannot be selected")
	_check(selector.apply_btn.disabled, "apply button disabled for unavailable provider")
	var applied_signals := []
	selector.selection_applied.connect(func(p, m): applied_signals.append([p, m]))
	selector._on_apply_pressed()
	_check_eq(applied_signals.size(), 0, "unavailable provider cannot be applied programmatically")

	# Switching back to available provider re-enables Apply
	selector._on_provider_selected(0)
	_check(not selector.apply_btn.disabled, "apply button re-enabled on available provider")
	_check(selector.status_label.text.contains("Ready"), "status explains provider is ready")

	main.queue_free()
	_end()


func _test_selector_handles_offline_fallback() -> void:
	print("\nTest: ProviderSelector gracefully degrades to offline rules (rules-baseline/builtin-v1) when config fails")
	var transport := ScriptedTransport.new()
	var packed := load("res://scenes/main.tscn")
	var main: Node = packed.instantiate()
	root.add_child(main)

	var selector: ProviderSelector = main.provider_selector
	transport.auto_config = {"transport_ok": false, "error_kind": "offline", "http_status": 0, "body": ""}
	selector.open_selector(transport, "", "")
	transport.poll()

	_check(selector.status_label.text.contains("offline fallback"), "offline fallback reported in status")
	_check_eq(selector.provider_option.item_count, 1, "only 1 fallback option")
	_check(selector.provider_option.get_item_text(0).contains("offline"), "fallback option text indicates offline")
	_check_eq(selector.model_option.item_count, 1, "only 1 fallback model option")
	_check_eq(selector.model_option.get_item_text(0), "builtin-v1", "fallback model is builtin-v1")
	_check(not selector.apply_btn.disabled, "apply button enabled for offline fallback")

	# Verify applying offline fallback forwards rules-baseline and builtin-v1
	var applied_signals := []
	selector.selection_applied.connect(func(p, m): applied_signals.append([p, m]))
	selector._on_apply_pressed()

	_check_eq(applied_signals.size(), 1, "selection_applied emitted on apply")
	_check_eq(applied_signals[0], ["rules-baseline", "builtin-v1"], "offline fallback forwards rules-baseline and builtin-v1")

	main.queue_free()
	_end()


func _test_debug_hud_toggle_and_display() -> void:
	print("\nTest: DebugHUD toggles and renders latency, status, and decisions")
	var state := GameState.new()
	state.enable_dynamic_world(1)
	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)

	var hud := DebugHUD.new()
	var panel := PanelContainer.new()
	panel.name = "Panel"
	var vbox := VBoxContainer.new()
	vbox.name = "VBox"
	var header := HBoxContainer.new()
	header.name = "Header"
	var close_btn := Button.new()
	close_btn.name = "CloseBtn"
	var scroll := ScrollContainer.new()
	scroll.name = "Scroll"
	var content_label := RichTextLabel.new()
	content_label.name = "ContentLabel"

	header.add_child(close_btn)
	scroll.add_child(content_label)
	vbox.add_child(header)
	vbox.add_child(scroll)
	panel.add_child(vbox)
	hud.add_child(panel)
	root.add_child(hud)

	_check(not hud.is_hud_visible, "HUD hidden initially")
	hud.toggle_hud()
	_check(hud.is_hud_visible, "HUD visible after toggle")

	# Feed mock coordinator stats
	coord.last_generation_status = "committed"
	coord.last_latency_ms = 42.5
	coord.last_provider = "groq"
	coord.last_model = "openai/gpt-oss-20b"
	coord._record_decision({
		"room_id": "r-001",
		"depth": 1,
		"room_type": "corridor",
		"size": "small",
		"danger": 2,
		"exits": [{"direction": "south"}, {"direction": "north"}]
	}, "director", {"provider": "groq", "model": "openai/gpt-oss-20b", "latency_ms": 42.5})

	hud.update_hud(coord, "groq", "openai/gpt-oss-20b")
	var text := content_label.text
	_check(text.contains("Configured Provider:[/b] groq"), "HUD shows provider")
	_check(text.contains("Gen Status:[/b] [color=green]COMMITTED[/color]"), "HUD shows generation status")
	_check(text.contains("42.5 ms"), "HUD shows latency")
	_check(text.contains("r-001"), "HUD shows recent RoomPlan decision")

	hud.queue_free()
	_end()


func _test_debug_hud_metadata_and_jev_probabilities() -> void:
	print("\nTest: DebugHUD formats generic provider metadata including Jev probabilities and scores")
	var state := GameState.new()
	state.enable_dynamic_world(1)
	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)

	var hud := DebugHUD.new()
	var panel := PanelContainer.new()
	panel.name = "Panel"
	var vbox := VBoxContainer.new()
	vbox.name = "VBox"
	var header := HBoxContainer.new()
	header.name = "Header"
	var close_btn := Button.new()
	close_btn.name = "CloseBtn"
	var scroll := ScrollContainer.new()
	scroll.name = "Scroll"
	var content_label := RichTextLabel.new()
	content_label.name = "ContentLabel"

	header.add_child(close_btn)
	scroll.add_child(content_label)
	vbox.add_child(header)
	vbox.add_child(scroll)
	panel.add_child(vbox)
	hud.add_child(panel)
	root.add_child(hud)
	hud.set_hud_visible(true)

	# Jev metadata payload
	coord.last_generation_status = "committed"
	coord.last_latency_ms = 85.0
	coord.last_provider_metadata = {
		"jev_model": "jev-1.13.0",
		"room_type_probabilities": {
			"room": 0.75,
			"cavern": 0.25
		},
		"danger_score": 2.4,
		"has_secret_probability": 0.88,
		"tag_probabilities": {
			"dark": 0.95,
			"fungal": 0.12
		}
	}

	hud.update_hud(coord, "cloudflare-jev", "typesafe/jev")
	var text := content_label.text
	_check(text.contains("jev-1.13.0"), "metadata jev_model rendered")
	_check(text.contains("room_type_probabilities"), "room_type_probabilities header rendered")
	_check(text.contains("0.7500"), "calibrated choice probability rendered")
	_check(text.contains("has_secret_probability:[/b] 0.88"), "secret probability rendered")
	_check(text.contains("tag_probabilities:"), "tag_probabilities header rendered")
	_check(text.contains("dark: 0.950"), "individual tag probability rendered")

	hud.queue_free()
	_end()


func _test_debug_hud_clears_stale_metadata_on_fallback() -> void:
	print("\nTest: GenerationCoordinator and HUD clear stale director metadata and latency on fallback")
	var state := GameState.new()
	state.enable_dynamic_world(42)
	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)
	var current_tick := [1000]
	coord.clock = func() -> int: return current_tick[0]
	coord.provider = "cerebras"
	coord.model = "qwen-3.8-27b"

	var hud := DebugHUD.new()
	var panel := PanelContainer.new()
	panel.name = "Panel"
	var vbox := VBoxContainer.new()
	vbox.name = "VBox"
	var header := HBoxContainer.new()
	header.name = "Header"
	var close_btn := Button.new()
	close_btn.name = "CloseBtn"
	var scroll := ScrollContainer.new()
	scroll.name = "Scroll"
	var content_label := RichTextLabel.new()
	content_label.name = "ContentLabel"

	header.add_child(close_btn)
	scroll.add_child(content_label)
	vbox.add_child(header)
	vbox.add_child(scroll)
	panel.add_child(vbox)
	hud.add_child(panel)
	root.add_child(hud)
	hud.set_hud_visible(true)

	# 1. First request succeeds canonically from director with server latency and provider metadata
	state.player_pos = Vector2i(4, 2)
	coord.update()
	_check_eq(transport.submitted.size(), 1, "request 1 submitted")
	var req_id1: String = transport.submitted[0].request.request_id

	current_tick[0] += 120 # 120ms client time
	var success_response := {
		"transport_ok": true,
		"http_status": 200,
		"body": JSON.stringify({
			"contract_version": "1.0.0",
			"request_id": req_id1,
			"run_id": state.world.run_id,
			"success": true,
			"room": {
				"room_id": "r-canonical",
				"depth": 1,
				"room_type": "room",
				"size": "small",
				"danger": 1,
				"exits": [
					{"direction": "south", "kind": "door", "locked": false}
				]
			},
			"metadata": {
				"provider": "cerebras",
				"model": "qwen-3.8-27b",
				"started_at": "2026-09-19T10:00:00.100Z",
				"completed_at": "2026-09-19T10:00:00.178Z",
				"latency_ms": 78.4,
				"provider_metadata": {
					"tokens_used": 140,
					"temperature": 0.7
				}
			}
		})
	}
	transport.deliver(0, success_response)
	coord.update()

	_check_eq(coord.last_generation_status, "committed", "request 1 committed")
	_check_eq(coord.last_provider, "cerebras", "provider is cerebras")
	_check_eq(coord.last_model, "qwen-3.8-27b", "model is qwen-3.8-27b")
	_check_eq(coord.last_latency_ms, 78.4, "server latency preserved on canonical success")
	_check_eq(coord.last_provider_metadata.get("tokens_used"), 140, "metadata stored")

	hud.update_hud(coord, coord.provider, coord.model)
	var text_success := content_label.text
	_check(text_success.contains("cerebras"), "HUD shows cerebras")
	_check(text_success.contains("78.4 ms"), "HUD shows 78.4 ms server latency")
	_check(text_success.contains("tokens_used"), "HUD shows director metadata")

	# 2. Second request fails / times out and falls back to local rules
	# Unblock a frontier to trigger next request
	coord.update()
	if transport.submitted.size() < 2:
		# Force a pending frontier if none triggered
		var unres := state.world.frontiers_with_status(DungeonWorld.STATUS_UNRESOLVED)
		if not unres.is_empty():
			coord._start(unres[0])

	_check_eq(transport.submitted.size(), 2, "request 2 submitted")
	var req_id2: String = transport.submitted[1].request.request_id

	# Advance clock by 250ms (simulating failure duration)
	current_tick[0] += 250
	var failure_response := {
		"transport_ok": false,
		"error_kind": "http_error:503",
		"http_status": 503,
		"body": ""
	}
	transport.deliver(1, failure_response)
	coord.update()

	_check_eq(coord.last_generation_status, "fallback", "request 2 resolved to fallback")
	_check_eq(coord.last_provider, "rules-baseline", "fallback sets last_provider to rules-baseline")
	_check_eq(coord.last_model, "builtin-v1", "fallback sets last_model to builtin-v1")
	_check_eq(coord.last_latency_ms, 250.0, "fallback sets measured request duration latency")
	_check_eq(coord.last_provider_metadata, {"fallback_reason": "http_error:503"}, "fallback metadata set with fallback_reason")
	_check(not coord.last_provider_metadata.has("tokens_used"), "stale director metadata cleared")

	hud.update_hud(coord, coord.provider, coord.model)
	var text_fallback := content_label.text
	_check(text_fallback.contains("rules-baseline"), "HUD shows rules-baseline fallback")
	_check(text_fallback.contains("builtin-v1"), "HUD shows builtin-v1 fallback model")
	_check(text_fallback.contains("250.0 ms"), "HUD shows measured 250.0 ms request latency")
	_check(text_fallback.contains("http_error:503"), "HUD shows fallback_reason")
	_check(not text_fallback.contains("tokens_used"), "HUD does not contain stale tokens_used")

	# Verify recent decisions recorded fallback properly
	var last_decision: Dictionary = coord.recent_decisions.back()
	_check_eq(last_decision.provider, "rules-baseline", "decision provider is rules-baseline")
	_check_eq(last_decision.model, "builtin-v1", "decision model is builtin-v1")
	_check_eq(last_decision.latency_ms, 250.0, "decision latency_ms is 250.0")
	_check_eq(last_decision.provider_metadata, {"fallback_reason": "http_error:503"}, "decision metadata contains fallback_reason")

	hud.queue_free()
	_end()


func _test_main_scene_selector_and_hud_interaction() -> void:
	print("\nTest: Main scene desktop (keybindings) and touch (buttons) toggle HUD and selector")
	var packed := load("res://scenes/main.tscn")
	var transport := ScriptedTransport.new()
	var config_dict := {
		"default_provider": "rules-baseline",
		"default_model": "builtin-v1",
		"providers": [
			{
				"id": "rules-baseline",
				"available": true,
				"default_model": "builtin-v1",
				"models": ["builtin-v1"]
			},
			{
				"id": "cerebras",
				"available": true,
				"default_model": "qwen-3.8-27b",
				"models": ["qwen-3.8-27b"]
			}
		]
	}
	transport.auto_config = {"transport_ok": true, "http_status": 200, "body": JSON.stringify(config_dict)}

	var main: MainGame = packed.instantiate()
	main.generation_transport = transport
	root.add_child(main)

	_check(not main.debug_hud.is_hud_visible, "HUD hidden on start")
	_check(not main.provider_selector.is_selector_visible, "Selector hidden on start")

	# 1. Desktop keybinding: Backquote (KEY_QUOTELEFT) toggles HUD
	var ev_hud := InputEventKey.new()
	ev_hud.keycode = KEY_QUOTELEFT
	ev_hud.pressed = true
	main._unhandled_input(ev_hud)
	_check(main.debug_hud.is_hud_visible, "HUD opened via Backquote shortcut")

	# Toggle off
	main._unhandled_input(ev_hud)
	_check(not main.debug_hud.is_hud_visible, "HUD closed via Backquote shortcut")

	# 2. Touch HUD button toggles HUD
	main.ui.hud_btn.emit_signal("pressed")
	_check(main.debug_hud.is_hud_visible, "HUD opened via touch button")

	# 3. Touch Provider CFG button opens selector
	main.ui.provider_btn.emit_signal("pressed")
	transport.poll()
	_check(main.provider_selector.is_selector_visible, "ProviderSelector opened via touch button")

	# Select cerebras and apply
	main.coordinator.last_generation_status = "committed"
	main.coordinator.last_provider = "rules-baseline"
	main.coordinator.last_model = "builtin-v1"
	main.coordinator.last_provider_metadata = {"old_run": true}
	main.coordinator.recent_decisions.append({"room_id": "old-run"})
	main.provider_selector._on_provider_selected(1)
	main.provider_selector._on_apply_pressed()

	_check(not main.provider_selector.is_selector_visible, "Selector closed after apply")
	_check_eq(main.selected_provider, "cerebras", "MainGame selected_provider updated")
	_check_eq(main.coordinator.provider, "cerebras", "Coordinator provider updated for new runs")
	_check_eq(main.state.active_provider, "cerebras", "GameState active_provider updated")
	_check(main.ui.provider_label.text.contains("cerebras"), "Top bar label updated with selected provider")
	_check_eq(main.coordinator.last_generation_status, "idle", "new run clears the previous generation status")
	_check(main.coordinator.last_provider_metadata.is_empty(), "new run clears previous provider metadata")
	_check(main.coordinator.recent_decisions.is_empty(), "new run clears previous decision history")

	main.queue_free()
	_end()
