class_name DungeonWorld
extends RefCounted
## Durable dungeon state for deferred generation (Issue #7).
##
## Owns the committed world: world-space tiles, committed rooms (bounds, tiles,
## entities), and one *frontier* per room exit with a one-way lifecycle:
##
##   unresolved -> pending -> committed | sealed
##
## The world is the single authority for "a frontier is pending once and
## committed once": completions that arrive for a frontier that is committed,
## sealed, unknown, or pending under a different request id never mutate
## anything (they are counted and logged). Placement is evaluated without side
## effects; a commit is applied atomically only after every check has passed.
##
## Geometry: a generated room is placed so that its backlink exit (facing the
## opposite way of the target frontier) lands on the frontier's door tile. The
## two rooms share that one tile (the committed tile wins). Rooms may otherwise
## only overlap wall-on-wall, so floors never merge, and every traversable tile
## stays in one connected component by construction.

const GeneratedRoom = preload("res://generation/generated_room.gd")

const STATUS_UNRESOLVED := "unresolved"
const STATUS_PENDING := "pending"
const STATUS_COMMITTED := "committed"
const STATUS_SEALED := "sealed"

const DIRECTION_VECTORS := {
	"north": Vector2i(0, -1),
	"south": Vector2i(0, 1),
	"east": Vector2i(1, 0),
	"west": Vector2i(-1, 0),
}
const OPPOSITE := {"north": "south", "south": "north", "east": "west", "west": "east"}
const LOG_LIMIT := 400
const NEIGHBORS: Array[Vector2i] = [Vector2i.UP, Vector2i.DOWN, Vector2i.LEFT, Vector2i.RIGHT]

## Placement conflicts a re-plan (fewer exits / smaller footprint) may fix.
const GEOMETRIC_REASONS := ["overlap", "blocks_frontier", "blocked_exit"]

var run_id := "run-1"
## Vector2i -> int (TileType). GameState aliases this dictionary as `map_tiles`.
var tiles: Dictionary = {}
## room_id -> room record (see _commit_room).
var rooms: Dictionary = {}
var room_order: Array[String] = []
## frontier key -> frontier record.
var frontiers: Dictionary = {}
## Incremented on every committed mutation; renderers/UI poll this.
var revision := 0
var generation_log: Array[Dictionary] = []
var counters := {
	"committed_director": 0,
	"committed_fallback": 0,
	"rejected": 0,
	"duplicates": 0,
	"stale": 0,
	"sealed": 0,
	"fallbacks": 0,
	"breaches": 0,
}

## Optional telemetry sink for lifecycle events (Issue #25).
var telemetry_sink: Variant = null

var _log_seq := 0
## outward cell -> frontier key for every open (unresolved/pending) frontier.
var _outward_index: Dictionary = {}


static func frontier_key(room_id: String, direction: String) -> String:
	return "%s:%s" % [room_id, direction]


# --- queries -----------------------------------------------------------------


func get_frontier(key: String) -> Dictionary:
	return frontiers.get(key, {})


func open_frontiers() -> Array[Dictionary]:
	var out: Array[Dictionary] = []
	for key in frontiers:
		var f: Dictionary = frontiers[key]
		if f.status == STATUS_UNRESOLVED or f.status == STATUS_PENDING:
			out.append(f)
	return out


func frontiers_with_status(status: String) -> Array[Dictionary]:
	var out: Array[Dictionary] = []
	for key in frontiers:
		if frontiers[key].status == status:
			out.append(frontiers[key])
	return out


## True while at least one exit is unresolved or pending.
func has_open_frontier() -> bool:
	return not _outward_index.is_empty()


func pending_count() -> int:
	return frontiers_with_status(STATUS_PENDING).size()


## Open frontier whose door tile is `pos`, or {}.
func open_frontier_at(pos: Vector2i) -> Dictionary:
	for f in open_frontiers():
		if f.pos == pos:
			return f
	return {}


## Open frontier whose unknown cell (just beyond the door) is `pos`, or {}.
func open_frontier_leading_to(pos: Vector2i) -> Dictionary:
	if _outward_index.has(pos):
		return frontiers[_outward_index[pos]]
	return {}


func room_id_at(pos: Vector2i) -> String:
	# Search most recently committed rooms first (children before parents)
	for i in range(room_order.size() - 1, -1, -1):
		var room_id: String = room_order[i]
		var r: Dictionary = rooms[room_id]
		if r.tiles.has(pos):
			if r.tiles[pos] != GeneratedRoom.TileType.WALL:
				return room_id
		elif r.get("link_pos") != null and r.link_pos == pos:
			return room_id
	return ""


# --- lifecycle ---------------------------------------------------------------


## Commit the starting room. It has no parent frontier; every cardinal exit
## becomes an unresolved frontier.
func commit_start_room(room: GeneratedRoom, origin: Vector2i, turn: int) -> Dictionary:
	var checked := _check_room_shape(room, {})
	if not checked.ok:
		return {"ok": false, "outcome": "rejected", "reason": checked.reason}
	if rooms.has(room.room_id):
		return {"ok": false, "outcome": "rejected", "reason": "duplicate_room_id"}
	var placement := {"ok": true, "origin": origin, "link_pos": null}
	var record := _commit_room(room, placement, "", "start", turn, {})
	return {"ok": true, "outcome": "committed", "room": record}


## unresolved -> pending, exactly once. False for unknown or non-unresolved.
func begin_generation(key: String, request_id: String) -> bool:
	var f: Dictionary = frontiers.get(key, {})
	if f.is_empty() or f.status != STATUS_UNRESOLVED:
		return false
	f.status = STATUS_PENDING
	f.request_id = request_id
	_outward_index[f.outward] = key  # stays reserved while pending
	revision += 1
	_log("requested", key, {"request_id": request_id})
	return true


## Side-effect-free placement check for `room` against frontier `key`.
## Returns {ok, origin, link_pos} or {ok:false, reason, blocked_directions, pos?}.
func evaluate_placement(key: String, room: GeneratedRoom) -> Dictionary:
	var f: Dictionary = frontiers.get(key, {})
	if f.is_empty():
		return _reject("unknown_frontier")
	if not DIRECTION_VECTORS.has(f.direction):
		return _reject("unsupported_direction")
	var backlink := _exit_facing(room, OPPOSITE[f.direction])
	if backlink.is_empty():
		return _reject("missing_backlink")
	var shape := _check_room_shape(room, backlink)
	if not shape.ok:
		return _reject(shape.reason)
	var origin: Vector2i = f.pos - backlink.pos
	var link: Vector2i = f.pos
	var door_positions := {}
	for exit_entry in room.exits:
		if exit_entry != backlink and DIRECTION_VECTORS.has(exit_entry.direction):
			door_positions[origin + exit_entry.pos] = exit_entry.direction

	var blocked: Array[String] = []
	for local in room.tiles:
		var world_pos: Vector2i = origin + local
		if world_pos == link:
			continue
		var conflict := ""
		var existing: Variant = tiles.get(world_pos)
		if existing != null and not (existing == GeneratedRoom.TileType.WALL and room.tiles[local] == GeneratedRoom.TileType.WALL):
			conflict = "overlap"
		elif _outward_index.has(world_pos) and _outward_index[world_pos] != key:
			conflict = "blocks_frontier"
		if conflict == "":
			continue
		if door_positions.has(world_pos):
			blocked.append(door_positions[world_pos])
		else:
			return {"ok": false, "reason": conflict, "pos": world_pos, "blocked_directions": []}
	# Every new exit must lead into unknown space, not into committed geometry
	# or another frontier's reserved cell.
	for world_pos in door_positions:
		var direction: String = door_positions[world_pos]
		if direction in blocked:
			continue
		var beyond: Vector2i = world_pos + DIRECTION_VECTORS[direction]
		if tiles.has(beyond) or _outward_index.has(beyond) or _room_covers(room, origin, beyond):
			blocked.append(direction)
	if not blocked.is_empty():
		return {"ok": false, "reason": "blocked_exit", "blocked_directions": blocked}
	return {"ok": true, "origin": origin, "link_pos": link, "blocked_directions": []}


## pending -> committed. Validates everything first; mutates only on success.
## Outcomes: committed | duplicate | stale | rejected. A rejected commit leaves
## the frontier pending so the caller can retry (e.g. with a fallback plan).
func commit_generated(key: String, request_id: String, room: GeneratedRoom, source: String, turn: int, meta: Dictionary) -> Dictionary:
	var f: Dictionary = frontiers.get(key, {})
	if f.is_empty():
		counters.stale += 1
		_log("stale", key, {"request_id": request_id, "reason": "unknown_frontier"})
		return {"ok": false, "outcome": "stale", "reason": "unknown_frontier"}
	if f.status == STATUS_COMMITTED or f.status == STATUS_SEALED:
		counters.duplicates += 1
		_log("duplicate", key, {"request_id": request_id, "status": f.status, "source": source})
		return {"ok": false, "outcome": "duplicate", "reason": "already_%s" % f.status}
	if f.status != STATUS_PENDING or f.request_id != request_id:
		counters.stale += 1
		_log("stale", key, {"request_id": request_id, "reason": "not_pending" if f.status != STATUS_PENDING else "request_id_mismatch"})
		return {"ok": false, "outcome": "stale", "reason": "not_pending" if f.status != STATUS_PENDING else "request_id_mismatch"}
	if rooms.has(room.room_id):
		return _rejected(key, request_id, "duplicate_room_id", [])
	var placement := evaluate_placement(key, room)
	if not placement.ok:
		return _rejected(key, request_id, placement.reason, placement.get("blocked_directions", []))
	var record := _commit_room(room, placement, key, source, turn, meta)
	f.status = STATUS_COMMITTED
	f.resolved_room_id = room.room_id
	_outward_index.erase(f.outward)
	counters["committed_" + ("fallback" if source == "fallback" else "director")] += 1
	_log("committed", key, {"request_id": request_id, "room_id": room.room_id, "source": source})
	return {"ok": true, "outcome": "committed", "room": record}


## pending -> sealed: the exit becomes a wall. Used when no plan (not even the
## smallest fallback) fits. Terminal; connectivity is unaffected.
func seal_frontier(key: String, request_id: String, reason: String) -> Dictionary:
	var f: Dictionary = frontiers.get(key, {})
	if f.is_empty() or f.status != STATUS_PENDING or f.request_id != request_id:
		counters.stale += 1
		_log("stale", key, {"request_id": request_id, "reason": "seal_not_pending"})
		return {"ok": false, "outcome": "stale", "reason": "not_pending"}
	f.status = STATUS_SEALED
	f.seal_reason = reason
	tiles[f.pos] = GeneratedRoom.TileType.WALL
	if rooms.has(f.room_id):
		rooms[f.room_id].tiles[f.pos] = GeneratedRoom.TileType.WALL
	_outward_index.erase(f.outward)
	counters.sealed += 1
	revision += 1
	_log("sealed", key, {"request_id": request_id, "reason": reason})
	return {"ok": true, "outcome": "sealed"}


## Recovery so the dungeon can always continue: if no exit is open (every one
## was resolved or sealed), turn a wall of a committed room into a new
## unresolved exit. A candidate is only used if `probe_for(direction)` (the
## smallest fallback room, backlink facing the wall) is verified to fit beyond
## it, so the frontier can always be resolved locally. Candidates nearest to
## `near` (the player) are tried first. The plane is unbounded, so the outer
## wall of the outermost room always qualifies.
func open_breach(probe_for: Callable, near: Vector2i, turn: int) -> Dictionary:
	if has_open_frontier():
		return {"ok": false, "reason": "exits_available"}
	var candidates: Array = []
	for room_id in room_order:
		var rec: Dictionary = rooms[room_id]
		for pos in rec.tiles:
			if rec.tiles[pos] != GeneratedRoom.TileType.WALL or tiles.get(pos) != GeneratedRoom.TileType.WALL:
				continue
			for direction in DIRECTION_VECTORS:
				var step: Vector2i = DIRECTION_VECTORS[direction]
				if rec.tiles.get(pos - step, -1) == GeneratedRoom.TileType.FLOOR and not tiles.has(pos + step):
					candidates.append([absi(pos.x - near.x) + absi(pos.y - near.y), room_id, pos, direction])
	candidates.sort_custom(func(a: Array, b: Array) -> bool:
		if a[0] != b[0]:
			return a[0] < b[0]
		return a[2].x < b[2].x if a[2].x != b[2].x else a[2].y < b[2].y)
	for candidate in candidates:
		var room_id: String = candidate[1]
		var pos: Vector2i = candidate[2]
		var direction: String = candidate[3]
		var key := "%s:%s#b%d" % [room_id, direction, counters.breaches + 1]
		var record := {
			"key": key,
			"room_id": room_id,
			"direction": direction,
			"kind": "door",
			"pos": pos,
			"outward": pos + DIRECTION_VECTORS[direction],
			"since_turn": turn,
			"status": STATUS_UNRESOLVED,
			"request_id": "",
			"resolved_room_id": "",
		}
		frontiers[key] = record  # provisional, so evaluate_placement can use it
		var fits: bool = evaluate_placement(key, probe_for.call(direction)).ok
		if not fits:
			frontiers.erase(key)
			continue
		tiles[pos] = GeneratedRoom.TileType.DOOR_CLOSED
		rooms[room_id].tiles[pos] = GeneratedRoom.TileType.DOOR_CLOSED
		_outward_index[record.outward] = key
		counters.breaches += 1
		revision += 1
		_log("breach", key, {"room_id": room_id})
		if telemetry_sink != null and telemetry_sink.has_method("enqueue_event"):
			telemetry_sink.enqueue_event(
				"frontier.discovered",
				run_id,
				null,
				{"frontier_id": key, "depth": 1, "exit_direction": direction}
			)
		return {"ok": true, "frontier": record}
	return {"ok": false, "reason": "no_candidate"}


func note_fallback(key: String, reason: String, detail: Dictionary = {}) -> void:
	counters.fallbacks += 1
	var entry := detail.duplicate()
	entry["reason"] = reason
	_log("fallback", key, entry)


func log_event(event: String, key: String, detail: Dictionary = {}) -> void:
	_log(event, key, detail)


func status_summary() -> Dictionary:
	return {
		"rooms": rooms.size(),
		"open_frontiers": open_frontiers().size(),
		"pending": pending_count(),
		"fallbacks": counters.fallbacks,
		"counters": counters.duplicate(),
	}


## Independent audit of the invariants; returns human-readable violations.
func integrity_violations() -> Array[String]:
	var problems: Array[String] = []
	# 1. Every committed room tile is present in the world, and rooms never
	#    contradict each other: a shared position must be wall-on-wall.
	var owner: Dictionary = {}
	for room_id in room_order:
		var rec: Dictionary = rooms[room_id]
		for pos in rec.tiles:
			var t: int = rec.tiles[pos]
			if not tiles.has(pos):
				problems.append("room %s tile %s missing from world" % [room_id, pos])
			if owner.has(pos):
				var other: Dictionary = owner[pos]
				if not (other.type == GeneratedRoom.TileType.WALL and t == GeneratedRoom.TileType.WALL):
					problems.append("contradictory occupancy at %s between %s and %s" % [pos, other.room, room_id])
			else:
				owner[pos] = {"room": room_id, "type": t}
	# 2. Frontier bookkeeping: unique keys, committed frontiers link to rooms.
	for key in frontiers:
		var f: Dictionary = frontiers[key]
		if f.key != key:
			problems.append("frontier key mismatch %s" % key)
		if f.status == STATUS_COMMITTED and not rooms.has(f.resolved_room_id):
			problems.append("committed frontier %s links to unknown room" % key)
		if (f.status == STATUS_UNRESOLVED or f.status == STATUS_PENDING) and tiles.has(f.outward):
			problems.append("open frontier %s leads into committed tile %s" % [key, f.outward])
	# 3. One connected traversable component.
	var traversable := 0
	var start := Vector2i.ZERO
	for pos in tiles:
		if tiles[pos] != GeneratedRoom.TileType.WALL:
			if traversable == 0:
				start = pos
			traversable += 1
	if traversable > 0:
		var seen := {start: true}
		var queue: Array[Vector2i] = [start]
		while not queue.is_empty():
			var cur: Vector2i = queue.pop_back()
			for d in NEIGHBORS:
				var n: Vector2i = cur + d
				if not seen.has(n) and tiles.has(n) and tiles[n] != GeneratedRoom.TileType.WALL:
					seen[n] = true
					queue.append(n)
		if seen.size() != traversable:
			problems.append("world disconnected: %d of %d traversable tiles reachable" % [seen.size(), traversable])
	return problems


# --- internals ---------------------------------------------------------------


func _reject(reason: String) -> Dictionary:
	return {"ok": false, "reason": reason, "blocked_directions": []}


func _rejected(key: String, request_id: String, reason: String, blocked: Array) -> Dictionary:
	counters.rejected += 1
	_log("rejected", key, {"request_id": request_id, "reason": reason})
	return {"ok": false, "outcome": "rejected", "reason": reason, "blocked_directions": blocked}


func _log(event: String, key: String, detail: Dictionary) -> void:
	_log_seq += 1
	var entry := {"seq": _log_seq, "event": event, "frontier": key}
	entry.merge(detail)
	generation_log.append(entry)
	if generation_log.size() > LOG_LIMIT:
		generation_log.pop_front()


func _exit_facing(room: GeneratedRoom, direction: String) -> Dictionary:
	for exit_entry in room.exits:
		if exit_entry.direction == direction:
			return exit_entry
	return {}


func _room_covers(room: GeneratedRoom, origin: Vector2i, world_pos: Vector2i) -> bool:
	return room.tiles.has(world_pos - origin)


## Shape validation independent of the world: tiles exist, the backlink tile is
## traversable, and all traversable tiles are one component reachable from it.
func _check_room_shape(room: GeneratedRoom, backlink: Dictionary) -> Dictionary:
	if room == null or room.tiles.is_empty() or room.room_id == "":
		return {"ok": false, "reason": "empty_room"}
	var seed_pos := Vector2i(-1, -1)
	if not backlink.is_empty():
		seed_pos = backlink.pos
	else:
		for pos in room.tiles:
			if room.tiles[pos] != GeneratedRoom.TileType.WALL:
				seed_pos = pos
				break
	if not room.tiles.has(seed_pos) or room.tiles[seed_pos] == GeneratedRoom.TileType.WALL:
		return {"ok": false, "reason": "missing_backlink"}
	var total := 0
	for pos in room.tiles:
		if room.tiles[pos] != GeneratedRoom.TileType.WALL:
			total += 1
	var seen := {seed_pos: true}
	var queue: Array[Vector2i] = [seed_pos]
	while not queue.is_empty():
		var cur: Vector2i = queue.pop_back()
		for d in NEIGHBORS:
			var n: Vector2i = cur + d
			if not seen.has(n) and room.tiles.has(n) and room.tiles[n] != GeneratedRoom.TileType.WALL:
				seen[n] = true
				queue.append(n)
	if seen.size() != total:
		return {"ok": false, "reason": "disconnected_room"}
	return {"ok": true}


## Apply a validated placement. Only called after every check has passed.
func _commit_room(room: GeneratedRoom, placement: Dictionary, parent_key: String, source: String, turn: int, meta: Dictionary) -> Dictionary:
	var commit_start_msec := Time.get_ticks_msec()
	var origin: Vector2i = placement.origin
	var link: Variant = placement.link_pos
	var owned: Dictionary = {}
	for local in room.tiles:
		var world_pos: Vector2i = origin + local
		if link != null and world_pos == link:
			continue  # the committed neighbour keeps the shared door tile
		owned[world_pos] = room.tiles[local]
	for world_pos in owned:
		tiles[world_pos] = owned[world_pos]

	var exits: Array[Dictionary] = []
	var unsupported: Array[String] = []
	var backlink_direction := ""
	if parent_key != "":
		backlink_direction = OPPOSITE[frontiers[parent_key].direction]
	for exit_entry in room.exits:
		var direction: String = exit_entry.direction
		var world_pos: Vector2i = origin + exit_entry.pos
		if not DIRECTION_VECTORS.has(direction):
			unsupported.append(direction)
			continue
		exits.append({"direction": direction, "kind": exit_entry.kind, "pos": world_pos})
		var key := frontier_key(room.room_id, direction)
		var record := {
			"key": key,
			"room_id": room.room_id,
			"direction": direction,
			"kind": exit_entry.kind,
			"pos": world_pos,
			"outward": world_pos + DIRECTION_VECTORS[direction],
			"since_turn": turn,
			"status": STATUS_UNRESOLVED,
			"request_id": "",
			"resolved_room_id": "",
		}
		if direction == backlink_direction:
			record.status = STATUS_COMMITTED
			record.resolved_room_id = frontiers[parent_key].room_id
		else:
			_outward_index[record.outward] = key
		frontiers[key] = record

	var enemies: Array[Dictionary] = []
	for enemy in room.enemies:
		var pos: Vector2i = origin + enemy.pos
		if owned.get(pos, -1) == GeneratedRoom.TileType.FLOOR:
			enemies.append({"type": enemy.type, "pos": pos})
	var items: Array[Dictionary] = []
	for item in room.items:
		var pos: Vector2i = origin + item.pos
		if owned.get(pos, -1) == GeneratedRoom.TileType.FLOOR:
			items.append({"type": item.type, "pos": pos})

	var record := {
		"room_id": room.room_id,
		"origin": origin,
		"bounds": Rect2i(origin, Vector2i(room.width, room.height)),
		"tiles": owned,
		"link_pos": link if link != null else Vector2i(-99999, -99999),
		"exits": exits,
		"unsupported_exits": unsupported,
		"enemies": enemies,
		"items": items,
		"source": source,
		"parent_frontier": parent_key,
		"turn": turn,
		"seed_used": room.seed_used,
		"meta": meta.duplicate(),
		"diagnostics": room.diagnostics.duplicate(),
		"committed_at_msec": Time.get_ticks_msec(),
	}
	rooms[room.room_id] = record
	room_order.append(room.room_id)
	revision += 1

	var commit_end_msec := Time.get_ticks_msec()
	var materialization_ms := float(maxi(0, commit_end_msec - int(meta.get("materialization_started_msec", commit_start_msec))))

	if telemetry_sink != null and telemetry_sink.has_method("enqueue_event"):
		# 1. room.committed event
		var room_req_id: Variant = frontiers[parent_key].request_id if parent_key != "" and frontiers.has(parent_key) else null
		if room_req_id == null or str(room_req_id) == "":
			room_req_id = "req-%s-init" % run_id
		var room_type := str(meta.get("room_type", "room"))
		var room_size := str(meta.get("size_used", "medium"))
		var danger := int(meta.get("danger", 1))
		var prov := str(meta.get("provider", "rules-baseline" if source == "fallback" else "director"))
		var mod := str(meta.get("model", "builtin-v1" if source == "fallback" else "default"))

		var committed_attrs := {
			"room_id": room.room_id,
			"room_type": room_type,
			"room_size": room_size,
			"danger": danger,
			"exit_count": mini(exits.size(), 8),
			"has_secret": not room.secrets.is_empty() or exits.any(func(e): return e.kind == "secret"),
			"enemy_density": snappedf(clampf(float(meta.get("enemy_density", float(enemies.size()) / float(maxi(1, room.width * room.height)))), 0.0, 1.0), 0.0001),
			"loot_density": snappedf(clampf(float(meta.get("loot_density", float(items.size()) / float(maxi(1, room.width * room.height)))), 0.0, 1.0), 0.0001),
			"materialization_ms": materialization_ms,
			"provider": prov,
			"model": mod,
		}
		if parent_key != "":
			committed_attrs["frontier_id"] = parent_key

		# Placement has been validated and committed. Publish the decision stage
		# before the commit observation so consumers see one ordered outcome.
		if source == "director":
			var decision := {"provider": prov, "model": mod, "room_type": room_type,
				"room_size": room_size, "danger": danger}
			var normalized := ""
			if not meta.get("pruned_exits", []).is_empty():
				normalized = "exit_pruned"
			elif meta.get("repositioned", false):
				normalized = "exit_conflict"
			if normalized != "":
				decision["normalize_reason"] = normalized
				telemetry_sink.enqueue_event("generation.normalized", run_id, room_req_id, decision)
			else:
				telemetry_sink.enqueue_event("generation.accepted", run_id, room_req_id, decision)
		elif source == "fallback":
			telemetry_sink.enqueue_event("generation.fallback_applied", run_id, room_req_id, {
				"provider": meta.get("failed_provider", "unknown"),
				"model": meta.get("failed_model", "unknown"),
				"fallback_reason": meta.get("telemetry_fallback_reason", "rejected_by_game"),
			})
		telemetry_sink.enqueue_event("room.committed", run_id, room_req_id, committed_attrs)

		# 3. frontier.discovered for newly created unresolved exits (null request_id)
		for exit_entry in exits:
			var dir: String = exit_entry.direction
			var f_key := frontier_key(room.room_id, dir)
			var f_rec: Dictionary = frontiers.get(f_key, {})
			if f_rec.get("status") == STATUS_UNRESOLVED:
				telemetry_sink.enqueue_event(
					"frontier.discovered",
					run_id,
					null,
					{"frontier_id": f_key, "depth": 1, "exit_direction": dir}
				)

	return record
