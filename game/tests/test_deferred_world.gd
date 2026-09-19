extends SceneTree
## Issue #7: durable dungeon world state (frontiers, commits, placement).
## Pure state tests: no scene, no transport, no network.

const DungeonWorld := preload("res://world/dungeon_world.gd")
const GeneratedRoom := preload("res://generation/generated_room.gd")
const RoomGenerator := preload("res://generation/room_generator.gd")
const TileType = GeneratedRoom.TileType

var _checks := 0
var _failures := PackedStringArray()
var _completed := false
var _reached_end := false


func _initialize() -> void:
	print("=== Running test_deferred_world.gd ===")
	_step("test_start_world_is_small_with_unresolved_frontiers", _test_start_world_is_small_with_unresolved_frontiers)
	_step("test_frontier_pending_once", _test_frontier_pending_once)
	_step("test_commit_aligns_backlink_and_records_state", _test_commit_aligns_backlink_and_records_state)
	_step("test_duplicate_completion_is_idempotent", _test_duplicate_completion_is_idempotent)
	_step("test_stale_and_unknown_completions_never_mutate", _test_stale_and_unknown_completions_never_mutate)
	_step("test_overlap_is_rejected_without_mutation", _test_overlap_is_rejected_without_mutation)
	_step("test_blocked_exit_is_reported_and_prunable", _test_blocked_exit_is_reported_and_prunable)
	_step("test_missing_backlink_and_duplicate_room_id_rejected", _test_missing_backlink_and_duplicate_room_id_rejected)
	_step("test_commit_keeps_all_tiles_connected", _test_commit_keeps_all_tiles_connected)
	_step("test_seal_terminal_and_frontier_unique", _test_seal_terminal_and_frontier_unique)
	_step("test_disconnected_room_rejected", _test_disconnected_room_rejected)
	_step("test_sealed_exit_is_recorded_as_wall_in_its_room", _test_sealed_exit_is_recorded_as_wall_in_its_room)
	_step("test_breach_reopens_a_boxed_in_dungeon", _test_breach_reopens_a_boxed_in_dungeon)
	_completed = true
	_finish()


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
	print("\n--- World Test Results: %d Passed, %d Failed (completed=%s) ---" % [_checks - _failures.size(), _failures.size(), str(_completed)])
	if _failures.is_empty() and _completed:
		print("SUCCESS: All deferred world checks passed!")
		quit(0)
	else:
		quit(1)


# --- helpers -----------------------------------------------------------------


static func plan(room_id: String, size: String, exits: Array, room_type: String = "room") -> Dictionary:
	var exit_defs: Array = []
	for direction in exits:
		exit_defs.append({"direction": direction, "kind": "door", "locked": false})
	return {"room_id": room_id, "depth": 1, "room_type": room_type, "size": size, "exits": exit_defs}


static func room_from(p: Dictionary, seed_value: int = 7) -> GeneratedRoom:
	return RoomGenerator.generate(p, seed_value)


static func start_world(exits: Array = ["north", "east", "south"]) -> DungeonWorld:
	var world := DungeonWorld.new()
	world.run_id = "run-test"
	var res := world.commit_start_room(room_from(plan("r-000", "small", exits, "entrance")), Vector2i.ZERO, 0)
	assert(res.ok)
	return world


func _snapshot(world: DungeonWorld) -> Dictionary:
	return {"tiles": world.tiles.duplicate(), "rooms": world.rooms.keys(), "revision": world.revision}


func _reachable_component_size(world: DungeonWorld) -> int:
	var start := Vector2i.ZERO
	for pos in world.tiles.keys():
		if world.tiles[pos] != TileType.WALL:
			start = pos
			break
	var seen := {start: true}
	var queue: Array[Vector2i] = [start]
	while not queue.is_empty():
		var cur: Vector2i = queue.pop_front()
		for d in [Vector2i.UP, Vector2i.DOWN, Vector2i.LEFT, Vector2i.RIGHT]:
			var n: Vector2i = cur + d
			if world.tiles.has(n) and world.tiles[n] != TileType.WALL and not seen.has(n):
				seen[n] = true
				queue.append(n)
	return seen.size()


func _traversable_count(world: DungeonWorld) -> int:
	var n := 0
	for pos in world.tiles.keys():
		if world.tiles[pos] != TileType.WALL:
			n += 1
	return n


# --- tests -------------------------------------------------------------------


func _test_start_world_is_small_with_unresolved_frontiers() -> void:
	print("\nTest: start world is small and exposes unresolved frontiers")
	var world := start_world()
	_check_eq(world.rooms.size(), 1, "only the start room is committed")
	var rec: Dictionary = world.rooms["r-000"]
	_check_eq(rec.bounds, Rect2i(Vector2i.ZERO, Vector2i(9, 9)), "room bounds recorded")
	_check_eq(world.open_frontiers().size(), 3, "three exits are open frontiers")
	for f in world.open_frontiers():
		_check_eq(f.status, DungeonWorld.STATUS_UNRESOLVED, "%s starts unresolved" % f.key)
		_check(not world.tiles.has(f.outward), "%s leads into unknown space (no committed tile beyond)" % f.key)
	_check_eq(world.pending_count(), 0, "nothing pending at start")
	_end()


func _test_frontier_pending_once() -> void:
	print("\nTest: a frontier can be pending only once")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "north")
	_check(world.begin_generation(key, "req-1"), "first begin_generation succeeds")
	_check(not world.begin_generation(key, "req-2"), "second begin_generation is refused")
	_check_eq(world.get_frontier(key).status, DungeonWorld.STATUS_PENDING, "status is pending")
	_check_eq(world.get_frontier(key).request_id, "req-1", "original request id retained")
	_check(not world.begin_generation("nope:north", "req-3"), "unknown frontier cannot go pending")
	_end()


func _test_commit_aligns_backlink_and_records_state() -> void:
	print("\nTest: commit aligns the backlink door with the target frontier")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "north")
	world.begin_generation(key, "req-1")
	var room := room_from(plan("r-001", "small", ["south", "east", "north"]))
	var res := world.commit_generated(key, "req-1", room, "director", 3, {})
	_check(res.ok, "commit accepted")
	_check_eq(res.outcome, "committed", "outcome committed")
	var frontier := world.get_frontier(key)
	_check_eq(frontier.status, DungeonWorld.STATUS_COMMITTED, "frontier committed")
	_check_eq(frontier.resolved_room_id, "r-001", "frontier links the new room")
	var rec: Dictionary = world.rooms["r-001"]
	# North door of the start room sits at (4,0); new room is 9x9 with its south door at local (4,8).
	_check_eq(rec.origin, Vector2i(0, -8), "new room placed relative to the exit")
	_check_eq(rec.link_pos, frontier.pos, "backlink shares the frontier door tile")
	_check_eq(rec.bounds, Rect2i(Vector2i(0, -8), Vector2i(9, 9)), "room bounds recorded")
	_check_eq(rec.source, "director", "source recorded")
	_check_eq(rec.parent_frontier, key, "parent frontier recorded")
	_check(world.tiles.has(Vector2i(4, -1)) and world.tiles[Vector2i(4, -1)] == TileType.FLOOR, "lead-in tile beyond the door is floor")
	# new exits of r-001 (east, north) become unresolved frontiers; south backlink is committed
	_check_eq(world.get_frontier(DungeonWorld.frontier_key("r-001", "east")).status, DungeonWorld.STATUS_UNRESOLVED, "new east exit unresolved")
	_check_eq(world.get_frontier(DungeonWorld.frontier_key("r-001", "north")).status, DungeonWorld.STATUS_UNRESOLVED, "new north exit unresolved")
	_check_eq(world.get_frontier(DungeonWorld.frontier_key("r-001", "south")).status, DungeonWorld.STATUS_COMMITTED, "backlink frontier committed")
	_check_eq(world.get_frontier(DungeonWorld.frontier_key("r-001", "south")).resolved_room_id, "r-000", "backlink points to the parent room")
	_check_eq(world.counters.committed_director, 1, "director commit counted")
	_end()


func _test_duplicate_completion_is_idempotent() -> void:
	print("\nTest: duplicate completion never replaces committed data")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "north")
	world.begin_generation(key, "req-1")
	world.commit_generated(key, "req-1", room_from(plan("r-001", "small", ["south", "east"])), "director", 1, {})
	var before := _snapshot(world)
	var other := room_from(plan("r-002", "large", ["south", "west"]))
	var res := world.commit_generated(key, "req-1", other, "director", 2, {})
	_check(not res.ok, "second completion refused")
	_check_eq(res.outcome, "duplicate", "reported as duplicate")
	res = world.commit_generated(key, "req-9", other, "fallback", 2, {})
	_check_eq(res.outcome, "duplicate", "different request id on a committed frontier is still a duplicate")
	_check_eq(world.tiles, before.tiles, "tiles untouched")
	_check_eq(world.rooms.keys(), before.rooms, "rooms untouched")
	_check_eq(world.revision, before.revision, "revision untouched")
	_check_eq(world.counters.duplicates, 2, "duplicates counted in diagnostics")
	_end()


func _test_stale_and_unknown_completions_never_mutate() -> void:
	print("\nTest: stale and unknown completions never mutate state")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "north")
	var room := room_from(plan("r-001", "small", ["south", "east"]))
	var before := _snapshot(world)
	var res := world.commit_generated(key, "req-1", room, "director", 1, {})
	_check_eq(res.outcome, "stale", "commit on a frontier that is not pending is stale")
	world.begin_generation(key, "req-1")
	res = world.commit_generated(key, "req-OLD", room, "director", 1, {})
	_check_eq(res.outcome, "stale", "request id mismatch is stale")
	res = world.commit_generated("ghost:north", "req-1", room, "director", 1, {})
	_check_eq(res.outcome, "stale", "unknown frontier is stale")
	_check_eq(world.tiles, before.tiles, "tiles untouched")
	_check_eq(world.rooms.keys(), before.rooms, "rooms untouched")
	_check_eq(world.get_frontier(key).status, DungeonWorld.STATUS_PENDING, "frontier still pending for its real request")
	_check_eq(world.counters.stale, 3, "stale completions counted")
	_end()


func _test_overlap_is_rejected_without_mutation() -> void:
	print("\nTest: overlapping placement is rejected and leaves state unchanged")
	var world := start_world(["north", "east"])
	# Commit a big room east first...
	var east := DungeonWorld.frontier_key("r-000", "east")
	world.begin_generation(east, "req-e")
	var r1 := world.commit_generated(east, "req-e", room_from(plan("r-001", "huge", ["west", "north"])), "director", 1, {})
	_check(r1.ok, "huge east room committed")
	# ...then ask for a room north of the start room that would run into it.
	var north := DungeonWorld.frontier_key("r-000", "north")
	world.begin_generation(north, "req-n")
	var before := _snapshot(world)
	var res := world.commit_generated(north, "req-n", room_from(plan("r-002", "huge", ["south"])), "director", 2, {})
	_check(not res.ok, "overlapping room refused")
	_check_eq(res.outcome, "rejected", "outcome rejected")
	_check(res.reason in ["overlap", "blocks_frontier", "blocked_exit"], "reason explains the conflict (%s)" % res.reason)
	_check_eq(world.tiles, before.tiles, "tiles untouched by rejected commit")
	_check_eq(world.rooms.keys(), before.rooms, "no room added")
	_check_eq(world.get_frontier(north).status, DungeonWorld.STATUS_PENDING, "frontier stays pending so a fallback can still commit")
	_check_eq(world.counters.rejected, 1, "rejection counted")
	var evaluated := world.evaluate_placement(north, room_from(plan("r-003", "tiny", ["south"])))
	_check(evaluated.ok, "a smaller room still fits (repositioning by re-planning)")
	_end()


func _test_blocked_exit_is_reported_and_prunable() -> void:
	print("\nTest: exit facing committed geometry is reported as blocked")
	var world := start_world(["north"])
	var key := DungeonWorld.frontier_key("r-000", "north")
	world.begin_generation(key, "req-1")
	# r-001 (small 9x9) north of start: its west-facing exit would lead at x=-1, which is free.
	# Occupy that outward cell by a stray wall tile owned by nobody but present in the map.
	world.tiles[Vector2i(-1, -4)] = TileType.WALL
	var room := room_from(plan("r-001", "small", ["south", "west"]))
	var res := world.evaluate_placement(key, room)
	_check(not res.ok, "placement not ok when an exit faces occupied space")
	_check_eq(res.reason, "blocked_exit", "reason blocked_exit")
	_check("west" in res.blocked_directions, "west reported as blocked")
	var pruned := room_from(plan("r-001", "small", ["south"]))
	_check(world.evaluate_placement(key, pruned).ok, "pruned plan places fine")
	_end()


func _test_missing_backlink_and_duplicate_room_id_rejected() -> void:
	print("\nTest: missing backlink and duplicate room id are rejected")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "north")
	world.begin_generation(key, "req-1")
	var res := world.commit_generated(key, "req-1", room_from(plan("r-001", "small", ["east", "west"])), "director", 1, {})
	_check_eq(res.reason, "missing_backlink", "no exit facing back is refused")
	res = world.commit_generated(key, "req-1", room_from(plan("r-000", "small", ["south"])), "director", 1, {})
	_check_eq(res.reason, "duplicate_room_id", "already-committed room id refused")
	_check_eq(world.rooms.size(), 1, "still one committed room")
	_end()


func _test_commit_keeps_all_tiles_connected() -> void:
	print("\nTest: all committed traversable tiles stay connected")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "east")
	world.begin_generation(key, "req-1")
	world.commit_generated(key, "req-1", room_from(plan("r-001", "medium", ["west", "north", "south"], "cavern")), "director", 1, {})
	var key2 := DungeonWorld.frontier_key("r-001", "north")
	world.begin_generation(key2, "req-2")
	world.commit_generated(key2, "req-2", room_from(plan("r-002", "small", ["south", "east"], "corridor")), "director", 2, {})
	_check_eq(world.rooms.size(), 3, "three rooms committed")
	_check_eq(_reachable_component_size(world), _traversable_count(world), "single connected traversable component")
	_check_eq(world.integrity_violations(), [], "integrity check clean")
	_end()


func _test_seal_terminal_and_frontier_unique() -> void:
	print("\nTest: sealing is terminal; a sealed frontier cannot be committed")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "south")
	world.begin_generation(key, "req-1")
	var res := world.seal_frontier(key, "req-1", "no_room_fits")
	_check(res.ok, "seal accepted for the pending request")
	_check_eq(world.get_frontier(key).status, DungeonWorld.STATUS_SEALED, "frontier sealed")
	_check_eq(world.tiles[world.get_frontier(key).pos], TileType.WALL, "sealed exit becomes wall")
	res = world.commit_generated(key, "req-1", room_from(plan("r-001", "small", ["north"])), "director", 1, {})
	_check_eq(res.outcome, "duplicate", "commit after seal refused")
	_check(not world.begin_generation(key, "req-2"), "sealed frontier cannot be requested again")
	_check_eq(world.integrity_violations(), [], "integrity check clean")
	_end()


func _test_disconnected_room_rejected() -> void:
	print("\nTest: a room whose floor is not connected to its backlink is rejected")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "north")
	world.begin_generation(key, "req-1")
	var room := room_from(plan("r-001", "small", ["south"]))
	# Corrupt the generated room: cut it in two with a wall row across the interior.
	for x in range(1, room.width - 1):
		room.tiles[Vector2i(x, 4)] = TileType.WALL
	room.tiles[Vector2i(4, 1)] = TileType.FLOOR
	room.tiles[Vector2i(4, 2)] = TileType.FLOOR
	var before := _snapshot(world)
	var res := world.commit_generated(key, "req-1", room, "director", 1, {})
	_check_eq(res.reason, "disconnected_room", "disconnected geometry refused")
	_check_eq(world.tiles, before.tiles, "no tiles committed")
	_end()


func _test_sealed_exit_is_recorded_as_wall_in_its_room() -> void:
	print("\nTest: sealing updates the owning room's record so rooms never contradict")
	var world := start_world()
	var key := DungeonWorld.frontier_key("r-000", "south")
	var door: Vector2i = world.get_frontier(key).pos
	world.begin_generation(key, "req-1")
	world.seal_frontier(key, "req-1", "no_room_fits")
	_check_eq(world.rooms["r-000"].tiles[door], TileType.WALL, "room record shows the sealed exit as wall")
	_check_eq(world.integrity_violations(), [], "integrity clean")
	_end()


func _probe(direction: String) -> GeneratedRoom:
	var back: String = DungeonWorld.OPPOSITE[direction]
	return room_from(plan("probe", "tiny", [back]))


func _test_breach_reopens_a_boxed_in_dungeon() -> void:
	print("\nTest: when no exit is open, a wall is breached so the dungeon can continue")
	var world := start_world(["north"])
	_check(world.has_open_frontier(), "an exit is open at start")
	_check(not world.open_breach(Callable(self, "_probe"), Vector2i.ZERO, 5).ok, "no breach while an exit is still open")
	var key := DungeonWorld.frontier_key("r-000", "north")
	world.begin_generation(key, "req-1")
	world.seal_frontier(key, "req-1", "no_room_fits")
	_check(not world.has_open_frontier(), "boxed in: nothing open after sealing the only exit")
	var res := world.open_breach(Callable(self, "_probe"), Vector2i(4, 4), 5)
	_check(res.ok, "breach opened")
	_check(world.has_open_frontier(), "an unresolved exit exists again")
	var f: Dictionary = res.frontier
	_check_eq(f.status, DungeonWorld.STATUS_UNRESOLVED, "breach frontier is unresolved")
	_check_eq(world.tiles[f.pos], TileType.DOOR_CLOSED, "the wall became a door")
	_check_eq(world.rooms["r-000"].tiles[f.pos], TileType.DOOR_CLOSED, "room record updated")
	_check(not world.tiles.has(f.outward), "it leads into unknown space")
	_check(world.evaluate_placement(f.key, _probe(f.direction)).ok, "the smallest room is known to fit there")
	_check_eq(world.integrity_violations(), [], "integrity clean after a breach")
	_check_eq(world.counters.breaches, 1, "breach counted in diagnostics")
	# the breach is a normal frontier: it can be requested and committed
	world.begin_generation(f.key, "req-2")
	var committed := world.commit_generated(f.key, "req-2", room_from(plan("r-001", "small", [DungeonWorld.OPPOSITE[f.direction], "north"])), "director", 6, {})
	_check(committed.ok, "a room commits through the breach frontier")
	_end()
