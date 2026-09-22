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
##   error_kind          "timeout" | "transport_failure" | "request_failed" |
##                       "cancelled" (config requests only)

const GENERATE_PATH := "/v1/generate"
const CONFIG_PATH := "/v1/config"
const MAX_BODY_BYTES := 1_048_576

var base_url := "http://127.0.0.1:8000"
var timeout_sec := 5.0
## HTTPRequest -> {on_done: Callable, report_cancel: bool}. Generation requests
## deliberately stay silent on coordinator shutdown, while config requests need
## a terminal result so the provider selector can leave its fetching state.
var _pending: Dictionary = {}


func fetch_config(on_done: Callable) -> void:
	var http := HTTPRequest.new()
	http.timeout = timeout_sec
	http.body_size_limit = MAX_BODY_BYTES
	add_child(http)
	_pending[http] = {"on_done": on_done, "report_cancel": true}
	http.request_completed.connect(_on_completed.bind(http), CONNECT_ONE_SHOT)
	var headers := PackedStringArray(["Accept: application/json"])
	var url := base_url.rstrip("/") + CONFIG_PATH
	var err := http.request(url, headers, HTTPClient.METHOD_GET)
	if err != OK:
		_pending.erase(http)
		http.queue_free()
		on_done.call_deferred(_failure("request_failed"))



func submit(request: Dictionary, options: Dictionary, on_done: Callable) -> void:
	var http := HTTPRequest.new()
	http.timeout = float(options.get("timeout_sec", timeout_sec))
	http.body_size_limit = MAX_BODY_BYTES
	add_child(http)
	_pending[http] = {"on_done": on_done, "report_cancel": false}
	http.request_completed.connect(_on_completed.bind(http), CONNECT_ONE_SHOT)
	var headers := PackedStringArray(["Content-Type: application/json", "Accept: application/json"])
	var err := http.request(_url(options), headers, HTTPClient.METHOD_POST, JSON.stringify(request))
	if err != OK:
		_pending.erase(http)
		http.queue_free()
		on_done.call_deferred(_failure("request_failed"))


func poll() -> void:
	pass


func cancel_all() -> void:
	var pending := _pending.duplicate()
	_pending.clear()
	for http: HTTPRequest in pending:
		var item: Dictionary = pending[http]
		if is_instance_valid(http):
			http.cancel_request()
			http.queue_free()
		if item.report_cancel:
			var on_done: Callable = item.on_done
			on_done.call_deferred(_failure("cancelled"))


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


func _on_completed(result: int, response_code: int, headers: PackedStringArray, body: PackedByteArray, http: HTTPRequest) -> void:
	if not _pending.has(http):
		return
	var item: Dictionary = _pending[http]
	_pending.erase(http)
	var on_done: Callable = item.on_done
	http.queue_free()
	if result == HTTPRequest.RESULT_TIMEOUT:
		on_done.call(_failure("timeout"))
	elif result != HTTPRequest.RESULT_SUCCESS:
		on_done.call(_failure("transport_failure"))
	else:
		on_done.call({"transport_ok": true, "error_kind": "", "http_status": response_code, "body": body.get_string_from_utf8(), "traceparent": _response_traceparent(headers)})


static func _failure(kind: String) -> Dictionary:
	return {"transport_ok": false, "error_kind": kind, "http_status": 0, "body": ""}


static func _response_traceparent(headers: PackedStringArray) -> Variant:
	var found: Variant = null
	for header in headers:
		var colon := header.find(":")
		if colon < 0 or header.left(colon).strip_edges().to_lower() != "traceparent":
			continue
		var value := header.substr(colon + 1).strip_edges()
		# Ambiguous or invalid context must not suppress the gameplay event.
		if found != null or not GameTelemetrySink._is_valid_traceparent(value):
			return null
		found = value
	return found
