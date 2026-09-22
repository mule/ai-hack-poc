extends SceneTree
## Issue #25: Game telemetry sink, schema validation, event lifecycle,
## safe drop/backpressure, attribute allowlist/redaction, and offline mode.

const GameTelemetrySink = preload("res://world/game_telemetry_sink.gd")
const GameState = preload("res://src/game_state.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")
const GenerationCoordinator = preload("res://world/generation_coordinator.gd")
const ScriptedTransport = preload("res://tests/support/scripted_transport.gd")
const RoomGenerator = preload("res://generation/room_generator.gd")

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

	# String length clamping / validation
	var long_str := "a".repeat(129)
	var too_long_attrs := {"provider": long_str}
	var res_too_long := GameTelemetrySink.sanitize_event_attributes("generation.queued", too_long_attrs)
	_check(!res_too_long.has("provider"), "Overly long string (>128 chars) rejected")

	# Newlines forbidden in string values
	var newline_attrs := {"provider": "provider\nwith_newline"}
	var res_nl := GameTelemetrySink.sanitize_event_attributes("generation.queued", newline_attrs)
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
	var res_tok := GameTelemetrySink.sanitize_event_attributes("generation.queued", token_attrs)
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

	var sink_none := GameTelemetrySink.new("none")
	_check(!sink_none.enabled, "Sink with url='none' is disabled")

	_end()


func _test_full_generation_lifecycle_telemetry() -> void:
	var events_captured: Array[Dictionary] = []
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true
	sink.custom_http_post = func(_url: String, _headers: PackedStringArray, body: String, on_done: Callable) -> void:
		var parsed: Dictionary = JSON.parse_string(body)
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

	# Initial world start emits room.committed, frontier.discovered, and door.revealed
	sink.poll(1.0)
	var names_at_start: Array[String] = []
	for ev in events_captured:
		names_at_start.append(ev.event_name)

	_check(names_at_start.has("room.committed"), "Initial start room emitted room.committed")
	_check(names_at_start.has("frontier.discovered"), "Start exits emitted frontier.discovered")
	_check(names_at_start.has("door.revealed"), "Start exits emitted door.revealed")

	events_captured.clear()

	# Move near north door and trigger coordinator update
	state.player_pos = Vector2i(4, 2)
	coord.update()

	sink.poll(1.0)
	_check_eq(events_captured.size(), 1, "generation.queued captured")
	_check_eq(events_captured[0].event_name, "generation.queued", "Event is generation.queued")
	_check_eq(events_captured[0].attributes.provider, "test-provider", "Provider correlated")
	_check_eq(events_captured[0].attributes.model, "test-model", "Model correlated")

	var req_id: String = events_captured[0].request_id
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

	_check(lifecycle_names.has("generation.accepted"), "Emitted generation.accepted")
	_check(lifecycle_names.has("room.committed"), "Emitted room.committed for r-001")
	_check(lifecycle_names.has("frontier.discovered"), "Emitted frontier.discovered for east exit of r-001")
	_check(lifecycle_names.has("door.revealed"), "Emitted door.revealed for east exit of r-001")

	_end()


func _test_fallback_and_normalization_telemetry() -> void:
	var events_captured: Array[Dictionary] = []
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true
	sink.custom_http_post = func(_url: String, _headers: PackedStringArray, body: String, on_done: Callable) -> void:
		var parsed: Dictionary = JSON.parse_string(body)
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

	_check(names.has("generation.rejected"), "Emitted generation.rejected on transport failure")
	_check(names.has("generation.fallback_applied"), "Emitted generation.fallback_applied")
	_check(names.has("room.committed"), "Emitted room.committed for fallback room")

	_end()


func _test_room_transition_telemetry() -> void:
	var events_captured: Array[Dictionary] = []
	var sink := GameTelemetrySink.new("http://test-collector:8000")
	sink.enabled = true
	sink.custom_http_post = func(_url: String, _headers: PackedStringArray, body: String, on_done: Callable) -> void:
		var parsed: Dictionary = JSON.parse_string(body)
		for ev in parsed.get("events", []):
			events_captured.append(ev)
		on_done.call(true, parsed.events.size())

	var state := GameState.new()
	state.telemetry_sink = sink
	state.enable_dynamic_world(1)

	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)
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

	# Clear previous events
	sink.poll(1.0)
	events_captured.clear()

	# Step player across the door into room 2
	# Start room player is at (4, 2), door at (4, 1). First step opens the closed door.
	# Second step moves into door (4, 1). Third step moves into r-002 floor tile (4, 0).
	state.player_action_step(Vector2i.UP) # Open door
	state.player_action_step(Vector2i.UP) # Walk into door (4, 1)
	state.player_action_step(Vector2i.UP) # Walk into r-002 (4, 0)

	sink.poll(1.0)
	var entered_events := events_captured.filter(func(e): return e.event_name == "room.entered")
	_check_eq(entered_events.size(), 1, "room.entered captured when player entered r-002")
	if not entered_events.is_empty():
		_check_eq(entered_events[0].attributes.room_id, "r-002", "room_id matches r-002")
		_check(entered_events[0].attributes.has("time_to_entry_ms"), "time_to_entry_ms attribute present")

	_end()
