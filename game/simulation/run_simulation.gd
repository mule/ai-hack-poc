extends SceneTree
## Entry point for the headless dungeon simulation (Issue #16).
##
##   godot --headless --path game -s res://simulation/run_simulation.gd -- <flags>
##
## Normally run through `make simulate`. Flags are documented by `--help` and
## in benchmarks/simulation/README.md. Defaults to the offline rules baseline;
## real provider calls need an explicit --remote.

const SimulationCli = preload("res://simulation/simulation_cli.gd")


func _initialize() -> void:
	var outcome: Dictionary = await SimulationCli.execute(OS.get_cmdline_user_args(), self)
	quit(int(outcome.exit_code))
