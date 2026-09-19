class_name SimulationDataset
extends RefCounted
## Serialisation of a simulation dataset (Issue #16).
##
## A dataset is five UTF-8 files, all plain JSON so any tool can read them:
##   manifest.json   what was run: configuration, provider/model, seeds
##   steps.jsonl     one JSON object per generation step
##   rooms.jsonl     one JSON object per committed room
##   failures.jsonl  one JSON object per detected failure / conflict
##   summary.json    per-run and overall metrics and the pass/fail verdict
## JSONL files hold one object per line and end with a newline. Keys are sorted
## so identical runs produce identical bytes. The format is specified in
## benchmarks/simulation/README.md and benchmarks/simulation/schemas/.

const SCHEMA_VERSION := "1.0.0"
const FILE_NAMES := {
	"manifest": "manifest.json",
	"steps": "steps.jsonl",
	"rooms": "rooms.jsonl",
	"failures": "failures.jsonl",
	"summary": "summary.json",
}
const MARKER := "manifest.json"


## Serialise to {file name: text}. `include_engine` false drops the engine
## version so fixtures stay stable across Godot patch releases.
static func to_files(dataset: Dictionary, include_engine: bool = true) -> Dictionary:
	var manifest: Dictionary = dataset.manifest.duplicate(true)
	if not include_engine:
		manifest.erase("engine")
	return {
		FILE_NAMES.manifest: JSON.stringify(manifest, "  ") + "\n",
		FILE_NAMES.steps: _jsonl(dataset.steps),
		FILE_NAMES.rooms: _jsonl(dataset.rooms),
		FILE_NAMES.failures: _jsonl(dataset.failures),
		FILE_NAMES.summary: JSON.stringify(dataset.summary, "  ") + "\n",
	}


## Empty string when `out_dir` can receive a new dataset, else the reason.
static func check_target(out_dir: String) -> String:
	if out_dir.strip_edges() == "":
		return "no output directory given"
	if FileAccess.file_exists(out_dir.path_join(MARKER)):
		return "%s already contains a simulation dataset; choose a new --out directory" % out_dir
	return ""


static func write(dataset: Dictionary, out_dir: String) -> Dictionary:
	var blocked := check_target(out_dir)
	if blocked != "":
		return {"ok": false, "error": blocked}
	var made := DirAccess.make_dir_recursive_absolute(out_dir)
	if made != OK and not DirAccess.dir_exists_absolute(out_dir):
		return {"ok": false, "error": "cannot create %s (error %d)" % [out_dir, made]}
	var files := to_files(dataset)
	# The manifest goes last: its presence marks a complete dataset.
	var names: Array = files.keys().filter(func(n: String) -> bool: return n != FILE_NAMES.manifest)
	names.append(FILE_NAMES.manifest)
	for name in names:
		var handle := FileAccess.open(out_dir.path_join(name), FileAccess.WRITE)
		if handle == null:
			return {"ok": false, "error": "cannot write %s (error %d)" % [out_dir.path_join(name), FileAccess.get_open_error()]}
		handle.store_string(files[name])
		handle.close()
	return {"ok": true, "files": names}


static func _jsonl(rows: Array) -> String:
	var lines := PackedStringArray()
	for row in rows:
		lines.append(JSON.stringify(row))
	return "" if lines.is_empty() else "\n".join(lines) + "\n"
