#!/usr/bin/env python3
"""Regenerate the JSON Schemas under contracts/schemas from the Pydantic models.

Usage:
    python contracts/export_schemas.py [output_dir]

Default output directory is ``contracts/schemas`` next to this file. The
script is intentionally dependency-light (pydantic only) so it can run from
a bare virtualenv before the director service bootstrap lands.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "director"))

# The dungeon_director import must follow the sys.path setup above.
from dungeon_director import contracts as C  # noqa: E402

SCHEMAS: dict[str, type[C.BaseModel]] = {
    "dungeon_state": C.DungeonState,
    "room_plan": C.RoomPlan,
    "generation_request": C.GenerationRequest,
    "generation_response": C.GenerationResponse,
}


def build_schema(name: str, model: type[C.BaseModel]) -> dict[str, Any]:
    """Return the JSON Schema for ``model`` with stable document metadata."""
    schema = model.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["$id"] = f"{C.SCHEMA_ID_PREFIX}/v{C.CONTRACT_VERSION}/{name}.schema.json"
    schema["x-contract-version"] = C.CONTRACT_VERSION
    return schema


def export(target_dir: Path) -> list[Path]:
    written: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, model in SCHEMAS.items():
        path = target_dir / f"{name}.schema.json"
        path.write_text(
            json.dumps(build_schema(name, model), indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        written.append(path)
    return written


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else REPO_ROOT / "contracts" / "schemas"
    for p in export(out):
        print(f"wrote {p}")
