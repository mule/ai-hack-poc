"""Machine-readable validation of the simulation dataset format (issue #16).

The Godot harness (``game/simulation/``) writes datasets; this suite is the
portable, engine-free check that the published JSON Schemas, the committed
sanitized fixture, the documentation and the repository wiring agree:

* every schema is a valid draft 2020-12 schema and accepts the fixture;
* the files of a dataset are consistent with each other (counts, verdicts);
* the schemas really reject malformed rows (they are not vacuous);
* generated run output is git-ignored while fixtures stay tracked;
* the make target defaults to the offline mode, and the docs describe the
  remote opt-in, the cost warning and every field.
"""

from __future__ import annotations

import copy
import json
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
SIM_DIR = REPO_ROOT / "benchmarks" / "simulation"
SCHEMAS_DIR = SIM_DIR / "schemas"
FIXTURE_DIR = SIM_DIR / "fixtures" / "sample"
README = SIM_DIR / "README.md"

# dataset file -> (schema, is-jsonl)
FILES = {
    "manifest.json": ("manifest.schema.json", False),
    "steps.jsonl": ("step.schema.json", True),
    "rooms.jsonl": ("room.schema.json", True),
    "failures.jsonl": ("failure.schema.json", True),
    "summary.json": ("summary.schema.json", False),
}


def load_schema(name: str) -> dict:
    return json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))


def load_dataset(directory: Path) -> dict[str, object]:
    data: dict[str, object] = {}
    for name, (_, is_jsonl) in FILES.items():
        text = (directory / name).read_text(encoding="utf-8")
        if is_jsonl:
            assert text == "" or text.endswith("\n"), f"{name} must end with a newline"
            data[name] = [json.loads(line) for line in text.splitlines()]
        else:
            data[name] = json.loads(text)
    return data


@pytest.fixture(scope="module")
def dataset() -> dict[str, object]:
    return load_dataset(FIXTURE_DIR)


def validator_for(name: str) -> Draft202012Validator:
    return Draft202012Validator(load_schema(FILES[name][0]))


class TestSchemas:
    @pytest.mark.parametrize("schema_name", [schema for schema, _ in FILES.values()])
    def test_schema_is_valid_draft_2020_12(self, schema_name: str) -> None:
        Draft202012Validator.check_schema(load_schema(schema_name))

    def test_schemas_carry_the_dataset_version(self, dataset: dict[str, object]) -> None:
        versions = {load_schema(schema)["x-dataset-schema-version"] for schema, _ in FILES.values()}
        assert versions == {dataset["manifest.json"]["schema_version"]}  # type: ignore[index]

    @pytest.mark.parametrize("name", list(FILES))
    def test_fixture_file_validates(self, dataset: dict[str, object], name: str) -> None:
        validator = validator_for(name)
        value = dataset[name]
        rows = value if isinstance(value, list) else [value]
        assert rows, f"{name} fixture must not be empty"
        for row in rows:
            errors = sorted(validator.iter_errors(row), key=lambda e: list(e.path))
            assert not errors, f"{name}: {errors[0].message} at {list(errors[0].path)}"

    def test_schemas_reject_malformed_rows(self, dataset: dict[str, object]) -> None:
        step = copy.deepcopy(dataset["steps.jsonl"][0])  # type: ignore[index]
        failure = {
            "kind": "topology",
            "severity": "error",
            "run": 0,
            "step": 1,
            "frontier": None,
            "message": "x",
            "detail": {},
        }
        mutations = {
            "steps.jsonl": [
                {**step, "unexpected": 1},
                {**step, "outcome": "exploded"},
                {**step, "danger": 9},
                {key: value for key, value in step.items() if key != "frontier"},
            ],
            "failures.jsonl": [
                {**failure, "severity": "fatal"},
                {**failure, "kind": "gremlins"},
            ],
            "rooms.jsonl": [{"room_id": "r-000"}],
        }
        for name, rows in mutations.items():
            validator = validator_for(name)
            for row in rows:
                assert not validator.is_valid(row), f"{name} accepted {row}"

    def test_manifest_rejects_a_non_integer_request_budget(
        self, dataset: dict[str, object]
    ) -> None:
        manifest = copy.deepcopy(dataset["manifest.json"])
        manifest["remote"] = {"opt_in": True, "request_budget": "lots"}  # type: ignore[index]
        assert not validator_for("manifest.json").is_valid(manifest)


class TestFixtureConsistency:
    def test_counts_agree_across_files(self, dataset: dict[str, object]) -> None:
        manifest = dataset["manifest.json"]
        steps = dataset["steps.jsonl"]
        rooms = dataset["rooms.jsonl"]
        failures = dataset["failures.jsonl"]
        summary = dataset["summary.json"]
        overall = summary["overall"]  # type: ignore[index]
        assert overall["steps"] == len(steps)  # type: ignore[arg-type]
        assert overall["rooms_committed"] == len(rooms)  # type: ignore[arg-type]
        assert overall["rooms_explored"] == sum(1 for r in rooms if r["explored"])  # type: ignore[union-attr]
        assert [r["run"] for r in summary["runs"]] == [r["run"] for r in manifest["runs"]]  # type: ignore[index,union-attr]
        severity = Counter(f["severity"] for f in failures)  # type: ignore[union-attr]
        for level in ("error", "warning", "info"):
            assert overall["failures"][level] == severity.get(level, 0)
        by_kind = Counter(f["kind"] for f in failures)  # type: ignore[union-attr]
        assert overall["failures"]["by_kind"] == dict(by_kind)
        assert overall["passed"] == (severity.get("error", 0) == 0)

    def test_fallback_frequency_matches_the_step_rows(self, dataset: dict[str, object]) -> None:
        steps = dataset["steps.jsonl"]
        fallbacks = [s for s in steps if s["fallback"]]  # type: ignore[union-attr]
        overall = dataset["summary.json"]["overall"]  # type: ignore[index]
        assert overall["fallback_frequency"] == pytest.approx(len(fallbacks) / len(steps))  # type: ignore[arg-type]
        assert overall["fallback_reasons"] == dict(Counter(s["fallback_reason"] for s in fallbacks))
        assert fallbacks, "the fixture must exercise the fallback path"
        assert all(s["fallback_reason"] for s in fallbacks)

    def test_rows_are_internally_consistent(self, dataset: dict[str, object]) -> None:
        rooms = dataset["rooms.jsonl"]
        steps = dataset["steps.jsonl"]
        assert [r["index"] for r in rooms] == list(range(len(rooms)))  # type: ignore[arg-type,union-attr]
        assert rooms[0]["source"] == "start" and rooms[0]["parent_frontier"] == ""  # type: ignore[index]
        room_ids = {r["room_id"] for r in rooms}  # type: ignore[union-attr]
        assert len(room_ids) == len(rooms)  # type: ignore[arg-type]
        for step in steps:  # type: ignore[union-attr]
            if step["outcome"] == "committed":
                assert step["room_id"] in room_ids
            else:
                assert step["room_id"] is None and step["source"] == "none"
        assert [s["step"] for s in steps] == list(range(1, len(steps) + 1))  # type: ignore[arg-type,union-attr]
        for room in rooms:  # type: ignore[union-attr]
            assert sum(room["enemies"].values()) == room["enemy_count"]
            assert sum(room["items"].values()) == room["item_count"]
            assert room["exit_count"] == len(room["exits"])

    def test_fixture_is_offline_and_sanitized(self) -> None:
        manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
        assert manifest["provider"]["mode"] == "rules-baseline"
        assert manifest["provider"]["endpoint"] is None
        assert manifest["remote"] == {"opt_in": False, "request_budget": None}
        assert "engine" not in manifest, "fixtures must not pin the engine version"
        blob = "\n".join((FIXTURE_DIR / name).read_text(encoding="utf-8") for name in FILES)
        for needle in ("/home/", "/tmp/", "/Users/", "C:\\", "token", "api_key", "secret"):
            assert needle not in blob, f"fixture leaks {needle!r}"

    def test_fixture_has_exactly_the_dataset_files(self) -> None:
        assert sorted(p.name for p in FIXTURE_DIR.iterdir()) == sorted(FILES)


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git not available")
class TestRepositoryWiring:
    @pytest.fixture(autouse=True)
    def _in_git_checkout(self) -> None:
        if _git("rev-parse", "--is-inside-work-tree").returncode != 0:
            pytest.skip("not a git checkout")

    def test_generated_output_is_ignored_but_fixtures_are_not(self) -> None:
        ignored = _git("check-ignore", "-q", "simulation-output/20260101T000000Z/summary.json")
        assert ignored.returncode == 0, "simulation-output/ must be git-ignored"
        for name in FILES:
            kept = _git("check-ignore", "-q", f"benchmarks/simulation/fixtures/sample/{name}")
            assert kept.returncode == 1, f"fixture {name} must not be ignored"

    def test_no_generated_output_is_tracked(self) -> None:
        tracked = _git("ls-files", "--", "simulation-output", "*/simulation-output/*").stdout
        assert tracked.strip() == ""


class TestMakeAndDocs:
    def test_make_simulate_defaults_to_the_offline_mode(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assert re.search(r"^SIM_ARGS\s*\?=\s*$", makefile, re.MULTILINE), (
            "SIM_ARGS must default to empty so `make simulate` never enables --remote"
        )
        assert "--remote" not in makefile
        assert re.search(r"^simulate:", makefile, re.MULTILINE)
        assert "SIMULATION COMPLETE" in makefile, "simulate must require its success sentinel"
        assert "simulation-output" in makefile

    def test_godot_test_runs_the_harness_suite_with_a_sentinel(self) -> None:
        makefile = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")
        assert "test_simulation_harness.gd" in makefile
        assert "SUCCESS: All simulation harness checks passed" in makefile
        suite = (REPO_ROOT / "game" / "tests" / "test_simulation_harness.gd").read_text(
            encoding="utf-8"
        )
        assert "SUCCESS: All simulation harness checks passed!" in suite

    def test_readme_documents_offline_default_remote_opt_in_and_cost(self) -> None:
        text = README.read_text(encoding="utf-8")
        for needle in (
            "make simulate",
            "--remote",
            "COST WARNING",
            "--max-requests",
            "--endpoint",
            "--provider",
            "--model",
            "budget_exhausted",
            "simulation-output/",
            "Limitations",
        ):
            assert needle in text, f"README must mention {needle!r}"

    @pytest.mark.parametrize(
        "name",
        ["manifest.schema.json", "step.schema.json", "room.schema.json", "failure.schema.json"],
    )
    def test_readme_documents_every_schema_field(self, name: str) -> None:
        text = README.read_text(encoding="utf-8")
        properties = load_schema(name)["properties"]
        nested = {
            key
            for value in properties.values()
            if isinstance(value, dict)
            for key in value.get("properties", {})
        }
        for field in list(properties) + sorted(nested):
            assert f"`{field}`" in text or f"{field}," in text or f"{field})" in text, (
                f"README does not document {name}: {field}"
            )

    def test_failure_kinds_in_schema_are_documented(self) -> None:
        text = README.read_text(encoding="utf-8")
        for kind in load_schema("failure.schema.json")["properties"]["kind"]["enum"]:
            assert f"`{kind}`" in text
