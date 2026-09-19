class_name StubDirector
extends RefCounted
## Canonical-contract response builders and a deterministic varied plan source
## for tests. Stands in for a director; contains no provider-specific logic.

const OPPOSITE := {"north": "south", "south": "north", "east": "west", "west": "east"}
const CARDINALS := ["north", "south", "east", "west"]
const TYPE_SIZES := {
	"room": ["small", "medium", "large"],
	"corridor": ["tiny", "small"],
	"cavern": ["medium", "large", "huge"],
	"chamber": ["medium", "large"],
	"vault": ["small", "medium"],
	"shrine": ["tiny", "small"],
	"treasure": ["small", "medium"],
	"shop": ["small", "medium"],
}


static func metadata(success: bool) -> Dictionary:
	var meta := {
		"provider": "stub",
		"model": "stub-1",
		"started_at": "2026-09-19T10:00:00.100Z",
		"completed_at": "2026-09-19T10:00:00.104Z",
		"latency_ms": 4.0,
	}
	if not success:
		meta["error"] = {"code": "provider_error", "message": "Stub provider failed."}
	return meta


static func success_body(request: Dictionary, plan: Dictionary) -> String:
	return JSON.stringify(
		{
			"contract_version": "1.0.0",
			"request_id": request.request_id,
			"run_id": request.run_id,
			"success": true,
			"room": plan,
			"metadata": metadata(true),
		}
	)


static func failure_body(request: Dictionary) -> String:
	return JSON.stringify(
		{
			"contract_version": "1.0.0",
			"request_id": request.request_id,
			"run_id": request.run_id,
			"success": false,
			"room": null,
			"metadata": metadata(false),
		}
	)


static func ok_result(body: String, status: int = 200) -> Dictionary:
	return {"transport_ok": true, "http_status": status, "body": body}


static func success_result(request: Dictionary, plan: Dictionary) -> Dictionary:
	return ok_result(success_body(request, plan))


static func failure_result(kind: String) -> Dictionary:
	return {"transport_ok": false, "error_kind": kind, "http_status": 0, "body": ""}


static func exit_def(direction: String, kind: String = "door") -> Dictionary:
	return {"direction": direction, "kind": kind, "locked": false}


## A valid plan for `request`: backlink to the target frontier plus `extra` exits.
static func simple_plan(request: Dictionary, room_id: String, size: String, extra: Array = [], room_type: String = "room") -> Dictionary:
	var exits: Array = [exit_def(OPPOSITE[request.target_exit.direction])]
	for direction in extra:
		exits.append(exit_def(direction))
	return {
		"room_id": room_id,
		"depth": request.state.depth,
		"room_type": room_type,
		"size": size,
		"danger": 2,
		"exits": exits,
		"enemy_density": 0.15,
		"loot_density": 0.3,
		"secret_probability": 0.0,
		"description": "A stub-generated room.",
	}


## Deterministic, varied plan (all sizes/types, 0..3 extra exits) for simulations.
static func varied_plan(request: Dictionary, rng: RandomNumberGenerator, room_id: String) -> Dictionary:
	var types: Array = TYPE_SIZES.keys()
	var room_type: String = types[rng.randi_range(0, types.size() - 1)]
	var sizes: Array = TYPE_SIZES[room_type]
	var size: String = sizes[rng.randi_range(0, sizes.size() - 1)]
	var candidates: Array = []
	for direction in CARDINALS:
		if direction != OPPOSITE[request.target_exit.direction]:
			candidates.append(direction)
	var extra: Array = []
	var extra_count := rng.randi_range(0, 3)
	for i in range(extra_count):
		var idx := rng.randi_range(0, candidates.size() - 1)
		extra.append(candidates[idx])
		candidates.remove_at(idx)
	if extra.is_empty() and rng.randf() < 0.7:
		extra.append(candidates[rng.randi_range(0, candidates.size() - 1)])
	return simple_plan(request, room_id, size, extra, room_type)
