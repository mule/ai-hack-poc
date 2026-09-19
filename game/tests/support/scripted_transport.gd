class_name ScriptedTransport
extends RefCounted
## Test double for the generation transport interface:
##   submit(request, options, on_done), poll(), cancel_all()
## Nothing completes on its own: tests call deliver()/deliver_all() (or set
## `auto` to answer on poll), so asynchronous ordering is fully controlled.

## Array of {request, options, on_done, deliveries}
var submitted: Array[Dictionary] = []
## Array of Callables waiting for config
var config_requests: Array[Callable] = []
## Optional Callable(request) -> Dictionary result ({} = keep holding).
var auto: Callable = Callable()
## Optional config response dictionary to return automatically on poll
var auto_config: Variant = null
var cancel_calls := 0


func fetch_config(on_done: Callable) -> void:
	config_requests.append(on_done)


func submit(request: Dictionary, options: Dictionary, on_done: Callable) -> void:
	submitted.append({"request": request, "options": options, "on_done": on_done, "deliveries": 0})


func poll() -> void:
	if auto_config != null:
		var c_reqs := config_requests.duplicate()
		config_requests.clear()
		for cb in c_reqs:
			cb.call(auto_config)
	if not auto.is_valid():
		return
	for item in submitted:
		if item.deliveries == 0:
			var result: Dictionary = auto.call(item.request)
			if not result.is_empty():
				item.deliveries += 1
				item.on_done.call(result)



func cancel_all() -> void:
	cancel_calls += 1


## Complete submitted request `index` (again, if already delivered).
func deliver(index: int, result: Dictionary) -> void:
	var item: Dictionary = submitted[index]
	item.deliveries += 1
	item.on_done.call(result)


func deliver_config(result: Dictionary) -> void:
	var reqs := config_requests.duplicate()
	config_requests.clear()
	for cb in reqs:
		cb.call(result)



func request_ids() -> Array[String]:
	var ids: Array[String] = []
	for item in submitted:
		ids.append(item.request.request_id)
	return ids


func target_keys() -> Array[String]:
	var keys: Array[String] = []
	for item in submitted:
		keys.append("%s:%s" % [item.request.target_exit.room_id, item.request.target_exit.direction])
	return keys
