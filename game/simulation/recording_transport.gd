class_name RecordingTransport
extends RefCounted
## Transport decorator for the simulation harness (Issue #16).
##
## Wraps any generation transport (submit/poll/cancel_all) to
##   * record per-request telemetry: outcome, HTTP status, the provider/model
##     the *response* reported, token usage and estimated cost when given, and
##     latency when a real clock is supplied;
##   * enforce a hard request budget: once `budget` requests have been passed
##     to the wrapped transport, further requests are never forwarded. They are
##     answered on the next poll() with error_kind "budget_exhausted" so the
##     coordinator falls back locally at zero cost.
##
## The decorator is transparent to the coordinator: results are forwarded
## unchanged, and delivery stays asynchronous.

const BUDGET_ERROR := "budget_exhausted"

var inner: Variant
## Requests forwarded to the wrapped transport.
var submitted := 0
## Requests refused locally because the budget was spent.
var refused := 0
## request_id -> record dictionary (see _new_record).
var records: Dictionary = {}

var _clock: Callable
var _budget := -1
var _refusals: Array[Dictionary] = []


## `clock` returns milliseconds (leave invalid to skip latency measurement);
## `budget` < 0 means unlimited.
func _init(wrapped: Variant, clock: Callable = Callable(), budget: int = -1) -> void:
	inner = wrapped
	_clock = clock
	_budget = budget


func budget_exhausted() -> bool:
	return _budget >= 0 and submitted >= _budget


func record_for(request_id: String) -> Dictionary:
	return records.get(request_id, {})


func submit(request: Dictionary, options: Dictionary, on_done: Callable) -> void:
	var request_id := str(request.get("request_id", ""))
	var record := _new_record()
	records[request_id] = record
	if budget_exhausted():
		refused += 1
		record.refused = true
		record.error_kind = BUDGET_ERROR
		_refusals.append({"record": record, "on_done": on_done})
		return
	submitted += 1
	inner.submit(request, options, _on_inner_done.bind(record, on_done))


func poll() -> void:
	inner.poll()
	var pending := _refusals
	_refusals = []
	for item in pending:
		var result := {"transport_ok": false, "error_kind": BUDGET_ERROR, "http_status": 0, "body": ""}
		item.on_done.call(result)


func cancel_all() -> void:
	_refusals.clear()
	inner.cancel_all()


func _now() -> int:
	return _clock.call() if _clock.is_valid() else -1


func _new_record() -> Dictionary:
	return {
		"submitted_msec": _now(),
		"completed": false,
		"refused": false,
		"transport_ok": false,
		"error_kind": "",
		"http_status": 0,
		"provider": null,
		"model": null,
		"latency_ms": null,
		"reported_latency_ms": null,
		"input_tokens": null,
		"output_tokens": null,
		"cost_usd": null,
	}


func _on_inner_done(result: Dictionary, record: Dictionary, on_done: Callable) -> void:
	if not record.completed:  # a duplicate delivery must not overwrite the first
		record.completed = true
		record.transport_ok = bool(result.get("transport_ok", false))
		record.error_kind = str(result.get("error_kind", ""))
		record.http_status = int(result.get("http_status", 0))
		if _clock.is_valid() and int(record.submitted_msec) >= 0:
			record.latency_ms = _now() - int(record.submitted_msec)
		_read_metadata(record, str(result.get("body", "")))
	on_done.call(result)


## Best-effort, silent read of the response metadata (the coordinator does the
## strict contract validation; this only feeds telemetry).
func _read_metadata(record: Dictionary, body: String) -> void:
	if not body.strip_edges().begins_with("{"):
		return
	var parser := JSON.new()
	if parser.parse(body) != OK or not (parser.data is Dictionary):
		return
	var meta: Variant = parser.data.get("metadata")
	if not (meta is Dictionary):
		return
	if meta.get("provider") is String:
		record.provider = meta.provider
	if meta.get("model") is String:
		record.model = meta.model
	if meta.get("latency_ms") is float or meta.get("latency_ms") is int:
		record.reported_latency_ms = meta.latency_ms
	var usage: Variant = meta.get("usage")
	if usage is Dictionary:
		for field in [["input_tokens", "input_tokens"], ["output_tokens", "output_tokens"], ["estimated_cost_usd", "cost_usd"]]:
			var value: Variant = usage.get(field[0])
			if value is float or value is int:
				record[field[1]] = value
