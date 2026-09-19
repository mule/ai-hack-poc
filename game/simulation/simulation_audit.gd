class_name SimulationAudit
extends RefCounted
## Independent audit of a committed DungeonWorld (Issue #16).
##
## Deliberately re-derives every property from the world's public records
## rather than trusting the bookkeeping the placement code maintains, then also
## folds in the world's own integrity_violations() so both gates run. Each
## failure is {kind, severity:"error", message, detail} with kind one of
##   overlap       rooms claim the same tile (other than wall-on-wall), or the
##                 world and a room record disagree about a tile
##   topology      the traversable space is not one connected component, the
##                 room graph is not a tree hanging off the start room, or a
##                 frontier link is dangling/inconsistent
##   reachability  a room, open exit or spawned resource cannot be reached
##                 from the start
## Placement conflicts and stalls are event-derived and live in the harness.

const DungeonWorld = preload("res://world/dungeon_world.gd")
const GeneratedRoom = preload("res://generation/generated_room.gd")

const SAMPLE_LIMIT := 5
const NEIGHBORS: Array[Vector2i] = [Vector2i.UP, Vector2i.DOWN, Vector2i.LEFT, Vector2i.RIGHT]
const WALL := GeneratedRoom.TileType.WALL


static func audit(world: DungeonWorld, start: Vector2i) -> Array[Dictionary]:
	var failures: Array[Dictionary] = []
	_audit_overlap(world, failures)
	var reached := _flood(world, start)
	_audit_topology(world, start, reached, failures)
	_audit_reachability(world, reached, failures)
	_fold_world_integrity(world, failures)
	return failures


static func _failure(kind: String, message: String, detail: Dictionary = {}) -> Dictionary:
	return {"kind": kind, "severity": "error", "message": message, "detail": detail}


static func _pos(p: Vector2i) -> Array:
	return [p.x, p.y]


static func _sample(positions: Array) -> Array:
	var out: Array = []
	for p in positions.slice(0, SAMPLE_LIMIT):
		out.append(_pos(p))
	return out


## Traversable tiles reachable from `start`.
static func _flood(world: DungeonWorld, start: Vector2i) -> Dictionary:
	var seen := {}
	if not world.tiles.has(start) or world.tiles[start] == WALL:
		return seen
	seen[start] = true
	var queue: Array[Vector2i] = [start]
	while not queue.is_empty():
		var cur: Vector2i = queue.pop_back()
		for d in NEIGHBORS:
			var n: Vector2i = cur + d
			if not seen.has(n) and world.tiles.has(n) and world.tiles[n] != WALL:
				seen[n] = true
				queue.append(n)
	return seen


static func _audit_overlap(world: DungeonWorld, failures: Array[Dictionary]) -> void:
	var owner := {}
	var clashes: Array = []
	var mismatches: Array = []
	var clash_rooms := {}
	for room_id in world.room_order:
		var rec: Dictionary = world.rooms[room_id]
		for pos in rec.tiles:
			var t: int = rec.tiles[pos]
			if world.tiles.get(pos, -1) != t:
				mismatches.append(pos)
			if owner.has(pos):
				var previous: Dictionary = owner[pos]
				if not (previous.type == WALL and t == WALL):
					clashes.append(pos)
					clash_rooms["%s|%s" % [previous.room, room_id]] = true
			else:
				owner[pos] = {"room": room_id, "type": t}
	if not clashes.is_empty():
		failures.append(_failure("overlap", "%d tile(s) are claimed by more than one room without being wall-on-wall" % clashes.size(), {"count": clashes.size(), "sample": _sample(clashes), "rooms": clash_rooms.keys().slice(0, SAMPLE_LIMIT)}))
	if not mismatches.is_empty():
		failures.append(_failure("overlap", "%d room tile(s) disagree with the world map" % mismatches.size(), {"reason": "tile_mismatch", "count": mismatches.size(), "sample": _sample(mismatches)}))
	var orphans: Array = []
	for pos in world.tiles:
		if not owner.has(pos):
			orphans.append(pos)
	if not orphans.is_empty():
		failures.append(_failure("topology", "%d world tile(s) belong to no committed room" % orphans.size(), {"reason": "orphan_tile", "count": orphans.size(), "sample": _sample(orphans)}))


static func _audit_topology(world: DungeonWorld, start: Vector2i, reached: Dictionary, failures: Array[Dictionary]) -> void:
	var traversable: Array = []
	for pos in world.tiles:
		if world.tiles[pos] != WALL:
			traversable.append(pos)
	if reached.is_empty() and not traversable.is_empty():
		failures.append(_failure("topology", "the start tile is not traversable", {"start": _pos(start)}))
	elif reached.size() != traversable.size():
		var unreached: Array = traversable.filter(func(p: Vector2i) -> bool: return not reached.has(p))
		failures.append(_failure("topology", "%d of %d traversable tiles are disconnected from the start" % [unreached.size(), traversable.size()], {"reason": "disconnected", "count": unreached.size(), "sample": _sample(unreached)}))

	for key in world.frontiers:
		var f: Dictionary = world.frontiers[key]
		if f.status == DungeonWorld.STATUS_COMMITTED:
			if not world.rooms.has(f.resolved_room_id):
				failures.append(_failure("topology", "committed frontier %s links to unknown room '%s'" % [key, f.resolved_room_id], {"frontier": key}))
			if world.tiles.get(f.pos, WALL) == WALL:
				failures.append(_failure("topology", "committed frontier %s has a wall where its door should be" % key, {"frontier": key, "pos": _pos(f.pos)}))
		elif f.status == DungeonWorld.STATUS_SEALED and world.tiles.get(f.pos, -1) != WALL:
			failures.append(_failure("topology", "sealed frontier %s is not a wall" % key, {"frontier": key, "pos": _pos(f.pos)}))

	# The room graph must be a tree rooted at the start room.
	if world.room_order.is_empty():
		return
	var root: String = world.room_order[0]
	var children := {}
	for room_id in world.room_order:
		if room_id == root:
			continue
		var parent_key: String = world.rooms[room_id].parent_frontier
		var parent_frontier: Dictionary = world.frontiers.get(parent_key, {})
		if parent_frontier.is_empty() or parent_frontier.status != DungeonWorld.STATUS_COMMITTED or parent_frontier.resolved_room_id != room_id:
			failures.append(_failure("topology", "room %s is not the committed result of its parent frontier '%s'" % [room_id, parent_key], {"room": room_id, "frontier": parent_key}))
			continue
		if not world.rooms.has(parent_frontier.room_id):
			failures.append(_failure("topology", "room %s hangs off unknown room '%s'" % [room_id, parent_frontier.room_id], {"room": room_id}))
			continue
		children[parent_frontier.room_id] = children.get(parent_frontier.room_id, []) + [room_id]
	var seen := {root: true}
	var queue: Array = [root]
	while not queue.is_empty():
		var current: String = queue.pop_back()
		for child in children.get(current, []):
			if not seen.has(child):
				seen[child] = true
				queue.append(child)
	if seen.size() != world.room_order.size():
		var lost: Array = world.room_order.filter(func(r: String) -> bool: return not seen.has(r))
		failures.append(_failure("topology", "%d room(s) are not connected to the start room through committed frontiers" % lost.size(), {"reason": "room_graph", "rooms": lost.slice(0, SAMPLE_LIMIT)}))


static func _audit_reachability(world: DungeonWorld, reached: Dictionary, failures: Array[Dictionary]) -> void:
	var stranded_rooms: Array = []
	for room_id in world.room_order:
		var rec: Dictionary = world.rooms[room_id]
		var any := false
		for pos in rec.tiles:
			if rec.tiles[pos] != WALL and reached.has(pos):
				any = true
				break
		if not any:
			stranded_rooms.append(room_id)
	if not stranded_rooms.is_empty():
		failures.append(_failure("reachability", "%d room(s) cannot be reached from the start" % stranded_rooms.size(), {"rooms": stranded_rooms.slice(0, SAMPLE_LIMIT)}))

	var stranded_exits: Array = []
	for f in world.open_frontiers():
		var approach: Vector2i = f.pos - DungeonWorld.DIRECTION_VECTORS.get(f.direction, Vector2i.ZERO)
		if not reached.has(f.pos) or not reached.has(approach):
			stranded_exits.append(f.key)
	if not stranded_exits.is_empty():
		failures.append(_failure("reachability", "%d open exit(s) cannot be reached from the start" % stranded_exits.size(), {"frontiers": stranded_exits.slice(0, SAMPLE_LIMIT)}))

	var stranded: Array = []
	for room_id in world.room_order:
		var rec: Dictionary = world.rooms[room_id]
		for kind in ["enemies", "items"]:
			for entity in rec[kind]:
				if world.tiles.get(entity.pos, WALL) != GeneratedRoom.TileType.FLOOR or not reached.has(entity.pos):
					stranded.append({"room": room_id, "kind": kind, "type": entity.type, "pos": _pos(entity.pos)})
	if not stranded.is_empty():
		failures.append(_failure("reachability", "%d spawned enemy/item(s) sit on a tile the player cannot reach" % stranded.size(), {"count": stranded.size(), "sample": stranded.slice(0, SAMPLE_LIMIT)}))


static func _fold_world_integrity(world: DungeonWorld, failures: Array[Dictionary]) -> void:
	var seen := {}
	for problem in world.integrity_violations():
		if seen.has(problem):
			continue
		seen[problem] = true
		var kind := "overlap" if "contradictory occupancy" in problem else "topology"
		failures.append(_failure(kind, "world integrity check: %s" % problem, {"source": "world.integrity_violations"}))
