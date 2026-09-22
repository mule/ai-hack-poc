class_name GameTelemetrySink
extends Node
## Asynchronous, bounded lifecycle telemetry sink for Godot (Issue #25).
##
## Emits bounded GameEvents over POST /v1/telemetry/game using a non-blocking
## batching queue. Gameplay NEVER blocks on telemetry delivery or network timeouts.
##
## Features:
##   - Bounded in-memory event queue (defaults to MAX_QUEUE_SIZE = 256).
##   - Safe drops under backpressure without corrupting state or halting execution.
##   - Safe drop counters (dropped_total, dropped_backpressure, dropped_invalid,
##     dropped_payload_limit, batches_sent, batches_failed, events_sent).
##   - Bounded attribute allowlisting per event name, string length caps (<=128),
##     numeric range checks, scalar type validation, and sensitive key redaction.
##   - Batch POST /v1/telemetry/game with schema_version="1" and <= 100 events/batch.
##   - Offline/disabled mode: when enabled is false or base_url is "offline"/"off"/"none",
##     telemetry is a no-op or silently discards events with bounded counters.
##   - Supports custom/injected transport (Callable or Node) for testing.

const MAX_QUEUE_SIZE := 256
const MAX_EVENTS_PER_BATCH := 100
const MAX_ATTRIBUTES_PER_EVENT := 16
const MAX_STRING_LENGTH := 128
const SCHEMA_VERSION := "1"
const TELEMETRY_PATH := "/v1/telemetry/game"
const MAX_BODY_BYTES := 1_048_576

# Event Name Enum
const EVENT_FRONTIER_DISCOVERED := "frontier.discovered"
const EVENT_GENERATION_QUEUED := "generation.queued"
const EVENT_GENERATION_ACCEPTED := "generation.accepted"
const EVENT_GENERATION_NORMALIZED := "generation.normalized"
const EVENT_GENERATION_REJECTED := "generation.rejected"
const EVENT_GENERATION_FALLBACK_APPLIED := "generation.fallback_applied"
const EVENT_ROOM_COMMITTED := "room.committed"
const EVENT_DOOR_REVEALED := "door.revealed"
const EVENT_ROOM_ENTERED := "room.entered"

const KNOWN_EVENTS: PackedStringArray = [
	EVENT_FRONTIER_DISCOVERED,
	EVENT_GENERATION_QUEUED,
	EVENT_GENERATION_ACCEPTED,
	EVENT_GENERATION_NORMALIZED,
	EVENT_GENERATION_REJECTED,
	EVENT_GENERATION_FALLBACK_APPLIED,
	EVENT_ROOM_COMMITTED,
	EVENT_DOOR_REVEALED,
	EVENT_ROOM_ENTERED,
]

# Sensitive keys and patterns that must never be recorded
const REDACTED_KEYS: PackedStringArray = [
	"authorization", "bearer", "api_key", "token", "password", "secret",
	"prompt", "raw_prompt", "raw_payload", "exception", "traceback", "payload",
	"error_message", "stack_trace"
]

# Allowed attributes per event
const EVENT_ALLOWLIST := {
	EVENT_FRONTIER_DISCOVERED: ["frontier_id", "depth", "exit_direction", "shadow_comparison_id", "replay_id", "execution_mode"],
	EVENT_GENERATION_QUEUED: ["frontier_id", "depth", "exit_direction", "provider", "model", "shadow_comparison_id", "replay_id", "execution_mode"],
	EVENT_GENERATION_ACCEPTED: ["provider", "model", "room_type", "room_size", "danger", "duration_ms", "shadow_comparison_id", "replay_id", "execution_mode"],
	EVENT_GENERATION_NORMALIZED: ["provider", "model", "room_type", "room_size", "danger", "normalize_reason", "duration_ms", "shadow_comparison_id", "replay_id", "execution_mode"],
	EVENT_GENERATION_REJECTED: ["reject_reason", "provider", "model", "duration_ms", "shadow_comparison_id", "replay_id", "execution_mode"],
	EVENT_GENERATION_FALLBACK_APPLIED: ["fallback_reason", "provider", "model", "duration_ms", "shadow_comparison_id", "replay_id", "execution_mode"],
	EVENT_ROOM_COMMITTED: ["room_id", "frontier_id", "room_type", "room_size", "danger", "duration_ms", "shadow_comparison_id", "replay_id", "execution_mode"],
	EVENT_DOOR_REVEALED: ["room_id", "frontier_id", "exit_direction", "door_kind", "shadow_comparison_id", "replay_id", "execution_mode"],
	EVENT_ROOM_ENTERED: ["room_id", "time_to_entry_ms", "turn", "shadow_comparison_id", "replay_id", "execution_mode"],
}

# Bounded enum sets for scalar validation
const ALLOWED_EXIT_DIRECTIONS: PackedStringArray = ["north", "south", "east", "west", "up", "down"]
const ALLOWED_ROOM_TYPES: PackedStringArray = [
	"entrance", "room", "corridor", "cavern", "chamber", "vault",
	"shrine", "shop", "treasure", "stairs_down", "stairs_up"
]
const ALLOWED_ROOM_SIZES: PackedStringArray = ["tiny", "small", "medium", "large", "huge"]
const ALLOWED_DOOR_KINDS: PackedStringArray = ["door", "passage", "stairs", "secret"]
const ALLOWED_EXECUTION_MODES: PackedStringArray = ["active", "shadow", "replay"]

const ALLOWED_NORMALIZE_REASONS: PackedStringArray = [
	"duplicate_room_id_rewritten",
	"exit_pruned",
	"room_size_reduced",
	"blocked_direction_removed",
	"repositioned",
]

const ALLOWED_REJECT_REASONS: PackedStringArray = [
	"schema_invalid",
	"policy_violation",
	"empty_room",
	"disconnected_room",
	"missing_backlink",
	"unsupported_direction",
	"depth_mismatch",
	"duplicate_room_id",
	"overlap",
	"blocks_frontier",
	"blocked_exit",
	"unknown_frontier",
]

const ALLOWED_FALLBACK_REASONS: PackedStringArray = [
	"provider_error",
	"provider_timeout",
	"schema_error",
	"selection_error",
	"rejected_by_game",
	"transport_failure",
	"timeout",
	"invalid_request",
	"invalid_response",
	"response_mismatch",
	"placement_failure",
	"no_placement_fits",
	"offline",
]

var base_url := "http://127.0.0.1:8000"
var enabled := true
var max_queue_size := MAX_QUEUE_SIZE
var batch_size := 20
var flush_interval_sec := 0.2
var timeout_sec := 5.0

## Optional custom HTTP dispatcher for testing: Callable(url: String, headers: PackedStringArray, body: String, on_done: Callable) -> void
var custom_http_post: Callable = Callable()

## Metrics and counters for observation
var events_enqueued := 0
var events_sent := 0
var batches_sent := 0
var batches_failed := 0
var dropped_total := 0
var dropped_backpressure := 0
var dropped_invalid := 0
var dropped_payload_limit := 0

var _queue: Array[Dictionary] = []
var _in_flight_http: HTTPRequest = null
var _time_since_flush := 0.0


func _init(url: String = "") -> void:
	if url != "":
		base_url = url
	else:
		base_url = _get_default_url()
	_update_enabled_flag()


static func _get_default_url() -> String:
	var env_url := OS.get_environment("DUNGEON_DIRECTOR_URL").strip_edges()
	if env_url != "":
		return env_url
	var tel_url := OS.get_environment("DUNGEON_TELEMETRY_URL").strip_edges()
	if tel_url != "":
		return tel_url
	return "http://127.0.0.1:8000"


func _update_enabled_flag() -> void:
	var lower := base_url.to_lower()
	if lower in ["offline", "off", "none", "disabled"]:
		enabled = false
	var env_off := OS.get_environment("DUNGEON_TELEMETRY_ENABLED").strip_edges().to_lower()
	if env_off in ["false", "0", "off", "no"]:
		enabled = false


func _ready() -> void:
	_update_enabled_flag()


func _process(delta: float) -> void:
	poll(delta)


## Update / flush loop. Safe to call manually or per-frame from _process.
func poll(delta: float = 0.0) -> void:
	if not enabled or _queue.is_empty():
		return
	_time_since_flush += delta
	if _time_since_flush >= flush_interval_sec or _queue.size() >= batch_size:
		_time_since_flush = 0.0
		_flush_batch()


## Clean shutdown: cancel in-flight and flush remaining synchronously or drop.
func shutdown() -> void:
	if is_instance_valid(_in_flight_http):
		_in_flight_http.cancel_request()
		_in_flight_http.queue_free()
		_in_flight_http = null
	_queue.clear()


## Enqueue a game event. Returns true if accepted, false if dropped.
## Never blocks gameplay or throws exceptions.
func enqueue_event(
	event_name: String,
	run_id: String,
	request_id: Variant, # String or null
	raw_attributes: Dictionary = {},
	traceparent: Variant = null
) -> bool:
	if not enabled:
		return false

	if not KNOWN_EVENTS.has(event_name):
		dropped_invalid += 1
		dropped_total += 1
		return false

	if not _is_valid_id(run_id):
		dropped_invalid += 1
		dropped_total += 1
		return false

	var req_id_str: Variant = null
	if request_id != null and str(request_id) != "":
		req_id_str = str(request_id)
		if not _is_valid_id(req_id_str):
			dropped_invalid += 1
			dropped_total += 1
			return false
	else:
		# request_id can only be null for frontier.discovered, door.revealed, or generation.queued
		if event_name != EVENT_FRONTIER_DISCOVERED and event_name != EVENT_GENERATION_QUEUED and event_name != EVENT_DOOR_REVEALED:
			dropped_invalid += 1
			dropped_total += 1
			return false

	var tp_str: Variant = null
	if traceparent != null and str(traceparent) != "":
		tp_str = str(traceparent)
		if not _is_valid_traceparent(tp_str):
			dropped_invalid += 1
			dropped_total += 1
			return false

	var sanitized_attrs := sanitize_event_attributes(event_name, raw_attributes)

	var timestamp := Time.get_datetime_string_from_system(true, false) + "Z"

	var event := {
		"event_name": event_name,
		"run_id": run_id,
		"request_id": req_id_str,
		"timestamp": timestamp,
		"attributes": sanitized_attrs,
	}
	if tp_str != null:
		event["traceparent"] = tp_str

	# Backpressure drop: if queue is full, drop the oldest event to make room
	# for newer lifecycle progression.
	if _queue.size() >= max_queue_size:
		_queue.pop_front()
		dropped_backpressure += 1
		dropped_total += 1

	_queue.append(event)
	events_enqueued += 1
	return true


## Bounded attribute sanitizer and validator
static func sanitize_event_attributes(event_name: String, raw_attrs: Dictionary) -> Dictionary:
	var sanitized := {}
	if not EVENT_ALLOWLIST.has(event_name):
		return sanitized

	var allowlist: Array = EVENT_ALLOWLIST[event_name]
	for k in raw_attrs:
		var key_str := str(k).strip_edges().to_lower()
		if key_str == "" or not allowlist.has(key_str):
			continue

		# Check for sensitive key names
		var sensitive := false
		for red in REDACTED_KEYS:
			if key_str.contains(red):
				sensitive = true
				break
		if sensitive:
			continue

		var val: Variant = raw_attrs[k]
		var cleaned_val: Variant = _sanitize_attribute_value(key_str, val)
		if cleaned_val != null:
			sanitized[key_str] = cleaned_val
			if sanitized.size() >= MAX_ATTRIBUTES_PER_EVENT:
				break

	return sanitized


static func _sanitize_attribute_value(key: String, val: Variant) -> Variant:
	# Scalar check: only String, int, float, bool allowed
	if val is bool:
		return val

	if val is int:
		if key == "danger":
			return clampi(val, 1, 5)
		if key == "depth":
			return clampi(val, 1, 128)
		if key == "turn":
			return clampi(val, 0, 10_000_000)
		return val

	if val is float:
		if not is_finite(val):
			return null
		if key in ["time_to_entry_ms", "duration_ms"]:
			if val < 0.0:
				return null
			return snappedf(val, 0.001)
		return val

	if val is String:
		var s: String = (val as String).strip_edges()
		if s.length() == 0 or s.length() > MAX_STRING_LENGTH:
			return null
		# Ban newlines
		if s.contains("\n") or s.contains("\r"):
			return null
		# Ban credential strings
		if s.to_lower().contains("bearer ") or s.to_lower().contains("sk-"):
			return null

		# Validate bounded enums
		match key:
			"exit_direction":
				return s if ALLOWED_EXIT_DIRECTIONS.has(s) else null
			"room_type":
				return s if ALLOWED_ROOM_TYPES.has(s) else null
			"room_size":
				return s if ALLOWED_ROOM_SIZES.has(s) else null
			"door_kind":
				return s if ALLOWED_DOOR_KINDS.has(s) else null
			"execution_mode":
				return s if ALLOWED_EXECUTION_MODES.has(s) else null
			"normalize_reason":
				return _validate_enum_prefix(s, ALLOWED_NORMALIZE_REASONS)
			"reject_reason":
				return _validate_enum_prefix(s, ALLOWED_REJECT_REASONS)
			"fallback_reason":
				return _validate_enum_prefix(s, ALLOWED_FALLBACK_REASONS)
			"frontier_id", "room_id", "shadow_comparison_id", "replay_id":
				return s if _is_valid_id(s) else null
			_:
				return s

	return null


static func _validate_enum_prefix(val: String, allowed: PackedStringArray) -> Variant:
	# Allows raw match or prefix like http_error:503 -> transport_failure / provider_error
	if allowed.has(val):
		return val
	var prefix := val.get_slice(":", 0)
	if allowed.has(prefix):
		return prefix
	if val.begins_with("http_error:"):
		return "transport_failure"
	if val.begins_with("provider_failure:"):
		return "provider_error"
	if val.begins_with("plan_rejected:"):
		return "rejected_by_game"
	return null


static func _is_valid_id(text: String) -> bool:
	if text.length() < 1 or text.length() > 64:
		return false
	var first := text[0]
	if not _is_alnum(first):
		return false
	for i in range(1, text.length()):
		var c := text[i]
		if not (_is_alnum(c) or c == "_" or c == "." or c == "-" or c == ":" or c == "#"):
			return false
	return true


static func _is_alnum(c: String) -> bool:
	return (c >= "a" and c <= "z") or (c >= "A" and c <= "Z") or (c >= "0" and c <= "9")


static func _is_valid_traceparent(text: String) -> bool:
	# W3C traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01
	if text.length() != 55:
		return false
	var parts := text.split("-")
	if parts.size() != 4:
		return false
	if parts[0].length() != 2 or parts[1].length() != 32 or parts[2].length() != 16 or parts[3].length() != 2:
		return false
	if parts[0] == "ff":
		return false
	return true


func _flush_batch() -> void:
	if _queue.is_empty():
		return
	if is_instance_valid(_in_flight_http):
		# Already dispatching a batch via HTTPRequest child; wait for next poll
		return

	var count := mini(_queue.size(), MAX_EVENTS_PER_BATCH)
	var batch_events: Array[Dictionary] = []
	for i in range(count):
		batch_events.append(_queue[i])

	var payload_dict := {
		"schema_version": SCHEMA_VERSION,
		"events": batch_events,
	}
	var json_payload := JSON.stringify(payload_dict)
	if json_payload.to_utf8_buffer().size() > MAX_BODY_BYTES:
		# Batch is oversized; remove top half to prevent payload explosion
		var drop_count := maxi(1, count / 2)
		for i in range(drop_count):
			_queue.pop_front()
		dropped_payload_limit += drop_count
		dropped_total += drop_count
		return

	# Remove items from queue
	for i in range(count):
		_queue.pop_front()

	if custom_http_post.is_valid():
		# Dispatched via custom callback
		batches_sent += 1
		events_sent += count
		custom_http_post.call(
			_target_url(),
			PackedStringArray(["Content-Type: application/json", "Accept: application/json"]),
			json_payload,
			_on_custom_completed
		)
		return

	_dispatch_http(json_payload, count)


func _target_url() -> String:
	return base_url.rstrip("/") + TELEMETRY_PATH


func _dispatch_http(json_payload: String, count: int) -> void:
	var http := HTTPRequest.new()
	http.timeout = timeout_sec
	http.body_size_limit = MAX_BODY_BYTES
	add_child(http)
	_in_flight_http = http
	http.request_completed.connect(_on_request_completed.bind(http, count), CONNECT_ONE_SHOT)

	var headers := PackedStringArray(["Content-Type: application/json", "Accept: application/json"])
	var err := http.request(_target_url(), headers, HTTPClient.METHOD_POST, json_payload)
	if err != OK:
		batches_failed += 1
		_in_flight_http = null
		http.queue_free()
	else:
		batches_sent += 1
		events_sent += count


func _on_request_completed(result: int, response_code: int, _headers: PackedStringArray, _body: PackedByteArray, http: HTTPRequest, count: int) -> void:
	if http == _in_flight_http:
		_in_flight_http = null
	if is_instance_valid(http):
		http.queue_free()

	if result != HTTPRequest.RESULT_SUCCESS or response_code >= 400:
		batches_failed += 1


func _on_custom_completed(ok: bool = true, _count: int = 0) -> void:
	if not ok:
		batches_failed += 1


func get_stats() -> Dictionary:
	return {
		"queued": _queue.size(),
		"enqueued": events_enqueued,
		"events_sent": events_sent,
		"batches_sent": batches_sent,
		"batches_failed": batches_failed,
		"dropped_total": dropped_total,
		"dropped_backpressure": dropped_backpressure,
		"dropped_invalid": dropped_invalid,
		"dropped_payload_limit": dropped_payload_limit,
	}
