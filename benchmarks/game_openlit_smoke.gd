extends SceneTree
## Real game/coordinator/client/sink path; no synthetic telemetry insertions.

const State = preload("res://src/game_state.gd")
const Client = preload("res://world/generation_client.gd")
const Coordinator = preload("res://world/generation_coordinator.gd")
const Sink = preload("res://world/game_telemetry_sink.gd")

func _initialize() -> void:
	create_timer(20.0).timeout.connect(func(): quit(2))
	_run.call_deferred()

func _run() -> void:
	var state = State.new()
	state.enable_dynamic_world(1)
	state.world.run_id = OS.get_environment("DUNGEON_SMOKE_RUN_ID")
	var sink = Sink.new(OS.get_environment("DUNGEON_DIRECTOR_URL"))
	root.add_child(sink)
	sink.enabled = true
	state.telemetry_sink = sink
	state.world.telemetry_sink = sink
	state.active_provider = "rules-baseline"
	var client = Client.new()
	client.base_url = OS.get_environment("DUNGEON_DIRECTOR_URL")
	root.add_child(client)
	var coordinator = Coordinator.new(state, client)
	coordinator.provider = "rules-baseline"
	coordinator.telemetry_sink = sink
	coordinator.max_in_flight = 1
	state.player_pos = Vector2i(4, 2)
	coordinator.update()
	while coordinator.recent_decisions.is_empty():
		await process_frame
	if coordinator.recent_decisions[0].source != "director":
		printerr("Game smoke unexpectedly used fallback")
		quit(1)
		return
	var initial_room: String = state.current_room_id
	for i in range(3):
		state.player_action_step(Vector2i.UP)
	if state.current_room_id == initial_room:
		printerr("Game smoke failed to enter generated room")
		quit(1)
		return
	while sink.get_stats().events_sent < sink.get_stats().enqueued or is_instance_valid(sink._in_flight_http):
		if sink.get_stats().batches_failed > 0 or sink.get_stats().dropped_total > 0:
			printerr("Game smoke telemetry delivery failed")
			quit(1)
			return
		await process_frame
	if sink.get_stats().batches_failed > 0 or sink.get_stats().dropped_total > 0:
		quit(1)
		return
	var evidence := {
		"run_id": state.world.run_id,
		"room_id": state.current_room_id,
		"provider": coordinator.last_provider,
		"model": coordinator.last_model,
		"events_sent": sink.get_stats().events_sent,
		"rooms": state.world.rooms.size(),
	}
	print("GAME_SMOKE_RESULT=" + JSON.stringify(evidence))
	coordinator.shutdown()
	sink.shutdown()
	quit(0)
