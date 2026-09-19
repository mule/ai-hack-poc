class_name GenerationClient
extends Node
## Provider-neutral HTTP client for the dungeon director's POST /v1/generate.
##
## Sends the canonical GenerationRequest as JSON and hands back the raw
## response body. It knows nothing about providers or models beyond passing
## optional stable ids as the `provider` / `model` query parameters (the
## director's selectors); omit them for the director's defaults.
##
## Fully asynchronous: submit() returns immediately and completion is reported
## through `on_done` from the HTTPRequest signal, so the game loop never blocks.
## Every request gets its own HTTPRequest child, so requests run concurrently.
##
## Implements the transport interface documented on OfflineTransport. The
## result dictionary is {transport_ok, error_kind, http_status, body}:
##   transport_ok=true   an HTTP response arrived (any status; the body is
##                       canonical for both success and failure envelopes)
##   error_kind          "timeout" | "transport_failure" | "request_failed"

const GENERATE_PATH := "/v1/generate"
const MAX_BODY_BYTES := 1_048_576

var base_url := "http://127.0.0.1:8000"
var timeout_sec := 5.0


func submit(request: Dictionary, options: Dictionary, on_done: Callable) -> void:
	var http := HTTPRequest.new()
	http.timeout = float(options.get("timeout_sec", timeout_sec))
	http.body_size_limit = MAX_BODY_BYTES
	add_child(http)
	http.request_completed.connect(_on_completed.bind(http, on_done), CONNECT_ONE_SHOT)
	var headers := PackedStringArray(["Content-Type: application/json", "Accept: application/json"])
	var err := http.request(_url(options), headers, HTTPClient.METHOD_POST, JSON.stringify(request))
	if err != OK:
		http.queue_free()
		on_done.call_deferred(_failure("request_failed"))


func poll() -> void:
	pass


func cancel_all() -> void:
	for child in get_children():
		if child is HTTPRequest:
			child.cancel_request()
			child.queue_free()


func _url(options: Dictionary) -> String:
	var url := base_url.rstrip("/") + GENERATE_PATH
	var params: PackedStringArray = []
	var provider := str(options.get("provider", "")).strip_edges()
	var model := str(options.get("model", "")).strip_edges()
	if provider != "":
		params.append("provider=" + provider.uri_encode())
	if model != "":
		params.append("model=" + model.uri_encode())
	if not params.is_empty():
		url += "?" + "&".join(params)
	return url


func _on_completed(result: int, response_code: int, _headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest, on_done: Callable) -> void:
	http.queue_free()
	if result == HTTPRequest.RESULT_TIMEOUT:
		on_done.call(_failure("timeout"))
	elif result != HTTPRequest.RESULT_SUCCESS:
		on_done.call(_failure("transport_failure"))
	else:
		on_done.call({"transport_ok": true, "error_kind": "", "http_status": response_code, "body": body.get_string_from_utf8()})


static func _failure(kind: String) -> Dictionary:
	return {"transport_ok": false, "error_kind": kind, "http_status": 0, "body": ""}
