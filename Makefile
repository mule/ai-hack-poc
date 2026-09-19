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
# Usage: $(call godot_run,<godot args>,<log file>)
define godot_run
rm -f $(2); \
$(GODOT) --headless --path game $(1) --log-file $(2); rc=$$?; \
test -f $(2) || { echo "FAIL: no Godot log written to $(2)"; exit 1; }; \
if grep -nE 'SCRIPT ERROR:|Parse Error:' $(2); then echo "FAIL: script errors found in $(2)"; exit 1; fi; \
exit $$rc
endef

.DEFAULT_GOAL := help
.PHONY: help need-venv setup run-director test lint format check godot-check godot-test godot-lint clean

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

godot-check: ## Headless-load the Godot project; fails on script parse/runtime errors in the log
	@test -f game/project.godot || { echo "game/project.godot not found: the Godot shell is not in this checkout yet"; exit 1; }
	@$(call godot_run,--quit-after 10,$(GODOT_LOG_DIR)/godot-load.log)

godot-test: ## Run Godot tests headlessly (mechanics, + shared-contract and scene-smoke if present)
	@test -f game/project.godot || { echo "game/project.godot not found: the Godot shell is not in this checkout yet"; exit 1; }
	@test -f game/tests/test_mechanics.gd || { echo "game/tests/test_mechanics.gd not found"; exit 1; }
	@$(call godot_run,-s res://tests/test_mechanics.gd,$(GODOT_LOG_DIR)/godot-mechanics.log)
	@if test -f game/tests/test_contracts.gd; then \
		echo "Running shared-contract test"; \
		$(call godot_run,-s res://tests/test_contracts.gd,$(GODOT_LOG_DIR)/godot-contracts.log); \
	else \
		echo "game/tests/test_contracts.gd not present: skipping shared-contract test"; \
	fi
	@if test -f game/tests/test_scene_smoke.gd; then \
		echo "Running scene smoke test"; \
		$(call godot_run,-s res://tests/test_scene_smoke.gd,$(GODOT_LOG_DIR)/godot-scene-smoke.log); \
	else \
		echo "game/tests/test_scene_smoke.gd not present: skipping scene smoke test"; \
	fi

godot-lint: ## Lint GDScript with gdlint (pip install gdtoolkit)
	@command -v gdlint >/dev/null || { echo "gdlint not found: pip install gdtoolkit"; exit 1; }
	@test -d game || { echo "game/ not found: the Godot shell is not in this checkout yet"; exit 1; }
	gdlint game

clean: ## Remove the director virtualenv, Python caches and build output
	rm -rf $(VENV) director/.pytest_cache director/.ruff_cache director/*.egg-info director/build director/dist
	find director -type d -name __pycache__ -prune -exec rm -rf {} +
