extends SceneTree
## Headless contract-fixture validation for the Godot side (issue #3).
##
## Runs without a project (no project.godot required):
##     godot --headless --script game/tests/test_contracts.gd
##
## Fixtures are located via (in order):
##   1. DUNGEON_CONTRACTS_DIR environment variable
##   2. ../..//contracts/fixtures relative to this script (repo layout)
##   3. res://contracts/fixtures and a few cwd-relative fallbacks
## Exit code 0 = all checks passed, 1 = failures (also printed as errors).

const DungeonContracts := preload("../contracts/dungeon_contracts.gd")

const FIXTURE_FILES := {
	"request": "generation_request.json",
	"response": "generation_response.json",
	"response_failure": "generation_response_failure.json",
	"malformed": "malformed_provider_response.json",
}

var _checks := 0
var _failures := PackedStringArray()
var _active_marker := ""


func _initialize() -> void:
	var fixtures_dir := _find_fixtures_dir()
	if fixtures_dir == "":
		_report_missing_fixtures()
		return
	_run_all(fixtures_dir)
	_finish()


## Guard against silent false-passes: a runtime script error aborts the
## current function, so every test brackets its body with _begin/_end and an
## unremoved marker counts as a failure.
func _begin(test_name: String) -> void:
	_active_marker = "FAIL: %s aborted mid-run (runtime script error?)" % test_name
	_failures.append(_active_marker)


func _end() -> void:
	var idx := _failures.rfind(_active_marker)
	if idx >= 0:
		_failures.remove_at(idx)
	_active_marker = ""


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


func _run_all(fixtures_dir: String) -> void:
	var request_text := _read_fixture(fixtures_dir, FIXTURE_FILES.request)
	var response_text := _read_fixture(fixtures_dir, FIXTURE_FILES.response)
	var failure_text := _read_fixture(fixtures_dir, FIXTURE_FILES.response_failure)
	var malformed_text := _read_fixture(fixtures_dir, FIXTURE_FILES.malformed)
	if request_text.is_empty() or response_text.is_empty() or failure_text.is_empty() or malformed_text.is_empty():
		return

	_run("request fixture", _test_request_fixture.bind(request_text))
	_run("response fixture", _test_response_fixture.bind(response_text))
	_run("failure fixture", _test_failure_fixture.bind(failure_text))
	_run("malformed provider output", _test_malformed_provider_output.bind(malformed_text))
	_run("negative validations", _test_negative_validations)
	_run("version policy", _test_version_policy)
	_run("hardened invariants", _test_hardened_invariants.bind(request_text))
	_run("director config contract", _test_director_config_contract)



func _run(test_name: String, test: Callable) -> void:
	_begin(test_name)
	test.call()
	_end()


func _test_request_fixture(text: String) -> void:
	var parsed := DungeonContracts.parse_generation_request(text)
	_check(parsed.ok, "request fixture parses: %s" % _err(parsed))
	if not parsed.ok:
		return
	var request: Dictionary = parsed.request
	_check(request.state.depth == 3, "request.state.depth == 3")
	_check(request.state.unresolved_exits.size() == 2, "request.state.unresolved_exits has 2 entries")
	_check(request.state.unresolved_exits[0].direction == "north", "first unresolved exit points north")
	_check(request.options.max_danger == 3, "request.options.max_danger == 3")
	_check(request.state.player.hp <= request.state.player.max_hp, "player hp within max_hp")
	_check(request.state.pacing.rooms_on_depth == 6, "pacing.rooms_on_depth == 6")
	_check(request.target_exit.room_id == "r-003", "request.target_exit identifies frontier room r-003")
	_check(request.target_exit.direction == "north", "request.target_exit direction is north")


func _test_response_fixture(text: String) -> void:
	var parsed := DungeonContracts.parse_generation_response(text)
	_check(parsed.ok, "response fixture parses: %s" % _err(parsed))
	if not parsed.ok:
		return
	var response: Dictionary = parsed.response
	_check(response.success == true, "response.success == true")
	_check(response.room != null, "response.room present")
	if response.room == null:
		return
	_check(response.room.room_id == "r-004", "room.room_id == 'r-004'")
	_check(response.metadata.provider == "rules-baseline", "metadata.provider == 'rules-baseline'")

	# Deserialize into the typed game-side structure and check equivalence.
	var plan: DungeonContracts.RoomPlanData = DungeonContracts.RoomPlanData.from_room_plan(response.room)
	_check(plan.room_id == "r-004", "RoomPlanData.room_id")
	_check(plan.room_type == "room", "RoomPlanData.room_type")
	_check(plan.size == "small", "RoomPlanData.size")
	_check(plan.depth == 3, "RoomPlanData.depth")
	_check(plan.danger == 2, "RoomPlanData.danger")
	_check(is_equal_approx(plan.enemy_density, 0.15), "RoomPlanData.enemy_density == 0.15")
	_check(is_equal_approx(plan.loot_density, 0.45), "RoomPlanData.loot_density == 0.45")
	_check(is_equal_approx(plan.secret_probability, 0.2), "RoomPlanData.secret_probability == 0.2")
	_check(plan.exits.size() == 2, "RoomPlanData.exits has 2 entries")
	if plan.exits.size() == 2:
		_check(plan.exits[0]["direction"] == "south", "first exit back-connection points south")
		_check(plan.exits[1]["kind"] == "passage", "second exit is a passage")
	_check(plan.environmental_tags.size() == 1 and plan.environmental_tags[0] == "dark", "RoomPlanData tags == ['dark']")
	_check(plan.description.contains("larder"), "RoomPlanData.description carried over")


func _test_failure_fixture(text: String) -> void:
	var parsed := DungeonContracts.parse_generation_response(text)
	_check(parsed.ok, "failure envelope still parses at contract level: %s" % _err(parsed))
	if not parsed.ok:
		return
	var response: Dictionary = parsed.response
	_check(response.success == false, "failure fixture success == false")
	_check(response.get("room", null) == null, "failure fixture carries no room")
	_check(response.metadata.error.code == "schema_violation", "error.code == 'schema_violation'")
	_check(response.metadata.error.message.length() > 0, "error.message non-empty")
	_check(response.metadata.error.raw_excerpt.length() > 0, "error.raw_excerpt captured provider output")
	_check(response.metadata.provider == "groq", "failure metadata.provider == 'groq'")
	_check(response.metadata.provider_metadata.has("finish_reason"), "provider_metadata preserved in envelope")


func _test_malformed_provider_output(text: String) -> void:
	# Raw provider payload with unknown enum, out-of-range danger, wrong-typed
	# density and leaked tile geometry must fail RoomPlan validation.
	var parsed := DungeonContracts.parse_room_plan(text)
	_check(not parsed.ok, "malformed provider output rejected by RoomPlan contract")
	if not parsed.ok:
		_check(parsed.error.length() > 0, "malformed output yields a descriptive error")
	# Documented failure path: the game falls back instead of consuming the room.
	var leaked_room: Variant = parsed.get("room", null)
	var fallback_ok: bool = leaked_room == null or not (leaked_room as Dictionary).has("tiles")
	_check(fallback_ok, "no geometry data escapes into game structures")
	# Non-JSON garbage is also rejected.
	var garbage := DungeonContracts.parse_generation_response("this is not json {{{")
	_check(not garbage.ok, "non-JSON response text rejected")


func _test_negative_validations() -> void:
	var room := {
		"room_id": "r-neg-1",
		"depth": 2,
		"room_type": "room",
		"size": "medium",
		"danger": 2,
	}
	_check(DungeonContracts.validate_room_plan(room).ok, "baseline room validates")

	var bad: Array[Dictionary] = [
		{"label": "danger 6", "data": _room_with(room, {"danger": 6})},
		{"label": "density 1.5", "data": _room_with(room, {"enemy_density": 1.5})},
		{"label": "unknown room_type", "data": _room_with(room, {"room_type": "gymnasium"})},
		{"label": "leaked geometry key", "data": _room_with(room, {"tiles": [[1, 1]]})},
		{"label": "bad room_id", "data": _room_with(room, {"room_id": "bad id!"})},
		{"label": "depth 0", "data": _room_with(room, {"depth": 0})},
	]
	for case in bad:
		var result := DungeonContracts.validate_room_plan(case.data)
		_check(not result.ok, "room rejected: %s" % case.label)

	var bad_player: Array[Dictionary] = [
		{"label": "hp above max_hp", "data": {"hp": 11, "max_hp": 10}},
		{"label": "negative hp", "data": {"hp": -1, "max_hp": 10}},
	]
	for case in bad_player:
		var state := {"depth": 1, "player": case.data}
		var result := DungeonContracts.validate_dungeon_state(state)
		_check(not result.ok, "state rejected: %s" % case.label)

	# Envelope invariants.
	_check(
		not DungeonContracts.validate_generation_response(_response_with(null, true)).ok,
		"success without room rejected"
	)
	_check(
		not DungeonContracts.validate_generation_response(_response_with(room, false)).ok,
		"failure with room rejected"
	)
	var failure_without_error := _response_with(null, false)
	failure_without_error.metadata.erase("error")
	_check(
		not DungeonContracts.validate_generation_response(failure_without_error).ok,
		"failure without error detail rejected"
	)
	var leak := _response_with(room, true)
	leak["finish_reason"] = "stop"
	_check(
		not DungeonContracts.validate_generation_response(leak).ok,
		"provider field leaked to envelope top level rejected"
	)


func _test_version_policy() -> void:
	_check(DungeonContracts.CONTRACT_VERSION == "1.0.0", "GDScript contract version is 1.0.0")
	var room := {
		"room_id": "r-ver-1",
		"depth": 1,
		"room_type": "room",
		"size": "tiny",
	}
	var bumped := _response_with(room, true)
	bumped["contract_version"] = "1.9.3"
	_check(DungeonContracts.validate_generation_response(bumped).ok, "same-major version 1.9.3 accepted")
	var next_major := _response_with(room, true)
	next_major["contract_version"] = "2.0.0"
	_check(
		not DungeonContracts.validate_generation_response(next_major).ok,
		"major version 2.0.0 rejected"
	)


func _test_hardened_invariants(request_text: String) -> void:
	var room := {
		"room_id": "r-hard-1",
		"depth": 1,
		"room_type": "room",
		"size": "medium",
	}

	# contract_version is required on envelopes.
	var missing_version := _response_with(room, true)
	missing_version.erase("contract_version")
	_check(
		not DungeonContracts.validate_generation_response(missing_version).ok,
		"response without contract_version rejected"
	)

	# Naive timestamps (no explicit UTC offset) are rejected.
	var naive_ts := _response_with(room, true)
	naive_ts.metadata["started_at"] = "2026-09-19T10:00:00"
	_check(
		not DungeonContracts.validate_generation_response(naive_ts).ok,
		"naive started_at rejected"
	)
	var naive_error := _response_with(null, false)
	naive_error.metadata.error["occurred_at"] = "2026-09-19T10:00:01"
	_check(
		not DungeonContracts.validate_generation_response(naive_error).ok,
		"naive error.occurred_at rejected"
	)

	# target_exit is required and must exactly match an unresolved_exits entry.
	var request_data: Dictionary = JSON.parse_string(request_text)
	var missing_target_exit: Dictionary = request_data.duplicate(true)
	missing_target_exit.erase("target_exit")
	_check(
		not DungeonContracts.validate_generation_request(missing_target_exit).ok,
		"request without target_exit rejected"
	)
	var mismatched_since_turn: Dictionary = request_data.duplicate(true)
	mismatched_since_turn.target_exit["since_turn"] = 999
	_check(
		not DungeonContracts.validate_generation_request(mismatched_since_turn).ok,
		"target_exit with wrong since_turn rejected"
	)
	var mismatched_direction: Dictionary = request_data.duplicate(true)
	mismatched_direction.target_exit["direction"] = "west"
	_check(
		not DungeonContracts.validate_generation_request(mismatched_direction).ok,
		"target_exit with direction not on frontier rejected"
	)

	# Unique exit directions.
	var duplicate_exits := _room_with(room, {
		"exits": [{"direction": "north"}, {"direction": "north"}],
	})
	_check(
		not DungeonContracts.validate_room_plan(duplicate_exits).ok,
		"duplicate exit directions rejected"
	)

	# has_secret requires positive secret_probability.
	var secret_without_probability := _room_with(room, {"has_secret": true})
	_check(
		not DungeonContracts.validate_room_plan(secret_without_probability).ok,
		"has_secret=true with default secret_probability 0 rejected"
	)
	var secret_with_probability := _room_with(room, {"has_secret": true, "secret_probability": 0.3})
	_check(
		DungeonContracts.validate_room_plan(secret_with_probability).ok,
		"has_secret=true with positive secret_probability accepted"
	)

	# provider_metadata serialized byte budget.
	var oversized_metadata := _response_with(room, true)
	oversized_metadata.metadata["provider_metadata"] = {"blob": "x".repeat(9000)}
	_check(
		not DungeonContracts.validate_generation_response(oversized_metadata).ok,
		"provider_metadata over 8192 serialized bytes rejected"
	)
	var compact_metadata := _response_with(room, true)
	compact_metadata.metadata["provider_metadata"] = {"blob": "x".repeat(4000)}
	_check(
		DungeonContracts.validate_generation_response(compact_metadata).ok,
		"provider_metadata within byte budget accepted"
	)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


func _room_with(base: Dictionary, overrides: Dictionary) -> Dictionary:
	var merged: Dictionary = base.duplicate(true)
	for key in overrides:
		merged[key] = overrides[key]
	return merged


func _response_with(room: Variant, success: bool) -> Dictionary:
	var metadata := {
		"provider": "rules-baseline",
		"model": "builtin-v1",
		"started_at": "2026-09-19T10:00:00Z",
		"completed_at": "2026-09-19T10:00:01+00:00",
	}
	if not success:
		metadata["error"] = {
			"code": "provider_error",
			"message": "boom",
		}
	return {
		"contract_version": "1.0.0",
		"request_id": "req-test-1",
		"run_id": "run-test-1",
		"success": success,
		"room": room,
		"metadata": metadata,
	}


func _test_director_config_contract() -> void:
	var valid_config_json := JSON.stringify({
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
				"id": "cloudflare-jev",
				"available": false,
				"default_model": "typesafe/jev",
				"models": ["typesafe/jev"]
			}
		],
		"shadow": {
			"targets": [{"provider": "cloudflare-jev", "model": "typesafe/jev"}],
			"rejected_config_entries": 0
		}
	})

	var parsed := DungeonContracts.parse_director_config(valid_config_json)
	_check(parsed.ok, "valid director config parses successfully")
	var cfg: DungeonContracts.DirectorConfigData = DungeonContracts.DirectorConfigData.from_dict(parsed.config)
	_check(cfg.default_provider == "rules-baseline", "cfg.default_provider")
	_check(cfg.default_model == "builtin-v1", "cfg.default_model")
	_check(cfg.providers.size() == 2, "cfg.providers.size == 2")
	_check(cfg.providers[0].id == "rules-baseline" and cfg.providers[0].available == true, "provider 0 is available")
	_check(cfg.providers[1].id == "cloudflare-jev" and cfg.providers[1].available == false, "provider 1 is unavailable")

	# Negative cases:
	# 1. Missing required key
	var missing_key := {
		"default_provider": "rules-baseline",
		"providers": []
	}
	_check(not DungeonContracts.validate_director_config(missing_key).ok, "missing default_model rejected")

	# 2. Unknown key rejected
	var unknown_key := {
		"default_provider": "rules-baseline",
		"default_model": "builtin-v1",
		"providers": [],
		"secret_token": "leak"
	}
	_check(not DungeonContracts.validate_director_config(unknown_key).ok, "unknown key rejected")

	# 3. default_model not in models
	var bad_models := {
		"default_provider": "rules-baseline",
		"default_model": "builtin-v1",
		"providers": [
			{
				"id": "rules-baseline",
				"available": true,
				"default_model": "builtin-v1",
				"models": ["other_model"]
			}
		]
	}
	_check(not DungeonContracts.validate_director_config(bad_models).ok, "default_model not in models rejected")


func _read_fixture(fixtures_dir: String, file_name: String) -> String:

	var path := fixtures_dir.path_join(file_name)
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null:
		_failures.append("FAIL: cannot read fixture '%s'" % path)
		return ""
	return file.get_as_text()


func _find_fixtures_dir() -> String:
	var env_dir := OS.get_environment("DUNGEON_CONTRACTS_DIR")
	if not env_dir.is_empty() and _dir_exists(env_dir):
		return env_dir
	var script_dir := _script_dir()
	var pwd := OS.get_environment("PWD")
	var candidates := [
		script_dir.path_join("../../contracts/fixtures"),
		script_dir.path_join("../../../contracts/fixtures"),
		"res://contracts/fixtures",
		pwd.path_join("contracts/fixtures"),
		pwd.path_join("../contracts/fixtures"),
		pwd.path_join("../../contracts/fixtures"),
	]
	for candidate in candidates:
		if _dir_exists(candidate):
			return candidate
	return ""


func _dir_exists(path: String) -> bool:
	var absolute := path
	if absolute.begins_with("res://"):
		absolute = ProjectSettings.globalize_path(absolute)
	return DirAccess.dir_exists_absolute(absolute)


func _script_dir() -> String:
	var script_path := String(get_script().get_path())
	if script_path.begins_with("res://"):
		script_path = ProjectSettings.globalize_path(script_path)
	return script_path.get_base_dir()


func _report_missing_fixtures() -> void:
	_failures.append(
		"FAIL: cannot locate contracts/fixtures; set DUNGEON_CONTRACTS_DIR or run from the repository"
	)
	_finish()


func _check(condition: bool, label: String) -> void:
	_checks += 1
	if not condition:
		_failures.append("FAIL: %s" % label)


func _err(result: Dictionary) -> String:
	return str(result.get("error", ""))


func _finish() -> void:
	if _failures.is_empty():
		print("test_contracts.gd: all %d checks passed" % _checks)
		quit(0)
	else:
		for failure in _failures:
			push_error(failure)
		print("test_contracts.gd: %d/%d checks FAILED" % [_failures.size(), _checks + _failures.size()])
		quit(1)
