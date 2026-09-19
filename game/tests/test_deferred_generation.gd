extends SceneTree
## Issue #7: deferred generation coordinator, HTTP client, fallback behaviour
## and main-scene integration. No live server: transports are injected fakes,
## plus a loopback mini HTTP server for the real client.

const DungeonContracts = preload("res://contracts/dungeon_contracts.gd")
const GameState = preload("res://src/game_state.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")
const GenerationCoordinator = preload("res://world/generation_coordinator.gd")
const GenerationClient = preload("res://world/generation_client.gd")
const RulesBaseline = preload("res://world/rules_baseline.gd")
const ScriptedTransport = preload("res://tests/support/scripted_transport.gd")
const StubDirector = preload("res://tests/support/stub_director.gd")
const MiniHttpServer = preload("res://tests/support/mini_http_server.gd")
const DungeonRenderer = preload("res://src/dungeon_renderer.gd")
const OfflineTransport = preload("res://world/offline_transport.gd")
const GenerationRecorder = preload("res://world/generation_recorder.gd")
const TileType = GameState.TileType

const NORTH := "r-000:north"
const EAST := "r-000:east"
const WEST := "r-000:west"

var _checks := 0
var _failures := PackedStringArray()
var _completed := false
var _reached_end := false


func _initialize() -> void:
	OS.set_environment("DUNGEON_DIRECTOR_URL", "offline")
	create_timer(60.0).timeout.connect(_on_watchdog)
	process_frame.connect(_run, CONNECT_ONE_SHOT)


func _on_watchdog() -> void:
	printerr("FAILED: deferred generation suite exceeded its 60s watchdog")
	quit(2)


func _run() -> void:
	print("=== Running test_deferred_generation.gd ===")
	await _step("test_starting_dungeon_is_small", _test_starting_dungeon_is_small)
	await _step("test_request_gating_and_contract", _test_request_gating_and_contract)
	await _step("test_max_in_flight", _test_max_in_flight)
	await _step("test_urgent_frontier_bypasses_prefetch_limit", _test_urgent_frontier_bypasses_prefetch_limit)
	await _step("test_run_and_request_ids_change_across_restarts", _test_run_and_request_ids_change_across_restarts)
	await _step("test_async_non_blocking_and_no_early_mutation", _test_async_non_blocking_and_no_early_mutation)
	await _step("test_success_commit_and_duplicate_completion", _test_success_commit_and_duplicate_completion)
	await _step("test_timeout_fallback_and_late_response", _test_timeout_fallback_and_late_response)
	await _step("test_transport_failure_fallback", _test_transport_failure_fallback)
	await _step("test_invalid_and_contradictory_responses_fall_back", _test_invalid_and_contradictory_responses_fall_back)
	await _step("test_overlap_repositions_to_smaller_room", _test_overlap_repositions_to_smaller_room)
	await _step("test_blocked_exit_is_pruned", _test_blocked_exit_is_pruned)
	await _step("test_vertical_exits_are_ignored", _test_vertical_exits_are_ignored)
	await _step("test_fallback_is_deterministic", _test_fallback_is_deterministic)
	await _step("test_sealed_when_nothing_fits", _test_sealed_when_nothing_fits)
	await _step("test_restart_drops_in_flight_requests", _test_restart_drops_in_flight_requests)
	await _step("test_rules_baseline_plans_are_contract_valid", _test_rules_baseline_plans_are_contract_valid)
	await _step("test_main_scene_shows_unknown_exits", _test_main_scene_shows_unknown_exits)
	await _step("test_exploring_reveals_rooms_permanently", _test_exploring_reveals_rooms_permanently)
	await _step("test_offline_main_scene_falls_back_and_stays_playable", _test_offline_main_scene_falls_back_and_stays_playable)
	await _step("test_transport_selection_from_environment", _test_transport_selection_from_environment)
	await _step("test_restart_in_main_scene", _test_restart_in_main_scene)
	await _step("test_shutdown_cancels_in_flight_requests", _test_shutdown_cancels_in_flight_requests)
	await _step("test_http_client", _test_http_client)
	await _step("test_http_client_fetch_config", _test_http_client_fetch_config)
	await _step("test_coordinator_over_real_http", _test_coordinator_over_real_http)
	await _step("test_generation_recording", _test_generation_recording)
	await _step("test_recorder_preserves_resolved_provider_and_metadata", _test_recorder_preserves_resolved_provider_and_metadata)
	await _step("test_recorder_captures_failure_envelope_and_fallback_without_secrets", _test_recorder_captures_failure_envelope_and_fallback_without_secrets)
	_completed = true
	_finish()


func _step(test_name: String, fn: Callable) -> void:
	_reached_end = false
	await fn.call()
	if not _reached_end:
		_failures.append("%s aborted before reaching its end (runtime script error?)" % test_name)
		printerr("  [FAIL] %s aborted before reaching its end (runtime script error?)" % test_name)


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
	print("\n--- Generation Test Results: %d Passed, %d Failed (completed=%s) ---" % [_checks - _failures.size(), _failures.size(), str(_completed)])
	if _failures.is_empty() and _completed:
		print("SUCCESS: All deferred generation checks passed!")
		quit(0)
	else:
		printerr("FAILED: deferred generation checks did not pass.")
		quit(1)


# --- helpers -----------------------------------------------------------------


func _env(radius: int = 3, max_in_flight: int = 2) -> Dictionary:
	var state := GameState.new()
	state.enable_dynamic_world(1)
	# Most fixtures compare deterministic fallback output across isolated test
	# states. Production run ids are random by design; pin the fixture identity.
	state.world.run_id = "test-run-1"
	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)
	coord.trigger_radius = radius
	coord.max_in_flight = max_in_flight
	coord.timeout_msec = 1000
	var clock := [0]
	coord.clock = func() -> int: return clock[0]
	return {"state": state, "transport": transport, "coord": coord, "clock": clock}


## Put the player next to the north door of the start room and start its request.
func _request_north(env: Dictionary) -> Dictionary:
	env.state.player_pos = Vector2i(4, 2)
	env.coord.update()
	return env.transport.submitted[0].request


func _snapshot(state: GameState) -> Dictionary:
	return {"tiles": state.world.tiles.duplicate(), "rooms": state.world.rooms.keys(), "revision": state.world.revision}


func _last_fallback_reason(state: GameState) -> String:
	for i in range(state.world.generation_log.size() - 1, -1, -1):
		var entry: Dictionary = state.world.generation_log[i]
		if entry.event == "fallback":
			return entry.reason
	return ""


## The synchronous tests run inside one long frame; HTTPRequest timeouts
## accumulate frame time, so let a few normal frames pass first.
func _settle_frames() -> void:
	for i in range(3):
		await process_frame


func _served(server: MiniHttpServer, results: Array, count: int) -> bool:
	server.poll()
	return results.size() >= count


func _wait_until(condition: Callable, max_frames: int = 600) -> bool:
	for i in range(max_frames):
		if condition.call():
			return true
		await process_frame
	return condition.call()


# --- tests -------------------------------------------------------------------


func _test_starting_dungeon_is_small() -> void:
	print("\nTest: starting dungeon is one small committed room with unresolved exits")
	var env := _env()
	var world: DungeonWorld = env.state.world
	_check_eq(world.rooms.size(), 1, "only one committed room")
	_check(world.tiles.size() <= 100, "committed area is small (%d tiles)" % world.tiles.size())
	_check_eq(world.open_frontiers().size(), 3, "three unresolved exits")
	_check_eq(env.transport.submitted.size(), 0, "nothing requested before the player approaches")
	env.coord.update()
	_check_eq(env.transport.submitted.size(), 0, "no request while the player is far from every exit")
	_end()


func _test_request_gating_and_contract() -> void:
	print("\nTest: only approached exits are requested, once, with a contract-valid request")
	var env := _env()
	env.coord.provider = "acme"
	env.coord.model = "big-1"
	var request := _request_north(env)
	_check_eq(env.transport.submitted.size(), 1, "exactly one request for the exit the player approached")
	_check_eq(env.transport.target_keys(), [NORTH], "target is the north frontier")
	_check(DungeonContracts.validate_generation_request(request).ok, "request satisfies the canonical contract: %s" % str(DungeonContracts.validate_generation_request(request)))
	_check_eq(request.state.unresolved_exits.size(), 3, "state lists all open frontiers")
	_check(not request.has("provider") and not request.has("model"), "provider/model are not part of the canonical body")
	_check_eq(env.transport.submitted[0].options.provider, "acme", "provider passed as transport option")
	_check_eq(env.transport.submitted[0].options.model, "big-1", "model passed as transport option")
	_check_eq(env.state.world.get_frontier(NORTH).status, DungeonWorld.STATUS_PENDING, "frontier is pending")
	env.coord.update()
	env.coord.update()
	_check_eq(env.transport.submitted.size(), 1, "pending frontier is never requested twice")
	_end()


func _test_max_in_flight() -> void:
	print("\nTest: concurrent prefetch is bounded and drains as responses arrive")
	var env := _env(10, 2)
	env.coord.update()
	_check_eq(env.transport.submitted.size(), 2, "at most max_in_flight requests")
	var first: Dictionary = env.transport.submitted[0].request
	env.transport.deliver(0, StubDirector.success_result(first, StubDirector.simple_plan(first, "s-1", "small", [])))
	env.coord.update()
	_check_eq(env.transport.submitted.size(), 3, "the freed slot is used by the next frontier")
	_check_eq(env.transport.target_keys().size(), 3, "three distinct requests")
	var keys: Array = env.transport.target_keys()
	_check(keys.find(keys[0]) == keys.rfind(keys[0]) and keys.find(keys[1]) == keys.rfind(keys[1]) and keys.find(keys[2]) == keys.rfind(keys[2]), "no frontier requested twice")
	_end()


func _test_urgent_frontier_bypasses_prefetch_limit() -> void:
	print("\nTest: the player's adjacent frontier is not starved by speculative prefetches")
	var env := _env(10, 2)
	env.coord.update()
	_check_eq(env.transport.submitted.size(), 2, "two distant prefetches fill the normal slots")
	var remaining: Array[Dictionary] = env.state.world.frontiers_with_status(DungeonWorld.STATUS_UNRESOLVED)
	_check_eq(remaining.size(), 1, "one frontier remains unresolved")
	var urgent: Dictionary = remaining[0]
	env.state.player_pos = urgent.pos - DungeonWorld.DIRECTION_VECTORS[urgent.direction]

	env.coord.update()

	_check_eq(env.transport.submitted.size(), 3, "adjacent frontier starts through the urgent reserve")
	_check_eq(env.transport.target_keys()[-1], urgent.key, "the reserve belongs to the approached frontier")
	_check_eq(env.coord.in_flight.size(), 3, "total work is bounded at max_in_flight plus one urgent request")
	env.coord.update()
	_check_eq(env.transport.submitted.size(), 3, "the urgent frontier is not submitted twice")
	_end()


func _test_run_and_request_ids_change_across_restarts() -> void:
	print("\nTest: every playthrough and request has a fresh correlation id")
	var state := GameState.new()
	state.enable_dynamic_world(7)
	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)
	var first_run: String = state.world.run_id
	state.player_pos = Vector2i(4, 2)
	coord.update()
	var first_request: Dictionary = transport.submitted[0].request

	state.reset_game()
	coord.update()
	var second_run: String = state.world.run_id
	state.player_pos = Vector2i(4, 2)
	coord.update()
	var second_request: Dictionary = transport.submitted[1].request

	_check(first_run != second_run, "reset creates a distinct run id")
	_check_eq(first_request.run_id, first_run, "first request carries its playthrough id")
	_check_eq(second_request.run_id, second_run, "second request carries its playthrough id")
	_check(first_request.request_id != second_request.request_id, "request ids do not repeat across restarts")
	_check(first_request.request_id.contains(first_run), "first request id is scoped to its run")
	_check(second_request.request_id.contains(second_run), "second request id is scoped to its run")
	_check(DungeonContracts.validate_generation_request(second_request).ok, "scoped ids remain contract-valid")
	_end()


func _test_async_non_blocking_and_no_early_mutation() -> void:
	print("\nTest: generation is asynchronous; the game stays playable and state is untouched until commit")
	var env := _env()
	var before := _snapshot(env.state)
	_request_north(env)
	_check_eq(env.state.world.pending_count(), 1, "request outstanding after update() returned")
	_check_eq(env.state.world.tiles, before.tiles, "no tiles committed while pending")
	_check_eq(env.state.world.rooms.keys(), before.rooms, "no rooms committed while pending")
	var turns: int = env.state.player_turns
	var moved: bool = env.state.player_action_step(Vector2i.LEFT)
	_check(moved and env.state.player_turns == turns + 1, "player can still act while a request is pending")
	_check_eq(env.state.world.pending_count(), 1, "still pending after the player acted")
	env.coord.update()
	_check_eq(env.transport.submitted.size(), 1, "update() with a pending request does not block or resubmit")
	_end()


func _test_success_commit_and_duplicate_completion() -> void:
	print("\nTest: director room commits permanently; duplicate/contradictory completions are ignored")
	var env := _env()
	var request := _request_north(env)
	var enemies_before: int = env.state.enemies.size()
	var plan := StubDirector.simple_plan(request, "r-001", "small", ["east", "north"])
	env.transport.deliver(0, StubDirector.success_result(request, plan))
	var world: DungeonWorld = env.state.world
	_check_eq(world.get_frontier(NORTH).status, DungeonWorld.STATUS_COMMITTED, "north frontier committed")
	_check(world.rooms.has("r-001"), "room r-001 is part of the world")
	_check_eq(world.rooms["r-001"].source, "director", "committed from the director plan")
	_check_eq(env.state.enemies.size() - enemies_before, world.rooms["r-001"].enemies.size(), "room entities spawned in world space")
	_check(env.state.map_tiles.has(Vector2i(4, -1)), "game map contains the new room's tiles")
	_check_eq(world.get_frontier("r-001:east").status, DungeonWorld.STATUS_UNRESOLVED, "the new room's other exits are unresolved frontiers")
	var before := _snapshot(env.state)
	var dup_before: int = world.counters.duplicates
	env.transport.deliver(0, StubDirector.success_result(request, plan))
	var other := StubDirector.simple_plan(request, "r-777", "huge", ["west"])
	env.transport.deliver(0, StubDirector.success_result(request, other))
	_check_eq(env.state.world.tiles, before.tiles, "duplicate + contradictory completions changed no tile")
	_check_eq(env.state.world.rooms.keys(), before.rooms, "no room replaced or added")
	_check_eq(env.state.world.revision, before.revision, "no revision bump")
	_check(env.state.world.counters.duplicates >= dup_before + 2, "duplicates counted in diagnostics")
	env.coord.update()
	_check_eq(env.transport.submitted.size(), 1, "the committed frontier is never regenerated")
	# Permanence: walk away and back; room is still there and unchanged.
	env.state.player_pos = Vector2i(2, 5)
	env.coord.update()
	_check_eq(env.state.world.tiles, before.tiles, "committed tiles unchanged after leaving")
	_end()


func _test_timeout_fallback_and_late_response() -> void:
	print("\nTest: request timeout falls back to the rules baseline; late response is ignored")
	var env := _env()
	var request := _request_north(env)
	var before := _snapshot(env.state)
	env.clock[0] = 500
	env.coord.update()
	_check_eq(env.state.world.get_frontier(NORTH).status, DungeonWorld.STATUS_PENDING, "still pending before the deadline")
	_check_eq(env.state.world.tiles, before.tiles, "no mutation before the deadline")
	env.clock[0] = 1200
	env.coord.update()
	var world: DungeonWorld = env.state.world
	_check_eq(world.get_frontier(NORTH).status, DungeonWorld.STATUS_COMMITTED, "frontier committed by the fallback after timeout")
	var room_id: String = world.get_frontier(NORTH).resolved_room_id
	_check_eq(world.rooms[room_id].source, "fallback", "room source is fallback")
	_check_eq(world.rooms[room_id].meta.fallback_reason, "timeout", "fallback reason recorded on the room")
	_check_eq(_last_fallback_reason(env.state), "timeout", "fallback visible in the generation log")
	_check_eq(world.counters.fallbacks, 1, "fallback counted")
	_check_eq(world.counters.committed_fallback, 1, "fallback commit counted")
	var committed := _snapshot(env.state)
	env.transport.deliver(0, StubDirector.success_result(request, StubDirector.simple_plan(request, "r-late", "large", ["east"])))
	_check_eq(env.state.world.tiles, committed.tiles, "late response did not replace the fallback room")
	_check(not env.state.world.rooms.has("r-late"), "late room never committed")
	_check(world.integrity_violations().is_empty(), "world integrity holds")
	_end()


func _test_transport_failure_fallback() -> void:
	print("\nTest: transport failure falls back and keeps gameplay possible")
	var env := _env()
	_request_north(env)
	env.transport.deliver(0, StubDirector.failure_result("transport_failure"))
	var world: DungeonWorld = env.state.world
	_check_eq(world.get_frontier(NORTH).status, DungeonWorld.STATUS_COMMITTED, "frontier committed via fallback")
	_check_eq(_last_fallback_reason(env.state), "transport_failure", "fallback reason logged")
	_check_eq(world.pending_count(), 0, "nothing left pending")
	env.state.player_pos = Vector2i(4, 0)
	_check(env.state.player_action_step(Vector2i.UP) or env.state.get_tile(Vector2i(4, -1)) != TileType.WALL, "the player can walk through the new exit")
	_end()


func _test_invalid_and_contradictory_responses_fall_back() -> void:
	print("\nTest: invalid or contradictory responses never commit; they fall back")
	var cases := [
		["invalid_json", func(req): return StubDirector.ok_result("this is not json"), "invalid_response"],
		["html_502", func(req): return StubDirector.ok_result("<html>bad gateway</html>", 502), "http_error:502_non_json"],
		["unknown_key", func(req): return StubDirector.ok_result(StubDirector.success_body(req, StubDirector.simple_plan(req, "r-1", "small")).replace("\"room_id\":\"r-1\"", "\"room_id\":\"r-1\",\"tiles\":[[1]]")), "invalid_response"],
		["bad_enum", func(req): var p := StubDirector.simple_plan(req, "r-1", "small"); p.room_type = "gymnasium"; return StubDirector.success_result(req, p), "invalid_response"],
		["provider_failure_502", func(req): return StubDirector.ok_result(StubDirector.failure_body(req), 502), "provider_failure"],
		["request_id_mismatch", func(req): var other: Dictionary = req.duplicate(); other.request_id = "req-other"; return StubDirector.success_result(other, StubDirector.simple_plan(req, "r-1", "small")), "response_mismatch"],
		["run_id_mismatch", func(req): var other: Dictionary = req.duplicate(); other.run_id = "run-other"; return StubDirector.success_result(other, StubDirector.simple_plan(req, "r-1", "small")), "response_mismatch"],
		["missing_backlink", func(req): var p := StubDirector.simple_plan(req, "r-1", "small"); p.exits = [StubDirector.exit_def("east"), StubDirector.exit_def("west")]; return StubDirector.success_result(req, p), "plan_rejected"],
		["depth_mismatch", func(req): var p := StubDirector.simple_plan(req, "r-1", "small"); p.depth = 4; return StubDirector.success_result(req, p), "plan_rejected"],
		["duplicate_room_id", func(req): return StubDirector.success_result(req, StubDirector.simple_plan(req, "r-000", "small")), "plan_rejected"],
		["empty_body", func(req): return StubDirector.ok_result(""), "invalid_response"],
	]
	for case in cases:
		var env := _env()
		var request := _request_north(env)
		var before_rooms: int = env.state.world.rooms.size()
		var response: Dictionary = case[1].call(request)
		env.transport.deliver(0, response)
		var world: DungeonWorld = env.state.world
		var reason := _last_fallback_reason(env.state)
		_check(reason.begins_with(case[2]), "%s -> fallback (%s)" % [case[0], reason])
		var room_id: String = world.get_frontier(NORTH).resolved_room_id
		_check(room_id != "" and world.rooms[room_id].source == "fallback", "%s: deterministic baseline room committed instead" % case[0])
		_check_eq(world.rooms.size(), before_rooms + 1, "%s: exactly one room committed" % case[0])
		_check(world.integrity_violations().is_empty(), "%s: integrity holds" % case[0])
	_end()


func _test_overlap_repositions_to_smaller_room() -> void:
	print("\nTest: a plan that overlaps committed geometry is repositioned (re-planned smaller)")
	var env := _env()
	var request := _request_north(env)
	env.state.world.tiles[Vector2i(4, -10)] = TileType.FLOOR  # committed geometry in the way of big rooms
	var before: Dictionary = env.state.world.tiles.duplicate()
	var plan := StubDirector.simple_plan(request, "r-001", "large", ["east"])
	env.transport.deliver(0, StubDirector.success_result(request, plan))
	var world: DungeonWorld = env.state.world
	var rec: Dictionary = world.rooms.get("r-001", {})
	_check(not rec.is_empty(), "room committed after repositioning")
	_check_eq(rec.source, "director", "still the director's room")
	_check(rec.meta.get("size_used", "large") != "large", "footprint reduced to fit (size_used=%s)" % rec.meta.get("size_used", "?"))
	_check_eq(world.tiles[Vector2i(4, -10)], before[Vector2i(4, -10)], "the pre-existing committed tile was preserved")
	_check(world.counters.rejected >= 1, "the rejected larger footprints are counted in diagnostics")
	_check(rec.meta.get("repositioned", false), "room marked as repositioned")
	_end()


func _test_blocked_exit_is_pruned() -> void:
	print("\nTest: an exit facing committed geometry is pruned instead of contradicting it")
	var env := _env()
	var request := _request_north(env)
	env.state.world.tiles[Vector2i(-1, -4)] = TileType.WALL
	var plan := StubDirector.simple_plan(request, "r-001", "small", ["west", "east"])
	env.transport.deliver(0, StubDirector.success_result(request, plan))
	var world: DungeonWorld = env.state.world
	_check(world.rooms.has("r-001"), "room committed")
	_check(not world.frontiers.has("r-001:west"), "blocked west exit is not a frontier")
	_check(world.frontiers.has("r-001:east"), "unblocked east exit is a frontier")
	_check_eq(world.rooms["r-001"].meta.get("pruned_exits", []), ["west"], "pruned exit recorded")
	_end()


func _test_vertical_exits_are_ignored() -> void:
	print("\nTest: vertical (stairs) exits do not create frontiers in the single-level world")
	var env := _env()
	var request := _request_north(env)
	var plan := StubDirector.simple_plan(request, "r-001", "medium", ["east"])
	plan.exits.append(StubDirector.exit_def("down", "stairs"))
	env.transport.deliver(0, StubDirector.success_result(request, plan))
	var world: DungeonWorld = env.state.world
	_check(world.rooms.has("r-001"), "room committed")
	_check(not world.frontiers.has("r-001:down"), "no frontier for the vertical exit")
	_check_eq(world.rooms["r-001"].source, "director", "not treated as a failure")
	_end()


func _test_fallback_is_deterministic() -> void:
	print("\nTest: the local rules baseline is deterministic for the same run and frontier")
	var tiles: Array = []
	for i in range(2):
		var env := _env()
		_request_north(env)
		env.transport.deliver(0, StubDirector.failure_result("timeout"))
		tiles.append(env.state.world.tiles.duplicate())
	_check_eq(tiles[0], tiles[1], "identical fallback geometry across independent runs")
	_end()


func _test_sealed_when_nothing_fits() -> void:
	print("\nTest: if not even the smallest fallback fits, the exit is sealed and play continues")
	var env := _env()
	var request := _request_north(env)
	for y in range(-12, 0):
		env.state.world.tiles[Vector2i(4, y)] = TileType.FLOOR  # solid committed column across every footprint
	env.transport.deliver(0, StubDirector.failure_result("timeout"))
	var world: DungeonWorld = env.state.world
	_check_eq(world.get_frontier(NORTH).status, DungeonWorld.STATUS_SEALED, "frontier sealed")
	_check_eq(world.tiles[Vector2i(4, 0)], TileType.WALL, "sealed door is a wall")
	_check_eq(world.pending_count(), 0, "nothing pending")
	_check_eq(world.get_frontier(EAST).status, DungeonWorld.STATUS_UNRESOLVED, "other exits still available")
	_check_eq(request.target_exit.room_id, "r-000", "sanity: request targeted the start room")
	_end()


func _test_restart_drops_in_flight_requests() -> void:
	print("\nTest: restarting the game drops in-flight requests; late responses cannot touch the new world")
	var env := _env()
	var request := _request_north(env)
	env.state.reset_game()
	env.coord.update()
	var fresh := _snapshot(env.state)
	env.transport.deliver(0, StubDirector.success_result(request, StubDirector.simple_plan(request, "r-001", "small", ["east"])))
	_check_eq(env.state.world.tiles, fresh.tiles, "new world untouched by the old run's response")
	_check_eq(env.state.world.rooms.keys(), ["r-000"], "still only the start room")
	_check_eq(env.state.world.pending_count(), 0, "new world has nothing pending")
	_end()


func _test_rules_baseline_plans_are_contract_valid() -> void:
	print("\nTest: rules-baseline plans are contract-valid and always link back")
	var env := _env()
	var request := _request_north(env)
	for variant in ["standard", "minimal"]:
		var plan := RulesBaseline.plan_for(request, "fb-1", variant)
		_check(DungeonContracts.validate_room_plan(plan).ok, "%s plan validates: %s" % [variant, str(DungeonContracts.validate_room_plan(plan))])
		var dirs: Array = []
		for e in plan.exits:
			dirs.append(e.direction)
		_check("south" in dirs, "%s plan has the backlink exit" % variant)
		_check_eq(plan.depth, request.state.depth, "%s plan depth matches the request" % variant)
	_check_eq(RulesBaseline.plan_for(request, "fb-1", "standard"), RulesBaseline.plan_for(request, "fb-1", "standard"), "plans are deterministic")
	_end()


func _spawn_main(transport: Variant = null) -> Node:
	var packed: PackedScene = load("res://scenes/main.tscn")
	var main: Node = packed.instantiate()
	if transport != null:
		main.generation_transport = transport
	root.add_child(main)
	return main


func _marker_doors(main: Node) -> Array:
	var doors: Array = []
	for marker in main.renderer.frontier_markers():
		doors.append(marker.door)
	return doors


func _test_main_scene_shows_unknown_exits() -> void:
	print("\nTest: the main scene starts small and draws unresolved exits as distinct unknown space")
	var transport := ScriptedTransport.new()
	var main := _spawn_main(transport)
	_check(main.state.dynamic_world and main.state.world != null, "main scene runs the deferred-generation world")
	_check_eq(main.state.world.rooms.size(), 1, "one committed room at start")
	var markers: Array[Dictionary] = main.renderer.frontier_markers()
	_check_eq(markers.size(), 3, "one marker per unresolved exit")
	for marker in markers:
		_check(not main.state.map_tiles.has(marker.unknown), "marker %s points into uncommitted space" % str(marker.door))
		_check(main.state.map_tiles.has(marker.door), "marker door %s is a real exit tile" % str(marker.door))
		_check(main.renderer.frontier_color(marker.status) != DungeonRenderer.COLOR_DOOR_CLOSED, "unresolved exit colour differs from an ordinary door")
	_check(main.renderer.frontier_color(DungeonWorld.STATUS_PENDING) != main.renderer.frontier_color(DungeonWorld.STATUS_UNRESOLVED), "pending exits look different from unresolved ones")
	_check(main.ui.stats_label.text.contains("Rooms: 1"), "HUD reports the committed room count")
	main.queue_free()
	_end()


func _test_exploring_reveals_rooms_permanently() -> void:
	print("\nTest: exploring reveals a generated room that stays for good")
	var transport := ScriptedTransport.new()
	var main := _spawn_main(transport)
	main._on_dpad_move(Vector2i.UP)
	main._on_dpad_move(Vector2i.UP)
	main._process(0.016)
	_check_eq(transport.submitted.size(), 1, "approaching the north door triggers exactly one request")
	_check(main.renderer.frontier_markers().any(func(m): return m.door == Vector2i(4, 0) and m.status == DungeonWorld.STATUS_PENDING), "north exit now shows as pending")
	_check(main.state.player_action_step(Vector2i.LEFT), "game stays playable while the request is pending")
	main.state.player_pos = Vector2i(4, 2)
	var request: Dictionary = transport.submitted[0].request
	var plan := StubDirector.simple_plan(request, "r-001", "small", ["east"])
	plan.enemy_density = 0.0
	plan.loot_density = 0.5
	transport.deliver(0, StubDirector.success_result(request, plan))
	main._process(0.016)
	_check(main.state.map_tiles.has(Vector2i(4, -5)), "new room tiles are part of the live map")
	_check(not _marker_doors(main).has(Vector2i(4, 0)), "resolved exit no longer drawn as unknown")
	_check(_marker_doors(main).has(Vector2i(8, -4)), "the new room's east exit is drawn as an unresolved exit")
	_check(main.ui.stats_label.text.contains("Rooms: 2"), "HUD count updated")
	# Walk in: up to the door, open it, step through.
	for i in range(4):
		main._on_dpad_move(Vector2i.UP)
	_check(main.state.player_pos.y < 0, "the player walked into the generated room (at %s)" % str(main.state.player_pos))
	var tiles_inside: Dictionary = main.state.map_tiles.duplicate()
	main.state.player_pos = Vector2i(4, 5)
	main._process(0.016)
	main._process(0.016)
	_check_eq(main.state.map_tiles, tiles_inside, "committed rooms are permanent after the player leaves")
	_check_eq(transport.submitted.size(), 1, "the committed exit is never requested again")
	main.queue_free()
	_end()


func _test_offline_main_scene_falls_back_and_stays_playable() -> void:
	print("\nTest: with no director the main scene falls back locally and the fallback is observable")
	var main := _spawn_main()
	_check(main.generation_transport.get_script() == OfflineTransport, "DUNGEON_DIRECTOR_URL=offline selects the offline transport")
	main._on_dpad_move(Vector2i.UP)
	main._on_dpad_move(Vector2i.UP)
	main._process(0.016)
	_check_eq(main.state.world.rooms.size(), 1, "nothing committed in the frame that started the request")
	main._process(0.016)
	_check_eq(main.state.world.rooms.size(), 2, "the next frame commits a local rules-baseline room")
	var room_id: String = main.state.world.get_frontier("r-000:north").resolved_room_id
	_check_eq(main.state.world.rooms[room_id].source, "fallback", "room is marked as fallback")
	_check_eq(main.state.world.counters.fallbacks, 1, "fallback counted in diagnostics")
	_check(main.ui.log_label.text.contains("fell back to local rules"), "fallback is visible in the on-screen log")
	main.queue_free()
	_end()


func _test_transport_selection_from_environment() -> void:
	print("\nTest: transport is chosen from the environment without provider-specific logic")
	OS.set_environment("DUNGEON_DIRECTOR_URL", "http://127.0.0.1:9/")
	OS.set_environment("DUNGEON_DIRECTOR_PROVIDER", "acme")
	OS.set_environment("DUNGEON_DIRECTOR_MODEL", "big-1")
	var main := _spawn_main()
	_check(main.generation_transport.get_script() == GenerationClient, "a URL selects the HTTP client")
	_check_eq(main.generation_transport.base_url, "http://127.0.0.1:9/", "base URL taken from the environment")
	_check_eq(main.coordinator.provider, "acme", "optional provider id forwarded")
	_check_eq(main.coordinator.model, "big-1", "optional model id forwarded")
	main.queue_free()
	OS.set_environment("DUNGEON_DIRECTOR_PROVIDER", "")
	OS.set_environment("DUNGEON_DIRECTOR_MODEL", "")
	OS.set_environment("DUNGEON_DIRECTOR_URL", "offline")
	_end()


func _test_restart_in_main_scene() -> void:
	print("\nTest: restarting resets the world and drops in-flight generation")
	var transport := ScriptedTransport.new()
	var main := _spawn_main(transport)
	main.state.player_pos = Vector2i(4, 2)
	main._process(0.016)
	var request: Dictionary = transport.submitted[0].request
	main._on_restart()
	main._process(0.016)
	transport.deliver(0, StubDirector.success_result(request, StubDirector.simple_plan(request, "r-001", "small", ["east"])))
	main._process(0.016)
	_check_eq(main.state.world.rooms.keys(), ["r-000"], "restarted world is back to the start room")
	_check_eq(main.state.world.pending_count(), 0, "no stale pending request")
	_check_eq(main.state.player_pos, Vector2i(4, 4), "player back at the start spawn")
	main.queue_free()
	_end()


func _test_shutdown_cancels_in_flight_requests() -> void:
	print("\nTest: shutdown cancels in-flight requests so no callback reaches a freed coordinator")
	var env := _env()
	_request_north(env)
	env.coord.shutdown()
	_check(env.transport.cancel_calls >= 1, "transport asked to cancel outstanding requests")
	_check(env.coord.in_flight.is_empty(), "coordinator forgot its in-flight requests")
	# Real client: cancelled requests never call back.
	await _settle_frames()
	var server := MiniHttpServer.new()
	server.start()
	server.mode = "hang"
	var client := GenerationClient.new()
	client.base_url = "http://127.0.0.1:%d" % server.port
	client.timeout_sec = 5.0
	root.add_child(client)
	var results: Array[Dictionary] = []
	client.submit({"a": 1}, {}, func(result: Dictionary) -> void: results.append(result))
	for i in range(10):
		server.poll()
		await process_frame
	client.cancel_all()
	for i in range(10):
		server.poll()
		await process_frame
	_check(results.is_empty(), "cancelled HTTP request never reports back")
	_check_eq(client.get_child_count(), 0, "cancelled HTTPRequest nodes are released")
	server.stop()
	client.queue_free()
	_end()


func _test_http_client() -> void:
	print("\nTest: HTTP client speaks POST /v1/generate and reports failures without blocking")
	await _settle_frames()
	var server := MiniHttpServer.new()
	_check(server.start(), "loopback test server started")
	var client := GenerationClient.new()
	client.base_url = "http://127.0.0.1:%d" % server.port
	client.timeout_sec = 0.5
	root.add_child(client)
	var env := _env()
	var request := _request_north(env)
	var results: Array[Dictionary] = []
	var sink := func(result: Dictionary) -> void: results.append(result)

	# success + provider/model selection travel in the query string
	server.response_body = StubDirector.success_body(request, StubDirector.simple_plan(request, "r-001", "small"))
	client.submit(request, {"provider": "acme corp", "model": "m/1"}, sink)
	_check(results.is_empty(), "submit returns before the response (non-blocking)")
	_check(await _wait_until(func(): return _served(server, results, 1)), "response delivered asynchronously")
	var seen: Dictionary = server.requests[0]
	_check_eq(seen.method, "POST", "uses POST")
	_check_eq(seen.target, "/v1/generate?provider=acme%20corp&model=m%2F1", "provider/model as URL-encoded query")
	_check(str(seen.headers.get("content-type", "")).begins_with("application/json"), "JSON content type")
	_check(DungeonContracts.validate_generation_request(JSON.parse_string(seen.body)).ok, "body is the canonical request")
	_check(results[0].transport_ok and results[0].http_status == 200, "transport ok, status 200")
	_check(DungeonContracts.parse_generation_response(results[0].body).ok, "body parses as canonical response")

	# no selectors -> no query string
	client.submit(request, {}, sink)
	_check(await _wait_until(func(): return _served(server, results, 2)), "second response")
	_check_eq(server.requests[1].target, "/v1/generate", "no query when provider/model are omitted")

	# canonical failure envelope with HTTP 502 still arrives as a body
	server.response_status = 502
	server.response_body = StubDirector.failure_body(request)
	client.submit(request, {}, sink)
	_check(await _wait_until(func(): return _served(server, results, 3)), "failure envelope delivered")
	_check(results[2].transport_ok and results[2].http_status == 502, "HTTP 502 reported with body")

	# hang -> timeout
	server.mode = "hang"
	client.submit(request, {"timeout_sec": 0.3}, sink)
	_check(await _wait_until(func(): return _served(server, results, 4), 900), "timeout reported")
	_check(not results[3].transport_ok and results[3].error_kind == "timeout", "timeout kind")

	# refused connection -> transport failure
	server.stop()
	client.submit(request, {}, sink)
	_check(await _wait_until(func(): return results.size() == 5, 900), "transport failure reported")
	_check(not results[4].transport_ok and results[4].error_kind == "transport_failure", "transport failure kind")
	client.queue_free()
	_end()


func _test_http_client_fetch_config() -> void:
	print("\nTest: HTTP client speaks GET /v1/config asynchronously")
	await _settle_frames()
	var server := MiniHttpServer.new()
	_check(server.start(), "loopback test server started for config")
	var client := GenerationClient.new()
	client.base_url = "http://127.0.0.1:%d" % server.port
	client.timeout_sec = 0.5
	root.add_child(client)

	var valid_config := {
		"default_provider": "rules-baseline",
		"default_model": "builtin-v1",
		"providers": [
			{
				"id": "rules-baseline",
				"available": true,
				"default_model": "builtin-v1",
				"models": ["builtin-v1"]
			}
		]
	}
	server.response_body = JSON.stringify(valid_config)

	var results: Array[Dictionary] = []
	client.fetch_config(func(r: Dictionary) -> void: results.append(r))
	_check(results.is_empty(), "fetch_config returns before network response")
	_check(await _wait_until(func(): return _served(server, results, 1)), "config response delivered asynchronously")

	var seen: Dictionary = server.requests[0]
	_check_eq(seen.method, "GET", "fetch_config uses GET")
	_check_eq(seen.target, "/v1/config", "target is /v1/config")
	_check_eq(results[0].transport_ok, true, "transport ok")
	_check_eq(results[0].http_status, 200, "status 200")

	var parsed := DungeonContracts.parse_director_config(results[0].body)
	_check(parsed.ok, "config body parsed via DungeonContracts")
	_check_eq(parsed.config.default_provider, "rules-baseline", "default_provider correct")

	# Server error -> reported with status code
	server.response_status = 503
	server.response_body = "service unavailable"
	client.fetch_config(func(r: Dictionary) -> void: results.append(r))
	_check(await _wait_until(func(): return _served(server, results, 2)), "503 error delivered")
	_check_eq(results[1].http_status, 503, "status 503 reported")

	# A config request shares this client with generation requests. Cancelling all
	# must finish the config callback so UI callers do not remain stuck fetching.
	server.mode = "hang"
	client.fetch_config(func(r: Dictionary) -> void: results.append(r))
	_check(await _wait_until(func():
		server.poll()
		return server.requests.size() == 3), "hanging config request reached the server")
	client.cancel_all()
	_check(await _wait_until(func(): return results.size() == 3), "cancelled config callback completed")
	_check(not results[2].transport_ok and results[2].error_kind == "cancelled", "config cancellation has a terminal result")

	server.stop()
	client.queue_free()
	_end()


func _test_coordinator_over_real_http() -> void:

	print("\nTest: coordinator + real client + loopback server commit and fall back asynchronously")
	await _settle_frames()
	var server := MiniHttpServer.new()
	server.start()
	var client := GenerationClient.new()
	client.base_url = "http://127.0.0.1:%d" % server.port
	client.timeout_sec = 0.4
	root.add_child(client)
	var state := GameState.new()
	state.enable_dynamic_world(1)
	state.world.run_id = "test-run-1"
	var coord := GenerationCoordinator.new(state, client)
	coord.timeout_msec = 400
	# 1) server answers with a valid room
	var probe_env := _env()
	var probe_request := _request_north(probe_env)
	server.response_body = StubDirector.success_body(probe_request, StubDirector.simple_plan(probe_request, "r-001", "small", ["east"]))
	state.player_pos = Vector2i(4, 2)
	coord.update()
	_check_eq(state.world.pending_count(), 1, "pending immediately, response comes later")
	var committed: bool = await _wait_until(func():
		server.poll()
		coord.update()
		return state.world.get_frontier(NORTH).status == DungeonWorld.STATUS_COMMITTED)
	_check(committed, "room committed from the HTTP response")
	_check(state.world.rooms.has("r-001") and state.world.rooms["r-001"].source == "director", "director room over HTTP")
	# 2) server hangs -> the next exit falls back after the timeout
	server.mode = "hang"
	state.player_pos = Vector2i(6, 4)
	coord.update()
	var fell_back: bool = await _wait_until(func():
		server.poll()
		coord.update()
		return state.world.get_frontier(EAST).status == DungeonWorld.STATUS_COMMITTED, 900)
	_check(fell_back, "hanging server -> fallback committed")
	var east_room: String = state.world.get_frontier(EAST).resolved_room_id
	_check_eq(state.world.rooms[east_room].source, "fallback", "east room is a fallback room")
	_check_eq(state.world.rooms[east_room].meta.fallback_reason, "timeout", "reason: timeout")
	# 3) server gone -> transport failure fallback
	server.stop()
	state.player_pos = Vector2i(2, 4)
	coord.update()
	var refused: bool = await _wait_until(func():
		coord.update()
		return state.world.get_frontier(WEST).status == DungeonWorld.STATUS_COMMITTED, 900)
	_check(refused, "unreachable server -> fallback committed")
	var west_room: String = state.world.get_frontier(WEST).resolved_room_id
	_check_eq(state.world.rooms[west_room].meta.fallback_reason, "transport_failure", "reason: transport_failure")
	_check(state.world.integrity_violations().is_empty(), "world integrity holds after mixed outcomes")
	client.queue_free()
	_end()


func _test_generation_recording() -> void:
	print("\nTest: GenerationRecorder persists requests and committed/fallback outcomes as JSONL")
	var log_path := "user://test_recordings/events.jsonl"
	if FileAccess.file_exists(log_path):
		DirAccess.remove_absolute(ProjectSettings.globalize_path(log_path))
	var rec := GenerationRecorder.new(log_path)
	_check(rec.is_active(), "recorder opened JSONL file for writing")
	var env := _env()
	env.coord.recorder = rec
	var request := _request_north(env)
	var plan := StubDirector.simple_plan(request, "r-rec-1", "small", ["east"])
	env.transport.deliver(0, StubDirector.success_result(request, plan))
	env.coord.update()
	_check_eq(env.state.world.get_frontier(NORTH).status, DungeonWorld.STATUS_COMMITTED, "north committed")
	env.state.reset_game()
	env.coord.reset_for_current_world()
	_check(rec.is_active(), "recorder remains active across a run restart")
	rec.close()

	_check(FileAccess.file_exists(log_path), "JSONL file exists on disk")
	var file := FileAccess.open(log_path, FileAccess.READ)
	_check(file != null, "opened recorded JSONL file")
	var line := file.get_line()
	_check(line != "", "at least one line recorded")
	var parsed: Variant = JSON.parse_string(line)
	_check(parsed is Dictionary, "recorded line is valid JSON")
	if parsed is Dictionary:
		_check_eq(parsed.get("contract_version", ""), DungeonContracts.CONTRACT_VERSION, "contract_version matches")
		_check_eq(parsed.get("request_id", ""), request.request_id, "request_id matches")
		_check_eq(parsed.get("run_id", ""), request.run_id, "run_id matches")
		_check_eq(parsed.get("outcome", ""), "committed", "outcome recorded as committed")
		_check_eq(parsed.get("source", ""), "director", "source recorded as director")
		_check(parsed.has("request"), "record contains canonical request")
		_check(parsed.has("result"), "record contains result payload")
		_check(parsed.has("seed"), "record contains seed")
		var target_exit: Dictionary = parsed.get("target_exit", {})
		_check_eq(target_exit.get("direction", ""), "north", "target exit recorded")
	file.close()
	DirAccess.remove_absolute(ProjectSettings.globalize_path(log_path))
	_end()


func _test_recorder_preserves_resolved_provider_and_metadata() -> void:
	print("\nTest: Recorder preserves resolved provider/model and response metadata on default-provider success")
	var log_path := "user://test_recordings/resolved_meta.jsonl"
	if FileAccess.file_exists(log_path):
		DirAccess.remove_absolute(ProjectSettings.globalize_path(log_path))
	var rec := GenerationRecorder.new(log_path)
	var env := _env()
	env.coord.recorder = rec
	# Coordinator has blank provider/model (director defaults configured)
	_check_eq(env.coord.provider, "", "coordinator provider is blank default")
	_check_eq(env.coord.model, "", "coordinator model is blank default")

	var request := _request_north(env)
	var plan := StubDirector.simple_plan(request, "r-meta-1", "small", ["east"])
	var body_dict: Dictionary = {
		"contract_version": "1.0.0",
		"request_id": request.request_id,
		"run_id": request.run_id,
		"success": true,
		"room": plan,
		"metadata": {
			"provider": "rules-baseline",
			"model": "builtin-v1",
			"started_at": "2026-09-19T10:00:00.000Z",
			"completed_at": "2026-09-19T10:00:00.012Z",
			"latency_ms": 12.34,
			"usage": {"input_tokens": 120, "output_tokens": 80, "estimated_cost_usd": 0.0002},
			"provider_metadata": {"adapter": "rules_direct"},
		},
	}
	env.transport.deliver(0, StubDirector.ok_result(JSON.stringify(body_dict), 200))
	env.coord.update()
	_check_eq(env.state.world.get_frontier(NORTH).status, DungeonWorld.STATUS_COMMITTED, "north committed")
	rec.close()

	var file := FileAccess.open(log_path, FileAccess.READ)
	_check(file != null, "opened resolved metadata log")
	var line := file.get_line()
	var entry: Variant = JSON.parse_string(line)
	_check(entry is Dictionary, "recorded entry is JSON")
	if entry is Dictionary:
		_check_eq(entry.get("provider", ""), "rules-baseline", "entry resolved provider from response metadata")
		_check_eq(entry.get("model", ""), "builtin-v1", "entry resolved model from response metadata")
		_check_eq(entry.get("outcome", ""), "committed", "entry outcome is committed")
		var resp_meta: Dictionary = entry.get("response_metadata", {})
		_check_eq(resp_meta.get("provider", ""), "rules-baseline", "response_metadata provider matches")
		_check_eq(resp_meta.get("model", ""), "builtin-v1", "response_metadata model matches")
		_check_eq(float(resp_meta.get("latency_ms", 0.0)), 12.34, "response_metadata latency_ms matches")
		var usage: Dictionary = resp_meta.get("usage", {})
		_check_eq(int(usage.get("input_tokens", 0)), 120, "usage input_tokens matches")
		_check_eq(int(usage.get("output_tokens", 0)), 80, "usage output_tokens matches")
		_check_eq(resp_meta.get("provider_metadata", {}).get("adapter", ""), "rules_direct", "provider_metadata matches")
		# Verify placement metadata is also preserved
		var meta: Dictionary = entry.get("metadata", {})
		_check_eq(meta.get("room_type", ""), "room", "placement metadata room_type preserved")
		_check_eq(meta.get("size_used", ""), "small", "placement metadata size_used preserved")
	file.close()
	DirAccess.remove_absolute(ProjectSettings.globalize_path(log_path))
	_end()


func _test_recorder_captures_failure_envelope_and_fallback_without_secrets() -> void:
	print("\nTest: Recorder captures failure envelope and fallback without secrets")
	var log_path := "user://test_recordings/failure_meta.jsonl"
	if FileAccess.file_exists(log_path):
		DirAccess.remove_absolute(ProjectSettings.globalize_path(log_path))
	var rec := GenerationRecorder.new(log_path)
	var env := _env()
	env.coord.recorder = rec
	var request := _request_north(env)
	var failure_body_dict: Dictionary = {
		"contract_version": "1.0.0",
		"request_id": request.request_id,
		"run_id": request.run_id,
		"success": false,
		"room": null,
		"metadata": {
			"provider": "groq",
			"model": "openai/gpt-oss-120b",
			"started_at": "2026-09-19T10:00:00.000Z",
			"completed_at": "2026-09-19T10:00:00.045Z",
			"latency_ms": 45.0,
			"error": {
				"code": "schema_violation",
				"message": "Provider output did not match the room contract.",
			},
		},
	}
	# Transport-only fields model request-library diagnostics that must never be
	# copied into the canonical recording.
	var transport_result := StubDirector.ok_result(JSON.stringify(failure_body_dict), 502)
	transport_result["request_headers"] = {"Authorization": "Bearer recording-canary"}
	transport_result["api_key"] = "sk-recording-canary"
	# Transport delivers HTTP 502 with canonical failure envelope.
	env.transport.deliver(0, transport_result)
	env.coord.update()

	# World should fall back and commit local rules baseline
	_check_eq(env.state.world.get_frontier(NORTH).status, DungeonWorld.STATUS_COMMITTED, "north committed via fallback")
	_check_eq(_last_fallback_reason(env.state), "provider_failure:schema_violation", "fallback reason recorded on world")
	rec.close()

	var file := FileAccess.open(log_path, FileAccess.READ)
	_check(file != null, "opened failure metadata log")
	var line := file.get_line()
	var entry: Variant = JSON.parse_string(line)
	_check(entry is Dictionary, "recorded fallback entry is JSON")
	if entry is Dictionary:
		_check_eq(entry.get("outcome", ""), "fallback", "outcome is fallback")
		_check_eq(entry.get("provider", ""), "groq", "resolved failing provider groq")
		_check_eq(entry.get("model", ""), "openai/gpt-oss-120b", "resolved failing model")
		_check_eq(entry.get("fallback_reason", ""), "provider_failure:schema_violation", "fallback reason recorded")
		var resp_meta: Dictionary = entry.get("response_metadata", {})
		_check_eq(float(resp_meta.get("latency_ms", 0.0)), 45.0, "failure latency preserved")
		var err: Dictionary = resp_meta.get("error", {})
		_check_eq(err.get("code", ""), "schema_violation", "error code matches")
		_check_eq(err.get("message", ""), "Provider output did not match the room contract.", "error message matches")
		_check(not line.contains("sk-recording-canary"), "transport api key is excluded from JSONL")
		_check(not line.contains("Bearer recording-canary"), "transport auth header is excluded from JSONL")
	file.close()
	DirAccess.remove_absolute(ProjectSettings.globalize_path(log_path))
	_end()
