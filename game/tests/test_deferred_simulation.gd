extends SceneTree
## Issue #7: deterministic expansion simulation (>= 100 rooms).
##
## Drives the real GenerationCoordinator + DungeonWorld with a scripted
## transport standing in for the director. A deterministic mix of good plans
## and injected failures (timeouts, invalid JSON, failure envelopes, duplicate
## and late deliveries, mismatched ids, missing backlinks) exercises commit,
## repositioning, fallback and sealing. Afterwards the invariants are audited
## independently of the world's own integrity checker:
##   * no overlaps / contradictory occupancy between committed rooms
##   * every traversable tile connected to the start
##   * committed frontiers unique, each linking exactly one room
##   * no committed frontier ever regenerated (one request per frontier)
##   * committed tiles never change after commit (except a sealed exit -> wall)
##   * the same seed reproduces the identical dungeon

const GameState = preload("res://src/game_state.gd")
const DungeonWorld = preload("res://world/dungeon_world.gd")
const GenerationCoordinator = preload("res://world/generation_coordinator.gd")
const ScriptedTransport = preload("res://tests/support/scripted_transport.gd")
const StubDirector = preload("res://tests/support/stub_director.gd")
const TileType = GameState.TileType

const TARGET_ROOMS := 105
const MAX_ITERATIONS := 1500

var _checks := 0
var _failures := PackedStringArray()
var _completed := false
var _reached_end := false


func _initialize() -> void:
	OS.set_environment("DUNGEON_DIRECTOR_URL", "offline")
	create_timer(120.0).timeout.connect(_on_watchdog)
	print("=== Running test_deferred_simulation.gd ===")
	_step("test_simulation_invariants_seed_1", _test_simulation_invariants_seed_1)
	_step("test_simulation_is_deterministic", _test_simulation_is_deterministic)
	_step("test_simulation_invariants_other_seeds", _test_simulation_invariants_other_seeds)
	_completed = true
	_finish()


func _on_watchdog() -> void:
	printerr("FAILED: simulation exceeded its 120s watchdog")
	quit(2)


func _step(test_name: String, fn: Callable) -> void:
	_reached_end = false
	fn.call()
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
	print("\n--- Simulation Results: %d Passed, %d Failed (completed=%s) ---" % [_checks - _failures.size(), _failures.size(), str(_completed)])
	if _failures.is_empty() and _completed:
		print("SUCCESS: All deferred simulation checks passed!")
		quit(0)
	else:
		printerr("FAILED: deferred simulation checks did not pass.")
		quit(1)


# --- simulation --------------------------------------------------------------


## Failure injected for the i-th request (0-based), by a fixed deterministic rule.
static func _kind_for(i: int) -> String:
	var n := i + 1
	if n % 7 == 3:
		return "hang"
	if n % 11 == 5:
		return "invalid_json"
	if n % 13 == 6:
		return "failure_envelope"
	if n % 17 == 8:
		return "duplicate"
	if n % 19 == 9:
		return "id_mismatch"
	if n % 23 == 10:
		return "missing_backlink"
	return "ok"


func _simulate(world_seed: int) -> Dictionary:
	var state := GameState.new()
	state.enable_dynamic_world(world_seed)
	# A fixed run identity keeps the seeded simulation reproducible while
	# production playthroughs receive random run ids.
	state.world.run_id = "simulation-%d" % world_seed
	var transport := ScriptedTransport.new()
	var coord := GenerationCoordinator.new(state, transport)
	coord.trigger_radius = 3
	coord.max_in_flight = 3
	coord.timeout_msec = 1000
	var clock := [0]
	coord.clock = func() -> int: return clock[0]
	var rng := RandomNumberGenerator.new()
	rng.seed = world_seed * 7919
	var world: DungeonWorld = state.world

	var delivered := 0
	var late: Array = []  # [index, result] responses that arrive after their timeout
	var kinds := {}
	var commit_snapshots: Array[Dictionary] = []  # periodic full-world snapshots
	var iteration := 0
	var stuck := false
	while world.rooms.size() < TARGET_ROOMS and iteration < MAX_ITERATIONS:
		iteration += 1
		var open := world.frontiers_with_status(DungeonWorld.STATUS_UNRESOLVED)
		if open.is_empty() and world.pending_count() == 0:
			coord.update()  # boxed in: the coordinator breaches a wall
			open = world.frontiers_with_status(DungeonWorld.STATUS_UNRESOLVED)
			if open.is_empty():
				stuck = true
				break
		if not open.is_empty():
			var target: Dictionary = open[rng.randi_range(0, open.size() - 1)]
			state.player_pos = target.pos - DungeonWorld.DIRECTION_VECTORS[target.direction]
		coord.update()
		clock[0] += 100
		var hung := false
		while delivered < transport.submitted.size():
			var index := delivered
			delivered += 1
			var request: Dictionary = transport.submitted[index].request
			var kind := _kind_for(index)
			kinds[kind] = int(kinds.get(kind, 0)) + 1
			var plan := StubDirector.varied_plan(request, rng, "sd-%d" % (index + 1))
			match kind:
				"ok":
					transport.deliver(index, StubDirector.success_result(request, plan))
				"duplicate":
					var result := StubDirector.success_result(request, plan)
					transport.deliver(index, result)
					transport.deliver(index, result)
				"invalid_json":
					transport.deliver(index, StubDirector.ok_result("{not json"))
				"failure_envelope":
					transport.deliver(index, StubDirector.ok_result(StubDirector.failure_body(request), 503))
				"id_mismatch":
					var other: Dictionary = request.duplicate()
					other.request_id = "req-elsewhere"
					transport.deliver(index, StubDirector.success_result(other, plan))
				"missing_backlink":
					plan.exits = [StubDirector.exit_def("east"), StubDirector.exit_def("west")]
					transport.deliver(index, StubDirector.success_result(request, plan))
				"hang":
					hung = true
					late.append([index, StubDirector.success_result(request, plan)])
		if hung:
			clock[0] += coord.timeout_msec + 1
			coord.update()
		elif not late.is_empty() and iteration % 3 == 0:
			var item: Array = late.pop_front()
			transport.deliver(item[0], item[1])  # a response after its timeout fired
		if iteration % 10 == 0:
			commit_snapshots.append(world.tiles.duplicate())
	return {
		"state": state,
		"transport": transport,
		"world": world,
		"iterations": iteration,
		"stuck": stuck,
		"kinds": kinds,
		"snapshots": commit_snapshots,
	}


func _fingerprint(world: DungeonWorld) -> int:
	var keys := world.tiles.keys()
	keys.sort_custom(func(a: Vector2i, b: Vector2i) -> bool: return a.x < b.x if a.x != b.x else a.y < b.y)
	var acc := 17
	for pos in keys:
		acc = (acc * 31 + pos.x * 73856093 + pos.y * 19349663 + int(world.tiles[pos])) % 2147483647
	return acc


func _audit(sim: Dictionary, label: String) -> void:
	var world: DungeonWorld = sim.world
	var state: GameState = sim.state
	var transport: ScriptedTransport = sim.transport
	_check(not sim.stuck, "%s: the dungeon never ran out of exits" % label)
	_check(world.rooms.size() >= 100, "%s: expanded to %d committed rooms (>= 100)" % [label, world.rooms.size()])
	_check(world.integrity_violations().is_empty(), "%s: world integrity check clean: %s" % [label, str(world.integrity_violations().slice(0, 3))])

	# 1. No overlaps / contradictory occupancy, audited from the per-room records.
	var owner := {}
	var contradictions := 0
	var floor_overlaps := 0
	for room_id in world.room_order:
		var rec: Dictionary = world.rooms[room_id]
		for pos in rec.tiles:
			if owner.has(pos):
				var previous: int = owner[pos]
				var current: int = rec.tiles[pos]
				if previous != TileType.WALL or current != TileType.WALL:
					floor_overlaps += 1
				if previous != current:
					contradictions += 1
			else:
				owner[pos] = rec.tiles[pos]
	_check_eq(floor_overlaps, 0, "%s: rooms overlap only wall-on-wall" % label)
	_check_eq(contradictions, 0, "%s: no tile is claimed with two different types" % label)
	_check_eq(owner.size(), world.tiles.size() - _link_only_tiles(world), "%s: every world tile belongs to a committed room" % label)

	# 2. Every traversable tile connected to the start.
	var traversable := 0
	for pos in state.map_tiles:
		if state.map_tiles[pos] != TileType.WALL:
			traversable += 1
	var seen := {state.player_pos: true}
	var start := Vector2i(4, 4)
	seen = {start: true}
	var queue: Array[Vector2i] = [start]
	while not queue.is_empty():
		var cur: Vector2i = queue.pop_back()
		for d in [Vector2i.UP, Vector2i.DOWN, Vector2i.LEFT, Vector2i.RIGHT]:
			var n: Vector2i = cur + d
			if not seen.has(n) and state.map_tiles.has(n) and state.map_tiles[n] != TileType.WALL:
				seen[n] = true
				queue.append(n)
	_check_eq(seen.size(), traversable, "%s: all %d traversable tiles are connected to the start" % [label, traversable])

	# 3. Unique committed frontiers: one room per committed frontier, one parent per room.
	var parents := {}
	var parent_dupes := 0
	var id_dupes := 0
	var seen_ids := {}
	for room_id in world.room_order:
		if seen_ids.has(room_id):
			id_dupes += 1
		seen_ids[room_id] = true
		var parent: String = world.rooms[room_id].parent_frontier
		if parent != "":
			if parents.has(parent):
				parent_dupes += 1
			parents[parent] = room_id
	_check_eq(id_dupes, 0, "%s: room ids are unique" % label)
	_check_eq(parent_dupes, 0, "%s: no frontier was committed twice" % label)
	var bad_links := 0
	for key in parents:
		var f: Dictionary = world.get_frontier(key)
		if f.status != DungeonWorld.STATUS_COMMITTED or f.resolved_room_id != parents[key]:
			bad_links += 1
	_check_eq(bad_links, 0, "%s: every committed frontier links to the room that was placed for it" % label)

	# 4. No regeneration: every frontier was made pending exactly once, and
	#    every transport request maps to exactly one frontier.
	var request_owner := {}
	var repeated := 0
	var committed_requests := 0
	for key in world.frontiers:
		var f: Dictionary = world.frontiers[key]
		if f.request_id != "":
			if request_owner.has(f.request_id):
				repeated += 1
			request_owner[f.request_id] = key
			if f.status == DungeonWorld.STATUS_COMMITTED:
				committed_requests += 1
	_check_eq(repeated, 0, "%s: no request id is shared between frontiers" % label)
	_check_eq(request_owner.size(), transport.submitted.size(), "%s: every one of the %d transport requests belongs to exactly one frontier (none regenerated)" % [label, transport.submitted.size()])
	var request_ids := {}
	for id in transport.request_ids():
		request_ids[id] = true
	_check_eq(request_ids.size(), transport.submitted.size(), "%s: request ids are unique" % label)
	_check(committed_requests > 0, "%s: requests led to commits" % label)

	# 5. Committed tiles never change after commit (a sealed exit becomes wall).
	var sealed_positions := {}
	for f in world.frontiers_with_status(DungeonWorld.STATUS_SEALED):
		sealed_positions[f.pos] = true
	for key in world.frontiers:
		if "#b" in key:
			sealed_positions[world.frontiers[key].pos] = true  # breach: wall -> door
	var mutated := 0
	for snapshot in sim.snapshots:
		for pos in snapshot:
			if world.tiles[pos] != snapshot[pos] and not sealed_positions.has(pos):
				mutated += 1
	_check_eq(mutated, 0, "%s: no committed tile changed after it was committed" % label)

	# 6. The failure paths were really exercised.
	_check(world.counters.fallbacks > 0 and world.counters.committed_fallback > 0, "%s: fallback rooms were committed (%d fallbacks)" % [label, world.counters.fallbacks])
	_check(world.counters.committed_director > 0, "%s: director rooms were committed (%d)" % [label, world.counters.committed_director])
	_check(world.counters.duplicates > 0, "%s: duplicate/late completions were refused (%d)" % [label, world.counters.duplicates])
	print("  [INFO] %s: iterations=%d rooms=%d tiles=%d kinds=%s counters=%s" % [label, sim.iterations, world.rooms.size(), world.tiles.size(), str(sim.kinds), str(world.counters)])


## Shared link tiles are owned by the parent room only; a link tile is never
## recorded by the child, so every world tile still has exactly one owner.
func _link_only_tiles(_world: DungeonWorld) -> int:
	return 0


func _test_simulation_invariants_seed_1() -> void:
	print("\nTest: 100+ room expansion keeps every invariant (seed 1)")
	_audit(_simulate(1), "seed 1")
	_end()


func _test_simulation_is_deterministic() -> void:
	print("\nTest: the same seed reproduces the identical dungeon")
	var a := _simulate(1)
	var b := _simulate(1)
	_check_eq(_fingerprint(a.world), _fingerprint(b.world), "identical tile fingerprint")
	_check_eq(a.world.room_order, b.world.room_order, "identical room commit order")
	_check_eq(a.world.tiles.size(), b.world.tiles.size(), "identical world size")
	_end()


func _test_simulation_invariants_other_seeds() -> void:
	print("\nTest: invariants hold for other seeds")
	for world_seed in [2, 3]:
		_audit(_simulate(world_seed), "seed %d" % world_seed)
	_end()
