class_name DungeonContracts
extends RefCounted
## Godot-side reader/validator for the canonical dungeon director contracts.
##
## Mirrors director/dungeon_director/contracts.py (contract version 1.0.0).
## Standalone by design: no autoloads, no project.godot settings, and no
## provider-specific logic. Every validator returns a result dictionary:
##   { "ok": true, ...payload }
##   { "ok": false, "error": "<human-readable reason>" }
##
## On a parse/validation failure the game must keep committed dungeon state
## unchanged and fall back to the rules baseline (see contracts/README.md).

const CONTRACT_VERSION := "1.0.0"

# Enumerations (kept as plain string arrays so they stay JSON-shaped).
const ROOM_TYPES: PackedStringArray = [
	"entrance", "room", "corridor", "cavern", "chamber", "vault",
	"shrine", "shop", "treasure", "stairs_down", "stairs_up",
]
const ROOM_SIZES: PackedStringArray = ["tiny", "small", "medium", "large", "huge"]
const EXIT_DIRECTIONS: PackedStringArray = ["north", "south", "east", "west", "up", "down"]
const EXIT_KINDS: PackedStringArray = ["door", "passage", "stairs", "secret"]
const ENVIRONMENTAL_TAGS: PackedStringArray = [
	"dark", "flooded", "fungal", "icy", "hot",
	"ruined", "hallowed", "trapped", "overgrown", "noisy",
]
const HUNGER_STATES: PackedStringArray = ["satiated", "normal", "hungry", "weak", "starving"]
const ERROR_KINDS: PackedStringArray = [
	"schema_violation", "invalid_json", "unsupported_contract_version",
	"empty_response", "provider_error", "provider_timeout", "rate_limited",
	"safety_refusal", "budget_exceeded", "internal_error",
]

# Bounds mirrored from the Pydantic models.
const DEPTH_MIN := 1
const DEPTH_MAX := 128
const TURN_MAX := 10_000_000
const HP_MAX := 99_999
const LEVEL_MIN := 1
const LEVEL_MAX := 100
const DANGER_MIN := 1
const DANGER_MAX := 5
const ID_MAX_LENGTH := 64
const MAX_EXITS := 8
const MAX_ENVIRONMENTAL_TAGS := 8
const MAX_CONDITIONS := 16
const MAX_RECENT_ROOMS := 32
const MAX_RECENT_EVENTS := 32
const MAX_INVENTORY_ITEMS := 128
const MAX_UNRESOLVED_EXITS := 64
const MAX_PROVIDER_METADATA_KEYS := 32
const MAX_PROVIDER_METADATA_JSON_BYTES := 8192
const MAX_DESCRIPTION_LENGTH := 200
const MAX_PROMPT_HINT_LENGTH := 500
const MAX_ERROR_MESSAGE_LENGTH := 500
const MAX_RAW_EXCERPT_LENGTH := 4096

static var _timestamp_regex: RegEx


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


## Parse and validate a director GenerationResponse (JSON text).
## On ok, `response.success` distinguishes generation success from the
## documented failure envelope (which still parses fine).
static func parse_generation_response(text: String) -> Dictionary:
	var data := _parse_json_object(text, "response")
	if not data.ok:
		return data
	var validated := validate_generation_response(data.value)
	if not validated.ok:
		return validated
	return {"ok": true, "response": data.value}


## Parse and validate a GenerationRequest (useful for director-side GDScript
## tooling and tests).
static func parse_generation_request(text: String) -> Dictionary:
	var data := _parse_json_object(text, "request")
	if not data.ok:
		return data
	var validated := validate_generation_request(data.value)
	if not validated.ok:
		return validated
	return {"ok": true, "request": data.value}


## Parse and validate a raw RoomPlan (e.g. a recorded provider decision).
static func parse_room_plan(text: String) -> Dictionary:
	var data := _parse_json_object(text, "room")
	if not data.ok:
		return data
	var validated := validate_room_plan(data.value)
	if not validated.ok:
		return validated
	return {"ok": true, "room": data.value}


## Validate an already-decoded GenerationResponse dictionary.
static func validate_generation_response(data: Variant) -> Dictionary:
	var err := _check_keys(data, ["contract_version", "request_id", "run_id", "metadata"], ["success", "room"], "response")
	if err != "":
		return _fail(err)
	err = _version_error(data.contract_version)
	if err != "":
		return _fail("response." + err)
	if data.has("success") and not (data.success is bool):
		return _fail("response.success: expected a boolean")
	var success: bool = not data.has("success") or data.success
	if success:
		if not data.has("room") or data.room == null:
			return _fail("response: successful response must carry a room plan")
		var room := validate_room_plan(data.room)
		if not room.ok:
			return room
	elif data.has("room") and data.room != null:
		return _fail("response: failed response must not carry a room plan")
	var meta := _validate_response_metadata(data.metadata, not success, "response.metadata")
	if not meta.ok:
		return meta
	return {"ok": true}


## Validate an already-decoded GenerationRequest dictionary.
static func validate_generation_request(data: Variant) -> Dictionary:
	var err := _check_keys(data, ["contract_version", "request_id", "run_id", "state", "target_exit"], ["prompt_hint", "options"], "request")
	if err != "":
		return _fail(err)
	err = _version_error(data.contract_version)
	if err != "":
		return _fail("request." + err)
	if not _is_valid_id(data.request_id):
		return _fail("request.request_id: expected an id (1..64 chars, [A-Za-z0-9_.-], starts alphanumeric)")
	if not _is_valid_id(data.run_id):
		return _fail("request.run_id: expected an id (1..64 chars, [A-Za-z0-9_.-], starts alphanumeric)")
	var target_err := _validate_unresolved_exit(data.target_exit, "request.target_exit")
	if target_err != "":
		return _fail(target_err)
	if not _matches_unresolved_exit(data.target_exit, data.state.unresolved_exits):
		return _fail("request.target_exit must exactly match an entry in state.unresolved_exits")
	if data.has("prompt_hint") and data.prompt_hint != null:
		if not (data.prompt_hint is String) or data.prompt_hint.length() < 1 or data.prompt_hint.length() > MAX_PROMPT_HINT_LENGTH:
			return _fail("request.prompt_hint: expected 1..%d characters" % MAX_PROMPT_HINT_LENGTH)
	if data.has("options") and data.options != null:
		var options := _validate_generation_options(data.options)
		if not options.ok:
			return options
	var state := validate_dungeon_state(data.state)
	if not state.ok:
		return state
	return {"ok": true}


## Validate an already-decoded DungeonState dictionary.
static func validate_dungeon_state(data: Variant) -> Dictionary:
	var err := _check_keys(data, ["depth", "player"], ["turn", "recent_rooms", "recent_events", "inventory", "unresolved_exits", "pacing"], "state")
	if err != "":
		return _fail(err)
	if not _is_int_in(data.depth, DEPTH_MIN, DEPTH_MAX):
		return _fail("state.depth: expected an integer within %d..%d" % [DEPTH_MIN, DEPTH_MAX])
	if data.has("turn") and not _is_int_in(data.turn, 0, TURN_MAX):
		return _fail("state.turn: expected an integer within 0..%d" % TURN_MAX)
	var player := _validate_player_state(data.player)
	if not player.ok:
		return player
	if data.has("recent_rooms"):
		err = _check_list(data.recent_rooms, MAX_RECENT_ROOMS, "state.recent_rooms", _validate_visited_room)
		if err != "":
			return _fail(err)
	if data.has("recent_events"):
		err = _check_list(data.recent_events, MAX_RECENT_EVENTS, "state.recent_events", _validate_recent_event)
		if err != "":
			return _fail(err)
	if data.has("inventory"):
		err = _check_list(data.inventory, MAX_INVENTORY_ITEMS, "state.inventory", _validate_inventory_item)
		if err != "":
			return _fail(err)
	if data.has("unresolved_exits"):
		err = _check_list(data.unresolved_exits, MAX_UNRESOLVED_EXITS, "state.unresolved_exits", _validate_unresolved_exit)
		if err != "":
			return _fail(err)
	if data.has("pacing") and data.pacing != null:
		var pacing := _validate_pacing_context(data.pacing)
		if not pacing.ok:
			return pacing
	return {"ok": true}


## Validate an already-decoded RoomPlan dictionary (semantic fields only;
## tile geometry and unknown provider fields are rejected).
static func validate_room_plan(data: Variant) -> Dictionary:
	var err := _check_keys(data, ["room_id", "depth", "room_type", "size"], ["danger", "exits", "enemy_density", "loot_density", "secret_probability", "has_secret", "environmental_tags", "description"], "room")
	if err != "":
		return _fail(err)
	if not _is_valid_id(data.room_id):
		return _fail("room.room_id: expected an id (1..64 chars, [A-Za-z0-9_.-], starts alphanumeric)")
	if not _is_int_in(data.depth, DEPTH_MIN, DEPTH_MAX):
		return _fail("room.depth: expected an integer within %d..%d" % [DEPTH_MIN, DEPTH_MAX])
	if not _in_enum(data.room_type, ROOM_TYPES):
		return _fail("room.room_type: unknown value '%s'" % [data.room_type])
	if not _in_enum(data.size, ROOM_SIZES):
		return _fail("room.size: unknown value '%s'" % [data.size])
	if data.has("danger") and not _is_int_in(data.danger, DANGER_MIN, DANGER_MAX):
		return _fail("room.danger: expected an integer within %d..%d" % [DANGER_MIN, DANGER_MAX])
	if data.has("has_secret") and data.has_secret != null and not (data.has_secret is bool):
		return _fail("room.has_secret: expected a boolean")
	if data.has("description") and data.description != null:
		if not (data.description is String) or data.description.length() < 1 or data.description.length() > MAX_DESCRIPTION_LENGTH:
			return _fail("room.description: expected 1..%d characters" % MAX_DESCRIPTION_LENGTH)
	for field in ["enemy_density", "loot_density", "secret_probability"]:
		if data.has(field) and not _is_unit_float(data[field]):
			return _fail("room.%s: expected a number within 0..1" % field)
	if data.has("has_secret") and data.has_secret == true:
		if float(data.get("secret_probability", 0.0)) <= 0.0:
			return _fail("room.has_secret=true requires secret_probability > 0")
	if data.has("exits"):
		err = _check_list(data.exits, MAX_EXITS, "room.exits", _validate_exit)
		if err != "":
			return _fail(err)
		var seen_directions := {}
		for exit_entry in data.exits:
			var direction: Variant = exit_entry["direction"]
			if seen_directions.has(direction):
				return _fail("room.exits: duplicate direction '%s'" % [direction])
			seen_directions[direction] = true
	if data.has("environmental_tags"):
		var tags: Array = data.environmental_tags
		if tags.size() > MAX_ENVIRONMENTAL_TAGS:
			return _fail("room.environmental_tags: exceeds max length %d" % MAX_ENVIRONMENTAL_TAGS)
		for i in tags.size():
			if not _in_enum(tags[i], ENVIRONMENTAL_TAGS):
				return _fail("room.environmental_tags[%d]: unknown value '%s'" % [i, tags[i]])
	return {"ok": true}


## Typed game-side structure built from a validated RoomPlan dictionary.
class RoomPlanData:
	extends RefCounted
	var room_id := ""
	var depth := 1
	var room_type := ""
	var size := ""
	var danger := 1
	var exits: Array[Dictionary] = []
	var enemy_density := 0.0
	var loot_density := 0.0
	var secret_probability := 0.0
	var has_secret: Variant = null
	var environmental_tags := PackedStringArray()
	var description := ""

	static func from_room_plan(room: Dictionary) -> RoomPlanData:
		var plan := RoomPlanData.new()
		plan.room_id = String(room.get("room_id", ""))
		plan.depth = int(room.get("depth", 1))
		plan.room_type = String(room.get("room_type", ""))
		plan.size = String(room.get("size", ""))
		plan.danger = int(room.get("danger", 1))
		plan.enemy_density = float(room.get("enemy_density", 0.0))
		plan.loot_density = float(room.get("loot_density", 0.0))
		plan.secret_probability = float(room.get("secret_probability", 0.0))
		plan.has_secret = room.get("has_secret", null)
		plan.description = String(room.get("description", ""))
		for tag in room.get("environmental_tags", []):
			plan.environmental_tags.append(String(tag))
		for exit in room.get("exits", []):
			plan.exits.append(exit as Dictionary)
		return plan


# ---------------------------------------------------------------------------
# Nested validators
# ---------------------------------------------------------------------------


static func _validate_player_state(data: Variant) -> Dictionary:
	var err := _check_keys(data, ["hp", "max_hp"], ["level", "hunger", "conditions"], "state.player")
	if err != "":
		return _fail(err)
	if not _is_int_in(data.hp, 0, HP_MAX):
		return _fail("state.player.hp: expected an integer within 0..%d" % HP_MAX)
	if not _is_int_in(data.max_hp, 1, HP_MAX):
		return _fail("state.player.max_hp: expected an integer within 1..%d" % HP_MAX)
	if data.hp > data.max_hp:
		return _fail("state.player.hp must not exceed max_hp")
	if data.has("level") and not _is_int_in(data.level, LEVEL_MIN, LEVEL_MAX):
		return _fail("state.player.level: expected an integer within %d..%d" % [LEVEL_MIN, LEVEL_MAX])
	if data.has("hunger") and data.hunger != null and not _in_enum(data.hunger, HUNGER_STATES):
		return _fail("state.player.hunger: unknown value '%s'" % [data.hunger])
	if data.has("conditions"):
		var conditions: Array = data.conditions
		if conditions.size() > MAX_CONDITIONS:
			return _fail("state.player.conditions: exceeds max length %d" % MAX_CONDITIONS)
		for i in conditions.size():
			var condition: Variant = conditions[i]
			if not (condition is String) or condition.length() < 1 or condition.length() > 64:
				return _fail("state.player.conditions[%d]: expected 1..64 characters" % i)
	return {"ok": true}


static func _validate_visited_room(data: Variant, path: String) -> String:
	var err := _check_keys(data, ["room_id", "room_type"], ["danger"], path)
	if err != "":
		return err
	if not _is_valid_id(data.room_id):
		return "%s.room_id: expected a bounded id" % path
	if not _in_enum(data.room_type, ROOM_TYPES):
		return "%s.room_type: unknown value '%s'" % [path, data.room_type]
	if data.has("danger") and not _is_int_in(data.danger, DANGER_MIN, DANGER_MAX):
		return "%s.danger: expected an integer within %d..%d" % [path, DANGER_MIN, DANGER_MAX]
	return ""


static func _validate_recent_event(data: Variant, path: String) -> String:
	var err := _check_keys(data, ["event"], ["turn"], path)
	if err != "":
		return err
	if not (data.event is String) or data.event.length() < 1 or data.event.length() > 128:
		return "%s.event: expected 1..128 characters" % path
	if data.has("turn") and data.turn != null and not _is_int_in(data.turn, 0, TURN_MAX):
		return "%s.turn: expected an integer within 0..%d" % [path, TURN_MAX]
	return ""


static func _validate_inventory_item(data: Variant, path: String) -> String:
	var err := _check_keys(data, ["item_id"], ["quantity", "category"], path)
	if err != "":
		return err
	if not _is_valid_id(data.item_id):
		return "%s.item_id: expected a bounded id" % path
	if data.has("quantity") and not _is_int_in(data.quantity, 1, 9999):
		return "%s.quantity: expected an integer within 1..9999" % path
	if data.has("category") and data.category != null:
		if not (data.category is String) or data.category.length() < 1 or data.category.length() > 32:
			return "%s.category: expected 1..32 characters" % path
	return ""


static func _validate_unresolved_exit(data: Variant, path: String) -> String:
	var err := _check_keys(data, ["room_id", "direction"], ["since_turn"], path)
	if err != "":
		return err
	if not _is_valid_id(data.room_id):
		return "%s.room_id: expected a bounded id" % path
	if not _in_enum(data.direction, EXIT_DIRECTIONS):
		return "%s.direction: unknown value '%s'" % [path, data.direction]
	if data.has("since_turn") and data.since_turn != null and not _is_int_in(data.since_turn, 0, TURN_MAX):
		return "%s.since_turn: expected an integer within 0..%d" % [path, TURN_MAX]
	return ""


## Exact match (room_id, direction, since_turn with null-normalization)
## against the frontier entries of a request's state.
static func _matches_unresolved_exit(target: Dictionary, exits: Variant) -> bool:
	if not (exits is Array):
		return false
	for entry in exits:
		if entry is Dictionary \
				and entry.get("room_id", null) == target.get("room_id", null) \
				and entry.get("direction", null) == target.get("direction", null) \
				and entry.get("since_turn", null) == target.get("since_turn", null):
			return true
	return false


static func _validate_exit(data: Variant, path: String) -> String:
	var err := _check_keys(data, ["direction"], ["kind", "locked"], path)
	if err != "":
		return err
	if not _in_enum(data.direction, EXIT_DIRECTIONS):
		return "%s.direction: unknown value '%s'" % [path, data.direction]
	if data.has("kind") and not _in_enum(data.kind, EXIT_KINDS):
		return "%s.kind: unknown value '%s'" % [path, data.kind]
	if data.has("locked") and not (data.locked is bool):
		return "%s.locked: expected a boolean" % path
	return ""


static func _validate_pacing_context(data: Variant) -> Dictionary:
	var err := _check_keys(data, [], ["rooms_on_depth", "secrets_found", "encounters_on_depth", "turns_on_depth", "average_recent_danger"], "state.pacing")
	if err != "":
		return _fail(err)
	for field in ["rooms_on_depth", "secrets_found", "encounters_on_depth"]:
		if data.has(field) and not _is_int_in(data[field], 0, 1024):
			return _fail("state.pacing.%s: expected an integer within 0..1024" % field)
	if data.has("turns_on_depth") and not _is_int_in(data.turns_on_depth, 0, TURN_MAX):
		return _fail("state.pacing.turns_on_depth: expected an integer within 0..%d" % TURN_MAX)
	if data.has("average_recent_danger") and data.average_recent_danger != null:
		if not _is_float_in(data.average_recent_danger, 0.0, 5.0):
			return _fail("state.pacing.average_recent_danger: expected a number within 0..5")
	return {"ok": true}


static func _validate_generation_options(data: Variant) -> Dictionary:
	var err := _check_keys(data, [], ["max_danger", "forbidden_room_types", "allow_secrets", "target_enemy_density", "target_loot_density"], "request.options")
	if err != "":
		return _fail(err)
	if data.has("max_danger") and data.max_danger != null and not _is_int_in(data.max_danger, DANGER_MIN, DANGER_MAX):
		return _fail("request.options.max_danger: expected an integer within %d..%d" % [DANGER_MIN, DANGER_MAX])
	if data.has("allow_secrets") and not (data.allow_secrets is bool):
		return _fail("request.options.allow_secrets: expected a boolean")
	for field in ["target_enemy_density", "target_loot_density"]:
		if data.has(field) and data[field] != null and not _is_unit_float(data[field]):
			return _fail("request.options.%s: expected a number within 0..1" % field)
	if data.has("forbidden_room_types"):
		var types: Array = data.forbidden_room_types
		if types.size() > 8:
			return _fail("request.options.forbidden_room_types: exceeds max length 8")
		for i in types.size():
			if not _in_enum(types[i], ROOM_TYPES):
				return _fail("request.options.forbidden_room_types[%d]: unknown value '%s'" % [i, types[i]])
	return {"ok": true}


static func _validate_response_metadata(data: Variant, expect_error: bool, path: String) -> Dictionary:
	var err := _check_keys(data, ["provider", "model", "started_at", "completed_at"], ["latency_ms", "usage", "error", "provider_metadata"], path)
	if err != "":
		return _fail(err)
	for field in ["provider", "model"]:
		if not (data[field] is String) or data[field].length() < 1 or data[field].length() > 128:
			return _fail("%s.%s: expected 1..128 characters" % [path, field])
	var started := _parse_iso8601(String(data.started_at))
	var completed := _parse_iso8601(String(data.completed_at))
	if started < 0.0:
		return _fail("%s.started_at: expected an ISO 8601 timestamp with explicit UTC offset" % path)
	if completed < 0.0:
		return _fail("%s.completed_at: expected an ISO 8601 timestamp with explicit UTC offset" % path)
	if completed < started:
		return _fail("%s.completed_at must not precede started_at" % path)
	if data.has("latency_ms") and data.latency_ms != null and not _is_float_in(data.latency_ms, 0.0, 1.0e12):
		return _fail("%s.latency_ms: expected a non-negative number" % path)
	if data.has("usage") and data.usage != null:
		var usage_err := _check_keys(data.usage, [], ["input_tokens", "output_tokens", "estimated_cost_usd"], path + ".usage")
		if usage_err != "":
			return _fail(usage_err)
		for field in ["input_tokens", "output_tokens"]:
			if data.usage.has(field) and data.usage[field] != null and not _is_int_in(data.usage[field], 0, 10_000_000):
				return _fail("%s.usage.%s: expected an integer within 0..10000000" % [path, field])
		if data.usage.has("estimated_cost_usd") and data.usage.estimated_cost_usd != null and not _is_float_in(data.usage.estimated_cost_usd, 0.0, 1.0e9):
			return _fail("%s.usage.estimated_cost_usd: expected a non-negative number" % path)
	if expect_error:
		if not data.has("error") or data.error == null:
			return _fail("%s.error: failed response must carry error detail" % path)
		var error_err := _check_keys(data.error, ["code", "message"], ["raw_excerpt", "occurred_at"], path + ".error")
		if error_err != "":
			return _fail(error_err)
		if not _in_enum(data.error.code, ERROR_KINDS):
			return _fail("%s.error.code: unknown value '%s'" % [path, data.error.code])
		var message: Variant = data.error.message
		if not (message is String) or message.length() < 1 or message.length() > MAX_ERROR_MESSAGE_LENGTH:
			return _fail("%s.error.message: expected 1..%d characters" % [path, MAX_ERROR_MESSAGE_LENGTH])
		if data.error.has("raw_excerpt") and data.error.raw_excerpt != null:
			var excerpt: Variant = data.error.raw_excerpt
			if not (excerpt is String) or excerpt.length() > MAX_RAW_EXCERPT_LENGTH:
				return _fail("%s.error.raw_excerpt: expected at most %d characters" % [path, MAX_RAW_EXCERPT_LENGTH])
		if data.error.has("occurred_at") and data.error.occurred_at != null and _parse_iso8601(String(data.error.occurred_at)) < 0.0:
			return _fail("%s.error.occurred_at: expected an ISO 8601 timestamp with explicit UTC offset" % path)
	elif data.has("error") and data.error != null:
		return _fail("%s.error: successful response must not carry error detail" % path)
	if data.has("provider_metadata"):
		var blob: Variant = data.provider_metadata
		if not (blob is Dictionary):
			return _fail("%s.provider_metadata: expected an object" % path)
		if blob.size() > MAX_PROVIDER_METADATA_KEYS:
			return _fail("%s.provider_metadata: exceeds max %d keys" % [path, MAX_PROVIDER_METADATA_KEYS])
		var serialized := JSON.stringify(blob)
		if serialized.to_utf8_buffer().size() > MAX_PROVIDER_METADATA_JSON_BYTES:
			return _fail(
				"%s.provider_metadata: serialized JSON exceeds %d UTF-8 bytes"
				% [path, MAX_PROVIDER_METADATA_JSON_BYTES]
			)
	return {"ok": true}


# ---------------------------------------------------------------------------
# Primitives
# ---------------------------------------------------------------------------


static func _fail(message: String) -> Dictionary:
	return {"ok": false, "error": message}


static func _parse_json_object(text: String, what: String) -> Dictionary:
	var data: Variant = JSON.parse_string(text)
	if data == null or not (data is Dictionary):
		return _fail("%s: invalid JSON" % what)
	return {"ok": true, "value": data}


static func _check_keys(data: Variant, required: Array, optional: Array, path: String) -> String:
	if not (data is Dictionary):
		return "%s: expected an object" % path
	for key in required:
		if not data.has(key):
			return "%s: missing required key '%s'" % [path, key]
	for key in data.keys():
		if not (key in required) and not (key in optional):
			return "%s: unknown key '%s' (provider-specific data belongs in metadata.provider_metadata)" % [path, key]
	return ""


static func _check_list(value: Variant, max_length: int, path: String, element_check: Callable) -> String:
	if not (value is Array):
		return "%s: expected an array" % path
	if value.size() > max_length:
		return "%s: exceeds max length %d" % [path, max_length]
	for i in value.size():
		var err: String = element_check.call(value[i], "%s[%d]" % [path, i])
		if err != "":
			return err
	return ""


static func _in_enum(value: Variant, allowed: PackedStringArray) -> bool:
	return value is String and allowed.has(value)


static func _is_int_in(value: Variant, lo: int, hi: int) -> bool:
	if value is int:
		return value >= lo and value <= hi
	if value is float:
		return is_finite(value) and roundf(value) == value and value >= lo and value <= hi
	return false


static func _is_float_in(value: Variant, lo: float, hi: float) -> bool:
	if value is int:
		return float(value) >= lo and float(value) <= hi
	if value is float:
		return is_finite(value) and value >= lo and value <= hi
	return false


static func _is_unit_float(value: Variant) -> bool:
	return _is_float_in(value, 0.0, 1.0)


static func _is_alnum(character: String) -> bool:
	return (character >= "a" and character <= "z") or (character >= "A" and character <= "Z") or (character >= "0" and character <= "9")


static func _is_digits(text: String) -> bool:
	if text.is_empty():
		return false
	for character in text:
		if character < "0" or character > "9":
			return false
	return true


static func _is_valid_id(value: Variant) -> bool:
	if not (value is String):
		return false
	var text := value as String
	if text.length() < 1 or text.length() > ID_MAX_LENGTH:
		return false
	if not _is_alnum(text[0]):
		return false
	for i in range(1, text.length()):
		var character := text[i]
		if not (_is_alnum(character) or character == "_" or character == "." or character == "-"):
			return false
	return true


static func _version_error(value: Variant) -> String:
	if not (value is String):
		return "contract_version: expected MAJOR.MINOR.PATCH string"
	var parts := (value as String).split(".")
	if parts.size() != 3 or not (_is_digits(parts[0]) and _is_digits(parts[1]) and _is_digits(parts[2])):
		return "contract_version: expected MAJOR.MINOR.PATCH, got '%s'" % value
	var expected_major := CONTRACT_VERSION.split(".")[0]
	if parts[0] != expected_major:
		return "contract_version: unsupported '%s' (this build speaks %s.x.y)" % [value, expected_major]
	return ""


## Parse an ISO 8601 timestamp with a mandatory explicit UTC offset
## ("YYYY-MM-DDTHH:MM:SS[.fff](Z|±HH:MM|±HHMM)") to a UTC epoch float,
## or -1.0 when the format is invalid (including naive timestamps).
static func _parse_iso8601(text: String) -> float:
	if _timestamp_regex == null:
		_timestamp_regex = RegEx.create_from_string("^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2}):(\\d{2})(?:\\.\\d+)?(Z|[+-]\\d{2}:?\\d{2})$")
	var match := _timestamp_regex.search(text)
	if match == null:
		return -1.0
	var datetime := {
		"year": int(match.get_string(1)),
		"month": int(match.get_string(2)),
		"day": int(match.get_string(3)),
		"hour": int(match.get_string(4)),
		"minute": int(match.get_string(5)),
		"second": int(match.get_string(6)),
	}
	var epoch := float(Time.get_unix_time_from_datetime_dict(datetime))
	var zone := match.get_string(7)
	if zone != "" and zone != "Z":
		var normalized := zone.replace(":", "")
		var offset_hours := int(normalized.substr(1, 2))
		var offset_minutes := int(normalized.substr(3, 2))
		var offset := offset_hours * 3600 + offset_minutes * 60
		epoch += -offset if zone[0] == "+" else offset
	return epoch
