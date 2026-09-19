"""Executes the protocol and writes the raw evidence bundle.

The runner only *measures*: it asks every selected provider/model the same
corpus request back to back (paired), one call at a time, and stores what came
back. Summaries, cost and behaviour are derived afterwards from those stored
files by :mod:`benchmarks.evaluation.report`, so they can be recomputed (for
example with a corrected price table) without spending another call.

Bundle layout (``evaluation-output/<UTC stamp>/``)::

    manifest.json          index and completion marker, written last
    protocol.json          effective parameters, selections, sample adequacy
    environment.json       code/runtime versions and non-secret provider config
    corpus/                the exact corpus that was replayed (manifest + requests)
    results.json           measured results, benchmarks.replay report shape
    warmup.jsonl           discarded warm-up results (not summarized)
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dungeon_director.contracts import CONTRACT_VERSION, GenerationRequest
from dungeon_director.registry import ProviderRegistry
from dungeon_director.rules import RULES_MODEL, RULES_PROVIDER_ID, RulesProvider
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings

from benchmarks.evaluation import BUNDLE_SCHEMA_VERSION
from benchmarks.evaluation.corpus import (
    MANIFEST_NAME,
    REQUESTS_NAME,
    Corpus,
    canonical_json,
    sha256_bytes,
)
from benchmarks.evaluation.environment import Selection, capture_environment
from benchmarks.evaluation.protocol import load_protocol, sample_adequacy
from benchmarks.replay import build_default_service, run_replay_item

ServiceFactory = Callable[[Selection, float], tuple[DirectorService, ProviderRegistry]]
DEFAULT_MAX_LIVE_CALLS = 2000
RAW_FILES = ("protocol.json", "environment.json", "results.json", "warmup.jsonl")


class EvaluationError(RuntimeError):
    """The run cannot start, or its output directory is unusable."""


def default_service_factory(
    selection: Selection, timeout_seconds: float
) -> tuple[DirectorService, ProviderRegistry]:
    """One director per selection, built from exactly the config that gets recorded."""
    registry = ProviderRegistry()
    registry.register(RulesProvider())
    if selection.is_live:
        registry.register(selection.build_provider())
    settings = DirectorSettings(
        default_provider=RULES_PROVIDER_ID,
        default_model=RULES_MODEL,
        timeout_seconds=timeout_seconds,
    )
    return DirectorService(registry, settings), registry


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def planned_calls(params: dict[str, Any], corpus_size: int, selections: list[Selection]) -> int:
    per_selection = min(params["warmup_requests"], corpus_size) + params["iterations"] * corpus_size
    return per_selection * len(selections)


def _row(result: Any, *, phase: str, iteration: int, position: int, sequence: int) -> dict:
    row = result.to_dict()
    row.setdefault("retry_count", None)  # not reported by the director: never a guessed 0
    row.update({"phase": phase, "iteration": iteration, "position": position, "sequence": sequence})
    return row


async def _execute(
    requests: list[GenerationRequest],
    selections: list[Selection],
    params: dict[str, Any],
    factory: ServiceFactory,
    progress: Callable[[str], None],
) -> tuple[list[dict], list[dict]]:
    services = {s.label: factory(s, params["timeout_seconds"]) for s in selections}
    warmup: list[dict] = []
    measured: list[dict] = []
    sequence = 0
    seed = params["order_seed"]

    async def ask(
        selection: Selection, request: GenerationRequest, phase: str, it: int, pos: int
    ) -> dict:
        nonlocal sequence
        sequence += 1
        result = await run_replay_item(
            services[selection.label][0], request, selection.provider, selection.model, it
        )
        return _row(result, phase=phase, iteration=it, position=pos, sequence=sequence)

    try:
        count = min(params["warmup_requests"], len(requests))
        for pos, request in enumerate(random.Random(f"{seed}:warmup").sample(requests, count)):
            for selection in selections:
                warmup.append(await ask(selection, request, "warmup", 0, pos))
        progress(f"warm-up done ({len(warmup)} calls, discarded)")

        for iteration in range(1, params["iterations"] + 1):
            order = list(requests)
            random.Random(f"{seed}:{iteration}").shuffle(order)
            for pos, request in enumerate(order):
                shift = (pos + iteration) % len(selections)
                for selection in selections[shift:] + selections[:shift]:
                    measured.append(await ask(selection, request, "measured", iteration, pos))
            progress(f"iteration {iteration}/{params['iterations']} done")
    finally:
        for service, registry in services.values():
            try:
                await service.aclose()
            finally:
                await registry.aclose()
    return warmup, measured


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_evaluation(
    corpus: Corpus,
    selections: list[Selection],
    params: dict[str, Any],
    out_dir: Path,
    *,
    repo_root: Path,
    live: bool = False,
    max_live_calls: int = DEFAULT_MAX_LIVE_CALLS,
    vantage_point: str | None = None,
    label: str | None = None,
    service_factory: ServiceFactory | None = None,
    godot_version: Callable[[], str | None] | None = None,
    progress: Callable[[str], None] | None = None,
) -> Path:
    """Run the protocol and write the raw bundle; returns ``out_dir``."""
    say = progress or (lambda message: print(message, file=sys.stderr))
    if not selections:
        raise EvaluationError("select at least one provider")
    labels = [s.label for s in selections]
    if len(set(labels)) != len(labels):
        raise EvaluationError("the same provider/model was selected twice")
    hosted = [s for s in selections if s.is_live]
    calls = planned_calls(params, len(corpus.requests), selections)
    if hosted and not live:
        raise EvaluationError(
            "hosted providers make billable network calls: pass --live to confirm "
            f"({', '.join(s.label for s in hosted)})"
        )
    live_calls = planned_calls(params, len(corpus.requests), hosted)
    if live_calls > max_live_calls:
        raise EvaluationError(
            f"planned {live_calls} live calls exceeds --max-live-calls {max_live_calls}; "
            "lower the tier/iterations or raise the cap deliberately"
        )
    if out_dir.exists() and any(out_dir.iterdir()):
        raise EvaluationError(f"{out_dir} already holds files; choose a new output directory")
    if hosted:
        say(
            f"!! LIVE RUN: {live_calls} billable calls to "
            f"{', '.join(s.label for s in hosted)}; latency includes your network path"
        )
    say(f"{calls} calls planned over {len(corpus.requests)} corpus requests")

    protocol = load_protocol()
    started = _utc_now()
    environment = capture_environment(
        selections,
        repo_root=repo_root,
        vantage_point=vantage_point,
        timeout_seconds=params["timeout_seconds"],
        godot_version=godot_version,
    )
    warmup, measured = asyncio.run(
        _execute(
            corpus.requests,
            selections,
            params,
            service_factory or default_service_factory,
            say,
        )
    )
    finished = _utc_now()

    out_dir.mkdir(parents=True, exist_ok=True)
    corpus_dir = out_dir / "corpus"
    corpus_dir.mkdir()
    (corpus_dir / MANIFEST_NAME).write_bytes(corpus.manifest_path.read_bytes())
    (corpus_dir / REQUESTS_NAME).write_bytes(
        (corpus.manifest_path.parent / REQUESTS_NAME).read_bytes()
    )
    n_measured = params["iterations"] * len(corpus.requests)
    _write_json(
        out_dir / "protocol.json",
        {
            "protocol_id": protocol["protocol_id"],
            "protocol_version": protocol["protocol_version"],
            "parameters": params,
            "min_tail_samples": protocol["min_tail_samples"],
            "percentiles": protocol["percentiles"],
            "selections": [{"provider": s.provider, "model": s.model} for s in selections],
            "sample_adequacy": {s.label: sample_adequacy(n_measured, protocol) for s in selections},
        },
    )
    _write_json(out_dir / "environment.json", environment)
    _write_json(
        out_dir / "results.json",
        {
            "contract_version": CONTRACT_VERSION,
            "input_file": f"{corpus.corpus_id} ({corpus.kind} corpus)",
            "iterations": params["iterations"],
            "concurrency": params["concurrency"],
            "origin": "measured by benchmarks.evaluation run; see manifest.json",
            "results": measured,
        },
    )
    (out_dir / "warmup.jsonl").write_text(
        "".join(canonical_json(row) + "\n" for row in warmup), encoding="utf-8"
    )
    raw = {
        name: {
            "sha256": sha256_bytes((out_dir / name).read_bytes()),
            "bytes": (out_dir / name).stat().st_size,
        }
        for name in RAW_FILES
    }
    _write_json(
        out_dir / "manifest.json",
        {
            "schema_version": BUNDLE_SCHEMA_VERSION,
            "kind": "evaluation-bundle",
            "run": {
                "label": label,
                "started_at": started,
                "finished_at": finished,
                "live": bool(hosted),
                "tier": params["tier"],
            },
            "corpus": corpus.identity(),
            "selections": labels,
            "expected_rows": {
                "measured_per_selection": n_measured,
                "warmup_per_selection": len(warmup) // len(selections),
            },
            "raw_files": raw,
        },
    )
    return out_dir


def compute_offline_plan_digest(requests: list[GenerationRequest]) -> str:
    """Plan digest of the offline rules baseline over ``requests`` (the corpus golden)."""
    from benchmarks.evaluation.bundle import plan_digest

    async def collect() -> list[dict]:
        service, registry = build_default_service()
        try:
            rows = []
            for request in requests:
                result = await run_replay_item(service, request, RULES_PROVIDER_ID, RULES_MODEL, 1)
                rows.append(result.to_dict())
            return rows
        finally:
            await service.aclose()
            await registry.aclose()

    return plan_digest(asyncio.run(collect()))
