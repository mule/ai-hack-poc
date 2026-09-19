class_name MiniHttpServer
extends RefCounted
## Tiny non-blocking HTTP/1.1 server on 127.0.0.1 for exercising the real
## HTTP client without a live director. Call poll() every frame.
##   mode "reply": answer every request with response_status / response_body
##   mode "hang":  accept and read requests but never answer (timeout tests)
## In "reply" mode an optional `responder` Callable(request: Dictionary) ->
## {status, body} answers each request from its decoded JSON body instead.

var mode := "reply"
var response_status := 200
var response_body := "{}"
var responder: Callable = Callable()
var port := 0
## Array of {method, target, headers (lowercase keys), body}
var requests: Array[Dictionary] = []

var _server := TCPServer.new()
var _clients: Array[Dictionary] = []


func start() -> bool:
	if _server.listen(0, "127.0.0.1") != OK:
		return false
	port = _server.get_local_port()
	return port > 0


func stop() -> void:
	for client in _clients:
		client.peer.disconnect_from_host()
	_clients.clear()
	_server.stop()


func poll() -> void:
	while _server.is_connection_available():
		_clients.append({"peer": _server.take_connection(), "buffer": PackedByteArray(), "handled": false})
	for client in _clients:
		if client.handled:
			continue
		var peer: StreamPeerTCP = client.peer
		peer.poll()
		var available := peer.get_available_bytes()
		if available > 0:
			var chunk: PackedByteArray = peer.get_data(available)[1]
			client["buffer"] = (client["buffer"] as PackedByteArray) + chunk
		var parsed := _try_parse(client["buffer"])
		if parsed.is_empty():
			continue
		client.handled = true
		requests.append(parsed)
		if mode == "reply":
			var status := response_status
			var text := response_body
			if responder.is_valid():
				var decoded: Variant = JSON.parse_string(str(parsed.body))
				var answer: Dictionary = responder.call(decoded if decoded is Dictionary else {})
				status = int(answer.status)
				text = str(answer.body)
			var payload := text.to_utf8_buffer()
			var head := "HTTP/1.1 %d Status\r\nContent-Type: application/json\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % [status, payload.size()]
			peer.put_data(head.to_utf8_buffer())
			peer.put_data(payload)


func _try_parse(buffer: PackedByteArray) -> Dictionary:
	var text := buffer.get_string_from_utf8()
	var head_end := text.find("\r\n\r\n")
	if head_end < 0:
		return {}
	var lines := text.substr(0, head_end).split("\r\n")
	var request_line := lines[0].split(" ")
	var headers := {}
	for i in range(1, lines.size()):
		var idx := lines[i].find(":")
		if idx > 0:
			headers[lines[i].substr(0, idx).strip_edges().to_lower()] = lines[i].substr(idx + 1).strip_edges()
	var length := int(headers.get("content-length", "0"))
	var body_bytes := buffer.slice(head_end + 4)
	if body_bytes.size() < length:
		return {}
	return {
		"method": request_line[0],
		"target": request_line[1] if request_line.size() > 1 else "",
		"headers": headers,
		"body": body_bytes.get_string_from_utf8(),
	}
