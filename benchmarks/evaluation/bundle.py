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


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _document_problems(bundle: Bundle) -> list[str]:
    """Validate nested values consumed by analysis after bundle verification."""
    problems: list[str] = []

    run = bundle.manifest.get("run")
    if not isinstance(run, dict):
        problems.append("manifest run must be an object")
    else:
        for name in ("started_at", "finished_at"):
            if not isinstance(run.get(name), str) or not run[name]:
                problems.append(f"manifest run {name} must be a non-empty string")
        if run.get("label") is not None and not isinstance(run["label"], str):
            problems.append("manifest run label must be a string or null")
        if not isinstance(run.get("live"), bool):
            problems.append("manifest run live must be boolean")

    identity = bundle.manifest.get("corpus")
    if isinstance(identity, dict):
        for name in ("corpus_id", "kind", "requests_sha256"):
            if not isinstance(identity.get(name), str) or not identity[name]:
                problems.append(f"manifest corpus {name} must be a non-empty string")
        records = identity.get("records")
        if isinstance(records, bool) or not isinstance(records, int) or records < 1:
            problems.append("manifest corpus records must be a positive integer")

    protocol = bundle.protocol
    for name in ("protocol_id", "protocol_version"):
        if not isinstance(protocol.get(name), str) or not protocol[name]:
            problems.append(f"protocol {name} must be a non-empty string")
    tail = protocol.get("min_tail_samples")
    if isinstance(tail, bool) or not isinstance(tail, int) or tail < 1:
        problems.append("protocol min_tail_samples must be a positive integer")
    percentiles = protocol.get("percentiles")
    if not isinstance(percentiles, dict) or not percentiles:
        problems.append("protocol percentiles must be a non-empty object")
    else:
        for name, value in percentiles.items():
            if (
                not isinstance(name, str)
                or not name
                or not _is_finite_number(value)
                or not 0 <= value < 1
            ):
                problems.append(
                    "protocol percentiles must map non-empty names to numbers from 0 up to 1"
                )
                break

    parameters = protocol.get("parameters")
    if isinstance(parameters, dict):
        if not isinstance(parameters.get("tier"), str) or not parameters["tier"]:
            problems.append("protocol tier must be a non-empty string")
        timeout = parameters.get("timeout_seconds")
        if not _is_finite_number(timeout) or not 0 < timeout <= 300:
            problems.append("protocol timeout_seconds must be greater than 0 and at most 300")
        if not isinstance(parameters.get("overrides"), dict):
            problems.append("protocol overrides must be an object")

    environment = bundle.environment
    code = environment.get("code")
    if not isinstance(code, dict):
        problems.append("environment code must be an object")
    else:
        if code.get("git_commit") is not None and not isinstance(code["git_commit"], str):
            problems.append("environment git_commit must be a string or null")
        if code.get("git_dirty") is not None and not isinstance(code["git_dirty"], bool):
            problems.append("environment git_dirty must be boolean or null")
    runtime = environment.get("runtime")
    if not isinstance(runtime, dict):
        problems.append("environment runtime must be an object")
    else:
        for name in ("python", "platform"):
            if not isinstance(runtime.get(name), str) or not runtime[name]:
                problems.append(f"environment runtime {name} must be a non-empty string")
        packages = runtime.get("packages")
        if not isinstance(packages, dict) or any(
            not isinstance(name, str) or value is not None and not isinstance(value, str)
            for name, value in packages.items()
        ):
            problems.append("environment runtime packages must map names to strings or null")
    if environment.get("vantage_point") is not None and not isinstance(
        environment["vantage_point"], str
    ):
        problems.append("environment vantage_point must be a string or null")
    providers = environment.get("providers")
    if isinstance(providers, list):
        for index, entry in enumerate(providers, start=1):
            if not isinstance(entry, dict):
                continue
            config = entry.get("config")
            if not isinstance(config, dict):
                problems.append(f"environment provider {index} config must be an object")
                continue
            if not isinstance(config.get("fields"), dict):
                problems.append(f"environment provider {index} config fields must be an object")
            withheld = config.get("withheld")
            if not isinstance(withheld, list) or not all(
                isinstance(name, str) for name in withheld
            ):
                problems.append(
                    f"environment provider {index} config withheld must be a list of strings"
                )
            if not isinstance(config.get("credentials_present"), bool):
                problems.append(
                    f"environment provider {index} config credentials_present must be boolean"
                )

    provenance = bundle.corpus_manifest.get("provenance")
    if not isinstance(provenance, dict):
        problems.append("corpus provenance must be an object")
    elif provenance.get("recorded_providers") is not None and not isinstance(
        provenance["recorded_providers"], dict
    ):
        problems.append("corpus recorded_providers must be an object when present")
    golden = bundle.corpus_manifest.get("expected_offline")
    if golden is not None and not isinstance(golden, dict):
        problems.append("corpus expected_offline must be an object when present")
    elif isinstance(golden, dict):
        for label, entry in golden.items():
            if (
                not isinstance(label, str)
                or not isinstance(entry, dict)
                or not isinstance(entry.get("plan_digest"), str)
            ):
                problems.append(
                    "corpus expected_offline must map labels to objects with a plan_digest"
                )
                break
    return problems


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
    if not _is_finite_number(latency) or latency < 0:
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
    usage = row.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            problems.append(f"{where}: usage must be an object or null")
        else:
            for name in ("input_tokens", "output_tokens", "total_tokens"):
                value = usage.get(name)
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int) or value < 0
                ):
                    problems.append(f"{where}: usage {name} must be a non-negative integer or null")
            cost = usage.get("estimated_cost_usd")
            if cost is not None and (not _is_finite_number(cost) or cost < 0):
                problems.append(
                    f"{where}: usage estimated_cost_usd must be a finite non-negative "
                    "number or null"
                )
    provider_metadata = row.get("provider_metadata")
    if provider_metadata is not None and not isinstance(provider_metadata, dict):
        problems.append(f"{where}: provider_metadata must be an object or null")
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
    problems.extend(_document_problems(bundle))

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
