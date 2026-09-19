class_name OfflineTransport
extends RefCounted
## Generation transport that never touches the network: every request "fails"
## on the next poll() with error_kind "offline", so the coordinator uses the
## local rules baseline. Deliberately asynchronous (delivery happens in poll(),
## never inside submit()) so it exercises the same non-blocking path as HTTP.
##
## Transport interface (duck-typed, shared with GenerationClient):
##   submit(request: Dictionary, options: Dictionary, on_done: Callable) -> void
##   poll() -> void
##   cancel_all() -> void
## on_done receives {transport_ok, error_kind, http_status, body}.

var _queue: Array[Callable] = []


func submit(_request: Dictionary, _options: Dictionary, on_done: Callable) -> void:
	_queue.append(on_done)


func poll() -> void:
	var ready := _queue
	_queue = []
	for on_done in ready:
		on_done.call({"transport_ok": false, "error_kind": "offline", "http_status": 0, "body": ""})


func cancel_all() -> void:
	_queue.clear()
