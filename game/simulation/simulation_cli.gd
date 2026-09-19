class_name SimulationCli
extends RefCounted
## Command-line driver for the simulation harness (Issue #16).
##
## Exit codes: 0 completed without error-severity failures; 1 the run finished
## but detected failures (the dataset is still written); 2 usage or output
## error (nothing was run). The remote cost warning is printed to stderr before
## a single request can be sent.

const SimulationConfig = preload("res://simulation/simulation_config.gd")
const SimulationDataset = preload("res://simulation/simulation_dataset.gd")
const SimulationHarness = preload("res://simulation/simulation_harness.gd")

const ENV_NAMES := ["DUNGEON_DIRECTOR_URL", "DUNGEON_DIRECTOR_PROVIDER", "DUNGEON_DIRECTOR_MODEL"]


## Run one invocation. Returns {exit_code, warning_shown, warning, out_dir}.
static func execute(args: PackedStringArray, tree: SceneTree) -> Dictionary:
	var outcome := {"exit_code": 2, "warning_shown": false, "warning": "", "out_dir": ""}
	var env := {}
	for name in ENV_NAMES:
		env[name] = OS.get_environment(name)
	var parsed := SimulationConfig.parse(args, env)
	if not parsed.ok:
		printerr("simulate: %s" % parsed.error)
		printerr("Run with --help for usage.")
		return outcome
	if parsed.help:
		print(SimulationConfig.usage())
		outcome.exit_code = 0
		return outcome
	var config: SimulationConfig = parsed.config
	outcome.out_dir = config.out_dir
	var blocked := SimulationDataset.check_target(config.out_dir)
	if blocked != "":
		printerr("simulate: %s" % blocked)
		return outcome

	var warning := config.cost_warning()
	if warning != "":
		outcome.warning = warning
		outcome.warning_shown = true
		printerr(warning)

	var provider := config.provider_summary()
	print("simulate: %d run(s) x %d step(s), seed %d, provider %s / %s%s" % [config.runs, config.steps, config.base_seed, str(provider.provider), str(provider.model), "" if provider.endpoint == null else " @ " + str(provider.endpoint)])
	var harness := SimulationHarness.new(config, tree)
	var dataset: Dictionary = await harness.run()
	var written := SimulationDataset.write(dataset, config.out_dir)
	if not written.ok:
		printerr("simulate: %s" % written.error)
		return outcome
	_print_report(dataset, config.out_dir)
	outcome.exit_code = 0 if dataset.summary.overall.passed else 1
	return outcome


static func _print_report(dataset: Dictionary, out_dir: String) -> void:
	var overall: Dictionary = dataset.summary.overall
	print("")
	print("run  status            steps  rooms  explored  fallback  errors  warnings")
	for run in dataset.summary.runs:
		print("%3d  %-16s  %5d  %5d  %8d  %7.1f%%  %6d  %8d" % [run.run, run.status, run.steps, run.rooms_committed, run.rooms_explored, float(run.fallback_frequency) * 100.0, run.failures.error, run.failures.warning])
	print("")
	print("overall: %d steps, %d rooms (%d explored), fallback %.1f%%, danger mean %s (slope %s), failures %s" % [overall.steps, overall.rooms_committed, overall.rooms_explored, float(overall.fallback_frequency) * 100.0, str(overall.danger.mean), str(overall.danger.mean_slope), str(overall.failures)])
	if overall.truncated != null:
		print("truncated: %s" % str(overall.truncated))
	print("dataset: %s" % out_dir)
	print("SIMULATION COMPLETE: %s" % ("PASSED" if overall.passed else "FAILED (see failures.jsonl)"))
