"""Derived analysis and the report for a verified evidence bundle.

``analyze`` never talks to a provider. It reads a bundle that passed
``verify_bundle`` and writes, next to the raw files:

* ``summary.{txt,json,csv}``  the output of ``python -m benchmarks.summarize`` (issue #15)
* ``metrics.json``            sample adequacy, cold start, derived cost, determinism
* ``behavior.json``           descriptive behaviour metrics
* ``report.md``               everything above with measured / derived / subjective apart

The report orders providers alphabetically and contains no ranking or verdict.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from benchmarks.evaluation.behavior import pairwise_agreement, selection_behavior
from benchmarks.evaluation.bundle import Bundle, plan_digest, verify_bundle
from benchmarks.evaluation.corpus import verify_corpus
from benchmarks.evaluation.cost import PRICING_TEMPLATE, derive_cost, load_pricing
from benchmarks.evaluation.protocol import sample_adequacy

SUMMARIZER_MODULE = "benchmarks.summarize"
_IDENTITY_KEY_RE = re.compile(r"model|fingerprint|version|tier", re.IGNORECASE)
OBSERVATION_KEYS = ("author", "date", "context", "text")


class AnalysisError(ValueError):
    """The bundle failed verification or an input file is invalid."""


def load_observations(path: str | Path) -> list[dict[str, Any]]:
    """Subjective notes; each must say who, when, in what context, and be marked subjective."""
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AnalysisError(f"cannot read observations {path}: {exc}") from exc
    items = document.get("observations") if isinstance(document, dict) else None
    if not isinstance(items, list):
        raise AnalysisError(f"{path}: expected an object with an 'observations' list")
    for index, item in enumerate(items, start=1):
        missing = [k for k in OBSERVATION_KEYS if not isinstance(item.get(k), str) or not item[k]]
        if missing or item.get("kind") != "subjective":
            raise AnalysisError(
                f"{path}: observations[{index}] needs {OBSERVATION_KEYS} and "
                "kind == 'subjective' (measurements do not belong here)"
            )
    return items


def run_summarizer(
    results_path: Path, out_dir: Path, *, repo_root: Path, module: str = SUMMARIZER_MODULE
) -> dict[str, Any]:
    """Call the issue #15 summarizer on the bundle's own results; never reimplement it."""
    try:
        present = importlib.util.find_spec(module) is not None
    except ModuleNotFoundError:
        present = False
    if not present:
        return {
            "status": "unavailable",
            "reason": f"{module} (issue #15) is not present in this checkout",
        }
    files = {}
    for fmt, ext in (("text", "txt"), ("json", "json"), ("csv", "csv")):
        target = out_dir / f"summary.{ext}"
        command = [
            sys.executable, "-m", module,
            "--input", str(results_path), "--format", fmt, "--output", str(target),
        ]  # fmt: skip
        proc = subprocess.run(command, capture_output=True, text=True, cwd=repo_root, timeout=300)
        if proc.returncode != 0:
            return {"status": "failed", "reason": (proc.stderr or proc.stdout).strip()[-400:]}
        files[fmt] = target.name
    return {"status": "ok", "module": module, "files": files}


def _observed_identity(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    seen: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        for key, value in (row.get("provider_metadata") or {}).items():
            if _IDENTITY_KEY_RE.search(key) and isinstance(value, str | int | float):
                seen[key][str(value)] += 1
    return {key: dict(sorted(counts.items())) for key, counts in sorted(seen.items())}


def compute_metrics(bundle: Bundle, pricing: dict[tuple[str, str], dict[str, Any]]) -> dict:
    golden = bundle.corpus_manifest.get("expected_offline", {})
    metrics: dict[str, Any] = {}
    for label in bundle.labels:
        rows = bundle.rows_for(label)
        provider, _, model = label.partition("/")
        warm = sorted(bundle.rows_for(label, "warmup"), key=lambda r: r["sequence"])
        first = [r for r in rows if r["iteration"] == 1]
        by_request: dict[str, set[str]] = defaultdict(set)
        for r in rows:
            if r.get("success"):
                by_request[r["request_id"]].add(json.dumps(r["room"], sort_keys=True))
        repeated = [rid for rid in by_request if sum(x["request_id"] == rid for x in rows) > 1]
        metrics[label] = {
            "sample_adequacy": sample_adequacy(len(rows), _protocol_view(bundle)),
            "cold_start": (
                {
                    "latency_ms": warm[0]["latency_ms"],
                    "success": warm[0]["success"],
                    "error_code": warm[0]["error_code"],
                    "note": "single sample: the first warm-up call, after process start",
                }
                if warm
                else None
            ),
            "cost": derive_cost(rows, pricing.get((provider, model))),
            "determinism": {
                "plan_digest": plan_digest(first),
                "identical_across_iterations_rate": (
                    round(sum(len(by_request[r]) == 1 for r in repeated) / len(repeated), 4)
                    if repeated
                    else None
                ),
                "corpus_expected_digest_matches": (
                    golden[label]["plan_digest"] == plan_digest(first) if label in golden else None
                ),
            },
            "observed_upstream_identity": _observed_identity(rows + warm),
        }
    return metrics


def _protocol_view(bundle: Bundle) -> dict[str, Any]:
    return {
        "min_tail_samples": bundle.protocol["min_tail_samples"],
        "percentiles": bundle.protocol["percentiles"],
    }


def analyze(
    bundle_dir: str | Path,
    *,
    repo_root: Path,
    pricing_path: str | Path | None = None,
    observations_path: str | Path | None = None,
    summarizer_module: str = SUMMARIZER_MODULE,
) -> Path:
    """Verify the bundle, derive everything, write ``report.md``; returns its path."""
    root = Path(bundle_dir)
    problems = verify_bundle(root)
    if problems:
        raise AnalysisError(
            f"{root} is not a trustworthy evaluation bundle:\n  - " + "\n  - ".join(problems)
        )
    bundle = Bundle(root)
    pricing_source = Path(pricing_path) if pricing_path else PRICING_TEMPLATE
    pricing = load_pricing(pricing_source)
    (root / "pricing.json").write_bytes(pricing_source.read_bytes())

    observations: list[dict[str, Any]] = []
    if observations_path:
        observations = load_observations(observations_path)
        (root / "observations.json").write_bytes(Path(observations_path).read_bytes())
    elif (root / "observations.json").is_file():
        observations = load_observations(root / "observations.json")

    corpus_order = [
        (r.run_id, r.request_id) for r in verify_corpus(root / "corpus" / "corpus.json")[1]
    ]
    rows_by_label = {label: bundle.rows_for(label) for label in bundle.labels}
    metrics = compute_metrics(bundle, pricing)
    behavior = {
        "selections": {
            label: selection_behavior(rows, corpus_order) for label, rows in rows_by_label.items()
        },
        "pairwise_agreement": pairwise_agreement(rows_by_label),
    }
    summary = run_summarizer(
        root / "results.json", root, repo_root=repo_root, module=summarizer_module
    )
    for name, value in (("metrics.json", metrics), ("behavior.json", behavior)):
        (root / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", "utf-8")
    report = root / "report.md"
    report.write_text(render_report(bundle, metrics, behavior, summary, observations), "utf-8")
    return report


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    lines += ["| " + " | ".join(_fmt(c) for c in row) + " |" for row in rows]
    return "\n".join(lines) + "\n"


def _limitations(bundle: Bundle, metrics: dict[str, Any], summary: dict[str, Any]) -> list[str]:
    run, corpus = bundle.manifest["run"], bundle.manifest["corpus"]
    flags: list[str] = []
    for label, m in metrics.items():
        weak = [p for p, v in m["sample_adequacy"]["percentiles"].items() if not v["reliable"]]
        if weak:
            flags.append(
                f"{label}: {m['sample_adequacy']['samples']} samples are too few for "
                f"{', '.join(weak)}; treat those values as indicative only."
            )
        if m["cost"]["status"] != "computed":
            flags.append(f"{label}: cost is {m['cost']['status']}; no cost comparison is possible.")
        for key, values in m["observed_upstream_identity"].items():
            if len(values) > 1:
                flags.append(
                    f"{label}: the provider reported several values for {key!r} during the run "
                    f"({', '.join(values)}): the served model may have changed mid-run."
                )
    if run["live"]:
        flags.append(
            "Live run: latency includes this machine's network path"
            + (
                f" (vantage point: {bundle.environment['vantage_point']})"
                if bundle.environment["vantage_point"]
                else " (vantage point not recorded)"
            )
            + " and provider load at that moment; repeat at other times before generalizing."
        )
        if bundle.protocol["parameters"]["iterations"] > 1:
            flags.append(
                "The same requests are repeated across iterations, so provider-side caching "
                "could flatter later iterations; compare iteration 1 with the rest in results.json."
            )
    if corpus["kind"] == "live":
        flags.append(
            "Live corpus: recorded from play, not reproducible from the repository; "
            "do not compare with results from the fixed replay corpus."
        )
    if summary["status"] != "ok":
        flags.append(f"Summary tables unavailable: {summary.get('reason')}")
    recorded = bundle.corpus_manifest.get("provenance", {}).get("recorded_providers")
    if recorded:
        flags.append(
            "The corpus states were shaped by the providers that played it "
            f"({', '.join(sorted(recorded))}); other providers are answering states they "
            "would not necessarily have reached."
        )
    return flags


def render_report(
    bundle: Bundle,
    metrics: dict[str, Any],
    behavior: dict[str, Any],
    summary: dict[str, Any],
    observations: list[dict[str, Any]],
) -> str:
    manifest, env, params = bundle.manifest, bundle.environment, bundle.protocol["parameters"]
    corpus = manifest["corpus"]
    protocol_name = f"{bundle.protocol['protocol_id']} {bundle.protocol['protocol_version']}"
    corpus_line = f"{corpus['corpus_id']} ({corpus['kind']}), {corpus['records']} requests"
    warmup_line = f"{params['warmup_requests']} / {params['iterations']}"
    timeout_line = f"{params['timeout_seconds']} / {params['concurrency']} / 0"
    out = [
        f"# Evaluation report: {corpus['corpus_id']} ({corpus['kind']} corpus), "
        f"tier {params['tier']}",
        "",
        "> Generated by `python -m benchmarks.evaluation`. Sections 1 to 3 are **measured** "
        "or **derived** from calls this run made; section 4 **describes** returned plans; "
        "section 6 is **subjective**. Nothing here ranks providers or names a winner: "
        "rows are in alphabetical order and each reader weighs latency, cost, reliability "
        "and behaviour for their own use.",
        "",
        "## 1. Provenance and configuration (measured facts about the run)",
        "",
        _table(
            ["item", "value"],
            [
                ["protocol", protocol_name],
                ["run", f"{manifest['run']['started_at']} to {manifest['run']['finished_at']}"],
                ["label", manifest["run"]["label"]],
                ["live (billable) providers", manifest["run"]["live"]],
                ["corpus", corpus_line],
                ["corpus requests sha256", corpus["requests_sha256"]],
                ["git commit", env["code"]["git_commit"]],
                ["working tree dirty", env["code"]["git_dirty"]],
                ["python / platform", f"{env['runtime']['python']} / {env['runtime']['platform']}"],
                ["contract version", env["contract_version"]],
                ["packages", ", ".join(f"{k} {v}" for k, v in env["runtime"]["packages"].items())],
                ["vantage point", env["vantage_point"]],
                ["warm-up requests / iterations", warmup_line],
                ["timeout (s) / concurrency / retries", timeout_line],
                ["parameter overrides", params["overrides"] or "none"],
            ],
        ),
        "### Providers under test",
        "",
    ]
    provider_rows = []
    for entry in sorted(env["providers"], key=lambda e: (e["provider"], e["model"])):
        cfg = entry["config"]
        key = f"{entry['provider']}/{entry['model']}"
        provider_rows.append(
            [
                entry["provider"],
                entry["model"],
                json.dumps(cfg["fields"], sort_keys=True),
                ", ".join(cfg["withheld"]) or "-",
                cfg["credentials_present"],
                json.dumps(metrics[key]["observed_upstream_identity"]),
            ]
        )
    out += [
        _table(
            ["provider", "requested model", "config (non-secret)", "withheld fields",
             "credentials present", "upstream identity seen in responses"],
            provider_rows,
        ),
        "## 2. Measured: latency, reliability, tokens",
        "",
    ]  # fmt: skip
    if summary["status"] == "ok":
        text = (bundle.path / summary["files"]["text"]).read_text(encoding="utf-8")
        out += [
            "Produced by `python -m benchmarks.summarize` on this bundle's `results.json` "
            "(warm-up excluded). JSON and CSV siblings: `summary.json`, `summary.csv`.",
            "",
            "```text",
            text.rstrip(),
            "```",
            "",
        ]
    else:
        out += [
            f"Summary unavailable ({summary.get('reason')}). Once `benchmarks.summarize` is "
            "available run `make benchmark-summary BENCH_REPORT=<bundle>/results.json`.",
            "",
        ]
    out += ["### Sample adequacy and cold start", ""]
    adequacy_rows = []
    for label, m in metrics.items():
        for name, v in m["sample_adequacy"]["percentiles"].items():
            adequacy_rows.append(
                [label, name, m["sample_adequacy"]["samples"], v["min_samples"], v["reliable"]]
            )
    out += [
        _table(["provider/model", "percentile", "samples", "needed", "reliable"], adequacy_rows)
    ]
    out += [
        _table(
            ["provider/model", "first-call latency (ms)", "succeeded", "error code"],
            [
                [label, m["cold_start"]["latency_ms"], m["cold_start"]["success"],
                 m["cold_start"]["error_code"]]
                for label, m in metrics.items()
                if m["cold_start"]
            ],
        ),
        "",
        "## 3. Derived: normalized cost (measured tokens times a cited price table)",
        "",
    ]  # fmt: skip
    cost_rows = []
    for label, m in metrics.items():
        c = m["cost"]
        cost_rows.append(
            [label, c["status"], c.get("per_1000_decisions_usd"),
             c.get("per_1000_successful_decisions_usd"), c.get("price_source"),
             c.get("price_retrieved_on")]
        )  # fmt: skip
    out += [
        _table(
            ["provider/model", "status", "USD per 1000 decisions", "USD per 1000 successful",
             "price source", "retrieved"],
            cost_rows,
        ),
        "Failed calls are charged (a bad answer still spent tokens). `no_price` means the "
        "price table has no sourced price: cost is unknown, not zero.",
        "",
        "## 4. Descriptive: behaviour of the returned plans",
        "",
    ]  # fmt: skip
    behavior_rows = []
    for label, b in behavior["selections"].items():
        behavior_rows.append(
            [label, b["decisions"], b["diversity"]["room_type_entropy_bits"],
             b["diversity"]["distinct_signatures"], b["diversity"]["signature_repeat_rate"],
             b["repetition"]["consecutive_same_room_type_rate"],
             b["repetition"]["longest_same_room_type_streak"],
             b["danger_progression"]["first_third_mean"],
             b["danger_progression"]["last_third_mean"],
             b["self_consistency"]["same_room_type_across_iterations_rate"]]
        )  # fmt: skip
    out += [
        _table(
            ["provider/model", "decisions", "room-type entropy (bits)", "distinct signatures",
             "signature repeat rate", "consecutive same type", "longest streak",
             "danger first third", "danger last third", "same type across iterations"],
            behavior_rows,
        ),
        "Room-type and danger distributions are in the summary above. Whether more variety "
        "or a steeper danger curve is *better* is a design judgement, not a measurement.",
        "",
    ]  # fmt: skip
    if behavior["pairwise_agreement"]:
        out += [
            _table(
                ["a", "b", "requests", "same room type", "same signature"],
                [[p["a"], p["b"], p["requests_compared"], p["same_room_type_rate"],
                  p["same_signature_rate"]] for p in behavior["pairwise_agreement"]],
            )
        ]  # fmt: skip
    determinism_rows = [
        [label, m["determinism"]["plan_digest"][:16],
         m["determinism"]["identical_across_iterations_rate"],
         m["determinism"]["corpus_expected_digest_matches"]]
        for label, m in metrics.items()
    ]  # fmt: skip
    out += [
        "### Reproducibility of the returned plans",
        "",
        _table(
            ["provider/model", "plan digest (iteration 1)", "identical across iterations",
             "matches corpus expected digest"],
            determinism_rows,
        ),
        "## 5. Validity and limitations",
        "",
        "Flagged for this run:",
        "",
    ]  # fmt: skip
    flags = _limitations(bundle, metrics, summary)
    out += [f"- {flag}" for flag in flags] or ["- none beyond the standing limits below"]
    out += [
        "",
        "Standing limits (see `docs/evaluation-methodology.md`): network location and "
        "provider load move latency; hosted models change without notice; caching and "
        "cold starts shape early and repeated calls; a replayed request cannot show "
        "how a provider's earlier answers would have changed later states.",
        "",
        "## 6. Subjective observations (not measurements)",
        "",
    ]
    if observations:
        for item in observations:
            subject = f" [{item['selection']}]" if item.get("selection") else ""
            who = f"*{item['author']}, {item['date']}*{subject} ({item['context']})"
            out.append(f"- {who}: {item['text']}")
    else:
        out.append(
            "None recorded. Add impressions with `--observations <file>` "
            "(template: `benchmarks/evaluation/observations.template.json`)."
        )
    out.append("")
    return "\n".join(out)
