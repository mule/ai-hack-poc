extends SceneTree
## Issue #25: Game telemetry sink, schema validation, event lifecycle,
## safe drop/backpressure, attribute allowlist/redaction, and offline mode.

const GameTelemetrySink = preload("res://world/game_telemetry_sink.gd")
const GameState = preload("res://src/game_state.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")
const GenerationCoordinator = preload("res://world/generation_coordinator.gd")
const ScriptedTransport = preload("res://tests/support/scripted_transport.gd")
const StubDirector = preload("res://tests/support/stub_director.gd")
const RoomGenerator = preload("res://generation/room_generator.gd")

var _batches: Array[String] = []
var _checks := 0
var _failures := PackedStringArray()
var _completed := false
var _reached_end := false


func _initialize() -> void:
	create_timer(30.0).timeout.connect(_on_watchdog)
	process_frame.connect(_run, CONNECT_ONE_SHOT)


func _on_watchdog() -> void:
	printerr("FAILED: game telemetry test suite exceeded its 30s watchdog")
	quit(2)


func _run() -> void:
	print("=== Running test_game_telemetry.gd ===")
	await _step("test_event_validation_and_schema", _test_event_validation_and_schema)
	await _step("test_attribute_allowlist_and_sanitizer", _test_attribute_allowlist_and_sanitizer)
	await _step("test_sensitive_key_and_value_redaction", _test_sensitive_key_and_value_redaction)
	await _step("test_queue_backpressure_safe_drops", _test_queue_backpressure_safe_drops)
	await _step("test_batch_flush_and_custom_http", _test_batch_flush_and_custom_http)
	await _step("test_offline_and_disabled_mode", _test_offline_and_disabled_mode)
	await _step("test_full_generation_lifecycle_telemetry", _test_full_generation_lifecycle_telemetry)
	await _step("test_fallback_and_normalization_telemetry", _test_fallback_and_normalization_telemetry)
	await _step("test_room_transition_telemetry", _test_room_transition_telemetry)
	await _step("test_hidden_door_reveal", _test_hidden_door_reveal)
	await _step("test_large_batches", _test_large_batches)
	await _step("test_normalized_outcome_and_failing_provider", _test_normalized_outcome_and_failing_provider)
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
	var fixture_out := OS.get_environment("DUNGEON_TELEMETRY_FIXTURE_OUT")
	if fixture_out != "":
		var fixture := FileAccess.open(fixture_out, FileAccess.WRITE)
		_check(fixture != null, "Can write real emitted telemetry batches")
		if fixture != null:
			fixture.store_string(JSON.stringify({"batches": _batches}))
			fixture.close()
	print("\n--- Telemetry Test Results: %d Passed, %d Failed (completed=%s) ---" % [_checks - _failures.size(), _failures.size(), str(_completed)])
	if _failures.is_empty() and _completed:
		print("SUCCESS: All game telemetry checks passed!")
		quit(0)
	else:
		printerr("FAILED: game telemetry checks did not pass.")
		quit(1)


# --- Tests -------------------------------------------------------------------


func _test_event_validation_and_schema() -> void:
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true

	# Valid event
	var ok1 := sink.enqueue_event(
		"frontier.discovered",
		"run-123",
		null,
		{"frontier_id": "r-000:north", "depth": 1, "exit_direction": "north"}
	)
	_check(ok1, "Valid frontier.discovered event accepted")
	_check_eq(sink.get_stats().enqueued, 1, "One event enqueued")

	# Unknown event name
	var ok_bad_name := sink.enqueue_event("unknown.event", "run-123", "req-1", {})
	_check(!ok_bad_name, "Unknown event name rejected")
	_check_eq(sink.get_stats().dropped_invalid, 1, "dropped_invalid incremented on unknown event")

	# Invalid run_id (invalid characters)
	var ok_bad_run := sink.enqueue_event("frontier.discovered", "run with spaces!", null, {})
	_check(!ok_bad_run, "Invalid run_id rejected")

	# Null request_id where prohibited
	var ok_null_req := sink.enqueue_event("generation.accepted", "run-123", null, {})
	_check(!ok_null_req, "Null request_id on generation.accepted rejected")

	# Valid traceparent
	var ok_tp := sink.enqueue_event(
		"generation.accepted",
		"run-123",
		"req-1",
		{"provider": "mock-ai", "model": "test-v1", "room_type": "room", "room_size": "medium", "danger": 2},
		"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"
	)
	_check(ok_tp, "Valid event with W3C traceparent accepted")

	# Invalid traceparent
	var ok_bad_tp := sink.enqueue_event(
		"generation.accepted",
		"run-123",
		"req-1",
		{},
		"bad-traceparent-format"
	)
	_check(!ok_bad_tp, "Invalid traceparent rejected")

	_end()


func _test_attribute_allowlist_and_sanitizer() -> void:
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true

	# Attributes not in allowlist should be stripped
	var raw_attrs := {
		"room_type": "room",
		"danger": 3,
		"disallowed_key": "malicious_or_unexpected",
		"another_unregistered_field": 12345,
	}
	var sanitized := GameTelemetrySink.sanitize_event_attributes("room.committed", raw_attrs)
	_check(sanitized.has("room_type"), "Allowlisted key preserved")
	_check(sanitized.has("danger"), "Allowlisted int key preserved")
	_check(!sanitized.has("disallowed_key"), "Disallowed key stripped")
	_check(!sanitized.has("another_unregistered_field"), "Unregistered field stripped")

	# Provider is forbidden on generation.queued in PR29 schema!
	var queued_with_prov := {"frontier_id": "r-000:north", "depth": 1, "exit_direction": "north", "provider": "bad-prov"}
	var res_queued := GameTelemetrySink.sanitize_event_attributes("generation.queued", queued_with_prov)
	_check(!res_queued.has("provider"), "Provider is forbidden on generation.queued and stripped")

	_check_eq(GameTelemetrySink.sanitize_event_attributes("generation.accepted", {"model": "@cf/typesafe/jev"}).get("model"), "@cf/typesafe/jev", "Cloudflare model IDs retain leading @")
	# String length clamping / validation
	var long_str := "a".repeat(129)
	var too_long_attrs := {"provider": long_str}
	var res_too_long := GameTelemetrySink.sanitize_event_attributes("generation.accepted", too_long_attrs)
	_check(!res_too_long.has("provider"), "Overly long string (>128 chars) rejected")

	# Newlines forbidden in string values
	var newline_attrs := {"provider": "provider\nwith_newline"}
	var res_nl := GameTelemetrySink.sanitize_event_attributes("generation.accepted", newline_attrs)
	_check(!res_nl.has("provider"), "String containing newline rejected")

	# Int range clamping (danger 1..5, depth 1..128)
	var clamp_attrs := {"danger": 999, "depth": 500}
	var res_clamp := GameTelemetrySink.sanitize_event_attributes("generation.accepted", clamp_attrs)
	_check_eq(res_clamp.get("danger"), 5, "Danger clamped to 5")

	_end()


func _test_sensitive_key_and_value_redaction() -> void:
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true

	var sensitive_attrs := {
		"room_type": "room",
		"authorization": "Bearer secret-token",
		"api_key": "sk-1234567890",
		"prompt": "Tell me a secret",
		"raw_payload": "{'secret': 123}",
		"password": "my_secret_password",
	}
	var sanitized := GameTelemetrySink.sanitize_event_attributes("room.committed", sensitive_attrs)
	_check(sanitized.has("room_type"), "Normal field preserved")
	_check(!sanitized.has("authorization"), "authorization redacted")
	_check(!sanitized.has("api_key"), "api_key redacted")
	_check(!sanitized.has("prompt"), "prompt redacted")
	_check(!sanitized.has("raw_payload"), "raw_payload redacted")
	_check(!sanitized.has("password"), "password redacted")

	# Secret tokens inside valid scalar string values should also be rejected
	var token_attrs := {
		"provider": "Bearer sk-999999999",
	}
	var res_tok := GameTelemetrySink.sanitize_event_attributes("generation.accepted", token_attrs)
	_check(!res_tok.has("provider"), "Value with bearer token pattern dropped")

	_end()


func _test_queue_backpressure_safe_drops() -> void:
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true
	sink.max_queue_size = 5 # small queue for backpressure test

	for i in range(10):
		var ok := sink.enqueue_event(
			"frontier.discovered",
			"run-1",
			null,
			{"frontier_id": "r-000:exit-%d" % i, "depth": 1, "exit_direction": "north"}
		)
		_check(ok, "Event %d enqueue returned true" % i)

	var stats := sink.get_stats()
	_check_eq(stats.queued, 5, "Queue capped at max_queue_size (5)")
	_check_eq(stats.enqueued, 10, "10 events successfully accepted for queuing")
	_check_eq(stats.dropped_backpressure, 5, "5 oldest events dropped under backpressure")
	_check_eq(stats.dropped_total, 5, "Total dropped equals backpressure drops")

	_end()


func _test_batch_flush_and_custom_http() -> void:
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true
	sink.batch_size = 3
	sink.flush_interval_sec = 100.0 # prevent auto-flush

	var dispatched_batches: Array[Dictionary] = []
	sink.custom_http_post = func(url: String, headers: PackedStringArray, body: String, on_done: Callable) -> void:
		var parsed: Dictionary = JSON.parse_string(body)
		_batches.append(body)
		dispatched_batches.append({"url": url, "payload": parsed})
		on_done.call(true, parsed.events.size())

	# Enqueue 3 events -> triggers batch flush
	for i in range(3):
		sink.enqueue_event("frontier.discovered", "run-1", null, {"frontier_id": "f-%d" % i, "depth": 1, "exit_direction": "east"})
		sink.poll(0.0)

	_check_eq(dispatched_batches.size(), 1, "One batch dispatched upon reaching batch_size")
	var batch: Dictionary = dispatched_batches[0]
	_check_eq(batch.url, "http://test-collector:8000/v1/telemetry/game", "Target URL is /v1/telemetry/game")
	_check_eq(batch.payload.get("schema_version"), "1", "Payload schema_version is '1'")
	_check_eq(batch.payload.get("events", []).size(), 3, "Batch contains 3 events")

	var stats := sink.get_stats()
	_check_eq(stats.batches_sent, 1, "1 batch sent")
	_check_eq(stats.events_sent, 3, "3 events sent")
	_check_eq(stats.queued, 0, "Queue empty after flush")

	_end()


func _test_offline_and_disabled_mode() -> void:
	var sink_off := GameTelemetrySink.new("offline")
	_check(!sink_off.enabled, "Sink with url='offline' is disabled")
	var ok := sink_off.enqueue_event("frontier.discovered", "run-1", null, {"frontier_id": "f-1", "depth": 1, "exit_direction": "north"})
	_check(!ok, "Enqueue fails safely when disabled")
	_check_eq(sink_off.get_stats().queued, 0, "No events queued when disabled")

	var unset_env := OS.get_environment("DUNGEON_TELEMETRY_ENABLED")
	OS.unset_environment("DUNGEON_TELEMETRY_ENABLED")
	var default_sink := GameTelemetrySink.new()
	_check(!default_sink.enabled, "Sink is opt-in by default")
	OS.set_environment("DUNGEON_TELEMETRY_ENABLED", unset_env)
	default_sink.free()
	var sink_none := GameTelemetrySink.new("none")
	_check(!sink_none.enabled, "Sink with url='none' is disabled")

	_end()


func _test_full_generation_lifecycle_telemetry() -> void:
	var events_captured: Array[Dictionary] = []
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true
	sink.custom_http_post = func(_url: String, _headers: PackedStringArray, body: String, on_done: Callable) -> void:
		var parsed: Dictionary = JSON.parse_string(body)
		_batches.append(body)
		for ev in parsed.get("events", []):
			events_captured.append(ev)
		on_done.call(true, parsed.events.size())

	var state := GameState.new()
	state.telemetry_sink = sink
	state.enable_dynamic_world(1)

	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)
	coord.telemetry_sink = sink
	coord.provider = "test-provider"
	coord.model = "test-model"

	# Initial world start emits room.committed and frontier.discovered
	sink.poll(1.0)
	var names_at_start: Array[String] = []
	for ev in events_captured:
		names_at_start.append(ev.event_name)

	_check(names_at_start.has("room.committed"), "Initial start room emitted room.committed")
	_check(names_at_start.has("frontier.discovered"), "Start exits emitted frontier.discovered")
	_check(!names_at_start.has("door.revealed"), "Initial start exits do NOT emit door.revealed without request_id")

	events_captured.clear()

	# Move near north door and trigger coordinator update
	state.player_pos = Vector2i(4, 2)
	coord.update()

	sink.poll(1.0)
	var queued_and_sent: Array[String] = []
	for ev in events_captured:
		queued_and_sent.append(ev.event_name)

	_check(queued_and_sent.has("generation.queued"), "generation.queued captured")
	_check(queued_and_sent.has("generation.sent"), "generation.sent captured")

	var req_id: String = events_captured[0].request_id
	var timing := {"now": 1450}
	coord.in_flight[req_id].started = 1000
	coord.clock = func(): return timing.now
	events_captured.clear()

	# Complete generation with valid plan
	var plan := {
		"room_id": "r-001",
		"depth": 1,
		"room_type": "room",
		"size": "medium",
		"danger": 2,
		"exits": [{"direction": "south", "kind": "door"}, {"direction": "east", "kind": "door"}],
	}
	var resp := {
		"contract_version": "1.0.0",
		"request_id": req_id,
		"run_id": state.world.run_id,
		"success": true,
		"room": plan,
		"metadata": {
			"provider": "test-provider",
			"model": "test-model",
			"started_at": "2026-09-22T08:00:00Z",
			"completed_at": "2026-09-22T08:00:01Z",
			"latency_ms": 120.0,
		},
	}
	transport.deliver(0, {"transport_ok": true, "http_status": 200, "body": JSON.stringify(resp)})
	sink.poll(1.0)

	var lifecycle_names: Array[String] = []
	for ev in events_captured:
		lifecycle_names.append(ev.event_name)

	var response_events := events_captured.filter(func(e): return e.event_name == "generation.response_received")
	_check_eq(response_events[0].attributes.network_ms, 450.0, "Network time uses client clock, not provider latency 120ms")
	_check(lifecycle_names.has("generation.response_received"), "Emitted generation.response_received")
	_check(lifecycle_names.has("generation.accepted"), "Emitted generation.accepted")
	_check(lifecycle_names.has("room.committed"), "Emitted room.committed for r-001")
	_check(!lifecycle_names.has("door.revealed"), "Placement never reports an unopened door as revealed")
	_check(lifecycle_names.find("generation.accepted") < lifecycle_names.find("room.committed"), "Acceptance precedes room commit")
	_check(lifecycle_names.has("frontier.discovered"), "Emitted frontier.discovered for east exit of r-001")

	_end()


func _test_fallback_and_normalization_telemetry() -> void:
	var events_captured: Array[Dictionary] = []
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true
	sink.custom_http_post = func(_url: String, _headers: PackedStringArray, body: String, on_done: Callable) -> void:
		var parsed: Dictionary = JSON.parse_string(body)
		_batches.append(body)
		for ev in parsed.get("events", []):
			events_captured.append(ev)
		on_done.call(true, parsed.events.size())

	var state := GameState.new()
	state.telemetry_sink = sink
	state.enable_dynamic_world(1)

	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)
	coord.telemetry_sink = sink
	coord.provider = "test-provider"
	coord.model = "test-model"

	# Move near east door and update
	state.player_pos = Vector2i(6, 4)
	coord.update()
	sink.poll(1.0)
	events_captured.clear()

	# Trigger fallback via transport failure
	transport.deliver(0, {"transport_ok": false, "error_kind": "transport_failure", "http_status": 0, "body": ""})
	sink.poll(1.0)

	var names: Array[String] = []
	for ev in events_captured:
		names.append(ev.event_name)

	_check(!names.has("generation.rejected"), "Transport failure is not a game plan rejection")
	_check(names.has("generation.fallback_applied"), "Emitted generation.fallback_applied")
	_check(names.has("room.committed"), "Emitted room.committed for fallback room")

	_end()


func _test_room_transition_telemetry() -> void:
	var all_captured_events: Array[Dictionary] = []
	var captured_raw := {"json": ""}
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true
	sink.custom_http_post = func(_url: String, _headers: PackedStringArray, body: String, on_done: Callable) -> void:
		captured_raw["json"] = body
		var parsed: Dictionary = JSON.parse_string(body)
		_batches.append(body)
		for ev in parsed.get("events", []):
			all_captured_events.append(ev)
		on_done.call(true, parsed.events.size())

	var state := GameState.new()
	state.active_provider = "test-provider"
	state.active_model = "test-model"
	state.telemetry_sink = sink
	state.enable_dynamic_world(1)

	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)
	coord.provider = "test-provider"
	coord.model = "test-model"
	coord.telemetry_sink = sink

	# Trigger generation for north exit
	state.player_pos = Vector2i(4, 2)
	coord.update()

	# Complete north room with a valid plan
	var req_id: String = transport.submitted[0].request.request_id
	var plan2 := {
		"room_id": "r-002",
		"depth": 1,
		"room_type": "chamber",
		"size": "small",
		"danger": 1,
		"exits": [{"direction": "south", "kind": "door"}],
	}
	var resp2 := {
		"contract_version": "1.0.0",
		"request_id": req_id,
		"run_id": state.world.run_id,
		"success": true,
		"room": plan2,
		"metadata": {
			"provider": "test-provider",
			"model": "test-model",
			"started_at": "2026-09-22T08:00:00Z",
			"completed_at": "2026-09-22T08:00:01Z",
			"latency_ms": 50.0,
		},
	}
	transport.deliver(0, {"transport_ok": true, "http_status": 200, "body": JSON.stringify(resp2)})

	# Step player across the door into room 2
	# Start room player is at (4, 2), door at (4, 1). First step opens the closed door.
	# Second step moves into door (4, 1). Third step moves into r-002 floor tile (4, 0).
	state.player_action_step(Vector2i.UP) # Open door
	state.player_action_step(Vector2i.UP) # Walk into door (4, 1)
	state.player_action_step(Vector2i.UP) # Walk into r-002 (4, 0)

	sink.poll(1.0)
	var entered_events := all_captured_events.filter(func(e): return e.event_name == "room.entered")
	_check_eq(entered_events.size(), 1, "room.entered captured when player entered r-002")
	if not entered_events.is_empty():
		_check_eq(entered_events[0].attributes.room_id, "r-002", "room_id matches r-002")
		_check(entered_events[0].attributes.has("time_to_entry_ms"), "time_to_entry_ms attribute present")
		_check_eq(entered_events[0].attributes.provider, "test-provider", "provider attribute present on room.entered")
		_check_eq(entered_events[0].attributes.model, "test-model", "model attribute present on room.entered")


	_end()


func _test_hidden_door_reveal() -> void:
	var sink := GameTelemetrySink.new()
	sink.enabled = true
	var events: Array[Dictionary] = []
	sink.custom_http_post = func(_url, _headers, body, done):
		var parsed: Dictionary = JSON.parse_string(body)
		_batches.append(body)
		for event in parsed.events:
			events.append(event)
		done.call(true, parsed.events.size())
	var state := GameState.new()
	state.telemetry_sink = sink
	state.enable_dynamic_world(1)
	var door := Vector2i(4, 1)
	state.map_tiles[door] = GameState.TileType.SECRET_DOOR
	state.player_pos = Vector2i(4, 2)
	sink.poll(1.0)
	_check(events.filter(func(e): return e.event_name == "door.revealed").is_empty(), "Hidden door is not revealed at commit")
	events.clear()
	state.player_action_step(Vector2i.UP)
	sink.poll(1.0)
	var reveals := events.filter(func(e): return e.event_name == "door.revealed")
	_check_eq(reveals.size(), 1, "Opening hidden door emits exactly one reveal")
	_check(reveals[0].attributes.has("time_to_visible_ms"), "Reveal carries locally measured time")
	state.player_action_step(Vector2i.UP)
	sink.poll(1.0)
	_check_eq(events.filter(func(e): return e.event_name == "door.revealed").size(), 1, "Revisiting open door does not duplicate reveal")
	_check(GameTelemetrySink.sanitize_event_attributes("room.committed", {"has_secret": true}).get("has_secret") == true, "Semantic has_secret survives sensitive-key redaction")
	sink.free()
	_end()


func _test_large_batches() -> void:
	var sink := GameTelemetrySink.new()
	sink.enabled = true
	var observed := {"count": 0, "max_bytes": 0}
	sink.custom_http_post = func(_url, _headers, body, done):
		var parsed: Dictionary = JSON.parse_string(body)
		_batches.append(body)
		observed.count += parsed.events.size()
		observed.max_bytes = maxi(observed.max_bytes, body.to_utf8_buffer().size())
		done.call(true, parsed.events.size())
	for i in range(100):
		sink.enqueue_event("room.committed", "r".repeat(64), "q".repeat(64), {
			"room_id": "a".repeat(64), "frontier_id": "b".repeat(120) + ":north",
			"provider": "p".repeat(128), "model": "m".repeat(128),
			"shadow_comparison_id": "s".repeat(64), "replay_id": "e".repeat(64),
			"room_type": "room", "room_size": "medium", "danger": 1, "has_secret": true,
		})
	for i in range(10):
		sink.poll(1.0)
	_check_eq(observed.count, 100, "Large batches are split without dropping events")
	_check(observed.max_bytes <= 65536, "All POST bodies fit director 64KiB limit")
	_check(!GameTelemetrySink._is_valid_traceparent("00-zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz-1111111111111111-01"), "Trace context rejects nonhex")
	sink.free()
	_end()


func _test_normalized_outcome_and_failing_provider() -> void:
	for scenario in ["normalized", "provider_failure", "invalid_plan"]:
		var sink := GameTelemetrySink.new()
		sink.enabled = true
		var events: Array[Dictionary] = []
		sink.custom_http_post = func(_url, _headers, body, done):
			var parsed: Dictionary = JSON.parse_string(body)
			_batches.append(body)
			for event in parsed.events:
				events.append(event)
			done.call(true, parsed.events.size())
		var state := GameState.new()
		state.telemetry_sink = sink
		state.enable_dynamic_world(1)
		state.world.run_id = "test-run-1"
		var transport := ScriptedTransport.new()
		var coord := GenerationCoordinator.new(state, transport)
		coord.telemetry_sink = sink
		# Blank selectors: the server chooses the actual provider/model.
		state.player_pos = Vector2i(4, 2)
		coord.update()
		var request: Dictionary = transport.submitted[0].request
		var result: Dictionary
		if scenario == "normalized":
			state.world.tiles[Vector2i(-1, -4)] = GameState.TileType.WALL
			var plan := StubDirector.simple_plan(request, "r-001", "small", ["west", "east"])
			result = StubDirector.success_result(request, plan)
		elif scenario == "invalid_plan":
			var plan := StubDirector.simple_plan(request, "r-001", "small", ["east"])
			plan.depth = 2
			result = StubDirector.success_result(request, plan)
		else:
			result = StubDirector.ok_result(StubDirector.failure_body(request), 504)
		var response: Dictionary = JSON.parse_string(result.body)
		if scenario == "provider_failure":
			response.metadata.error.code = "provider_timeout"
		response.metadata.provider = "actual-provider"
		response.metadata.model = "actual-model"
		result.body = JSON.stringify(response)
		transport.deliver(0, result)
		sink.poll(1.0)
		var outcomes: Array[String] = []
		for event in events:
			outcomes.append(event.event_name)
		if scenario == "normalized":
			_check(outcomes.has("generation.normalized"), "Pruned exit emits normalized decision")
			_check(!outcomes.has("generation.accepted"), "Normalized decision is exclusive of accepted")
			# Ignore the initial room's commit when comparing the generated lifecycle.
			var generated := events.filter(func(e): return e.request_id == request.request_id)
			var stages: Array[String] = []
			for event in generated:
				stages.append(event.event_name)
			_check(stages.find("generation.normalized") < stages.find("room.committed"), "Normalization precedes its room commit")
		else:
			var fallback := events.filter(func(e): return e.event_name == "generation.fallback_applied")
			_check_eq(fallback.size(), 1, "Fallback applied only once after successful placement")
			_check_eq(fallback[0].attributes.provider, "actual-provider", "Fallback names actual failing provider, not empty selector")
			_check_eq(fallback[0].attributes.model, "actual-model", "Fallback names actual failing model")
			_check_eq(fallback[0].attributes.fallback_reason, "provider_timeout" if scenario == "provider_failure" else "rejected_by_game", "Fallback reason retains failure classification")
			_check_eq(outcomes.has("generation.rejected"), scenario == "invalid_plan", "Only rejected game plans emit generation.rejected")
			var committed := events.filter(func(e): return e.event_name == "room.committed" and e.request_id == request.request_id)
			_check_eq(committed[0].attributes.provider, "rules-baseline", "Committed fallback attributes materializing baseline")
			_check(committed[0].attributes.has("materialization_ms"), "Committed room contains client materialization timing")
		sink.free()
	_end()
