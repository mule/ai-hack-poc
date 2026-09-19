class_name RulesBaseline
extends RefCounted
## Local, deterministic rules-baseline RoomPlan (Issue #7 fallback).
##
## Used when the director cannot deliver a usable room (timeout, transport
## failure, invalid/failed response, placement rejection). Pure function of the
## request's run id and target frontier (never the request id or wall-clock), so
## a given frontier always falls back to the same plan. Mirrors the frontier
## semantics of the director's rules provider: exactly one exit facing back at
## the target, every other exit a distinct cardinal direction.

const OPPOSITE := {"north": "south", "south": "north", "east": "west", "west": "east"}
const CARDINALS := ["north", "east", "south", "west"]
## [room_type, size] pairs chosen by the seeded RNG for the "standard" variant.
const CHOICES := [
	["room", "small"],
	["room", "medium"],
	["corridor", "tiny"],
	["chamber", "medium"],
	["room", "small"],
	["corridor", "small"],
]


## Smallest possible room (tiny, one exit facing `back`): the last resort
## before an exit is sealed, and the probe used to verify a breach fits.
static func dead_end_plan(room_id: String, depth: int, back: String) -> Dictionary:
	return {
		"room_id": room_id,
		"depth": depth,
		"room_type": "room",
		"size": "tiny",
		"danger": 1,
		"exits": [{"direction": back, "kind": "door", "locked": false}],
		"enemy_density": 0.0,
		"loot_density": 0.1,
		"secret_probability": 0.0,
		"description": "Local rules-baseline room (minimal).",
	}


## `variant` is "standard" (varied room, 1-2 extra exits) or "minimal" (tiny
## dead-end room used as the last resort before an exit is sealed).
static func plan_for(request: Dictionary, room_id: String, variant: String = "standard") -> Dictionary:
	var target: Dictionary = request.target_exit
	var back: String = OPPOSITE.get(target.direction, "south")
	var depth: int = int(request.state.depth)
	var rng := RandomNumberGenerator.new()
	rng.seed = ("%s|%s|%s|%s" % [request.run_id, target.room_id, target.direction, variant]).hash()

	var room_type := "room"
	var size := "tiny"
	var extra: Array = []
	if variant != "minimal":
		var choice: Array = CHOICES[rng.randi_range(0, CHOICES.size() - 1)]
		room_type = choice[0]
		size = choice[1]
		var candidates: Array = []
		for direction in CARDINALS:
			if direction != back:
				candidates.append(direction)
		var extra_count := 1 if room_type == "corridor" else rng.randi_range(1, 2)
		for i in range(extra_count):
			var idx := rng.randi_range(0, candidates.size() - 1)
			extra.append(candidates[idx])
			candidates.remove_at(idx)

	var exits: Array = [{"direction": back, "kind": "door", "locked": false}]
	for direction in extra:
		var kind := "passage" if rng.randf() < 0.25 else "door"
		exits.append({"direction": direction, "kind": kind, "locked": false})
	return {
		"room_id": room_id,
		"depth": depth,
		"room_type": room_type,
		"size": size,
		"danger": clampi(1 + depth / 3, 1, 5),
		"exits": exits,
		"enemy_density": 0.0 if variant == "minimal" else 0.12,
		"loot_density": 0.1 if variant == "minimal" else 0.2,
		"secret_probability": 0.0,
		"description": "Local rules-baseline room (%s)." % variant,
	}
