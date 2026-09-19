# Local development workflow. Run `make help` for the target list.

PYTHON       ?= python3
VENV         ?= director/.venv
VENV_BIN     := $(VENV)/bin
DIRECTOR_HOST ?= 127.0.0.1
DIRECTOR_PORT ?= 8000
GODOT        ?= godot
GODOT_LOG_DIR ?= /tmp

# Godot exits 0 even when scripts fail to parse or hit runtime errors, so every
# headless run writes an explicit log and that log is scanned afterwards.
# Only `SCRIPT ERROR:` and `Parse Error:` fail the run; a generic `ERROR:` line
# is allowed (the contract test feeds deliberately invalid JSON).
# `Failed to load script` is scanned too: it is what a script that does not
# compile reports when it is loaded indirectly (e.g. a scene's script).
# Optional third argument: a success sentinel that must appear in the log, so a
# run that never reaches its own final verdict cannot pass on exit code alone.
# `--log-file` precedes the other arguments so flags after a `--` (user
# arguments for a script, e.g. `make simulate`) are not swallowed.
# Usage: $(call godot_run,<godot args>,<log file>[,<success sentinel>])
define godot_run
rm -f $(2); \
$(GODOT) --headless --path game --log-file $(2) $(1); rc=$$?; \
test -f $(2) || { echo "FAIL: no Godot log written to $(2)"; exit 1; }; \
if grep -nE 'SCRIPT ERROR:|Parse Error:|Failed to load script' $(2); then echo "FAIL: script errors found in $(2)"; exit 1; fi; \
if test -n "$(3)" && ! grep -qF "$(3)" $(2); then echo "FAIL: success sentinel '$(3)' missing from $(2)"; exit 1; fi; \
exit $$rc
endef

.DEFAULT_GOAL := help
# Headless dungeon simulation (issue #16). Output is generated data: it goes to
# the git-ignored simulation-output/ directory, one timestamped folder per run.
SIM_STAMP := $(shell date -u +%Y%m%dT%H%M%SZ)
SIM_OUT   ?= $(CURDIR)/simulation-output/$(SIM_STAMP)
SIM_ARGS  ?=

.PHONY: help need-venv setup run-director test lint format check replay-benchmark godot-check godot-test godot-lint simulate export-linux export-android clean

help: ## List available targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-14s %s\n", $$1, $$2}'

$(VENV_BIN)/python:
	$(PYTHON) -m venv $(VENV)

need-venv:
	@test -x $(VENV_BIN)/python || { echo "$(VENV) not found: run 'make setup' first"; exit 1; }

setup: $(VENV_BIN)/python ## Create director/.venv and install the director with dev tools
	$(VENV_BIN)/python -m pip install --upgrade pip
	$(VENV_BIN)/python -m pip install -e "director[dev]"

run-director: need-venv ## Start the director (default 127.0.0.1:8000) with auto-reload
	$(VENV_BIN)/python -m uvicorn dungeon_director.app:app --app-dir director \
		--host $(DIRECTOR_HOST) --port $(DIRECTOR_PORT) --reload

test: need-venv ## Run the director tests
	cd director && ../$(VENV_BIN)/python -m pytest

lint: need-venv ## Lint and format-check the director (ruff)
	cd director && ../$(VENV_BIN)/python -m ruff check . && ../$(VENV_BIN)/python -m ruff format --check .

format: need-venv ## Auto-fix and format the director (ruff)
	cd director && ../$(VENV_BIN)/python -m ruff check --fix . && ../$(VENV_BIN)/python -m ruff format .

check: lint test ## Python lint + tests (no Godot required)

replay-benchmark: need-venv ## Run replay benchmark against rules-baseline on sample fixture
	$(VENV_BIN)/python -m benchmarks.replay --input benchmarks/fixtures/sample_run.jsonl --providers rules-baseline

godot-check: ## Headless-load the Godot project; fails on script parse/runtime errors in the log
	@test -f game/project.godot || { echo "game/project.godot not found: the Godot shell is not in this checkout yet"; exit 1; }
	@$(call godot_run,--quit-after 10,$(GODOT_LOG_DIR)/godot-load.log)

godot-test: ## Run Godot tests headlessly (mechanics plus optional contract, generator, deferred-generation, and smoke suites)
	@test -f game/project.godot || { echo "game/project.godot not found: the Godot shell is not in this checkout yet"; exit 1; }
	@test -f game/tests/test_mechanics.gd || { echo "game/tests/test_mechanics.gd not found"; exit 1; }
	@$(call godot_run,-s res://tests/test_mechanics.gd,$(GODOT_LOG_DIR)/godot-mechanics.log)
	@if test -f game/tests/test_contracts.gd; then \
		echo "Running shared-contract test"; \
		$(call godot_run,-s res://tests/test_contracts.gd,$(GODOT_LOG_DIR)/godot-contracts.log); \
	else \
		echo "game/tests/test_contracts.gd not present: skipping shared-contract test"; \
	fi
	@if test -f game/tests/test_room_generator.gd; then \
		echo "Running room-generator test"; \
		$(call godot_run,-s res://tests/test_room_generator.gd,$(GODOT_LOG_DIR)/godot-room-generator.log); \
	else \
		echo "game/tests/test_room_generator.gd not present: skipping room-generator test"; \
	fi
	@if test -f game/tests/test_deferred_world.gd; then \
		echo "Running deferred-generation world test (#7)"; \
		$(call godot_run,-s res://tests/test_deferred_world.gd,$(GODOT_LOG_DIR)/godot-deferred-world.log,SUCCESS: All deferred world checks passed); \
	else \
		echo "game/tests/test_deferred_world.gd not present: skipping deferred world test"; \
	fi
	@if test -f game/tests/test_deferred_generation.gd; then \
		echo "Running deferred-generation coordinator/client/scene test (#7)"; \
		$(call godot_run,-s res://tests/test_deferred_generation.gd,$(GODOT_LOG_DIR)/godot-deferred-generation.log,SUCCESS: All deferred generation checks passed); \
	else \
		echo "game/tests/test_deferred_generation.gd not present: skipping deferred generation test"; \
	fi
	@if test -f game/tests/test_deferred_simulation.gd; then \
		echo "Running deferred-generation simulation (#7)"; \
		$(call godot_run,-s res://tests/test_deferred_simulation.gd,$(GODOT_LOG_DIR)/godot-deferred-simulation.log,SUCCESS: All deferred simulation checks passed); \
	else \
		echo "game/tests/test_deferred_simulation.gd not present: skipping deferred simulation"; \
	fi
	@if test -f game/tests/test_simulation_harness.gd; then \
		echo "Running dungeon simulation harness test (#16)"; \
		$(call godot_run,-s res://tests/test_simulation_harness.gd,$(GODOT_LOG_DIR)/godot-simulation-harness.log,SUCCESS: All simulation harness checks passed); \
	else \
		echo "game/tests/test_simulation_harness.gd not present: skipping simulation harness test"; \
	fi
	@if test -f game/tests/test_scene_smoke.gd; then \
		echo "Running scene smoke test"; \
		$(call godot_run,-s res://tests/test_scene_smoke.gd,$(GODOT_LOG_DIR)/godot-scene-smoke.log); \
	else \
		echo "game/tests/test_scene_smoke.gd not present: skipping scene smoke test"; \
	fi

simulate: ## Headless dungeon simulation: offline rules baseline, 5 runs x 100 steps. SIM_ARGS='...' SIM_OUT=dir
	@test -f game/simulation/run_simulation.gd || { echo "game/simulation/run_simulation.gd not found"; exit 1; }
	@$(call godot_run,-s res://simulation/run_simulation.gd -- --out "$(SIM_OUT)" $(SIM_ARGS),$(GODOT_LOG_DIR)/godot-simulation.log,SIMULATION COMPLETE)

godot-lint: ## Lint GDScript with gdlint (pip install gdtoolkit)
	@command -v gdlint >/dev/null || { echo "gdlint not found: pip install gdtoolkit"; exit 1; }
	@test -d game || { echo "game/ not found: the Godot shell is not in this checkout yet"; exit 1; }
	gdlint game

export-linux: ## Export headless Linux x86_64 debug build and package tar.gz
	@test -f game/project.godot || { echo "game/project.godot not found: the Godot shell is not in this checkout yet"; exit 1; }
	mkdir -p game/builds/linux
	$(GODOT) --headless --path game --export-debug "Linux Desktop" builds/linux/ai-hack-poc.x86_64
	tar -czf game/builds/linux/ai-hack-poc-linux-x86_64.tar.gz -C game/builds/linux ai-hack-poc.x86_64 ai-hack-poc.pck ai-hack-poc.sh

export-android: ## Export headless Android debug APK
	@test -f game/project.godot || { echo "game/project.godot not found: the Godot shell is not in this checkout yet"; exit 1; }
	mkdir -p game/builds/android
	$(GODOT) --headless --path game --export-debug "Android Debug" builds/android/ai-hack-poc-debug.apk

clean: ## Remove the director virtualenv, Python caches, and build output
	rm -rf $(VENV) director/.pytest_cache director/.ruff_cache director/*.egg-info director/build director/dist game/builds
	find director -type d -name __pycache__ -prune -exec rm -rf {} +
