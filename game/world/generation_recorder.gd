class_name GenerationRecorder
extends RefCounted
## Appends canonical generation inputs and committed resolutions to a JSONL dataset.
## Enabled when recording_path is set or via DUNGEON_GENERATION_LOG_PATH / DUNGEON_RECORD_DATASET.
## Non-fatal: write or serialization errors are logged and never crash gameplay.

var recording_path: String = ""
var _file: FileAccess = null


func _init(path: String = "") -> void:
	if path != "":
		recording_path = path
	else:
		recording_path = _get_default_path()
	if recording_path != "":
		_open_file()


static func _get_default_path() -> String:
	var path := OS.get_environment("DUNGEON_GENERATION_LOG_PATH").strip_edges()
	if path == "":
		path = OS.get_environment("DUNGEON_RECORD_DATASET").strip_edges()
	return path


func is_active() -> bool:
	return _file != null


func _open_file() -> void:
	if recording_path == "":
		return
	var dir := recording_path.get_base_dir()
	if dir != "" and not DirAccess.dir_exists_absolute(dir):
		DirAccess.make_dir_recursive_absolute(dir)
	var f := FileAccess.open(recording_path, FileAccess.READ_WRITE)
	if f == null:
		f = FileAccess.open(recording_path, FileAccess.WRITE)
	if f != null:
		f.seek_end()
		_file = f
	else:
		printerr("[GenerationRecorder] failed to open file for append: %s" % recording_path)


func record_entry(
	request: Dictionary,
	provider: String,
	model: String,
	outcome: String,
	source: String,
	seed_value: int,
	result_payload: Dictionary = {},
	fallback_reason: String = "",
	custom_meta: Dictionary = {},
	response_metadata: Dictionary = {}
) -> void:
	if _file == null:
		return
	var resolved_provider := provider
	var resolved_model := model
	if not response_metadata.is_empty():
		var resp_provider := str(response_metadata.get("provider", "")).strip_edges()
		if resp_provider != "":
			resolved_provider = resp_provider
		var resp_model := str(response_metadata.get("model", "")).strip_edges()
		if resp_model != "":
			resolved_model = resp_model

	var entry := {
		"contract_version": request.get("contract_version", "1.0.0"),
		"run_id": request.get("run_id", ""),
		"request_id": request.get("request_id", ""),
		"timestamp": Time.get_datetime_string_from_system(true, false) + "Z",
		"seed": seed_value,
		"provider": resolved_provider,
		"model": resolved_model,
		"outcome": outcome,
		"source": source,
		"target_exit": request.get("target_exit", {}),
		"request": request,
	}
	if fallback_reason != "":
		entry["fallback_reason"] = fallback_reason
	if not result_payload.is_empty():
		entry["result"] = result_payload
	if not custom_meta.is_empty():
		entry["metadata"] = custom_meta
	if not response_metadata.is_empty():
		entry["response_metadata"] = response_metadata

	var line := JSON.stringify(entry)
	_file.store_line(line)
	_file.flush()


func close() -> void:
	if _file != null:
		_file.close()
		_file = null
