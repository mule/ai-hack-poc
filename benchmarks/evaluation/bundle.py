"""Loading and verifying an evaluation evidence bundle.

Verification checks internal integrity and provenance: file digests, cross-file
identity, exact corpus coverage, the seeded call order, and the offline golden
plan. It catches edits, truncation, and accidentally substituted benchmark
fixtures. It is not a signature or an attestation that a caller really contacted
a hosted provider; that requires an external trust mechanism.
"""

from __future__ import annotations

import json
import math
import random
from collections import Counter
from pathlib import Path
from typing import Any

from dungeon_director.contracts import RoomPlan

from benchmarks.evaluation import BUNDLE_SCHEMA_VERSION
from benchmarks.evaluation.corpus import (
    MANIFEST_NAME,
    CorpusError,
    canonical_json,
    sha256_bytes,
    sha256_file,
    verify_corpus,
)
from benchmarks.evaluation.runner import RAW_FILES


class BundleError(ValueError):
    """The directory is not a usable evidence bundle."""


def plan_digest(rows: list[dict[str, Any]]) -> str:
    """Digest of the returned plans (or failure codes), independent of timing."""
    decisions = sorted(
        (
            [row["request_id"], row.get("room"), None if row.get("success") else row["error_code"]]
            for row in rows
        ),
        key=lambda item: item[0],
    )
    return sha256_bytes(canonical_json(decisions).encode("utf-8"))


def _read_object(path: Path, label: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise BundleError(f"{label} must contain a JSON object")
    return value


class Bundle:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.manifest = _read_object(path / "manifest.json", "manifest.json")
            self.protocol = _read_object(path / "protocol.json", "protocol.json")
            self.environment = _read_object(path / "environment.json", "environment.json")
            self.results_document = _read_object(path / "results.json", "results.json")
            self.corpus_manifest = _read_object(
                path / "corpus" / MANIFEST_NAME, f"corpus/{MANIFEST_NAME}"
            )
            results = self.results_document.get("results")
            if not isinstance(results, list) or not all(isinstance(row, dict) for row in results):
                raise BundleError("results.json 'results' must be a list of objects")
            warmup_text = (path / "warmup.jsonl").read_text(encoding="utf-8")
            warmup: list[dict[str, Any]] = []
            for number, line in enumerate(warmup_text.splitlines(), start=1):
                if not line.strip():
                    continue
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise BundleError(f"warmup.jsonl:{number} must contain a JSON object")
                warmup.append(row)
        except BundleError:
            raise
        except (OSError, ValueError) as exc:
            raise BundleError(f"{path}: not an evaluation bundle: {exc}") from exc
        self.results: list[dict[str, Any]] = results
        self.warmup = warmup

    @property
    def labels(self) -> list[str]:
        selections = self.manifest.get("selections")
        if not isinstance(selections, list) or not all(
            isinstance(label, str) and label for label in selections
        ):
            raise BundleError("manifest.json 'selections' must be a list of non-empty strings")
        return selections

    def rows_for(self, label: str, phase: str = "measured") -> list[dict[str, Any]]:
        source = self.results if phase == "measured" else self.warmup
        return [row for row in source if f"{row.get('provider')}/{row.get('model')}" == label]


def _major(value: Any) -> str | None:
    return str(value).split(".")[0] if isinstance(value, str) and value else None


def _selection_labels(value: Any) -> list[str] | None:
    if not isinstance(value, list):
        return None
    labels: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            return None
        provider, model = item.get("provider"), item.get("model")
        if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
            return None
        labels.append(f"{provider}/{model}")
    return labels


def _row_problems(row: dict[str, Any], where: str, *, phase: str) -> list[str]:
    problems: list[str] = []
    required = (
        "request_id",
        "run_id",
        "provider",
        "model",
        "iteration",
        "success",
        "status_code",
        "latency_ms",
        "error_code",
        "room",
        "phase",
        "position",
        "sequence",
        "retry_count",
    )
    missing = [name for name in required if name not in row]
    if missing:
        return [f"{where}: missing fields {missing}"]
    if row["phase"] != phase:
        problems.append(f"{where}: phase must be {phase!r}")
    for name in ("request_id", "run_id", "provider", "model"):
        if not isinstance(row[name], str) or not row[name]:
            problems.append(f"{where}: {name} must be a non-empty string")
    for name in ("iteration", "position", "sequence", "status_code"):
        if isinstance(row[name], bool) or not isinstance(row[name], int):
            problems.append(f"{where}: {name} must be an integer")
    latency = row["latency_ms"]
    if (
        isinstance(latency, bool)
        or not isinstance(latency, int | float)
        or not math.isfinite(latency)
        or latency < 0
    ):
        problems.append(f"{where}: latency_ms must be a finite non-negative number")
    if not isinstance(row["success"], bool):
        problems.append(f"{where}: success must be boolean")
    elif row["success"]:
        if not isinstance(row["room"], dict):
            problems.append(f"{where}: a successful result needs a room object")
        else:
            try:
                RoomPlan.model_validate(row["room"])
            except ValueError:
                problems.append(f"{where}: room does not satisfy the RoomPlan contract")
        if row["error_code"] is not None:
            problems.append(f"{where}: a successful result cannot have error_code")
    elif row["room"] is not None or not isinstance(row["error_code"], str):
        problems.append(f"{where}: a failed result needs no room and a string error_code")
    if row["retry_count"] is not None:
        problems.append(f"{where}: retry_count must be null because this protocol does not retry")
    return problems


def _expected_call_order(
    request_ids: list[str], labels: list[str], parameters: dict[str, Any]
) -> list[tuple[str, int, int, str, str]]:
    seed = parameters["order_seed"]
    expected: list[tuple[str, int, int, str, str]] = []
    count = min(parameters["warmup_requests"], len(request_ids))
    warmup = random.Random(f"{seed}:warmup").sample(request_ids, count)
    for position, request_id in enumerate(warmup):
        for label in labels:
            expected.append(("warmup", 0, position, request_id, label))
    for iteration in range(1, parameters["iterations"] + 1):
        order = list(request_ids)
        random.Random(f"{seed}:{iteration}").shuffle(order)
        for position, request_id in enumerate(order):
            shift = (position + iteration) % len(labels)
            for label in labels[shift:] + labels[:shift]:
                expected.append(("measured", iteration, position, request_id, label))
    return expected


def _actual_call_order(rows: list[dict[str, Any]]) -> list[tuple[str, int, int, str, str]] | None:
    sequences = [row.get("sequence") for row in rows]
    if any(isinstance(value, bool) or not isinstance(value, int) for value in sequences):
        return None
    if sorted(sequences) != list(range(1, len(rows) + 1)):
        return None
    ordered = sorted(rows, key=lambda row: row["sequence"])
    return [
        (
            row.get("phase"),
            row.get("iteration"),
            row.get("position"),
            row.get("request_id"),
            f"{row.get('provider')}/{row.get('model')}",
        )
        for row in ordered
    ]


def verify_bundle(path: str | Path) -> list[str]:
    """Return every problem found; an empty list means the bundle is internally valid."""
    root = Path(path)
    problems: list[str] = []
    try:
        bundle = Bundle(root)
        labels = bundle.labels
    except BundleError as exc:
        return [str(exc)]
    manifest = bundle.manifest
    if manifest.get("kind") != "evaluation-bundle" or _major(
        manifest.get("schema_version")
    ) != _major(BUNDLE_SCHEMA_VERSION):
        return [f"{root}: manifest is not an evaluation-bundle {BUNDLE_SCHEMA_VERSION}"]
    if len(labels) != len(set(labels)) or not labels:
        problems.append("manifest selections must be non-empty and unique")

    raw_files = manifest.get("raw_files")
    if not isinstance(raw_files, dict):
        problems.append("manifest raw_files must be an object")
        raw_files = {}
    for name in RAW_FILES:
        entry = raw_files.get(name)
        file = root / name
        if not isinstance(entry, dict) or not file.is_file():
            problems.append(f"{name}: missing file or manifest entry")
            continue
        digest, size = entry.get("sha256"), entry.get("bytes")
        if not isinstance(digest, str) or len(digest) != 64:
            problems.append(f"{name}: manifest sha256 is missing or malformed")
        elif sha256_file(file) != digest:
            problems.append(f"{name}: sha256 differs from the manifest (edited after the run?)")
        if isinstance(size, bool) or not isinstance(size, int) or file.stat().st_size != size:
            problems.append(f"{name}: byte size differs from the manifest")

    try:
        corpus_problems, requests = verify_corpus(root / "corpus" / MANIFEST_NAME)
    except (CorpusError, OSError, TypeError, ValueError) as exc:
        corpus_problems, requests = [f"cannot verify corpus: {exc}"], []
    problems.extend(f"corpus: {problem}" for problem in corpus_problems)
    identity = manifest.get("corpus")
    if not isinstance(identity, dict):
        problems.append("manifest corpus identity must be an object")
        identity = {}
    corpus_manifest_path = root / "corpus" / MANIFEST_NAME
    if corpus_manifest_path.is_file():
        if sha256_file(corpus_manifest_path) != identity.get("manifest_sha256"):
            problems.append("corpus/corpus.json differs from the manifest's corpus identity")
    files = bundle.corpus_manifest.get("files")
    requests_sha256 = (
        files[0].get("sha256")
        if isinstance(files, list) and files and isinstance(files[0], dict)
        else None
    )
    expected_identity = {
        "corpus_id": bundle.corpus_manifest.get("corpus_id"),
        "kind": bundle.corpus_manifest.get("kind"),
        "records": len(requests),
        "requests_sha256": requests_sha256,
    }
    for key, value in expected_identity.items():
        if identity.get(key) != value:
            problems.append(f"manifest corpus identity {key!r} does not match corpus/corpus.json")

    parameters = bundle.protocol.get("parameters")
    if not isinstance(parameters, dict):
        problems.append("protocol parameters must be an object")
        return problems
    iterations = parameters.get("iterations")
    warmup_requests = parameters.get("warmup_requests")
    order_seed = parameters.get("order_seed")
    concurrency = parameters.get("concurrency")
    if isinstance(iterations, bool) or not isinstance(iterations, int) or iterations < 1:
        problems.append("protocol iterations must be a positive integer")
    if (
        isinstance(warmup_requests, bool)
        or not isinstance(warmup_requests, int)
        or warmup_requests < 0
    ):
        problems.append("protocol warmup_requests must be a non-negative integer")
    if isinstance(order_seed, bool) or not isinstance(order_seed, int):
        problems.append("protocol order_seed must be an integer")
    if concurrency != 1:
        problems.append("protocol concurrency must be 1")
    if not all(
        isinstance(value, int) and not isinstance(value, bool)
        for value in (iterations, warmup_requests, order_seed)
    ):
        return problems

    protocol_labels = _selection_labels(bundle.protocol.get("selections"))
    if protocol_labels != labels:
        problems.append("protocol selections do not match manifest selections")
    environment_labels = _selection_labels(bundle.environment.get("providers"))
    if environment_labels != labels:
        problems.append("environment providers do not match manifest selections")

    result_doc = bundle.results_document
    if result_doc.get("iterations") != iterations:
        problems.append("results.json iterations do not match protocol parameters")
    if result_doc.get("concurrency") != concurrency:
        problems.append("results.json concurrency does not match protocol parameters")
    contract_versions = [
        result_doc.get("contract_version"),
        bundle.environment.get("contract_version"),
        bundle.corpus_manifest.get("contract_version"),
    ]
    if contract_versions[0] is None or any(
        value != contract_versions[0] for value in contract_versions[1:]
    ):
        problems.append("contract versions disagree across results, environment, and corpus")

    request_by_id = {request.request_id: request for request in requests}
    ids = set(request_by_id)
    expected_rows = manifest.get("expected_rows")
    if not isinstance(expected_rows, dict):
        problems.append("manifest expected_rows must be an object")
        expected_rows = {}
    measured_per_selection = iterations * len(requests)
    warmup_per_selection = min(warmup_requests, len(requests))
    if expected_rows.get("measured_per_selection") != measured_per_selection:
        problems.append("manifest measured row count does not match the corpus and protocol")
    if expected_rows.get("warmup_per_selection") != warmup_per_selection:
        problems.append("manifest warm-up row count does not match the corpus and protocol")

    row_shape_ok = True
    for phase, rows in (("measured", bundle.results), ("warmup", bundle.warmup)):
        for index, row in enumerate(rows, start=1):
            row_errors = _row_problems(row, f"{phase} row {index}", phase=phase)
            problems.extend(row_errors)
            row_shape_ok = row_shape_ok and not row_errors
            request_id = row.get("request_id")
            request = request_by_id.get(request_id) if isinstance(request_id, str) else None
            if request is not None and row.get("run_id") != request.run_id:
                problems.append(f"{phase} row {index}: run_id does not match the corpus request")

    for label in labels:
        rows = bundle.rows_for(label)
        if len(rows) != measured_per_selection:
            problems.append(
                f"{label}: {len(rows)} measured rows, expected {measured_per_selection}"
            )
        seen = Counter(
            (row["request_id"], row["iteration"])
            for row in rows
            if isinstance(row.get("request_id"), str)
            and isinstance(row.get("iteration"), int)
            and not isinstance(row.get("iteration"), bool)
        )
        wanted = {(request_id, it) for request_id in ids for it in range(1, iterations + 1)}
        if set(seen) != wanted or any(count != 1 for count in seen.values()):
            problems.append(f"{label}: results do not cover the corpus exactly once per iteration")
        if len(bundle.rows_for(label, "warmup")) != warmup_per_selection:
            problems.append(f"{label}: warm-up row count differs from the protocol")
    result_labels = {f"{row.get('provider')}/{row.get('model')}" for row in bundle.results}
    warmup_labels = {f"{row.get('provider')}/{row.get('model')}" for row in bundle.warmup}
    stray = (result_labels | warmup_labels) - set(labels)
    if stray:
        problems.append(f"rows contain unselected provider/model: {sorted(stray)}")

    if not labels:
        return problems
    actual_order = _actual_call_order(bundle.warmup + bundle.results)
    expected_order = _expected_call_order(
        [request.request_id for request in requests], labels, parameters
    )
    if actual_order is None or actual_order != expected_order:
        problems.append(
            "call sequence does not match the seeded warm-up and paired rotation policy"
        )

    golden = bundle.corpus_manifest.get("expected_offline")
    if isinstance(golden, dict) and row_shape_ok:
        for label in labels:
            entry = golden.get(label)
            if not isinstance(entry, dict):
                continue
            first = [row for row in bundle.rows_for(label) if row.get("iteration") == 1]
            if plan_digest(first) != entry.get("plan_digest"):
                problems.append(f"{label}: plans do not reproduce the corpus expected plan digest")
    return problems
