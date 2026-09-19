class_name RulesTransport
extends RefCounted
## Offline stand-in for the director that answers with the local rules baseline
## (Issue #16). Unlike OfflineTransport (which fails every request so the game
## falls back), this answers each request with a *valid canonical response*
## carrying a RulesBaseline plan, so rooms count as director-sourced and the
## fallback frequency measures only real failures.
##
## Implements the transport interface documented on OfflineTransport. Delivery
## happens in poll(), never inside submit(). The rules baseline is the same
## deterministic in-game code the coordinator falls back to, mirroring the
## director's `rules-baseline` provider without any process or network.
##
## Optional deterministic fault profiles (by 1-based request index):
##   "none"   every request succeeds
##   "flaky"  a repeating mix of hangs, invalid JSON, failure envelopes,
##            duplicate deliveries, id mismatches and missing backlinks
##   "hang"   nothing ever answers (exercises timeouts and stall detection)

const RulesBaseline = preload("res://world/rules_baseline.gd")

const FIXED_TIME := "2026-01-01T00:00:00Z"

var faults := "none"
var provider := "rules-baseline"
var model := "builtin-v1"
## Requests accepted so far.
var served := 0

var _queue: Array[Dictionary] = []


static func fault_for(index: int, profile: String) -> String:
	if profile == "hang":
		return "hang"
	if profile != "flaky":
		return "ok"
	if index % 7 == 3:
		return "hang"
	if index % 11 == 5:
		return "invalid_json"
	if index % 13 == 6:
		return "failure_envelope"
	if index % 17 == 8:
		return "duplicate"
	if index % 19 == 9:
		return "id_mismatch"
	if index % 23 == 10:
		return "missing_backlink"
	return "ok"


static func plan_for_request(request: Dictionary, room_id: String) -> Dictionary:
	return RulesBaseline.plan_for(request, room_id, "standard")


static func metadata(provider_id: String, model_id: String, success: bool) -> Dictionary:
	var meta := {
		"provider": provider_id,
		"model": model_id,
		"started_at": FIXED_TIME,
		"completed_at": FIXED_TIME,
		"latency_ms": 0.0,
	}
	if not success:
		meta["error"] = {"code": "provider_error", "message": "Injected provider failure."}
	return meta


static func success_body(request: Dictionary, plan: Dictionary, provider_id: String, model_id: String) -> String:
	return JSON.stringify({
		"contract_version": "1.0.0",
		"request_id": request.request_id,
		"run_id": request.run_id,
		"success": true,
		"room": plan,
		"metadata": metadata(provider_id, model_id, true),
	})


static func failure_body(request: Dictionary, provider_id: String, model_id: String) -> String:
	return JSON.stringify({
		"contract_version": "1.0.0",
		"request_id": request.request_id,
		"run_id": request.run_id,
		"success": false,
		"room": null,
		"metadata": metadata(provider_id, model_id, false),
	})


func submit(request: Dictionary, _options: Dictionary, on_done: Callable) -> void:
	served += 1
	_queue.append({"request": request, "on_done": on_done, "index": served})


func poll() -> void:
	var ready := _queue
	_queue = []
	for item in ready:
		_deliver(item.request, item.on_done, item.index)


func cancel_all() -> void:
	_queue.clear()


func _deliver(request: Dictionary, on_done: Callable, index: int) -> void:
	var fault := fault_for(index, faults)
	if fault == "hang":
		return
	var plan := plan_for_request(request, "rb-%d" % index)
	match fault:
		"invalid_json":
			on_done.call(_ok("{not json"))
		"failure_envelope":
			on_done.call(_ok(failure_body(request, provider, model), 503))
		"id_mismatch":
			var other := request.duplicate()
			other["request_id"] = "req-elsewhere"
			on_done.call(_ok(success_body(other, plan, provider, model)))
		"missing_backlink":
			var back: String = RulesBaseline.OPPOSITE.get(request.target_exit.direction, "south")
			var exits: Array = []
			for direction in RulesBaseline.CARDINALS:
				if direction != back and exits.size() < 2:
					exits.append({"direction": direction, "kind": "door", "locked": false})
			plan["exits"] = exits
			on_done.call(_ok(success_body(request, plan, provider, model)))
		"duplicate":
			var result := _ok(success_body(request, plan, provider, model))
			on_done.call(result)
			on_done.call(result)
		_:
			on_done.call(_ok(success_body(request, plan, provider, model)))


static func _ok(body: String, status: int = 200) -> Dictionary:
	return {"transport_ok": true, "error_kind": "", "http_status": status, "body": body}
