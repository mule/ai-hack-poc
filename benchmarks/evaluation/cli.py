"""Command line: ``python -m benchmarks.evaluation <corpus|run|report|verify> ...``."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

from dungeon_director.rules import RULES_MODEL, RULES_PROVIDER_ID

from benchmarks.evaluation.bundle import verify_bundle
from benchmarks.evaluation.corpus import (
    CORPUS_KINDS,
    CorpusError,
    build_corpus,
    load_corpus,
    verify_corpus,
)
from benchmarks.evaluation.cost import PricingError
from benchmarks.evaluation.environment import SelectionError, check_usable, resolve_selection
from benchmarks.evaluation.protocol import ProtocolError, load_protocol, resolve_parameters
from benchmarks.evaluation.report import AnalysisError, analyze
from benchmarks.evaluation.runner import (
    DEFAULT_MAX_LIVE_CALLS,
    EvaluationError,
    compute_offline_plan_digest,
    run_evaluation,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CORPUS = REPO_ROOT / "benchmarks" / "corpus" / "replay-v1" / "corpus.json"


def godot_version() -> str | None:
    binary = shutil.which(os.environ.get("GODOT", "godot"))
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks.evaluation",
        description="Run and report the POC provider evaluation protocol (issue #18).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    corpus = sub.add_parser("corpus", help="verify or build a replay corpus")
    corpus_sub = corpus.add_subparsers(dest="corpus_command", required=True)
    verify = corpus_sub.add_parser("verify", help="check a corpus against its manifest")
    verify.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="path to corpus.json")
    build = corpus_sub.add_parser("build", help="write a corpus from a GenerationRecorder file")
    build.add_argument("--from-recording", required=True, help="recording JSONL")
    build.add_argument("--out", required=True, help="output directory")
    build.add_argument("--id", required=True, dest="corpus_id")
    build.add_argument("--kind", choices=CORPUS_KINDS, default="replay")
    build.add_argument("--description", required=True)
    build.add_argument(
        "--provenance", action="append", default=[], metavar="KEY=VALUE", help="repeatable"
    )
    build.add_argument(
        "--stamp-offline-expected",
        action="store_true",
        help="run the offline rules baseline and store its plan digest as the corpus golden",
    )

    run = sub.add_parser("run", help="run the protocol and write an evidence bundle")
    run.add_argument("--corpus", default=str(DEFAULT_CORPUS), help="path to corpus.json")
    run.add_argument(
        "--select",
        nargs="+",
        default=[RULES_PROVIDER_ID],
        metavar="PROVIDER[:MODEL]",
        help="providers/models to compare (default: the offline rules baseline)",
    )
    run.add_argument("--tier", default="full", help="smoke, standard or full (see protocol.json)")
    run.add_argument("--warmup-requests", type=int, help="override the tier (recorded)")
    run.add_argument("--iterations", type=int, help="override the tier (recorded)")
    run.add_argument("--timeout", type=float, help="per-request timeout in seconds (recorded)")
    run.add_argument("--live", action="store_true", help="confirm billable hosted-provider calls")
    run.add_argument("--max-live-calls", type=int, default=DEFAULT_MAX_LIVE_CALLS)
    run.add_argument("--vantage-point", help="free text: where this ran from (network location)")
    run.add_argument("--label", help="free-text run label")
    run.add_argument("--pricing", help="price table (default: the template; hosted prices unknown)")
    run.add_argument("--observations", help="subjective notes file to include in the report")
    run.add_argument("--out", help="bundle directory (default: evaluation-output/<UTC stamp>)")

    report = sub.add_parser("report", help="(re)derive the report of an existing bundle")
    report.add_argument("bundle")
    report.add_argument("--pricing")
    report.add_argument("--observations")

    check = sub.add_parser("verify", help="verify a bundle: integrity, coverage, reproduction")
    check.add_argument("bundle")
    return parser


def _cmd_corpus(args: argparse.Namespace) -> int:
    if args.corpus_command == "verify":
        problems, requests = verify_corpus(args.corpus)
        for problem in problems:
            print(f"FAIL: {problem}", file=sys.stderr)
        if problems:
            return 1
        print(f"corpus OK: {len(requests)} requests, {args.corpus}")
        return 0
    provenance = {}
    for item in args.provenance:
        key, sep, value = item.partition("=")
        if not sep:
            raise CorpusError(f"--provenance expects KEY=VALUE, got {item!r}")
        provenance[key] = value
    manifest = build_corpus(
        args.from_recording,
        args.out,
        corpus_id=args.corpus_id,
        kind=args.kind,
        description=args.description,
        provenance=provenance,
    )
    if args.stamp_offline_expected:
        import json

        from benchmarks.evaluation.corpus import MANIFEST_NAME

        digest = compute_offline_plan_digest(load_corpus(Path(args.out) / MANIFEST_NAME).requests)
        manifest["expected_offline"] = {
            f"{RULES_PROVIDER_ID}/{RULES_MODEL}": {
                "plan_digest": digest,
                "definition": "sha256 of the canonical decisions of iteration 1, sorted by "
                "request_id (benchmarks.evaluation.bundle.plan_digest)",
            }
        }
        (Path(args.out) / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    print(f"corpus written: {args.out} ({manifest['files'][0]['records']} requests)")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    corpus = load_corpus(args.corpus)
    params = resolve_parameters(
        load_protocol(),
        args.tier,
        warmup_requests=args.warmup_requests,
        iterations=args.iterations,
        timeout_seconds=args.timeout,
    )
    selections = [resolve_selection(spec, os.environ) for spec in args.select]
    if args.live:
        for selection in selections:
            if selection.is_live:
                check_usable(selection)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = Path(args.out) if args.out else REPO_ROOT / "evaluation-output" / stamp
    run_evaluation(
        corpus,
        selections,
        params,
        out,
        repo_root=REPO_ROOT,
        live=args.live,
        max_live_calls=args.max_live_calls,
        vantage_point=args.vantage_point,
        label=args.label,
        godot_version=godot_version,
    )
    report = analyze(
        out, repo_root=REPO_ROOT, pricing_path=args.pricing, observations_path=args.observations
    )
    print(f"bundle: {out}\nreport: {report}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    report = analyze(
        args.bundle,
        repo_root=REPO_ROOT,
        pricing_path=args.pricing,
        observations_path=args.observations,
    )
    print(f"report: {report}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    problems = verify_bundle(args.bundle)
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    if not problems:
        print(f"bundle OK: {args.bundle}")
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    handlers = {
        "corpus": _cmd_corpus,
        "run": _cmd_run,
        "report": _cmd_report,
        "verify": _cmd_verify,
    }
    try:
        code = handlers[args.command](args)
    except (
        CorpusError,
        SelectionError,
        ProtocolError,
        EvaluationError,
        AnalysisError,
        PricingError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        code = 2
    sys.exit(code)
