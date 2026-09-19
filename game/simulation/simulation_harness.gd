class_name SimulationHarness
extends RefCounted
## Headless dungeon simulation (Issue #16).
##
## Drives the real game generation stack - GameState, DungeonWorld,
## GenerationCoordinator, RoomGenerator, RulesBaseline - with no player and no
## rendering. Each iteration a simulated player walks (via a checked path) to
## an unresolved exit, the coordinator requests and places the room exactly as
## in play, and the harness observes the outcome from the world's own log.
##
## It detects: topology, reachability and overlap violations (SimulationAudit),
## placement conflicts (rejections the coordinator recovered from, and exits it
## had to seal), stalls (a step that never resolves, or no exit left), and
## request-budget exhaustion. It records rooms explored, danger progression,
## resource distribution, repetition, fallback frequency, provider/model
## configuration, seeds and failure details as a dataset (see SimulationDataset).
##
## Time is virtual for the offline transports (100 ms per tick, no sleeping) so
## runs are fast and byte-for-byte reproducible; remote runs use the real clock
## and yield to the engine between polls. `run()` is a coroutine: `await` it.

const DungeonContracts = preload("res://contracts/dungeon_contracts.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")
const GameState = preload("res://src/game_state.gd")
const GenerationClient = preload("res://world/generation_client.gd")
const GenerationCoordinator = preload("res://world/generation_coordinator.gd")
const OfflineTransport = preload("res://world/offline_transport.gd")
const RecordingTransport = preload("res://simulation/recording_transport.gd")
const RulesTransport = preload("res://simulation/rules_transport.gd")
const SimulationAudit = preload("res://simulation/simulation_audit.gd")
const SimulationConfig = preload("res://simulation/simulation_config.gd")
const SimulationDataset = preload("res://simulation/simulation_dataset.gd")
const SimulationMetrics = preload("res://simulation/simulation_metrics.gd")

const TICK_MSEC := 100
const REAL_TICK_SEC := 0.005
const STALL_GRACE_MSEC := 2000
const TRIGGER_RADIUS := 3
const MAX_IN_FLIGHT := 3
const FALLBACK_DETAIL_LIMIT := 200
const ATTACH_FRAME_LIMIT := 120
const SETTLE_FRAMES := 3
const WALL := 1  # GameState.TileType.WALL

var config: SimulationConfig
var tree: SceneTree
## Called once per tick; tests use it to pump an in-process HTTP server.
var tick_hook: Callable = Callable()
## State and start tile of the most recent run (tests and diagnostics).
var last_state: GameState = null
var start_pos := Vector2i.ZERO

var _client: Node = null
var _requests_used := 0


## Per-run mutable state.
class RunState:
	var run_index := 0
	var seed_value := 0
	var vclock := 0
	var tick_counter := 0
	var log_seq := 0
	var step_count := 0
	var status := "completed"
	var steps: Array = []
	var failures: Array = []
	var seen_failures := {}
	var visited := {}
	var tile_room := {}
	var indexed_rooms := 0
	var started_tick := {}
	var fallback := {}
	var rejections := {}
	var rng := RandomNumberGenerator.new()


func _init(settings: SimulationConfig, scene_tree: SceneTree = null) -> void:
	config = settings
	tree = scene_tree
	assert(not config.is_remote() or tree != null, "a remote run needs the scene tree to yield between polls")


## Execute every configured run and return the dataset
## {manifest, steps, rooms, failures, summary}.
func run() -> Dictionary:
	var dataset := {"manifest": _manifest(), "steps": [], "rooms": [], "failures": [], "summary": {}}
	var run_summaries: Array = []
	var records: Array = []
	var submitted := 0
	var refused := 0
	var truncated: Variant = null
	for run_index in range(config.runs):
		var result: Dictionary = await _run_one(run_index)
		dataset.steps.append_array(result.steps)
		dataset.rooms.append_array(result.rooms)
		dataset.failures.append_array(result.failures)
		run_summaries.append(result.summary)
		records.append_array(result.records)
		submitted += result.submitted
		refused += result.refused
		_requests_used += result.submitted
		if result.status == "budget_exhausted":
			truncated = "request_budget_exhausted"
			break
	_release_client()
	dataset.summary = _summary(dataset, run_summaries, records, submitted, refused, truncated)
	return dataset


# --- manifest ----------------------------------------------------------------


func _manifest() -> Dictionary:
	var run_entries: Array = []
	for i in range(config.runs):
		var seed_value := config.base_seed + i
		run_entries.append({"run": i, "seed": seed_value, "run_id": _run_id(seed_value)})
	return {
		"schema_version": SimulationDataset.SCHEMA_VERSION,
		"kind": "dungeon-simulation",
		"contract_version": DungeonContracts.CONTRACT_VERSION,
		"engine": str(Engine.get_version_info().get("string", "")),
		"config": {
			"runs": config.runs,
			"steps": config.steps,
			"base_seed": config.base_seed,
			"policy": config.policy,
			"faults": config.faults,
			"audit_every": config.audit_every,
			"trigger_radius": TRIGGER_RADIUS,
			"max_in_flight": MAX_IN_FLIGHT,
			"timeout_sec": config.timeout_sec,
			"stall_after_msec": config.stall_after_msec,
			"tick_msec": TICK_MSEC,
		},
		"provider": config.provider_summary(),
		"remote": {"opt_in": config.is_remote(), "request_budget": config.max_requests if config.is_remote() else null},
		"runs": run_entries,
		"files": SimulationDataset.FILE_NAMES.duplicate(),
	}


static func _run_id(seed_value: int) -> String:
	return "sim-%d" % seed_value


# --- one run -----------------------------------------------------------------


func _run_one(run_index: int) -> Dictionary:
	if config.is_remote():
		await _attach_client()
	var ctx := RunState.new()
	ctx.run_index = run_index
	ctx.seed_value = config.base_seed + run_index
	ctx.rng.seed = ctx.seed_value * 7919 + 13

	var state := GameState.new()
	state.enable_dynamic_world(ctx.seed_value)
	# A fixed run identity keeps request ids (and so the dataset) reproducible;
	# production playthroughs receive random run ids.
	state.world.run_id = _run_id(ctx.seed_value)
	var world: DungeonWorld = state.world
	last_state = state
	start_pos = state.player_pos
	ctx.visited[world.room_order[0]] = true
	_index_rooms(ctx, world)

	var clock: Callable
	var latency_clock := Callable()
	if config.is_remote():
		clock = func() -> int: return Time.get_ticks_msec()
		latency_clock = clock
	else:
		clock = func() -> int: return ctx.vclock
	var budget := maxi(config.max_requests - _requests_used, 0) if config.is_remote() else -1
	var recording := RecordingTransport.new(_make_transport(), latency_clock, budget)
	var coordinator := GenerationCoordinator.new(state, recording)
	coordinator.trigger_radius = TRIGGER_RADIUS
	coordinator.max_in_flight = MAX_IN_FLIGHT
	coordinator.timeout_msec = int(config.timeout_sec * 1000.0)
	coordinator.clock = clock
	if config.is_remote():
		coordinator.provider = config.provider
		coordinator.model = config.model
	var stall_after := config.stall_after_msec if config.stall_after_msec > 0 else coordinator.timeout_msec + STALL_GRACE_MSEC
	var max_iterations := config.steps * 60 + 500
	var iterations := 0
	var steps_since_audit := 0
	var idle_since := -1

	while ctx.step_count < config.steps:
		iterations += 1
		if iterations > max_iterations:
			_stall(ctx, world, "", "iteration_limit", 0)
			break
		if recording.budget_exhausted():
			if not await _drain(ctx, coordinator, world, clock, stall_after):
				break
			_consume_log(ctx, world, state, recording)
			ctx.status = "budget_exhausted"
			break

		var open := world.frontiers_with_status(DungeonWorld.STATUS_UNRESOLVED)
		if open.is_empty():
			if world.pending_count() > 0:
				# Nothing new to start; wait for the in-flight requests.
				idle_since = int(clock.call()) if idle_since < 0 else idle_since
				coordinator.update()
				_consume_log(ctx, world, state, recording)
				await _tick(ctx)
				_consume_log(ctx, world, state, recording)
				if int(clock.call()) - idle_since > stall_after and world.pending_count() > 0:
					_stall(ctx, world, "", "pending_never_resolved", int(clock.call()) - idle_since)
					break
				continue
			coordinator.update()  # boxed in: the coordinator breaches a wall
			_consume_log(ctx, world, state, recording)
			open = world.frontiers_with_status(DungeonWorld.STATUS_UNRESOLVED)
			if open.is_empty():
				_stall(ctx, world, "", "no_open_frontier", 0)
				break
		idle_since = -1

		var target := _choose(ctx, state, open)
		var approach: Vector2i = target.pos - DungeonWorld.DIRECTION_VECTORS[target.direction]
		var path: Variant = _path(world, state.player_pos, approach)
		if path == null:
			_fail(ctx, "reachability", "error", "no walkable path from the player at %s to exit %s" % [str(state.player_pos), target.key], {"player": [state.player_pos.x, state.player_pos.y], "approach": [approach.x, approach.y]}, target.key)
			ctx.status = "aborted"
			break
		for tile in path:
			var owner_room: String = ctx.tile_room.get(tile, "")
			if owner_room != "":
				ctx.visited[owner_room] = true
		state.player_turns += maxi(path.size() - 1, 0)
		state.player_pos = approach

		var step_start := int(clock.call())
		coordinator.update()
		_consume_log(ctx, world, state, recording)  # stamps request start ticks
		var waited := 0
		var target_key: String = target.key
		while _is_open(world.get_frontier(target_key)):
			await _tick(ctx)
			coordinator.update()
			_consume_log(ctx, world, state, recording)
			waited = int(clock.call()) - step_start
			if waited > stall_after and _is_open(world.get_frontier(target_key)):
				break
		_consume_log(ctx, world, state, recording)
		if _is_open(world.get_frontier(target_key)):
			_stall(ctx, world, target_key, "step_timeout", waited)
			break

		var resolved := world.get_frontier(target_key)
		if resolved.status == DungeonWorld.STATUS_COMMITTED:
			state.player_pos = resolved.outward  # walk into the new room
			var entered: String = ctx.tile_room.get(resolved.outward, "")
			if entered != "":
				ctx.visited[entered] = true
		steps_since_audit += 1
		if steps_since_audit >= config.audit_every:
			steps_since_audit = 0
			_audit(ctx, world, target_key)

	if steps_since_audit > 0 or ctx.step_count == 0:
		_audit(ctx, world, "")
	if ctx.status == "budget_exhausted":
		_fail(ctx, "budget", "warning", "request budget of %d exhausted; the simulation was truncated" % config.max_requests, {"budget": config.max_requests, "submitted": _requests_used + recording.submitted, "refused_locally": recording.refused}, "")
	var open_end := world.open_frontiers().size()
	var pending_end := world.pending_count()
	coordinator.shutdown()
	return _finish_run(ctx, state, recording, open_end, pending_end)


func _is_open(frontier: Dictionary) -> bool:
	return not frontier.is_empty() and (frontier.status == DungeonWorld.STATUS_UNRESOLVED or frontier.status == DungeonWorld.STATUS_PENDING)


func _make_transport() -> Variant:
	match config.transport:
		SimulationConfig.TRANSPORT_OFFLINE:
			return OfflineTransport.new()
		SimulationConfig.TRANSPORT_REMOTE:
			return _client
		_:
			var transport := RulesTransport.new()
			transport.faults = config.faults
			return transport


## Create the HTTP client node and wait until the scene tree has adopted it.
## The tree may still be setting up its root when a script's _initialize runs,
## so the node is added deferred and the frame loop is awaited.
func _attach_client() -> void:
	if _client != null:
		return
	var client := GenerationClient.new()
	client.base_url = config.endpoint
	client.timeout_sec = config.timeout_sec
	tree.root.add_child.call_deferred(client)
	_client = client
	var frames := 0
	while not client.is_inside_tree() and frames < ATTACH_FRAME_LIMIT:
		await tree.process_frame
		frames += 1
	assert(client.is_inside_tree(), "the HTTP client could not be attached to the scene tree")
	# Let startup settle: HTTPRequest timeouts count engine frame time, so a
	# long first frame (e.g. a slow script start) would otherwise time out the
	# very first request.
	for i in range(SETTLE_FRAMES):
		await tree.process_frame


func _release_client() -> void:
	if _client != null:
		_client.queue_free()
		_client = null


func _tick(ctx: RunState) -> void:
	ctx.tick_counter += 1
	if config.is_remote():
		await tree.create_timer(REAL_TICK_SEC).timeout
	else:
		ctx.vclock += TICK_MSEC
	if tick_hook.is_valid():
		tick_hook.call()


## Wait for every in-flight request to resolve. False on a stall.
func _drain(ctx: RunState, coordinator: GenerationCoordinator, world: DungeonWorld, clock: Callable, stall_after: int) -> bool:
	var started := int(clock.call())
	while world.pending_count() > 0:
		await _tick(ctx)
		coordinator.update()
		var waited := int(clock.call()) - started
		if waited > stall_after and world.pending_count() > 0:
			_stall(ctx, world, "", "pending_never_resolved", waited)
			return false
	return true


func _choose(ctx: RunState, state: GameState, open: Array[Dictionary]) -> Dictionary:
	if config.policy == "nearest":
		var best := open[0]
		var best_distance := _manhattan(best.pos, state.player_pos)
		for f in open:
			var d := _manhattan(f.pos, state.player_pos)
			if d < best_distance or (d == best_distance and f.key < best.key):
				best = f
				best_distance = d
		return best
	return open[ctx.rng.randi_range(0, open.size() - 1)]


static func _manhattan(a: Vector2i, b: Vector2i) -> int:
	return absi(a.x - b.x) + absi(a.y - b.y)


## Shortest walkable path (BFS) or null when `to` cannot be reached.
static func _path(world: DungeonWorld, from: Vector2i, to: Vector2i) -> Variant:
	if not world.tiles.has(from) or world.tiles[from] == WALL:
		return null
	if not world.tiles.has(to) or world.tiles[to] == WALL:
		return null
	var parent := {from: from}
	var queue: Array[Vector2i] = [from]
	var head := 0
	while head < queue.size():
		var cur: Vector2i = queue[head]
		head += 1
		if cur == to:
			var path: Array[Vector2i] = [cur]
			while cur != from:
				cur = parent[cur]
				path.append(cur)
			path.reverse()
			return path
		for d in DungeonWorld.NEIGHBORS:
			var n: Vector2i = cur + d
			if not parent.has(n) and world.tiles.has(n) and world.tiles[n] != WALL:
				parent[n] = cur
				queue.append(n)
	return null


func _index_rooms(ctx: RunState, world: DungeonWorld) -> void:
	while ctx.indexed_rooms < world.room_order.size():
		var room_id: String = world.room_order[ctx.indexed_rooms]
		ctx.indexed_rooms += 1
		for pos in world.rooms[room_id].tiles:
			if not ctx.tile_room.has(pos):
				ctx.tile_room[pos] = room_id


# --- observation -------------------------------------------------------------


## Turn the world's new log entries into step rows and failures.
func _consume_log(ctx: RunState, world: DungeonWorld, state: GameState, recording: RecordingTransport) -> void:
	var fresh: Array = []
	for entry in world.generation_log:
		if int(entry.seq) > ctx.log_seq:
			fresh.append(entry)
	if fresh.is_empty():
		return
	ctx.log_seq = int(fresh[fresh.size() - 1].seq)
	_index_rooms(ctx, world)
	for failure in events_to_failures(fresh, ctx.run_index, ctx.step_count + 1):
		ctx.failures.append(failure)
	for entry in fresh:
		var key: String = entry.frontier
		match entry.event:
			"requested":
				ctx.started_tick[key] = ctx.tick_counter
			"fallback":
				if not ctx.fallback.has(key):
					ctx.fallback[key] = str(entry.get("reason", "")).substr(0, FALLBACK_DETAIL_LIMIT)
			"rejected":
				var reasons: Array = ctx.rejections.get(key, [])
				reasons.append(str(entry.get("reason", "")))
				ctx.rejections[key] = reasons
			"committed", "sealed":
				ctx.step_count += 1
				ctx.steps.append(_step_row(ctx, world, state, recording, entry))
				_index_rooms(ctx, world)


func _step_row(ctx: RunState, world: DungeonWorld, state: GameState, recording: RecordingTransport, entry: Dictionary) -> Dictionary:
	var key: String = entry.frontier
	var request_id := str(entry.get("request_id", ""))
	var record := recording.record_for(request_id)
	var committed: bool = entry.event == "committed"
	var room: Dictionary = world.rooms.get(str(entry.get("room_id", "")), {}) if committed else {}
	var meta: Dictionary = room.get("meta", {})
	var detail: Variant = ctx.fallback.get(key)
	var row := {
		"run": ctx.run_index,
		"step": ctx.step_count,
		"frontier": key,
		"request_id": request_id,
		"outcome": "committed" if committed else "sealed",
		"source": str(entry.get("source", "director")) if committed else "none",
		"fallback": detail != null,
		"fallback_reason": _reason_category(str(detail)) if detail != null else null,
		"fallback_detail": detail,
		"rejections": ctx.rejections.get(key, []),
		"room_id": str(entry.room_id) if committed else null,
		"room_type": meta.get("room_type") if committed else null,
		"size": meta.get("size_used") if committed else null,
		"danger": int(meta.get("danger", 1)) if committed else null,
		"repositioned": bool(meta.get("repositioned", false)),
		"pruned_exits": meta.get("pruned_exits", []),
		"wait_ticks": ctx.tick_counter - int(ctx.started_tick.get(key, ctx.tick_counter)),
		"latency_ms": record.get("latency_ms"),
		"http_status": record.get("http_status"),
		"error_kind": record.get("error_kind") if str(record.get("error_kind", "")) != "" else null,
		"reported_provider": record.get("provider"),
		"reported_model": record.get("model"),
		"cost_usd": record.get("cost_usd"),
		"player_turn": state.player_turns,
		"rooms_total": world.rooms.size(),
	}
	ctx.fallback.erase(key)
	ctx.rejections.erase(key)
	ctx.started_tick.erase(key)
	return row


static func _reason_category(detail: String) -> String:
	return detail.get_slice(":", 0).strip_edges()


## Placement conflicts as failures: a rejected placement that the coordinator
## then recovered from is informational; an exit that had to be sealed because
## nothing fit is a warning. Everything else the world logs is not a failure.
static func events_to_failures(entries: Array, run_index: int, step: int) -> Array:
	var failures: Array = []
	for entry in entries:
		var key := str(entry.get("frontier", ""))
		match str(entry.get("event", "")):
			"rejected":
				failures.append({
					"kind": "placement_conflict",
					"severity": "info",
					"run": run_index,
					"step": step,
					"frontier": key,
					"message": "placement rejected (%s); recovered by re-planning or the local fallback" % str(entry.get("reason", "")),
					"detail": {"reason": str(entry.get("reason", "")), "request_id": str(entry.get("request_id", ""))},
				})
			"sealed":
				failures.append({
					"kind": "placement_conflict",
					"severity": "warning",
					"run": run_index,
					"step": step,
					"frontier": key,
					"message": "no plan fit at %s; the exit was sealed (%s)" % [key, str(entry.get("reason", ""))],
					"detail": {"reason": str(entry.get("reason", "")), "request_id": str(entry.get("request_id", ""))},
				})
	return failures


func _fail(ctx: RunState, kind: String, severity: String, message: String, detail: Dictionary, frontier: String) -> void:
	ctx.failures.append({
		"kind": kind,
		"severity": severity,
		"run": ctx.run_index,
		"step": ctx.step_count,
		"frontier": frontier if frontier != "" else null,
		"message": message,
		"detail": detail,
	})


func _stall(ctx: RunState, world: DungeonWorld, frontier: String, reason: String, waited: int) -> void:
	ctx.status = "stalled"
	_fail(ctx, "stall", "error", "generation stalled (%s)%s" % [reason, "" if frontier == "" else " at " + frontier], {"reason": reason, "waited_msec": waited, "pending": world.pending_count(), "open_frontiers": world.open_frontiers().size()}, frontier)


func _audit(ctx: RunState, world: DungeonWorld, frontier: String) -> void:
	for failure in SimulationAudit.audit(world, start_pos):
		var signature := "%s|%s" % [failure.kind, failure.message]
		if ctx.seen_failures.has(signature):
			continue
		ctx.seen_failures[signature] = true
		_fail(ctx, failure.kind, failure.severity, failure.message, failure.detail, frontier)


# --- run results -------------------------------------------------------------


func _finish_run(ctx: RunState, state: GameState, recording: RecordingTransport, open_end: int, pending_end: int) -> Dictionary:
	var world: DungeonWorld = state.world
	var rooms := _room_rows(ctx, world)
	var fallback_rows: Array = ctx.steps.filter(func(r: Dictionary) -> bool: return r.fallback)
	var reasons := {}
	for row in fallback_rows:
		reasons[row.fallback_reason] = int(reasons.get(row.fallback_reason, 0)) + 1
	var sources := {"start": 0, "director": 0, "fallback": 0}
	for room in rooms:
		sources[room.source] = int(sources.get(room.source, 0)) + 1
	var by_severity := _failure_counts(ctx.failures)
	var summary := {
		"run": ctx.run_index,
		"seed": ctx.seed_value,
		"run_id": _run_id(ctx.seed_value),
		"status": ctx.status,
		"passed": int(by_severity.error) == 0,
		"steps": ctx.steps.size(),
		"rooms_committed": rooms.size(),
		"rooms_explored": ctx.visited.size(),
		"sealed_exits": int(world.counters.sealed),
		"breaches": int(world.counters.breaches),
		"open_frontiers_end": open_end,
		"pending_end": pending_end,
		"sources": sources,
		"fallback_frequency": SimulationMetrics.ratio(fallback_rows.size(), ctx.steps.size()),
		"fallback_reasons": reasons,
		"danger": SimulationMetrics.danger_stats(rooms.map(func(r: Dictionary) -> int: return r.danger)),
		"resources": SimulationMetrics.resource_stats(rooms),
		"repetition": SimulationMetrics.repetition_stats(rooms),
		"failures": by_severity,
		"counters": world.counters.duplicate(),
	}
	return {
		"summary": summary,
		"steps": ctx.steps,
		"rooms": rooms,
		"failures": ctx.failures,
		"records": recording.records.values(),
		"submitted": recording.submitted,
		"refused": recording.refused,
		"status": ctx.status,
	}


static func _failure_counts(failures: Array) -> Dictionary:
	var counts := {"error": 0, "warning": 0, "info": 0, "by_kind": {}}
	for failure in failures:
		counts[failure.severity] = int(counts[failure.severity]) + 1
		counts.by_kind[failure.kind] = int(counts.by_kind.get(failure.kind, 0)) + 1
	return counts


func _room_rows(ctx: RunState, world: DungeonWorld) -> Array:
	var rows: Array = []
	var depth := {}
	for index in range(world.room_order.size()):
		var room_id: String = world.room_order[index]
		var rec: Dictionary = world.rooms[room_id]
		var meta: Dictionary = rec.meta
		var parent_key: String = rec.parent_frontier
		var parent_room: String = str(world.get_frontier(parent_key).get("room_id", "")) if parent_key != "" else ""
		depth[room_id] = int(depth.get(parent_room, -1)) + 1
		var exit_directions: Array = rec.exits.map(func(e: Dictionary) -> String: return e.direction)
		exit_directions.sort()
		var floors := 0
		for pos in rec.tiles:
			if rec.tiles[pos] != WALL:
				floors += 1
		var enemies := _count_types(rec.enemies)
		var items := _count_types(rec.items)
		var origin: Vector2i = rec.origin
		var size: Vector2i = rec.bounds.size
		var room_type: String = str(meta.get("room_type", "room"))
		var danger := clampi(int(meta.get("danger", 1)), 1, 5)
		var fallback_detail: Variant = meta.get("fallback_reason")
		rows.append({
			"run": ctx.run_index,
			"index": index,
			"room_id": room_id,
			"source": rec.source,
			"room_type": room_type,
			"size": meta.get("size_used"),
			"width": size.x,
			"height": size.y,
			"danger": danger,
			"parent_frontier": parent_key,
			"distance_from_start": depth[room_id],
			"origin": [origin.x, origin.y],
			"floor_tiles": floors,
			"exit_count": exit_directions.size(),
			"exits": exit_directions,
			"enemy_count": rec.enemies.size(),
			"item_count": rec.items.size(),
			"enemies": enemies,
			"items": items,
			"explored": ctx.visited.has(room_id),
			"repositioned": bool(meta.get("repositioned", false)),
			"pruned_exits": meta.get("pruned_exits", []),
			"fallback_reason": _reason_category(str(fallback_detail)) if fallback_detail != null else null,
			"signature": "%s|%dx%d|%s|d%d" % [room_type, size.x, size.y, ",".join(exit_directions), danger],
		})
	return rows


static func _count_types(entities: Array) -> Dictionary:
	var counts := {}
	for entity in entities:
		counts[entity.type] = int(counts.get(entity.type, 0)) + 1
	return counts


# --- overall summary ---------------------------------------------------------


func _summary(dataset: Dictionary, run_summaries: Array, records: Array, submitted: int, refused: int, truncated: Variant) -> Dictionary:
	var steps: Array = dataset.steps
	var rooms: Array = dataset.rooms
	var fallback_rows: Array = steps.filter(func(r: Dictionary) -> bool: return r.fallback)
	var reasons := {}
	for row in fallback_rows:
		reasons[row.fallback_reason] = int(reasons.get(row.fallback_reason, 0)) + 1
	var sources := {"start": 0, "director": 0, "fallback": 0}
	for room in rooms:
		sources[room.source] = int(sources.get(room.source, 0)) + 1
	var explored := 0
	var sealed := 0
	var slopes: Array = []
	var repeat_rates: Array = []
	var longest_streak := 0
	var passed := true
	for run_summary in run_summaries:
		explored += int(run_summary.rooms_explored)
		sealed += int(run_summary.sealed_exits)
		slopes.append(run_summary.danger.slope)
		repeat_rates.append(run_summary.repetition.signature_repeat_rate)
		longest_streak = maxi(longest_streak, int(run_summary.repetition.max_consecutive_same_type))
		passed = passed and run_summary.passed
	var danger := SimulationMetrics.danger_stats(rooms.map(func(r: Dictionary) -> int: return r.danger))
	var pooled_danger := {"min": danger.min, "max": danger.max, "mean": danger.mean, "histogram": danger.histogram, "mean_slope": _mean_of(slopes)}
	var resources := SimulationMetrics.resource_stats(rooms)
	var repetition := {"mean_signature_repeat_rate": _mean_of(repeat_rates), "max_consecutive_same_type": longest_streak}
	var latencies: Array = []
	var providers := {}
	var models := {}
	var statuses := {}
	var error_kinds := {}
	var cost := 0.0
	var priced := false
	for record in records:
		if record.latency_ms != null:
			latencies.append(record.latency_ms)
		if record.provider != null:
			providers[record.provider] = int(providers.get(record.provider, 0)) + 1
		if record.model != null:
			models[record.model] = int(models.get(record.model, 0)) + 1
		if int(record.http_status) != 0:
			statuses[str(record.http_status)] = int(statuses.get(str(record.http_status), 0)) + 1
		if str(record.error_kind) != "":
			error_kinds[record.error_kind] = int(error_kinds.get(record.error_kind, 0)) + 1
		if record.cost_usd != null:
			cost += float(record.cost_usd)
			priced = true
	return {
		"overall": {
			"passed": passed,
			"truncated": truncated,
			"runs": run_summaries.size(),
			"steps": steps.size(),
			"rooms_committed": rooms.size(),
			"rooms_explored": explored,
			"sealed_exits": sealed,
			"sources": sources,
			"fallback_frequency": SimulationMetrics.ratio(fallback_rows.size(), steps.size()),
			"fallback_reasons": reasons,
			"danger": pooled_danger,
			"resources": resources,
			"repetition": repetition,
			"failures": _failure_counts(dataset.failures),
		},
		"runs": run_summaries,
		"telemetry": {
			"requests_submitted": submitted,
			"requests_refused": refused,
			"latency_ms": SimulationMetrics.latency_stats(latencies),
			"estimated_cost_usd": SimulationMetrics.r(cost) if priced else null,
			"reported_providers": providers,
			"reported_models": models,
			"http_statuses": statuses,
			"error_kinds": error_kinds,
		},
	}


static func _mean_of(values: Array) -> float:
	if values.is_empty():
		return 0.0
	var total := 0.0
	for v in values:
		total += float(v)
	return SimulationMetrics.r(total / values.size())
