extends SceneTree
## Issue #16: headless dungeon simulation harness.
##
## Covers configuration and the remote opt-in gate, the offline rules
## transport, the recording/budget transport, the independent world audit, the
## harness itself (offline, faulty, stalled and remote runs), dataset writing,
## and a golden fixture that pins the published dataset format.
##
## Set SIM_UPDATE_FIXTURES=1 to rewrite the golden fixture after an intentional
## generator or format change (then review the diff).

const DungeonContracts = preload("res://contracts/dungeon_contracts.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")
const GameState = preload("res://src/game_state.gd")
const GenerationCoordinator = preload("res://world/generation_coordinator.gd")
const OfflineTransport = preload("res://world/offline_transport.gd")
const GeneratedRoom = preload("res://generation/generated_room.gd")
const SimulationAudit = preload("res://simulation/simulation_audit.gd")
const SimulationCli = preload("res://simulation/simulation_cli.gd")
const SimulationConfig = preload("res://simulation/simulation_config.gd")
const SimulationDataset = preload("res://simulation/simulation_dataset.gd")
const SimulationHarness = preload("res://simulation/simulation_harness.gd")
const RecordingTransport = preload("res://simulation/recording_transport.gd")
const RulesTransport = preload("res://simulation/rules_transport.gd")
const MiniHttpServer = preload("res://tests/support/mini_http_server.gd")

const FIXTURE_DIR := "../benchmarks/simulation/fixtures/sample"
const REQUIRED_FILES := ["manifest.json", "steps.jsonl", "rooms.jsonl", "failures.jsonl", "summary.json"]

var _checks := 0
var _failures := PackedStringArray()
var _completed := false
var _reached_end := false


func _initialize() -> void:
	OS.set_environment("DUNGEON_DIRECTOR_URL", "offline")
	create_timer(180.0).timeout.connect(_on_watchdog)
	print("=== Running test_simulation_harness.gd ===")
	await _step("test_config_defaults_are_offline_rules", _test_config_defaults_are_offline_rules)
	await _step("test_config_parses_flags", _test_config_parses_flags)
	await _step("test_config_rejects_bad_input", _test_config_rejects_bad_input)
	await _step("test_remote_requires_explicit_opt_in", _test_remote_requires_explicit_opt_in)
	await _step("test_remote_config_and_cost_warning", _test_remote_config_and_cost_warning)
	await _step("test_rules_transport_answers_with_valid_contract_responses", _test_rules_transport_answers_with_valid_contract_responses)
	await _step("test_rules_transport_fault_profiles", _test_rules_transport_fault_profiles)
	await _step("test_recording_transport_records_and_enforces_budget", _test_recording_transport_records_and_enforces_budget)
	await _step("test_audit_accepts_a_grown_world", _test_audit_accepts_a_grown_world)
	await _step("test_audit_detects_corruption", _test_audit_detects_corruption)
	await _step("test_events_to_failures", _test_events_to_failures)
	await _step("test_offline_rules_run_is_clean_and_measured", _test_offline_rules_run_is_clean_and_measured)
	await _step("test_runs_are_deterministic", _test_runs_are_deterministic)
	await _step("test_flaky_provider_measures_fallbacks", _test_flaky_provider_measures_fallbacks)
	await _step("test_offline_transport_is_all_fallback", _test_offline_transport_is_all_fallback)
	await _step("test_stall_is_detected", _test_stall_is_detected)
	await _step("test_dataset_files_roundtrip_and_refuse_overwrite", _test_dataset_files_roundtrip_and_refuse_overwrite)
	await _step("test_golden_fixture_pins_dataset_format", _test_golden_fixture_pins_dataset_format)
	await _step("test_remote_run_over_http_respects_budget", _test_remote_run_over_http_respects_budget)
	await _step("test_cli_exit_codes_and_remote_warning", _test_cli_exit_codes_and_remote_warning)
	_completed = true
	_finish()


func _on_watchdog() -> void:
	printerr("FAILED: simulation harness tests exceeded their 180s watchdog")
	quit(2)


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
	print("\n--- Simulation Harness Results: %d Passed, %d Failed (completed=%s) ---" % [_checks - _failures.size(), _failures.size(), str(_completed)])
	if _failures.is_empty() and _completed:
		print("SUCCESS: All simulation harness checks passed!")
		quit(0)
	else:
		printerr("FAILED: simulation harness checks did not pass.")
		quit(1)


# --- helpers -----------------------------------------------------------------


func _config(args: Array) -> SimulationConfig:
	var parsed := SimulationConfig.parse(PackedStringArray(args))
	assert(parsed.ok, "test config must parse: %s" % parsed.get("error", ""))
	return parsed.config


func _run(args: Array) -> Dictionary:
	var harness := SimulationHarness.new(_config(args), self)
	var dataset: Dictionary = await harness.run()
	return {"harness": harness, "dataset": dataset}


func _make_request(request_id: String = "req-t-1") -> Dictionary:
	var state := GameState.new()
	state.enable_dynamic_world(1)
	state.world.run_id = "t-run"
	var coordinator := GenerationCoordinator.new(state, OfflineTransport.new())
	var frontier := state.world.frontiers_with_status(DungeonWorld.STATUS_UNRESOLVED)[0]
	return coordinator.build_request(frontier, request_id)


func _rows_of(dataset: Dictionary, name: String) -> Array:
	return dataset[name]


func _error_failures(dataset: Dictionary) -> Array:
	return dataset.failures.filter(func(f: Dictionary) -> bool: return f.severity == "error")


func _user_dir(leaf: String) -> String:
	return ProjectSettings.globalize_path("user://").path_join(leaf)


func _remove_dir(path: String) -> void:
	if not DirAccess.dir_exists_absolute(path):
		return
	for name in DirAccess.get_files_at(path):
		DirAccess.remove_absolute(path.path_join(name))
	DirAccess.remove_absolute(path)


# --- configuration -----------------------------------------------------------


func _test_config_defaults_are_offline_rules() -> void:
	print("\nTest: defaults are the safe offline rules baseline")
	var parsed := SimulationConfig.parse(PackedStringArray())
	_check(parsed.ok, "empty argument list parses")
	var config: SimulationConfig = parsed.config
	_check_eq(config.runs, 5, "default runs")
	_check_eq(config.steps, 100, "default steps per run (hundreds across runs)")
	_check_eq(config.transport, "rules", "default transport is the offline rules baseline")
	_check(not config.is_remote(), "remote is off by default")
	_check_eq(config.faults, "none", "no faults by default")
	var provider := config.provider_summary()
	_check_eq(provider.mode, "rules-baseline", "provider mode")
	_check_eq(provider.provider, "rules-baseline", "provider id")
	_check_eq(provider.model, "builtin-v1", "model id")
	_check(provider.endpoint == null, "no endpoint is recorded for offline runs")
	_check_eq(config.cost_warning(), "", "offline runs carry no cost warning")
	_end()


func _test_config_parses_flags() -> void:
	print("\nTest: flags parse in both --flag value and --flag=value forms")
	var parsed := SimulationConfig.parse(PackedStringArray(["--runs=3", "--steps", "20", "--seed", "9", "--faults", "flaky", "--policy=nearest", "--audit-every", "5", "--out", "/tmp/somewhere"]))
	_check(parsed.ok, "flags parse: %s" % parsed.get("error", ""))
	var config: SimulationConfig = parsed.config
	_check_eq(config.runs, 3, "runs")
	_check_eq(config.steps, 20, "steps")
	_check_eq(config.base_seed, 9, "seed")
	_check_eq(config.faults, "flaky", "faults")
	_check_eq(config.policy, "nearest", "policy")
	_check_eq(config.audit_every, 5, "audit interval")
	_check_eq(config.out_dir, "/tmp/somewhere", "output directory")
	var offline := SimulationConfig.parse(PackedStringArray(["--transport", "offline"]))
	_check(offline.ok and offline.config.provider_summary().mode == "offline-fallback", "offline transport is a distinct, labelled mode")
	var help := SimulationConfig.parse(PackedStringArray(["--help"]))
	_check(help.ok and help.get("help", false), "--help is recognised")
	_check("--remote" in SimulationConfig.usage() and "COST" in SimulationConfig.usage(), "usage documents the remote opt-in and its cost")
	_end()


func _test_config_rejects_bad_input() -> void:
	print("\nTest: bad input is rejected with a message")
	var bad := [
		["--nope"],
		["stray"],
		["--runs", "0"],
		["--runs", "5000"],
		["--steps", "abc"],
		["--steps", "0"],
		["--faults", "bogus"],
		["--policy", "bogus"],
		["--transport", "bogus"],
		["--seed"],
		["--audit-every", "0"],
		["--remote", "--faults", "flaky"],
		["--remote", "--transport", "offline"],
		["--remote", "--endpoint", "ftp://example.com"],
		["--remote", "--endpoint", "http://user:secret@example.com"],
		["--remote", "--endpoint", "http://example.com/?x=1"],
		["--remote", "--max-requests", "0"],
		["--remote", "--timeout", "0"],
		["--remote", "--provider", "bad id with spaces"],
	]
	for args in bad:
		var parsed := SimulationConfig.parse(PackedStringArray(args))
		_check(not parsed.ok and str(parsed.get("error", "")) != "", "%s is rejected (%s)" % [" ".join(args), parsed.get("error", "")])
	_end()


func _test_remote_requires_explicit_opt_in() -> void:
	print("\nTest: remote settings never take effect without --remote")
	for args in [["--provider", "cloudflare-jev"], ["--model", "typesafe/jev"], ["--endpoint", "http://127.0.0.1:8000"], ["--max-requests", "10"]]:
		var parsed := SimulationConfig.parse(PackedStringArray(args))
		_check(not parsed.ok and "--remote" in str(parsed.error), "%s without --remote is an error that names --remote" % " ".join(args))
	var env := {"DUNGEON_DIRECTOR_URL": "http://remote.example:9", "DUNGEON_DIRECTOR_PROVIDER": "cloudflare-jev", "DUNGEON_DIRECTOR_MODEL": "typesafe/jev"}
	var ambient := SimulationConfig.parse(PackedStringArray(), env)
	_check(ambient.ok and not ambient.config.is_remote(), "director environment variables alone do not enable remote calls")
	_check(ambient.config.provider_summary().endpoint == null, "and no endpoint is recorded")
	_end()


func _test_remote_config_and_cost_warning() -> void:
	print("\nTest: remote configuration and the cost warning")
	var parsed := SimulationConfig.parse(PackedStringArray(["--remote", "--endpoint", "http://127.0.0.1:9000/", "--provider", "cloudflare-jev", "--model", "typesafe/jev", "--max-requests", "50", "--timeout", "8"]))
	_check(parsed.ok, "remote configuration parses: %s" % parsed.get("error", ""))
	var config: SimulationConfig = parsed.config
	_check(config.is_remote(), "remote is on")
	_check_eq(config.endpoint, "http://127.0.0.1:9000", "trailing slash is normalised away")
	_check_eq(config.max_requests, 50, "request budget")
	_check_eq(config.timeout_sec, 8.0, "timeout")
	var provider := config.provider_summary()
	_check_eq(provider.mode, "remote", "provider mode")
	_check_eq(provider.provider, "cloudflare-jev", "provider id")
	_check_eq(provider.model, "typesafe/jev", "model id")
	_check_eq(provider.endpoint, "http://127.0.0.1:9000", "endpoint")
	var warning := config.cost_warning()
	_check("COST WARNING" in warning, "the warning is conspicuous")
	for needle in ["http://127.0.0.1:9000", "cloudflare-jev", "typesafe/jev", "50"]:
		_check(needle in warning, "the warning names %s" % needle)
	var defaults := SimulationConfig.parse(PackedStringArray(["--remote"]), {"DUNGEON_DIRECTOR_URL": "http://from-env:1", "DUNGEON_DIRECTOR_MODEL": "m-env"})
	_check(defaults.ok and defaults.config.endpoint == "http://from-env:1" and defaults.config.model == "m-env", "with --remote the director environment supplies defaults")
	_check(defaults.config.max_requests > 0, "a finite request budget is always set for remote runs")
	var bare := SimulationConfig.parse(PackedStringArray(["--remote"]))
	_check(bare.ok and bare.config.endpoint == "http://127.0.0.1:8000", "the default endpoint is the local director")
	_end()


# --- rules transport ---------------------------------------------------------


func _test_rules_transport_answers_with_valid_contract_responses() -> void:
	print("\nTest: the rules transport speaks the canonical contract, asynchronously and deterministically")
	var request := _make_request()
	var results: Array = []
	var transport := RulesTransport.new()
	transport.submit(request, {}, func(r: Dictionary) -> void: results.append(r))
	_check(results.is_empty(), "nothing is delivered inside submit()")
	transport.poll()
	_check_eq(results.size(), 1, "delivered on the next poll")
	var parsed := DungeonContracts.parse_generation_response(results[0].body)
	_check(parsed.ok, "response validates against the contract: %s" % parsed.get("error", ""))
	_check(results[0].transport_ok and results[0].http_status == 200, "successful HTTP-shaped result")
	_check_eq(parsed.response.metadata.provider, "rules-baseline", "reports the rules-baseline provider")
	_check_eq(parsed.response.metadata.model, "builtin-v1", "reports the builtin-v1 model")
	_check_eq(parsed.response.request_id, request.request_id, "echoes the request id")
	var directions: Array = parsed.response.room.exits.map(func(e: Dictionary) -> String: return e.direction)
	var opposite: String = DungeonWorld.OPPOSITE[request.target_exit.direction]
	_check(opposite in directions, "the room links back to the target frontier")
	var again: Array = []
	var twin := RulesTransport.new()
	twin.submit(request, {}, func(r: Dictionary) -> void: again.append(r))
	twin.poll()
	_check_eq(again[0].body, results[0].body, "same request, same response")
	transport.poll()
	_check_eq(results.size(), 1, "a delivered request is not delivered again")
	_end()


func _test_rules_transport_fault_profiles() -> void:
	print("\nTest: fault profiles are deterministic per request index")
	_check_eq(RulesTransport.fault_for(1, "none"), "ok", "no faults: ok")
	_check_eq(RulesTransport.fault_for(3, "none"), "ok", "no faults: even request 3 is ok")
	_check_eq(RulesTransport.fault_for(1, "flaky"), "ok", "flaky request 1")
	_check_eq(RulesTransport.fault_for(3, "flaky"), "hang", "flaky request 3 never answers")
	_check_eq(RulesTransport.fault_for(5, "flaky"), "invalid_json", "flaky request 5")
	_check_eq(RulesTransport.fault_for(6, "flaky"), "failure_envelope", "flaky request 6")
	_check_eq(RulesTransport.fault_for(8, "flaky"), "duplicate", "flaky request 8")
	_check_eq(RulesTransport.fault_for(9, "flaky"), "id_mismatch", "flaky request 9")
	_check_eq(RulesTransport.fault_for(33, "flaky"), "missing_backlink", "flaky request 33")
	_check_eq(RulesTransport.fault_for(1, "hang"), "hang", "hang profile never answers")
	var transport := RulesTransport.new()
	transport.faults = "hang"
	var results: Array = []
	transport.submit(_make_request(), {}, func(r: Dictionary) -> void: results.append(r))
	transport.poll()
	transport.poll()
	_check(results.is_empty(), "a hung request stays unanswered")
	_end()


# --- recording transport -----------------------------------------------------


func _test_recording_transport_records_and_enforces_budget() -> void:
	print("\nTest: the recording transport captures metadata and hard-caps requests")
	var inner := RulesTransport.new()
	var clock := [1000]
	var recording := RecordingTransport.new(inner, func() -> int: return clock[0], 1)
	var request := _make_request("req-t-1")
	var results: Array = []
	recording.submit(request, {"provider": "p", "model": "m"}, func(r: Dictionary) -> void: results.append(r))
	clock[0] = 1040
	recording.poll()
	_check_eq(results.size(), 1, "the result reaches the caller")
	var record: Dictionary = recording.record_for("req-t-1")
	_check_eq(record.http_status, 200, "http status recorded")
	_check(record.transport_ok, "transport_ok recorded")
	_check_eq(record.provider, "rules-baseline", "reported provider recorded")
	_check_eq(record.model, "builtin-v1", "reported model recorded")
	_check_eq(record.latency_ms, 40, "latency is measured on the injected clock")
	_check_eq(recording.submitted, 1, "one request submitted")
	_check(recording.budget_exhausted(), "a budget of one is now spent")

	var refused_results: Array = []
	recording.submit(_make_request("req-t-2"), {}, func(r: Dictionary) -> void: refused_results.append(r))
	_check(refused_results.is_empty(), "a refusal is asynchronous too")
	recording.poll()
	_check_eq(inner.served, 1, "the inner transport never saw the second request")
	_check_eq(refused_results.size(), 1, "the caller still gets an answer")
	_check(not refused_results[0].transport_ok and refused_results[0].error_kind == "budget_exhausted", "the answer is a budget_exhausted failure")
	_check_eq(recording.refused, 1, "refusal counted")
	_check_eq(recording.record_for("req-t-2").error_kind, "budget_exhausted", "refusal recorded")
	_end()


# --- audit -------------------------------------------------------------------


func _grown_world() -> Dictionary:
	var run := await _run(["--runs", "1", "--steps", "30", "--seed", "3"])
	return {"harness": run.harness, "state": run.harness.last_state, "world": run.harness.last_state.world}


func _kinds(failures: Array) -> Array:
	var kinds: Array = []
	for f in failures:
		if not (f.kind in kinds):
			kinds.append(f.kind)
	kinds.sort()
	return kinds


func _test_audit_accepts_a_grown_world() -> void:
	print("\nTest: the audit accepts a world the real generator grew")
	var grown := await _grown_world()
	var world: DungeonWorld = grown.world
	_check(world.rooms.size() >= 20, "the world grew (%d rooms)" % world.rooms.size())
	var failures := SimulationAudit.audit(world, grown.harness.start_pos)
	_check(failures.is_empty(), "no violations: %s" % str(failures.slice(0, 3)))
	_end()


func _test_audit_detects_corruption() -> void:
	print("\nTest: the audit names each kind of corruption")
	# overlap: a second room claims a floor tile another room already owns.
	var grown := await _grown_world()
	var world: DungeonWorld = grown.world
	var start: Vector2i = grown.harness.start_pos
	var victim_pos := Vector2i.ZERO
	var victim_room := ""
	for room_id in world.room_order:
		for pos in world.rooms[room_id].tiles:
			if world.rooms[room_id].tiles[pos] == GeneratedRoom.TileType.FLOOR:
				victim_pos = pos
				victim_room = room_id
				break
		if victim_room != "":
			break
	var other_room: String = world.room_order[world.room_order.size() - 1]
	_check(other_room != victim_room, "picked two different rooms")
	world.rooms[other_room].tiles[victim_pos] = GeneratedRoom.TileType.FLOOR
	var failures := SimulationAudit.audit(world, start)
	_check("overlap" in _kinds(failures), "overlap is reported: %s" % str(_kinds(failures)))
	_check(failures.all(func(f: Dictionary) -> bool: return f.severity == "error" and f.has("message") and f.has("detail")), "every failure carries a severity, message and detail")
	world.rooms[other_room].tiles.erase(victim_pos)
	_check(SimulationAudit.audit(world, start).is_empty(), "repairing the corruption clears the report")

	# topology: a floor tile floating in the void.
	world.tiles[Vector2i(5000, 5000)] = GeneratedRoom.TileType.FLOOR
	failures = SimulationAudit.audit(world, start)
	_check("topology" in _kinds(failures), "a disconnected traversable tile is a topology failure: %s" % str(_kinds(failures)))
	world.tiles.erase(Vector2i(5000, 5000))

	# topology: a committed frontier pointing at a room that does not exist.
	var committed_key := ""
	for key in world.frontiers:
		if world.frontiers[key].status == DungeonWorld.STATUS_COMMITTED and world.frontiers[key].resolved_room_id != "":
			committed_key = key
			break
	var original: String = world.frontiers[committed_key].resolved_room_id
	world.frontiers[committed_key].resolved_room_id = "ghost-room"
	failures = SimulationAudit.audit(world, start)
	_check("topology" in _kinds(failures), "a dangling frontier link is a topology failure")
	world.frontiers[committed_key].resolved_room_id = original

	# reachability: loot that spawned inside solid wall.
	var wall_pos := Vector2i.ZERO
	for pos in world.rooms[world.room_order[0]].tiles:
		if world.rooms[world.room_order[0]].tiles[pos] == GeneratedRoom.TileType.WALL:
			wall_pos = pos
			break
	world.rooms[world.room_order[0]].items.append({"type": "potion_health", "pos": wall_pos})
	failures = SimulationAudit.audit(world, start)
	_check("reachability" in _kinds(failures), "an unreachable resource is a reachability failure: %s" % str(_kinds(failures)))
	world.rooms[world.room_order[0]].items.pop_back()

	# reachability: an open frontier whose door tile was walled off.
	var open := world.open_frontiers()
	var door: Vector2i = open[0].pos
	var saved: int = world.tiles[door]
	world.tiles[door] = GeneratedRoom.TileType.WALL
	failures = SimulationAudit.audit(world, start)
	_check("reachability" in _kinds(failures), "an open exit that cannot be reached is a reachability failure")
	world.tiles[door] = saved
	_check(SimulationAudit.audit(world, start).is_empty(), "the fully repaired world is clean again")
	_end()


func _test_events_to_failures() -> void:
	print("\nTest: world log events map to placement-conflict failures")
	var entries: Array = [
		{"seq": 1, "event": "requested", "frontier": "r-000:north", "request_id": "a"},
		{"seq": 2, "event": "rejected", "frontier": "r-000:north", "request_id": "a", "reason": "overlap"},
		{"seq": 3, "event": "fallback", "frontier": "r-000:north", "reason": "timeout"},
		{"seq": 4, "event": "sealed", "frontier": "r-000:east", "request_id": "b", "reason": "no_placement_fits"},
		{"seq": 5, "event": "committed", "frontier": "r-000:west", "room_id": "x"},
		{"seq": 6, "event": "duplicate", "frontier": "r-000:west"},
	]
	var failures := SimulationHarness.events_to_failures(entries, 2, 7)
	_check_eq(failures.size(), 2, "only conflicts become failures (rejection + seal)")
	var rejection: Dictionary = failures[0]
	_check_eq(rejection.kind, "placement_conflict", "kind")
	_check_eq(rejection.severity, "info", "a rejection that was recovered is informational")
	_check_eq(rejection.run, 2, "run index")
	_check_eq(rejection.step, 7, "step index")
	_check_eq(rejection.frontier, "r-000:north", "frontier")
	_check_eq(rejection.detail.reason, "overlap", "reason")
	var seal: Dictionary = failures[1]
	_check_eq(seal.kind, "placement_conflict", "seal kind")
	_check_eq(seal.severity, "warning", "an exit that had to be sealed is a warning")
	_check_eq(seal.detail.reason, "no_placement_fits", "seal reason")
	_end()


# --- harness -----------------------------------------------------------------


func _test_offline_rules_run_is_clean_and_measured() -> void:
	print("\nTest: an offline rules run is clean and reports every metric")
	var run := await _run(["--runs", "2", "--steps", "40", "--seed", "11"])
	var dataset: Dictionary = run.dataset
	for key in ["manifest", "steps", "rooms", "failures", "summary"]:
		_check(dataset.has(key), "dataset has %s" % key)
	_check_eq(_error_failures(dataset).size(), 0, "no error failures: %s" % str(_error_failures(dataset).slice(0, 2)))
	_check(dataset.summary.overall.passed, "overall verdict is passed")
	_check_eq(dataset.summary.runs.size(), 2, "two runs summarised")
	for run_summary in dataset.summary.runs:
		_check(run_summary.steps >= 40, "run %d resolved at least 40 steps (%d)" % [run_summary.run, run_summary.steps])
		_check_eq(run_summary.status, "completed", "run %d completed" % run_summary.run)
		_check(run_summary.rooms_explored > 0 and run_summary.rooms_explored <= run_summary.rooms_committed, "run %d explored %d of %d rooms" % [run_summary.run, run_summary.rooms_explored, run_summary.rooms_committed])
		_check(run_summary.fallback_frequency < 0.3, "the baseline rarely falls back (%s)" % str(run_summary.fallback_frequency))
		var provider_reasons: Array = run_summary.fallback_reasons.keys().filter(func(k: String) -> bool: return k != "plan_rejected")
		_check(provider_reasons.is_empty(), "the baseline only falls back when a placement cannot fit, never for a provider failure: %s" % str(run_summary.fallback_reasons))
		_check_eq(run_summary.sealed_exits, run_summary.fallback_reasons.get("plan_rejected", 0), "every such fallback ended in a sealed exit")
		_check(run_summary.sources.director > 0, "rooms came from the (rules) director")
		for key in ["min", "max", "mean", "slope", "first_third_mean", "last_third_mean", "histogram", "by_bucket"]:
			_check(run_summary.danger.has(key), "danger progression has %s" % key)
		for key in ["enemies", "items", "per_room", "rooms_without_items", "rooms_without_enemies"]:
			_check(run_summary.resources.has(key), "resources have %s" % key)
		for key in ["unique_signatures", "signature_repeat_rate", "max_consecutive_same_type", "type_histogram", "size_histogram"]:
			_check(run_summary.repetition.has(key), "repetition has %s" % key)
	var steps_by_run := {}
	for row in dataset.steps:
		steps_by_run[row.run] = int(steps_by_run.get(row.run, 0)) + 1
		_check(row.outcome in ["committed", "sealed"], "step outcome is terminal (%s)" % row.outcome)
	_check(steps_by_run.size() == 2 and steps_by_run[0] >= 40 and steps_by_run[1] >= 40, "steps.jsonl covers both runs: %s" % str(steps_by_run))
	_check(dataset.steps.all(func(r: Dictionary) -> bool: return r.wait_ticks >= 1), "every step waited at least one tick for its (asynchronous) response")
	_check_eq(dataset.rooms.size(), dataset.summary.overall.rooms_committed, "rooms.jsonl matches the committed-room total")
	for row in dataset.rooms:
		for key in ["run", "index", "room_id", "source", "room_type", "width", "height", "danger", "parent_frontier", "distance_from_start", "floor_tiles", "exit_count", "enemies", "items", "explored", "signature"]:
			_check(row.has(key), "room row has %s" % key)
			if not row.has(key):
				break
		break
	var provider: Dictionary = dataset.manifest.provider
	_check_eq(provider.mode, "rules-baseline", "manifest provider mode")
	_check_eq(provider.model, "builtin-v1", "manifest model")
	_check(not dataset.manifest.remote.opt_in, "manifest records that remote was not enabled")
	_check_eq(dataset.manifest.runs.size(), 2, "manifest lists both run seeds")
	_check_eq(dataset.manifest.runs[1].seed, 12, "seeds count up from the base seed")
	_check(dataset.summary.telemetry.requests_submitted > 0, "requests were counted")
	_check(dataset.summary.telemetry.latency_ms == null, "virtual-clock runs report no latency")
	_end()


func _test_runs_are_deterministic() -> void:
	print("\nTest: the same configuration reproduces the dataset byte for byte")
	var args := ["--runs", "2", "--steps", "30", "--seed", "5", "--faults", "flaky"]
	var a := await _run(args)
	var b := await _run(args)
	var files_a := SimulationDataset.to_files(a.dataset)
	var files_b := SimulationDataset.to_files(b.dataset)
	for name in REQUIRED_FILES:
		_check(files_a.has(name) and files_a[name] == files_b[name], "%s is identical across runs" % name)
	var c := await _run(["--runs", "2", "--steps", "30", "--seed", "6", "--faults", "flaky"])
	_check(SimulationDataset.to_files(c.dataset)["rooms.jsonl"] != files_a["rooms.jsonl"], "a different seed gives a different dungeon")
	_end()


func _test_flaky_provider_measures_fallbacks() -> void:
	print("\nTest: an unreliable provider shows up as fallback frequency and conflicts")
	var run := await _run(["--runs", "1", "--steps", "80", "--seed", "2", "--faults", "flaky"])
	var dataset: Dictionary = run.dataset
	var summary: Dictionary = dataset.summary.runs[0]
	_check(summary.fallback_frequency > 0.0 and summary.fallback_frequency < 1.0, "some but not all steps fell back (%s)" % str(summary.fallback_frequency))
	_check(summary.fallback_reasons.has("timeout"), "timeouts are reported: %s" % str(summary.fallback_reasons))
	_check(summary.fallback_reasons.has("invalid_response"), "invalid responses are reported")
	_check(summary.sources.fallback > 0 and summary.sources.director > 0, "both sources present: %s" % str(summary.sources))
	var kinds := {}
	for failure in dataset.failures:
		kinds[failure.kind] = true
	_check(kinds.has("placement_conflict"), "rejected placements are recorded as placement conflicts")
	_check_eq(_error_failures(dataset).size(), 0, "faults are recovered, not invariant violations: %s" % str(_error_failures(dataset).slice(0, 2)))
	var fallback_rows: Array = dataset.steps.filter(func(r: Dictionary) -> bool: return r.fallback)
	_check(fallback_rows.size() > 0 and fallback_rows.all(func(r: Dictionary) -> bool: return r.fallback_reason != null), "fallback rows carry a reason")
	_check(dataset.summary.overall.fallback_frequency > 0.0, "overall fallback frequency is reported")
	_end()


func _test_offline_transport_is_all_fallback() -> void:
	print("\nTest: the game's offline transport exercises the pure fallback path")
	var run := await _run(["--runs", "1", "--steps", "30", "--seed", "4", "--transport", "offline"])
	var summary: Dictionary = run.dataset.summary.runs[0]
	_check_eq(summary.fallback_frequency, 1.0, "every resolution used the local fallback")
	_check_eq(summary.fallback_reasons.get("offline", 0), summary.steps, "all for reason 'offline'")
	_check_eq(_error_failures(run.dataset).size(), 0, "the fallback path keeps every invariant")
	_end()


func _test_stall_is_detected() -> void:
	print("\nTest: a step that never resolves is reported as a stall, not a hang")
	var config := _config(["--runs", "2", "--steps", "10", "--seed", "1", "--faults", "hang"])
	config.stall_after_msec = 300
	var harness := SimulationHarness.new(config, self)
	var dataset: Dictionary = await harness.run()
	var stalls: Array = dataset.failures.filter(func(f: Dictionary) -> bool: return f.kind == "stall")
	_check(stalls.size() >= 1, "a stall failure is recorded")
	_check(stalls.size() > 0 and stalls[0].severity == "error", "a stall is an error")
	_check(stalls.size() > 0 and stalls[0].frontier != null and stalls[0].detail.has("waited_msec"), "the stall names the frontier and how long it waited")
	_check_eq(dataset.summary.runs[0].status, "stalled", "the run is marked stalled")
	_check(not dataset.summary.overall.passed, "the overall verdict fails")
	_check_eq(dataset.summary.runs.size(), 2, "later runs still execute after a stall")
	_end()


# --- dataset files -----------------------------------------------------------


func _test_dataset_files_roundtrip_and_refuse_overwrite() -> void:
	print("\nTest: datasets are written as portable JSON / JSONL and never overwritten")
	var run := await _run(["--runs", "1", "--steps", "15", "--seed", "8"])
	var out := _user_dir("sim-dataset-test")
	_remove_dir(out)
	var written := SimulationDataset.write(run.dataset, out)
	_check(written.ok, "write succeeds: %s" % written.get("error", ""))
	for name in REQUIRED_FILES:
		_check(FileAccess.file_exists(out.path_join(name)), "%s exists" % name)
	var manifest = JSON.parse_string(FileAccess.get_file_as_string(out.path_join("manifest.json")))
	_check(manifest is Dictionary and manifest.schema_version == SimulationDataset.SCHEMA_VERSION, "manifest parses and carries the schema version")
	_check_eq(manifest.files.steps, "steps.jsonl", "manifest lists its files")
	var lines := FileAccess.get_file_as_string(out.path_join("rooms.jsonl")).split("\n", false)
	_check_eq(lines.size(), run.dataset.rooms.size(), "one JSONL line per room")
	var first = JSON.parse_string(lines[0])
	_check(first is Dictionary and first.room_id == "r-000", "each line is a JSON object; the first room is the start room")
	_check(FileAccess.get_file_as_string(out.path_join("rooms.jsonl")).ends_with("\n"), "JSONL files end with a newline")
	var again := SimulationDataset.write(run.dataset, out)
	_check(not again.ok and "already" in str(again.error), "an existing dataset is not overwritten: %s" % again.get("error", ""))
	_remove_dir(out)
	_end()


func _test_golden_fixture_pins_dataset_format() -> void:
	print("\nTest: the golden fixture pins the dataset format")
	var run := await _run(["--runs", "1", "--steps", "12", "--seed", "7", "--faults", "flaky"])
	var files := SimulationDataset.to_files(run.dataset, false)
	var dir := ProjectSettings.globalize_path("res://").path_join(FIXTURE_DIR).simplify_path()
	if OS.get_environment("SIM_UPDATE_FIXTURES") == "1":
		DirAccess.make_dir_recursive_absolute(dir)
		for name in files:
			var handle := FileAccess.open(dir.path_join(name), FileAccess.WRITE)
			handle.store_string(files[name])
		print("  [INFO] fixture rewritten at %s" % dir)
	for name in REQUIRED_FILES:
		var path: String = dir.path_join(name)
		_check(FileAccess.file_exists(path), "fixture %s exists" % name)
		if FileAccess.file_exists(path):
			_check(FileAccess.get_file_as_string(path) == files[name], "%s matches the golden fixture (SIM_UPDATE_FIXTURES=1 rewrites it)" % name)
	_end()


# --- remote ------------------------------------------------------------------


func _test_remote_run_over_http_respects_budget() -> void:
	print("\nTest: a remote run honours provider/model/endpoint and the request budget")
	var server := MiniHttpServer.new()
	_check(server.start(), "loopback test server started")
	server.responder = func(request: Dictionary) -> Dictionary:
		var plan := RulesTransport.plan_for_request(request, "rm-%s" % str(request.request_id).md5_text().substr(0, 10))
		return {"status": 200, "body": RulesTransport.success_body(request, plan, "stub-prov", "stub-model")}
	var base := "http://127.0.0.1:%d" % server.port
	var config := _config(["--remote", "--endpoint", base, "--provider", "stub-prov", "--model", "stub-model", "--max-requests", "6", "--runs", "3", "--steps", "10", "--timeout", "2"])
	var harness := SimulationHarness.new(config, self)
	harness.tick_hook = server.poll
	var dataset: Dictionary = await harness.run()
	server.stop()
	_check_eq(server.requests.size(), 6, "the server saw exactly the budgeted 6 requests")
	var targets_ok := true
	for seen in server.requests:
		targets_ok = targets_ok and "provider=stub-prov" in seen.target and "model=stub-model" in seen.target and seen.target.begins_with("/v1/generate")
	_check(targets_ok, "every request selected the configured provider and model")
	var manifest: Dictionary = dataset.manifest
	_check_eq(manifest.provider.mode, "remote", "manifest mode")
	_check_eq(manifest.provider.endpoint, base, "manifest endpoint")
	_check_eq(manifest.provider.provider, "stub-prov", "manifest provider")
	_check_eq(manifest.provider.model, "stub-model", "manifest model")
	_check(manifest.remote.opt_in, "manifest records the explicit opt-in")
	_check_eq(manifest.remote.request_budget, 6, "manifest records the request budget")
	var telemetry: Dictionary = dataset.summary.telemetry
	_check_eq(telemetry.requests_submitted, 6, "telemetry counts the requests sent")
	_check(telemetry.latency_ms != null and telemetry.latency_ms.count > 0, "latency was measured on the real clock")
	_check_eq(telemetry.reported_providers.get("stub-prov", 0), 6, "the provider reported in responses is tallied")
	var budget_rows: Array = dataset.failures.filter(func(f: Dictionary) -> bool: return f.kind == "budget")
	_check(budget_rows.size() == 1 and budget_rows[0].severity == "warning", "one budget warning is recorded")
	_check(dataset.summary.overall.truncated == "request_budget_exhausted", "the summary says the simulation was truncated by the budget")
	_check(dataset.summary.runs.size() < 3 or dataset.summary.runs[dataset.summary.runs.size() - 1].status == "budget_exhausted", "later runs are not started once the budget is spent")
	_check_eq(telemetry.requests_refused, dataset.steps.filter(func(r: Dictionary) -> bool: return r.fallback_reason == "budget_exhausted").size(), "every locally refused request surfaces as a budget_exhausted fallback")
	_end()


func _test_cli_exit_codes_and_remote_warning() -> void:
	print("\nTest: the CLI maps outcomes to exit codes and shows the warning before remote calls")
	var out := _user_dir("sim-cli-test")
	_remove_dir(out)
	var help: Dictionary = await SimulationCli.execute(PackedStringArray(["--help"]), self)
	_check_eq(help.exit_code, 0, "--help exits 0")
	var bad: Dictionary = await SimulationCli.execute(PackedStringArray(["--provider", "x"]), self)
	_check_eq(bad.exit_code, 2, "a usage error exits 2")
	_check(not bad.warning_shown, "no warning for an unrelated usage error")
	var ok: Dictionary = await SimulationCli.execute(PackedStringArray(["--runs", "1", "--steps", "10", "--out", out]), self)
	_check_eq(ok.exit_code, 0, "a clean offline run exits 0")
	_check(not ok.warning_shown, "an offline run shows no cost warning")
	_check(FileAccess.file_exists(out.path_join("summary.json")), "the dataset is written to --out")
	var clash: Dictionary = await SimulationCli.execute(PackedStringArray(["--runs", "1", "--steps", "10", "--out", out]), self)
	_check_eq(clash.exit_code, 2, "writing over an existing dataset is refused up front")
	_remove_dir(out)
	var remote_out := _user_dir("sim-cli-remote-test")
	_remove_dir(remote_out)
	var remote: Dictionary = await SimulationCli.execute(PackedStringArray(["--remote", "--endpoint", "http://127.0.0.1:1", "--max-requests", "3", "--runs", "1", "--steps", "3", "--timeout", "1", "--out", remote_out]), self)
	_check(remote.warning_shown, "the remote cost warning is shown")
	_check(str(remote.warning).contains("COST WARNING"), "and it is conspicuous")
	_check_eq(remote.exit_code, 0, "an unreachable provider falls back cleanly, so the run still exits 0")
	var manifest = JSON.parse_string(FileAccess.get_file_as_string(remote_out.path_join("manifest.json")))
	_check(manifest is Dictionary and manifest.remote.opt_in, "the manifest records the opt-in")
	_remove_dir(remote_out)
	_end()
