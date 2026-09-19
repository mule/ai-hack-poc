"""Loading and verifying an evidence bundle.

Only a bundle written by :func:`benchmarks.evaluation.runner.run_evaluation` passes
:func:`verify_bundle`. Verification ties the results to the corpus copy stored in
the bundle (every request id, every iteration, exactly once) and to the manifest's
file digests, so a results file that was edited, truncated, or produced by anything
else, including the synthetic sample results shipped with the summarizer tests,
cannot be reported as a measured benchmark.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from benchmarks.evaluation import BUNDLE_SCHEMA_VERSION
from benchmarks.evaluation.corpus import (
    MANIFEST_NAME,
    canonical_json,
    sha256_bytes,
    sha256_file,
    verify_corpus,
)
from benchmarks.evaluation.runner import RAW_FILES


class BundleError(ValueError):
    """The directory is not a usable evidence bundle."""


def plan_digest(rows: list[dict[str, Any]]) -> str:
    """Digest of the returned plans (or failure codes), independent of timing.

    Rows are ordered by request id and only the decision content is hashed, so two
    runs of a deterministic provider over the same corpus yield the same digest.
    """
    decisions = sorted(
        (
            [row["request_id"], row.get("room"), None if row.get("success") else row["error_code"]]
            for row in rows
        ),
        key=lambda item: item[0],
    )
    return sha256_bytes(canonical_json(decisions).encode("utf-8"))


class Bundle:
    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self.manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
            self.protocol = json.loads((path / "protocol.json").read_text(encoding="utf-8"))
            self.environment = json.loads((path / "environment.json").read_text(encoding="utf-8"))
            results_doc = json.loads((path / "results.json").read_text(encoding="utf-8"))
            self.corpus_manifest = json.loads(
                (path / "corpus" / MANIFEST_NAME).read_text(encoding="utf-8")
            )
            warmup_text = (path / "warmup.jsonl").read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            raise BundleError(f"{path}: not an evaluation bundle: {exc}") from exc
        self.results: list[dict[str, Any]] = results_doc.get("results", [])
        self.warmup = [json.loads(line) for line in warmup_text.splitlines() if line.strip()]

    @property
    def labels(self) -> list[str]:
        return list(self.manifest["selections"])

    def rows_for(self, label: str, phase: str = "measured") -> list[dict[str, Any]]:
        source = self.results if phase == "measured" else self.warmup
        return [r for r in source if f"{r['provider']}/{r['model']}" == label]


def verify_bundle(path: str | Path) -> list[str]:
    """Return every problem found; an empty list means the bundle is trustworthy."""
    root = Path(path)
    problems: list[str] = []
    try:
        bundle = Bundle(root)
    except BundleError as exc:
        return [str(exc)]
    manifest = bundle.manifest
    if (
        manifest.get("kind") != "evaluation-bundle"
        or manifest.get("schema_version", "").split(".")[0] != BUNDLE_SCHEMA_VERSION.split(".")[0]
    ):
        return [f"{root}: manifest is not an evaluation-bundle {BUNDLE_SCHEMA_VERSION}"]

    for name in RAW_FILES:
        entry = manifest.get("raw_files", {}).get(name)
        file = root / name
        if entry is None or not file.is_file():
            problems.append(f"{name}: missing")
        elif sha256_file(file) != entry["sha256"]:
            problems.append(f"{name}: sha256 differs from the manifest (edited after the run?)")

    corpus_problems, requests = verify_corpus(root / "corpus" / MANIFEST_NAME)
    problems.extend(f"corpus: {p}" for p in corpus_problems)
    identity = manifest.get("corpus", {})
    if sha256_file(root / "corpus" / MANIFEST_NAME) != identity.get("manifest_sha256"):
        problems.append("corpus/corpus.json differs from the manifest's corpus identity")
    ids = {r.request_id for r in requests}
    expected = manifest.get("expected_rows", {})
    iterations = bundle.protocol["parameters"]["iterations"]

    for label in bundle.labels:
        rows = bundle.rows_for(label)
        if len(rows) != expected.get("measured_per_selection"):
            problems.append(
                f"{label}: {len(rows)} measured rows, expected "
                f"{expected.get('measured_per_selection')}"
            )
        seen = Counter((r["request_id"], r["iteration"]) for r in rows)
        wanted = {(rid, it) for rid in ids for it in range(1, iterations + 1)}
        if set(seen) != wanted or any(count != 1 for count in seen.values()):
            problems.append(f"{label}: results do not cover the corpus exactly once per iteration")
        if any(r.get("phase") != "measured" for r in rows):
            problems.append(f"{label}: a non-measured row is in results.json")
        if len(bundle.rows_for(label, "warmup")) != expected.get("warmup_per_selection"):
            problems.append(f"{label}: warm-up row count differs from the manifest")
    stray = {f"{r['provider']}/{r['model']}" for r in bundle.results} - set(bundle.labels)
    if stray:
        problems.append(f"results contain unselected provider/model: {sorted(stray)}")

    golden = bundle.corpus_manifest.get("expected_offline", {})
    for label in bundle.labels:
        if label in golden:
            first = [r for r in bundle.rows_for(label) if r["iteration"] == 1]
            if plan_digest(first) != golden[label]["plan_digest"]:
                problems.append(f"{label}: plans do not reproduce the corpus expected plan digest")
    return problems
