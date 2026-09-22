"""Validate real serialized Godot lifecycle batches against the server contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from dungeon_director.telemetry_schema import GameEventBatch


def test_actual_godot_batches_match_python_contract(tmp_path):
    godot = os.environ.get("GODOT") or shutil.which("godot")
    if not godot:
        pytest.skip("Godot is required for the cross-language telemetry wire test")
    root = Path(__file__).resolve().parents[2]
    game = root / "game"
    output = tmp_path / "actual-batches.json"
    env = {
        **os.environ,
        "DUNGEON_TELEMETRY_ENABLED": "0",
        "DUNGEON_TELEMETRY_FIXTURE_OUT": str(output),
    }
    if not (game / ".godot/global_script_class_cache.cfg").exists():
        imported = subprocess.run(
            [
                godot,
                "--headless",
                "--path",
                str(game),
                "--editor",
                "--import",
                "--quit",
                "--log-file",
                str(tmp_path / "import.log"),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert imported.returncode == 0, imported.stdout + imported.stderr
    result = subprocess.run(
        [
            godot,
            "--headless",
            "--path",
            str(game),
            "--log-file",
            str(tmp_path / "game.log"),
            "--script",
            "res://tests/test_game_telemetry.gd",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    diagnostics = result.stdout + result.stderr
    assert result.returncode == 0, diagnostics
    assert "SCRIPT ERROR:" not in diagnostics and "Parse Error:" not in diagnostics, diagnostics
    assert "SUCCESS: All game telemetry checks passed!" in diagnostics
    # Preserve raw JSON from the sink: reparsing/re-serializing in Godot would
    # turn integer measurements into floats and hide the true wire contract.
    raw_batches = json.loads(output.read_text())["batches"]
    assert raw_batches
    events = []
    for raw in raw_batches:
        assert len(raw.encode()) <= 64 * 1024
        batch = GameEventBatch.model_validate_json(raw)
        events.extend(batch.events)
    assert {event.event_name.value for event in events} == {
        "frontier.discovered",
        "generation.queued",
        "generation.sent",
        "generation.response_received",
        "generation.accepted",
        "generation.normalized",
        "generation.rejected",
        "generation.fallback_applied",
        "room.committed",
        "door.revealed",
        "room.entered",
    }
