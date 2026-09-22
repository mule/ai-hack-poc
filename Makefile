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

# Provider/model summary of replay results (issue #15). BENCH_REPORT takes one or
# more .json/.jsonl files produced by `python -m benchmarks.replay --output`.
BENCH_REPORT ?= benchmarks/fixtures/sample_replay_results.json
BENCH_FORMAT ?= text
BENCH_OUT    ?=

# POC evaluation protocol (issue #18); see docs/evaluation-methodology.md.
# Output is generated evidence in the git-ignored evaluation-output/. The report
# gets its latency/reliability/token tables from the issue #15 summarizer
# (`make benchmark-summary`), run on the bundle's own results.json.
EVAL_CORPUS     ?= benchmarks/corpus/replay-v1/corpus.json
EVAL_TIER       ?= full
EVAL_OUT        ?=
EVAL_ARGS       ?=
EVAL_SELECT     ?=
EVAL_LIVE       ?= 0
EVAL_LIVE_TIER  ?= standard
EVAL_BUNDLE     ?=
EVAL_SIM_ARGS   := --runs 5 --steps 30 --seed 100
EVAL_CORPUS_OUT ?= $(CURDIR)/evaluation-output/corpus-rebuild
EVAL_SCRATCH    := $(CURDIR)/evaluation-output/corpus-scratch

.PHONY: eval-corpus-verify eval-corpus-build eval-corpus-rebuild eval-offline eval-live eval-report eval-verify help need-venv setup run-director test lint format check replay-benchmark benchmark-summary godot-check godot-test godot-lint simulate export-linux export-android clean

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

lint: need-venv ## Lint and format-check Python code (ruff)
	cd director && ../$(VENV_BIN)/python -m ruff check . && ../$(VENV_BIN)/python -m ruff format --check .
	$(VENV_BIN)/python -m ruff check --config director/pyproject.toml benchmarks
	$(VENV_BIN)/python -m ruff format --check --config director/pyproject.toml benchmarks

format: need-venv ## Auto-fix and format Python code (ruff)
	cd director && ../$(VENV_BIN)/python -m ruff check --fix . && ../$(VENV_BIN)/python -m ruff format .
	$(VENV_BIN)/python -m ruff check --fix --config director/pyproject.toml benchmarks
	$(VENV_BIN)/python -m ruff format --config director/pyproject.toml benchmarks

check: lint test ## Python lint + tests for director/ and benchmarks/ (no Godot required)

replay-benchmark: need-venv ## Run replay benchmark against rules-baseline on sample fixture
	$(VENV_BIN)/python -m benchmarks.replay --input benchmarks/fixtures/sample_run.jsonl --providers rules-baseline

benchmark-summary: need-venv ## Summarize replay results per provider/model. BENCH_REPORT='a.json b.jsonl' BENCH_FORMAT=text|json|csv BENCH_OUT=file
	@$(VENV_BIN)/python -m benchmarks.summarize --input $(BENCH_REPORT) --format $(BENCH_FORMAT) $(if $(BENCH_OUT),--output $(BENCH_OUT))

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
	@if test -f game/tests/test_provider_selector.gd; then \
		echo "Running provider selector and debug HUD test (#12)"; \
		$(call godot_run,-s res://tests/test_provider_selector.gd,$(GODOT_LOG_DIR)/godot-provider-selector.log,SUCCESS: All provider selector and debug HUD checks passed!); \
	else \
		echo "game/tests/test_provider_selector.gd not present: skipping provider selector test"; \
	fi

simulate: ## Headless dungeon simulation: offline rules baseline, 5 runs x 100 steps. SIM_ARGS='...' SIM_OUT=dir
	@test -f game/simulation/run_simulation.gd || { echo "game/simulation/run_simulation.gd not found"; exit 1; }
	@$(call godot_run,-s res://simulation/run_simulation.gd -- --out "$(SIM_OUT)" $(SIM_ARGS),$(GODOT_LOG_DIR)/godot-simulation.log,SIMULATION COMPLETE)

eval-corpus-verify: need-venv ## Check the fixed replay corpus against its checksummed manifest (offline)
	@$(VENV_BIN)/python -m benchmarks.evaluation corpus verify --corpus $(EVAL_CORPUS)

eval-corpus-build: need-venv ## Regenerate the replay corpus with Godot into EVAL_CORPUS_OUT (scratch by default)
	@command -v $(GODOT) >/dev/null || { echo "godot not found: needed to regenerate the corpus"; exit 1; }
	@rm -rf "$(EVAL_CORPUS_OUT)" "$(EVAL_SCRATCH)"
	@mkdir -p "$(EVAL_SCRATCH)"
	@DUNGEON_GENERATION_LOG_PATH="$(EVAL_SCRATCH)/recording.jsonl" $(MAKE) --no-print-directory simulate \
		SIM_OUT="$(EVAL_SCRATCH)/simulation" SIM_ARGS="$(EVAL_SIM_ARGS)" >/dev/null
	@$(VENV_BIN)/python -m benchmarks.evaluation corpus build \
		--from-recording "$(EVAL_SCRATCH)/recording.jsonl" --out "$(EVAL_CORPUS_OUT)" \
		--id replay-v1 --kind replay --stamp-offline-expected \
		--description "Fixed replay corpus: every generation request of a deterministic headless simulation (5 runs, seed 100). Offline; safe to commit." \
		--provenance generator="make eval-corpus-build (game/simulation, rules transport)" \
		--provenance simulation_args="$(EVAL_SIM_ARGS)" \
		--provenance godot_version="$$($(GODOT) --version)"

eval-corpus-rebuild: eval-corpus-build ## Prove the committed corpus reproduces: rebuild with Godot and compare bytes
	@cmp $(dir $(EVAL_CORPUS))requests.jsonl "$(EVAL_CORPUS_OUT)/requests.jsonl" \
		&& echo "corpus reproduces: rebuilt requests.jsonl is byte-identical to $(dir $(EVAL_CORPUS))requests.jsonl"

eval-offline: eval-corpus-verify ## Offline fixed-corpus protocol on rules-baseline (no credentials). EVAL_TIER= EVAL_OUT=dir
	@$(VENV_BIN)/python -m benchmarks.evaluation run --corpus $(EVAL_CORPUS) --tier $(EVAL_TIER) \
		--select rules-baseline --label offline-reproduction $(if $(EVAL_OUT),--out $(EVAL_OUT)) $(EVAL_ARGS)

eval-live: need-venv ## LIVE, BILLABLE: compare hosted providers. Needs EVAL_LIVE=1 and EVAL_SELECT
	@test "$(EVAL_LIVE)" = "1" || { echo "refusing: this makes billable calls to hosted providers. Re-run with EVAL_LIVE=1 EVAL_SELECT='typesafe-jev groq cerebras' (credentials in the environment)"; exit 1; }
	@test -n "$(EVAL_SELECT)" || { echo "EVAL_SELECT is empty: name providers, e.g. EVAL_SELECT='typesafe-jev groq cerebras:qwen-3.8-27b'"; exit 1; }
	@$(VENV_BIN)/python -m benchmarks.evaluation run --live --corpus $(EVAL_CORPUS) --tier $(EVAL_LIVE_TIER) \
		--select $(EVAL_SELECT) $(if $(EVAL_OUT),--out $(EVAL_OUT)) $(EVAL_ARGS)

eval-report: need-venv ## Re-derive report.md of a bundle (EVAL_BUNDLE=dir, optional EVAL_ARGS='--pricing f --observations f')
	@test -n "$(EVAL_BUNDLE)" || { echo "set EVAL_BUNDLE=<bundle directory>"; exit 1; }
	@$(VENV_BIN)/python -m benchmarks.evaluation report "$(EVAL_BUNDLE)" $(EVAL_ARGS)

eval-verify: need-venv ## Verify an evidence bundle: file digests, corpus coverage, offline reproduction
	@test -n "$(EVAL_BUNDLE)" || { echo "set EVAL_BUNDLE=<bundle directory>"; exit 1; }
	@$(VENV_BIN)/python -m benchmarks.evaluation verify "$(EVAL_BUNDLE)"

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

.PHONY: openlit-smoke
openlit-smoke: need-venv ## Verify fresh traces/logs/metrics in OpenLIT; see observability/openlit-runbook.md
	PYTHONPATH=director $(VENV_BIN)/python -m benchmarks.openlit_smoke $(SMOKE_ARGS)
