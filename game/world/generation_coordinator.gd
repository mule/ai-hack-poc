class_name GenerationCoordinator
extends RefCounted
## Decides when to generate, talks to the transport, validates what comes back
## and commits it to the world (Issue #7).
##
## Flow, all non-blocking (the game loop only ever calls update()):
##   1. update() finds unresolved frontiers near the player (prefetch) and
##      marks each pending in the world, then hands a canonical request to the
##      transport. Nothing is committed yet.
##   2. When the transport reports back (later, from a signal/poll), the
##      response is validated against the contract. A usable RoomPlan is turned
##      into geometry by RoomGenerator and committed through GameState -> world.
##   3. Any failure (timeout, transport error, invalid/failed/contradictory
##      response, placement rejection) switches to the deterministic local
##      RulesBaseline plan under the same request id. If even that cannot be
##      placed, the exit is sealed. The world is only mutated by the final,
##      validated commit.
##
## Late, duplicate or stale completions are routed to the world, which is the
## authority and refuses them without mutating anything.

const GameTelemetrySink = preload("res://world/game_telemetry_sink.gd")

const DungeonContracts = preload("res://contracts/dungeon_contracts.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")
const RoomGenerator = preload("res://generation/room_generator.gd")
const RulesBaseline = preload("res://world/rules_baseline.gd")
const GenerationRecorder = preload("res://world/generation_recorder.gd")

const WORLD_DEPTH := 1
const SIZE_CHAIN := ["huge", "large", "medium", "small", "tiny"]
const HANDLED_LIMIT := 256
const URGENT_DISTANCE := 1

var game_state: RefCounted
## Transport interface: submit(request, options, on_done), poll(), cancel_all().
var transport: Variant
## Optional generation dataset recorder.
var recorder: Variant = null
## Optional generation lifecycle telemetry sink.
var telemetry_sink: Variant = null
## Optional stable ids forwarded to the director as selectors.
var provider := ""
var model := ""
## Manhattan distance (tiles) from the player at which an exit is prefetched.
var trigger_radius := 3
var max_in_flight := 2
var timeout_msec := 5000
## Injectable clock returning milliseconds; defaults to the engine tick clock.
var clock: Callable = Callable()
## Room types the world cannot use (vertical connections are unsupported).
var forbidden_room_types: Array = ["stairs_down", "stairs_up"]

## Latest completed generation and a bounded per-run decision history for the
## debug HUD. Provider metadata remains opaque to gameplay.
var last_generation_status := "idle"
var last_latency_ms := -1.0
var last_provider := ""
var last_model := ""
var last_provider_metadata: Dictionary = {}
var recent_decisions: Array[Dictionary] = []
const MAX_RECENT_DECISIONS := 10

## request_id -> {key, started, request}
var in_flight: Dictionary = {}

var _handled: Dictionary = {}
var _bound_world: RefCounted = null
var _serial := 0


func _init(state: RefCounted, xport: Variant) -> void:
	game_state = state
	transport = xport
	_bound_world = state.world
	if GenerationRecorder._get_default_path() != "":
		recorder = GenerationRecorder.new()



## Drive generation. Cheap and non-blocking: call it every frame.
func update() -> void:
	_sync_world()
	transport.poll()
	_expire_deadlines()
	_ensure_exit_exists()
	_start_requests()


## Stop generating: forget in-flight requests and cancel them on the transport
## so no completion callback can reach this (soon to be freed) coordinator.
func shutdown() -> void:
	_cancel_active_requests()
	if recorder != null and recorder.has_method("close"):
		recorder.close()


func _cancel_active_requests() -> void:
	in_flight.clear()
	_handled.clear()
	transport.cancel_all()


## Rebind after GameState creates a new run. Recording remains active across
## restarts, while requests and HUD history from the old run are discarded.
func reset_for_current_world() -> void:
	_cancel_active_requests()
	_bound_world = game_state.world
	_reset_hud_state()


func request_count() -> int:
	return _serial


# --- request building --------------------------------------------------------


func build_request(f: Dictionary, request_id: String) -> Dictionary:
	var world: DungeonWorld = game_state.world
	var player: Vector2i = game_state.player_pos
	var open := world.open_frontiers()
	open.sort_custom(func(a: Dictionary, b: Dictionary) -> bool:
		var da := _distance(a.pos, player)
		var db := _distance(b.pos, player)
		return da < db if da != db else a.key < b.key)
	var exits: Array = []
	for other in open:
		if other.key == f.key:
			exits.push_front(_exit_entry(other))
		elif exits.size() < DungeonContracts.MAX_UNRESOLVED_EXITS:
			exits.append(_exit_entry(other))
	exits = exits.slice(0, DungeonContracts.MAX_UNRESOLVED_EXITS)
	var recent: Array = []
	for room_id in world.room_order.slice(-DungeonContracts.MAX_RECENT_ROOMS):
		var meta: Dictionary = world.rooms[room_id].meta
		recent.append({"room_id": room_id, "room_type": meta.get("room_type", "room"), "danger": clampi(int(meta.get("danger", 1)), 1, 5)})
	return {
		"contract_version": DungeonContracts.CONTRACT_VERSION,
		"request_id": request_id,
		"run_id": world.run_id,
		"state": {
			"depth": WORLD_DEPTH,
			"turn": clampi(game_state.player_turns, 0, DungeonContracts.TURN_MAX),
			"player": {"hp": game_state.player_hp, "max_hp": game_state.player_max_hp, "level": 1},
			"recent_rooms": recent,
			"unresolved_exits": exits,
			"pacing": {"rooms_on_depth": mini(world.rooms.size(), 1024)},
		},
		"target_exit": _exit_entry(f),
		"options": {"forbidden_room_types": forbidden_room_types, "allow_secrets": true},
	}


func _exit_entry(f: Dictionary) -> Dictionary:
	return {"room_id": f.room_id, "direction": f.direction, "since_turn": f.since_turn}


# --- triggering --------------------------------------------------------------


## Boxed in (every exit resolved or sealed)? Breach a wall so play can go on.
func _ensure_exit_exists() -> void:
	var world: DungeonWorld = game_state.world
	if world.has_open_frontier():
		return
	var probe := func(direction: String) -> RefCounted:
		var back: String = DungeonWorld.OPPOSITE[direction]
		return RoomGenerator.generate(RulesBaseline.dead_end_plan("probe", WORLD_DEPTH, back), 1)
	var res := world.open_breach(probe, game_state.player_pos, game_state.player_turns)
	if res.ok:
		print("[dungeon-gen] no open exits; breached %s" % res.frontier.key)
		game_state.log_message("A hidden passage opens in the walls.")


func _start_requests() -> void:
	var world: DungeonWorld = game_state.world
	var candidates: Array = []
	for f in world.frontiers_with_status(DungeonWorld.STATUS_UNRESOLVED):
		var d := _distance(f.pos, game_state.player_pos)
		if d <= trigger_radius:
			candidates.append([d, f.key])
	candidates.sort_custom(func(a: Array, b: Array) -> bool: return a[0] < b[0] if a[0] != b[0] else a[1] < b[1])
	# Prefetches may already occupy every normal slot when the player reaches a
	# different exit. Permit one adjacent unresolved frontier to bypass the cap;
	# an urgent request already in flight consumes that reserve. This bounds the
	# total at max_in_flight + 1 instead of making the player wait for unrelated
	# speculative work.
	var urgent_reserve := not _has_urgent_in_flight(world)
	for candidate in candidates:
		if in_flight.size() >= max_in_flight:
			if int(candidate[0]) > URGENT_DISTANCE or not urgent_reserve:
				break
			urgent_reserve = false
		_start(world.get_frontier(candidate[1]))


func _has_urgent_in_flight(world: DungeonWorld) -> bool:
	for active in in_flight.values():
		var f := world.get_frontier(active.key)
		if not f.is_empty() and _distance(f.pos, game_state.player_pos) <= URGENT_DISTANCE:
			return true
	return false


func _start(f: Dictionary) -> void:
	var world: DungeonWorld = game_state.world
	_serial += 1
	var request_id := "req-%s-%d" % [world.run_id, _serial]
	if not world.begin_generation(f.key, request_id):
		return
	last_generation_status = "generating"
	var request := build_request(f, request_id)
	var queue_start := _now()
	in_flight[request_id] = {"key": f.key, "started": queue_start, "request": request}

	if telemetry_sink != null and telemetry_sink.has_method("enqueue_event"):
		# generation.queued (attributes: frontier_id, depth, exit_direction)
		var queued_attrs := {
			"frontier_id": f.key,
			"depth": WORLD_DEPTH,
			"exit_direction": str(f.direction),
		}
		telemetry_sink.enqueue_event("generation.queued", world.run_id, request_id, queued_attrs)

	var validation := DungeonContracts.validate_generation_request(request)
	if not validation.ok:
		var finish_info := _finish(request_id)
		_fallback(f, request_id, "invalid_request: %s" % validation.error, request, {}, finish_info.elapsed)
		return

	# generation.sent (attributes: frontier_id, depth, exit_direction, queue_ms)
	if telemetry_sink != null and telemetry_sink.has_method("enqueue_event"):
		var queue_ms := float(maxi(0, _now() - queue_start))
		var sent_attrs := {
			"frontier_id": f.key,
			"depth": WORLD_DEPTH,
			"exit_direction": str(f.direction),
			"queue_ms": queue_ms,
		}
		telemetry_sink.enqueue_event("generation.sent", world.run_id, request_id, sent_attrs)

	var options := {"provider": provider, "model": model, "timeout_sec": timeout_msec / 1000.0}
	transport.submit(request, options, _on_result.bind(request_id, world))


func _expire_deadlines() -> void:
	var now := _now()
	for request_id in in_flight.keys():
		var req_data: Dictionary = in_flight[request_id]
		var started := int(req_data.get("started", now))
		if now - started >= timeout_msec:
			var key: String = req_data.key
			var request: Dictionary = req_data.get("request", {})
			var finish_info := _finish(request_id)
			_fallback(game_state.world.get_frontier(key), request_id, "timeout", request, {}, finish_info.elapsed)


func _sync_world() -> void:
	if game_state.world != _bound_world:
		reset_for_current_world()


func _reset_hud_state() -> void:
	last_generation_status = "idle"
	last_latency_ms = -1.0
	last_provider = ""
	last_model = ""
	last_provider_metadata.clear()
	recent_decisions.clear()


func _finish(request_id: String) -> Dictionary:
	var req_data: Dictionary = in_flight.get(request_id, {})
	var started := int(req_data.get("started", _now()))
	req_data["elapsed"] = float(_now() - started)
	in_flight.erase(request_id)
	_handled[request_id] = req_data
	if _handled.size() > HANDLED_LIMIT:
		_handled.erase(_handled.keys()[0])
	return req_data


# --- completion --------------------------------------------------------------


func _on_result(result: Dictionary, request_id: String, world: RefCounted) -> void:
	if world != game_state.world:
		return  # the run was restarted; this response belongs to a dead world
	var current: DungeonWorld = game_state.world
	if in_flight.has(request_id):
		var req_data := _finish(request_id)
		_resolve(current.get_frontier(req_data.key), request_id, result, false, req_data.get("request", {}), req_data.elapsed)
	elif _handled.has(request_id):
		var req_data: Dictionary = _handled[request_id]
		_resolve(current.get_frontier(req_data.key), request_id, result, true, req_data.get("request", {}), req_data.get("elapsed", -1.0))
	else:
		current.counters.stale += 1
		current.log_event("stale", "", {"request_id": request_id, "reason": "unknown_request"})


## `late` responses (after a timeout/duplicate delivery) are still offered to
## the world, which refuses them because the frontier is no longer pending.
func _resolve(f: Dictionary, request_id: String, result: Dictionary, late: bool, request: Dictionary = {}, elapsed_ms: float = -1.0) -> void:
	var interpreted := _interpret(result, request_id)
	var resp_meta: Dictionary = interpreted.get("metadata", {}).duplicate(true)
	var traceparent: Variant = result.get("traceparent", null)
	if traceparent != null and GameTelemetrySink._is_valid_traceparent(str(traceparent)):
		resp_meta["_telemetry_traceparent"] = traceparent

	# Emit generation.response_received if transport returned a response
	if result.get("transport_ok", false) and telemetry_sink != null and telemetry_sink.has_method("enqueue_event"):
		var prov_recv := str(resp_meta.get("provider", provider))
		if prov_recv == "":
			prov_recv = "unknown"
		var mod_recv := str(resp_meta.get("model", model))
		if mod_recv == "":
			mod_recv = "unknown"
		var net_ms: float = elapsed_ms
		if net_ms < 0.0:
			net_ms = 0.0
		var resp_attrs := {
			"provider": prov_recv,
			"model": mod_recv,
			"network_ms": net_ms,
		}
		telemetry_sink.enqueue_event("generation.response_received", game_state.world.run_id, request_id, resp_attrs, resp_meta.get("_telemetry_traceparent"))

	if not interpreted.ok:
		if late:
			game_state.world.log_event("late_invalid", f.key, {"request_id": request_id, "reason": interpreted.reason})
		else:
			_fallback(f, request_id, interpreted.reason, request, resp_meta, elapsed_ms)
		return
	var plan: Dictionary = interpreted.plan
	var placed: Dictionary
	if int(plan.depth) != WORLD_DEPTH:
		placed = {"ok": false, "outcome": "rejected", "reason": "depth_mismatch"}
	else:
		placed = _place_plan(f, request_id, plan, "director", resp_meta)
	if placed.ok:

		if recorder != null and not request.is_empty():
			var room_seed := int(placed.get("room", {}).get("seed_used", game_state.world_seed))
			recorder.record_entry(request, provider, model, "committed", "director", room_seed, plan, "", placed.get("room", {}).get("meta", {}), resp_meta)
		if not late:
			last_generation_status = "committed"
			last_provider = str(resp_meta.get("provider", provider))
			last_model = str(resp_meta.get("model", model))
			last_latency_ms = float(resp_meta.latency_ms) if resp_meta.get("latency_ms") != null else elapsed_ms
			last_provider_metadata = resp_meta.get("provider_metadata", {}).duplicate(true)
			_record_decision(plan, "director", resp_meta)
		return
	if late or placed.outcome != "rejected":
		return
	_fallback(f, request_id, "plan_rejected:%s" % placed.reason, request, resp_meta, elapsed_ms)



## Transport result -> {ok, plan, metadata} or {ok:false, reason, [metadata]}.
func _interpret(result: Dictionary, request_id: String) -> Dictionary:
	if not result.get("transport_ok", false):
		return {"ok": false, "reason": str(result.get("error_kind", "transport_failure"))}
	var body := str(result.get("body", ""))
	var http_status := int(result.get("http_status", 0))
	# The director contract is always a JSON object. Reject HTML/text errors
	# before invoking the parser, both for clearer diagnostics and to avoid a
	# noisy engine parse error on common proxy failure pages.
	if not body.strip_edges().begins_with("{"):
		if http_status >= 400:
			return {"ok": false, "reason": "http_error:%d_non_json" % http_status}
		return {"ok": false, "reason": "invalid_response: response is not a JSON object"}
	var parsed := DungeonContracts.parse_generation_response(body)
	if not parsed.ok:
		return {"ok": false, "reason": "invalid_response: %s" % parsed.error}
	var response: Dictionary = parsed.response
	if response.request_id != request_id or response.run_id != game_state.world.run_id:
		return {"ok": false, "reason": "response_mismatch"}
	var meta: Dictionary = response.get("metadata", {})
	if response.get("success", true) == false:
		var code: String = meta.get("error", {}).get("code", "unknown_error")
		return {"ok": false, "reason": "provider_failure:%s" % code, "metadata": meta}
	return {"ok": true, "plan": response.room, "metadata": meta}


# --- placement and fallback --------------------------------------------------


## Try to commit `plan` for frontier `f`. Re-plans on geometric conflicts:
## exits facing committed space are pruned, then the footprint shrinks step by
## step. Returns the last world result; only its final success mutates state.
func _place_plan(f: Dictionary, request_id: String, plan: Dictionary, source: String, meta: Dictionary) -> Dictionary:
	var world: DungeonWorld = game_state.world
	var base := plan.duplicate(true)
	var placement_meta := meta.duplicate(true)
	placement_meta["materialization_started_msec"] = Time.get_ticks_msec()
	var proposed_room_id := str(base.get("room_id", "room"))
	if world.rooms.has(proposed_room_id):
		var unique_room_id := _unique_room_id(world, proposed_room_id, f.key, request_id)
		base["room_id"] = unique_room_id
		# Keep recorder/HUD decisions aligned with the room that actually commits.
		plan["room_id"] = unique_room_id
		placement_meta["provider_room_id"] = proposed_room_id
		placement_meta["room_id_rewritten"] = true
	var cardinal: Array = []
	for exit_entry in base.get("exits", []):
		if RulesBaseline.OPPOSITE.has(exit_entry.direction):
			cardinal.append(exit_entry)
	base["exits"] = cardinal
	var room_seed := ("%s|%s" % [world.run_id, f.key]).hash()
	var last := {"ok": false, "outcome": "rejected", "reason": "no_attempt", "blocked_directions": []}
	var sizes: Array = SIZE_CHAIN.slice(maxi(0, SIZE_CHAIN.find(base.get("size", "medium"))))
	for size in sizes:
		var candidate := base.duplicate(true)
		candidate["size"] = size
		var pruned: Array = []
		for attempt in range(2):
			var attempt_meta := placement_meta.duplicate()
			attempt_meta["room_type"] = base.get("room_type", "room")
			attempt_meta["danger"] = base.get("danger", 1)
			attempt_meta["size_used"] = size
			if size != base.get("size", size):
				attempt_meta["repositioned"] = true
				attempt_meta["room_size_reduced"] = true
			if not pruned.is_empty():
				attempt_meta["pruned_exits"] = pruned.duplicate()
			var room := RoomGenerator.generate(candidate, room_seed)
			last = game_state.commit_generated_room(f.key, request_id, room, source, attempt_meta)
			if last.ok or last.outcome != "rejected":
				return last
			if attempt == 0 and last.reason == "blocked_exit" and not last.blocked_directions.is_empty():
				pruned = last.blocked_directions.duplicate()
				pruned.sort()
				candidate["exits"] = candidate.exits.filter(func(e: Dictionary) -> bool: return not (e.direction in pruned))
				continue
			break
		if not (last.reason in DungeonWorld.GEOMETRIC_REASONS):
			return last
	return last


## Provider room ids are correlation hints, not world-global authority. If a
## provider repeats one, derive a bounded replay-stable local id rather than
## rejecting sound room semantics and cascading into a sealed doorway.
func _unique_room_id(world: DungeonWorld, proposed: String, frontier_key: String, request_id: String) -> String:
	var digest := ("%s|%s|%s" % [proposed, frontier_key, request_id]).sha256_text().substr(0, 10)
	var suffix := "-" + digest
	var stem := proposed.left(maxi(1, DungeonContracts.ID_MAX_LENGTH - suffix.length()))
	var candidate := stem + suffix
	var counter := 2
	while world.rooms.has(candidate):
		var numbered_suffix := "%s-%d" % [suffix, counter]
		stem = proposed.left(maxi(1, DungeonContracts.ID_MAX_LENGTH - numbered_suffix.length()))
		candidate = stem + numbered_suffix
		counter += 1
	return candidate


## Deterministic local plan -> commit; last resort seals the exit.
func _fallback(
	f: Dictionary,
	request_id: String,
	reason: String,
	request: Dictionary = {},
	response_metadata: Dictionary = {},
	elapsed_ms: float = -1.0
) -> void:
	var world: DungeonWorld = game_state.world
	if f.is_empty() or f.status != DungeonWorld.STATUS_PENDING or f.request_id != request_id:
		return
	world.note_fallback(f.key, reason, {"request_id": request_id})
	print("[dungeon-gen] fallback for %s: %s" % [f.key, reason])
	game_state.log_message("Generation fell back to local rules (%s)." % reason.get_slice(":", 0))

	var reject_reason := ""
	var fallback_reason := "provider_error"
	if reason.begins_with("plan_rejected"):
		reject_reason = "placement_failure"
		if reason.ends_with("depth_mismatch"):
			reject_reason = "schema_invalid"
		fallback_reason = "rejected_by_game"
	elif reason.begins_with("invalid_response") or reason == "response_mismatch":
		reject_reason = "schema_invalid"
		fallback_reason = "schema_error"
	elif reason.begins_with("invalid_request"):
		fallback_reason = "schema_error"
	elif reason == "timeout" or reason == "provider_timeout":
		fallback_reason = "provider_timeout"
	elif reason.begins_with("transport_failure") or reason.begins_with("http_error"):
		fallback_reason = "transport_failure"
	elif reason.begins_with("provider_failure"):
		var code := reason.get_slice(":", 1)
		if code == "provider_timeout":
			fallback_reason = "provider_timeout"
		elif code in ["schema_violation", "invalid_json", "empty_response", "unsupported_contract_version"]:
			fallback_reason = "schema_error"
		elif response_metadata.get("provider_metadata", {}).has("selection_error"):
			fallback_reason = "selection_error"
	var failed_provider := str(response_metadata.get("provider", provider))
	var failed_model := str(response_metadata.get("model", model))
	if failed_provider == "":
		failed_provider = "unknown"
	if failed_model == "":
		failed_model = "unknown"
	if reject_reason != "" and telemetry_sink != null:
		telemetry_sink.enqueue_event("generation.rejected", world.run_id, request_id, {
			"reject_reason": reject_reason, "provider": failed_provider, "model": failed_model,
		}, response_metadata.get("_telemetry_traceparent"))
	last_provider = "rules-baseline"
	last_model = "builtin-v1"
	last_latency_ms = elapsed_ms if elapsed_ms >= 0.0 else -1.0
	last_provider_metadata = {"fallback_reason": reason}
	var fallback_metadata := {
		"_telemetry_traceparent": response_metadata.get("_telemetry_traceparent"),
		"fallback_reason": reason,
		"telemetry_fallback_reason": fallback_reason,
		"failed_provider": failed_provider,
		"failed_model": failed_model,
		"provider": last_provider,
		"model": last_model,
		"latency_ms": last_latency_ms,
		"provider_metadata": last_provider_metadata.duplicate(true),
	}
	var req := request if not request.is_empty() else build_request(f, request_id)
	for variant in ["standard", "minimal"]:
		var plan := RulesBaseline.plan_for(req, _fallback_room_id(world), variant)
		var placed := _place_plan(f, request_id, plan, "fallback", fallback_metadata)
		if placed.ok:
			if recorder != null and not req.is_empty():
				var room_seed := int(placed.get("room", {}).get("seed_used", game_state.world_seed))
				recorder.record_entry(req, provider, model, "fallback", "fallback", room_seed, plan, reason, placed.get("room", {}).get("meta", {}), response_metadata)
			last_generation_status = "fallback"
			_record_decision(plan, "fallback", fallback_metadata)
			return
		if placed.outcome != "rejected":
			return
	world.seal_frontier(f.key, request_id, "no_placement_fits")
	if recorder != null and not req.is_empty():
		recorder.record_entry(req, provider, model, "sealed", "sealed", game_state.world_seed, {}, "no_placement_fits", {}, response_metadata)
	last_generation_status = "failed"
	game_state.log_message("The passage collapses; that exit is sealed.")


func _record_decision(plan: Dictionary, source: String, metadata: Dictionary) -> void:
	var entry := {
		"room_id": str(plan.get("room_id", "")),
		"depth": int(plan.get("depth", 1)),
		"room_type": str(plan.get("room_type", "room")),
		"size": str(plan.get("size", "medium")),
		"danger": int(plan.get("danger", 1)),
		"exits": plan.get("exits", []).duplicate(true),
		"source": source,
		"provider": str(metadata.get("provider", last_provider)),
		"model": str(metadata.get("model", last_model)),
		"latency_ms": float(metadata.get("latency_ms", last_latency_ms)),
		"provider_metadata": metadata.get("provider_metadata", {}).duplicate(true),
		"turn": game_state.player_turns if game_state else 0,
	}
	recent_decisions.append(entry)
	if recent_decisions.size() > MAX_RECENT_DECISIONS:
		recent_decisions.pop_front()


func _fallback_room_id(world: DungeonWorld) -> String:
	var n := world.rooms.size()
	while world.rooms.has("fb-%d" % n):
		n += 1
	return "fb-%d" % n


# --- utilities ---------------------------------------------------------------


func _now() -> int:
	return clock.call() if clock.is_valid() else Time.get_ticks_msec()


static func _distance(a: Vector2i, b: Vector2i) -> int:
	return absi(a.x - b.x) + absi(a.y - b.y)
