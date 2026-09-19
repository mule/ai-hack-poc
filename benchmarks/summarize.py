"""Provider/model benchmark summaries from replay results (issue #15).

Turns the raw per-request output of ``python -m benchmarks.replay`` (JSON report
or JSONL stream) into apples-to-apples summaries, one row per provider + model.

Conventions
-----------
* **Grouping:** ``(provider, model)``. Two models of one provider are two groups.
* **Percentiles:** linear interpolation between closest ranks,
  ``rank = p * (n - 1)`` on the sorted samples, 0-indexed (Hyndman-Fan type 7,
  the numpy default). See :data:`PERCENTILE_CONVENTION`.
* **Latency** covers *every* request, failures included: a timeout is exactly
  the tail a caller experiences, so dropping it would flatter the provider.
* **Missing is not zero.** A metric no result reported is ``None`` (JSON
  ``null``, an empty CSV cell, ``n/a`` in text) and every metric carries a
  ``reported`` count so partial coverage is visible.
* **Error buckets** partition all failures: ``timeouts`` (``provider_timeout``),
  ``schema_failures`` (``schema_violation``, ``invalid_json``,
  ``empty_response``) and ``other_errors`` (everything else, including a failure
  with no error code). Rates use every request in the group as the denominator.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from dungeon_director.contracts import SUPPORTED_CONTRACT_MAJOR, RoomType

SCHEMA_VERSION = 1

PERCENTILE_CONVENTION = (
    "Percentiles use linear interpolation between closest ranks: "
    "rank = p * (n - 1) on the sorted samples (0-indexed), so with few samples the "
    "upper percentiles interpolate toward the maximum."
)

PERCENTILES = (("p50", 0.50), ("p90", 0.90), ("p95", 0.95), ("p99", 0.99))

TIMEOUT_CODES = frozenset({"provider_timeout"})
SCHEMA_FAILURE_CODES = frozenset({"schema_violation", "invalid_json", "empty_response"})

_SUMMARY_TYPE = "benchmark_summary"
_RESULT_TYPE = "benchmark_result"

_DANGER_LEVELS = tuple(str(level) for level in range(1, 6))
_ROOM_TYPES = tuple(room_type.value for room_type in RoomType)


class SummaryInputError(ValueError):
    """A replay output file is malformed or incompatible; the message names where."""


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def percentile(values: Sequence[float], p: float) -> float | None:
    """Percentile ``p`` (0.0-1.0) by linear interpolation; ``None`` without samples."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * p
    lower = math.floor(rank)
    upper = math.ceil(rank)
    if lower == upper:
        return float(ordered[lower])
    return float(ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower))


def _round(value: float | None, digits: int) -> float | None:
    return None if value is None else round(value, digits)


def _rate(count: int, total: int) -> float:
    return round(count / total, 4) if total else 0.0


# ---------------------------------------------------------------------------
# Loading and validation
# ---------------------------------------------------------------------------


def load_results(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """Read and validate replay results from JSON and/or JSONL files.

    Accepted shapes: the ``--output *.json`` report (an object with ``results``),
    the ``--output *.jsonl`` stream (a ``benchmark_summary`` header followed by
    ``benchmark_result`` lines), a bare JSON array of results, and bare JSONL
    of results. Results from several files are merged.
    """
    results: list[dict[str, Any]] = []
    seen: dict[tuple[str, str, str, Any], str] = {}
    for raw_path in paths:
        path = Path(raw_path)
        for where, record in _read_file(path):
            key = (record["provider"], record["model"], record["request_id"], record["iteration"])
            if key in seen:
                raise SummaryInputError(
                    f"{where}: duplicate result for provider={key[0]!r} model={key[1]!r} "
                    f"request_id={key[2]!r} iteration={key[3]!r} (already read from {seen[key]}); "
                    "the same replay output was probably passed twice"
                )
            seen[key] = where
            results.append(record)
    return results


def _read_file(path: Path) -> list[tuple[str, dict[str, Any]]]:
    if not path.is_file():
        raise SummaryInputError(f"{path}: file not found")
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SummaryInputError(f"{path}: cannot read file: {exc}") from exc

    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        records = _read_jsonl(path, text)
    else:
        records = _read_document(path, document)

    if not records:
        raise SummaryInputError(f"{path}: no replay results found in file")
    return records


def _read_document(path: Path, document: Any) -> list[tuple[str, dict[str, Any]]]:
    if isinstance(document, dict):
        if "results" in document:
            _check_contract_version(document.get("contract_version"), str(path))
            items = document["results"]
            if not isinstance(items, list):
                raise SummaryInputError(f"{path}: 'results' must be a list of replay results")
            return _validate_items(path, items, label="result")
        if document.get("type") in (_SUMMARY_TYPE, _RESULT_TYPE) or "success" in document:
            return _read_jsonl_objects(path, [(1, document)])
        raise SummaryInputError(
            f"{path}: not a benchmarks.replay output: no 'results' list. Re-run "
            "'python -m benchmarks.replay' so per-request results are included."
        )
    if isinstance(document, list):
        return _validate_items(path, document, label="result")
    raise SummaryInputError(
        f"{path}: not a benchmarks.replay output: expected a JSON object with 'results', "
        f"a JSON array, or JSONL, got {type(document).__name__}"
    )


def _read_jsonl(path: Path, text: str) -> list[tuple[str, dict[str, Any]]]:
    parsed: list[tuple[int, Any]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            parsed.append((line_number, json.loads(stripped)))
        except json.JSONDecodeError as exc:
            raise SummaryInputError(
                f"{path}: malformed JSON on line {line_number}: {exc.msg} (column {exc.colno})"
            ) from exc
    return _read_jsonl_objects(path, parsed)


def _read_jsonl_objects(
    path: Path, lines: list[tuple[int, Any]]
) -> list[tuple[str, dict[str, Any]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    for line_number, item in lines:
        where = f"{path}: line {line_number}"
        if not isinstance(item, dict):
            raise SummaryInputError(f"{where}: expected a JSON object, got {type(item).__name__}")
        record_type = item.get("type")
        if record_type == _SUMMARY_TYPE:
            _check_contract_version(item.get("contract_version"), where)
            continue
        if record_type not in (None, _RESULT_TYPE):
            raise SummaryInputError(f"{where}: unknown record type {record_type!r}")
        records.append((where, _validate_result(item, where)))
    return records


def _validate_items(
    path: Path, items: list[Any], *, label: str
) -> list[tuple[str, dict[str, Any]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    for index, item in enumerate(items, start=1):
        where = f"{path}: {label} {index}"
        if not isinstance(item, dict):
            raise SummaryInputError(f"{where}: expected a JSON object, got {type(item).__name__}")
        if item.get("type") not in (None, _RESULT_TYPE):
            raise SummaryInputError(f"{where}: unknown record type {item.get('type')!r}")
        records.append((where, _validate_result(item, where)))
    return records


def _check_contract_version(value: Any, where: str) -> None:
    if value is None:
        return
    try:
        major = int(str(value).split(".")[0])
    except ValueError:
        major = None
    if major != SUPPORTED_CONTRACT_MAJOR:
        raise SummaryInputError(
            f"{where}: incompatible contract version {value!r}; "
            f"this tool reads major version {SUPPORTED_CONTRACT_MAJOR}"
        )


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _validate_result(item: dict[str, Any], where: str) -> dict[str, Any]:
    if "success" not in item and "request" in item:
        raise SummaryInputError(
            f"{where}: this looks like a recorded dataset event (an input of benchmarks.replay), "
            "not a replay result; run benchmarks.replay first and summarize its output"
        )

    def fail(field: str, problem: str) -> SummaryInputError:
        return SummaryInputError(f"{where}: field '{field}' {problem}")

    for field in ("provider", "model", "request_id"):
        if field not in item:
            raise fail(field, "is required")
        if not isinstance(item[field], str) or not item[field]:
            raise fail(field, "must be a non-empty string")
    if "success" not in item:
        raise fail("success", "is required")
    if not isinstance(item["success"], bool):
        raise fail("success", "must be true or false")

    iteration = item.get("iteration", 1)
    if not _is_count(iteration):
        raise fail("iteration", "must be a non-negative integer")

    latency = item.get("latency_ms")
    if latency is not None and (not _is_number(latency) or latency < 0):
        raise fail("latency_ms", "must be a non-negative number or null")

    error_code = item.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        raise fail("error_code", "must be a string or null")

    retry_count = item.get("retry_count")
    if retry_count is not None and not _is_count(retry_count):
        raise fail("retry_count", "must be a non-negative integer or null")

    usage = item.get("usage")
    if usage is not None:
        if not isinstance(usage, dict):
            raise fail("usage", "must be an object or null")
        for token_field in ("input_tokens", "output_tokens", "total_tokens"):
            value = usage.get(token_field)
            if value is not None and not _is_count(value):
                raise fail(token_field, "must be a non-negative integer or null")
        cost = usage.get("estimated_cost_usd")
        if cost is not None and (not _is_number(cost) or cost < 0):
            raise fail("estimated_cost_usd", "must be a non-negative number or null")

    room = item.get("room")
    if room is not None:
        if not isinstance(room, dict):
            raise fail("room", "must be an object or null")
        if not isinstance(room.get("room_type"), str) or not room["room_type"]:
            raise fail("room_type", "is required in a room and must be a non-empty string")
        if not _is_count(room.get("danger")):
            raise fail("danger", "is required in a room and must be a non-negative integer")

    normalized = {key: value for key, value in item.items() if key != "type"}
    normalized["iteration"] = iteration
    return normalized


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _token_stats(values: list[int]) -> dict[str, Any]:
    if not values:
        return {"reported": 0, "min": None, "avg": None, "max": None}
    return {
        "reported": len(values),
        "min": min(values),
        "avg": round(math.fsum(values) / len(values), 3),
        "max": max(values),
    }


def _distribution(values: list[str]) -> dict[str, Any]:
    if not values:
        return {"reported": 0, "counts": None, "rates": None}
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    ordered = {key: counts[key] for key in sorted(counts)}
    return {
        "reported": len(values),
        "counts": ordered,
        "rates": {key: _rate(count, len(values)) for key, count in ordered.items()},
    }


def _summarize_group(provider: str, model: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(items)

    latencies = [float(i["latency_ms"]) for i in items if i.get("latency_ms") is not None]
    latency: dict[str, Any] = {"reported": len(latencies)}
    for name, p in PERCENTILES:
        latency[name] = _round(percentile(latencies, p), 3)
    latency["min"] = _round(min(latencies), 3) if latencies else None
    latency["mean"] = _round(math.fsum(latencies) / len(latencies), 3) if latencies else None
    latency["max"] = _round(max(latencies), 3) if latencies else None

    successes = sum(1 for i in items if i["success"])
    failures = [i for i in items if not i["success"]]
    timeouts = sum(1 for i in failures if i.get("error_code") in TIMEOUT_CODES)
    schema_failures = sum(1 for i in failures if i.get("error_code") in SCHEMA_FAILURE_CODES)
    other_errors = len(failures) - timeouts - schema_failures
    outcomes = {
        "successes": successes,
        "success_rate": _rate(successes, total),
        "timeouts": timeouts,
        "timeout_rate": _rate(timeouts, total),
        "schema_failures": schema_failures,
        "schema_failure_rate": _rate(schema_failures, total),
        "other_errors": other_errors,
        "other_error_rate": _rate(other_errors, total),
    }

    retry_counts = [int(i["retry_count"]) for i in items if i.get("retry_count") is not None]
    if retry_counts:
        retried = sum(1 for count in retry_counts if count > 0)
        retries: dict[str, Any] = {
            "reported": len(retry_counts),
            "count": sum(retry_counts),
            "requests_retried": retried,
            "rate": _rate(retried, len(retry_counts)),
        }
    else:
        retries = {"reported": 0, "count": None, "requests_retried": None, "rate": None}

    inputs: list[int] = []
    outputs: list[int] = []
    totals: list[int] = []
    costs: list[float] = []
    for item in items:
        usage = item.get("usage") or {}
        input_tokens = usage.get("input_tokens")
        output_tokens = usage.get("output_tokens")
        total_tokens = usage.get("total_tokens")
        if input_tokens is not None:
            inputs.append(input_tokens)
        if output_tokens is not None:
            outputs.append(output_tokens)
        if total_tokens is not None:
            totals.append(total_tokens)
        elif input_tokens is not None and output_tokens is not None:
            totals.append(input_tokens + output_tokens)
        if usage.get("estimated_cost_usd") is not None:
            costs.append(float(usage["estimated_cost_usd"]))
    cost_total = math.fsum(costs)
    cost = {
        "reported": len(costs),
        "total_usd": round(cost_total, 6) if costs else None,
        "per_1000_decisions_usd": round(cost_total / len(costs) * 1000, 6) if costs else None,
    }

    rooms = [i["room"] for i in items if i.get("room")]
    return {
        "provider": provider,
        "model": model,
        "requests": total,
        "unique_requests": len({i["request_id"] for i in items}),
        "latency_ms": latency,
        "outcomes": outcomes,
        "retries": retries,
        "tokens": {
            "input": _token_stats(inputs),
            "output": _token_stats(outputs),
            "total": _token_stats(totals),
        },
        "cost": cost,
        "room_types": _distribution([room["room_type"] for room in rooms]),
        "danger": _distribution([str(room["danger"]) for room in rooms]),
    }


def summarize_results(
    results: Iterable[dict[str, Any]], *, sources: Sequence[str | Path] | None = None
) -> dict[str, Any]:
    """Aggregate validated replay results into one summary per provider + model."""
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    count = 0
    for result in results:
        grouped[(result["provider"], result["model"])].append(result)
        count += 1

    groups = [
        _summarize_group(provider, model, grouped[(provider, model)])
        for provider, model in sorted(grouped)
    ]

    request_sets = {
        key: frozenset(item["request_id"] for item in items) for key, items in grouped.items()
    }
    warnings: list[str] = []
    if len(set(request_sets.values())) > 1:
        sizes = ", ".join(
            f"{provider}/{model}: {len(request_sets[(provider, model)])}"
            for provider, model in sorted(request_sets)
        )
        warnings.append(
            f"groups ran over different request sets ({sizes} unique requests); "
            "latency and rates are not directly comparable"
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "percentile_convention": PERCENTILE_CONVENTION,
        "sources": [str(source) for source in sources or ()],
        "total_results": count,
        "groups": groups,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_json(report: dict[str, Any]) -> str:
    return json.dumps(report, indent=2, allow_nan=False) + "\n"


def _csv_key_columns(report: dict[str, Any], section: str, baseline: Sequence[str]) -> list[str]:
    observed = {
        key for group in report["groups"] for key in (group[section]["counts"] or {}).keys()
    }
    return [*baseline, *sorted(observed.difference(baseline))]


def render_csv(report: dict[str, Any]) -> str:
    """One row per provider/model. Unavailable metrics are empty cells, never zero."""
    room_columns = _csv_key_columns(report, "room_types", _ROOM_TYPES)
    danger_columns = _csv_key_columns(report, "danger", _DANGER_LEVELS)

    header = [
        "provider",
        "model",
        "requests",
        "unique_requests",
        "latency_reported",
        "latency_p99_ms",
        "latency_p95_ms",
        "latency_p90_ms",
        "latency_p50_ms",
        "latency_min_ms",
        "latency_mean_ms",
        "latency_max_ms",
        "successes",
        "success_rate",
        "timeouts",
        "timeout_rate",
        "schema_failures",
        "schema_failure_rate",
        "other_errors",
        "other_error_rate",
        "retry_reported",
        "retry_count",
        "retry_requests_retried",
        "retry_rate",
    ]
    for kind in ("input", "output", "total"):
        header += [f"{kind}_tokens_{stat}" for stat in ("reported", "min", "avg", "max")]
    header += [
        "cost_reported",
        "cost_total_usd",
        "cost_per_1000_decisions_usd",
        "room_types_reported",
    ]
    for room_type in room_columns:
        header += [f"room_type_{room_type}_count", f"room_type_{room_type}_rate"]
    header.append("danger_reported")
    for level in danger_columns:
        header += [f"danger_{level}_count", f"danger_{level}_rate"]

    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(header)
    for group in report["groups"]:
        latency, outcomes, retries = group["latency_ms"], group["outcomes"], group["retries"]
        row: list[Any] = [
            group["provider"],
            group["model"],
            group["requests"],
            group["unique_requests"],
            latency["reported"],
            latency["p99"],
            latency["p95"],
            latency["p90"],
            latency["p50"],
            latency["min"],
            latency["mean"],
            latency["max"],
            outcomes["successes"],
            outcomes["success_rate"],
            outcomes["timeouts"],
            outcomes["timeout_rate"],
            outcomes["schema_failures"],
            outcomes["schema_failure_rate"],
            outcomes["other_errors"],
            outcomes["other_error_rate"],
            retries["reported"],
            retries["count"],
            retries["requests_retried"],
            retries["rate"],
        ]
        for kind in ("input", "output", "total"):
            stats = group["tokens"][kind]
            row += [stats["reported"], stats["min"], stats["avg"], stats["max"]]
        cost = group["cost"]
        row += [cost["reported"], cost["total_usd"], cost["per_1000_decisions_usd"]]
        row.append(group["room_types"]["reported"])
        row += _distribution_cells(group["room_types"], room_columns)
        row.append(group["danger"]["reported"])
        row += _distribution_cells(group["danger"], danger_columns)
        writer.writerow(["" if cell is None else cell for cell in row])
    return buffer.getvalue()


def _distribution_cells(distribution: dict[str, Any], columns: Sequence[str]) -> list[Any]:
    """(count, rate) per column; empty when the group recorded no rooms at all."""
    counts, rates = distribution["counts"], distribution["rates"]
    cells: list[Any] = []
    for column in columns:
        if counts is None:
            cells += [None, None]
        else:
            cells += [counts.get(column, 0), rates.get(column, 0.0)]
    return cells


def _fmt(value: float | int | None, digits: int = 1) -> str:
    if value is None:
        return "n/a"
    return f"{value:,.{digits}f}"


def _fmt_count_rate(count: int, rate: float) -> str:
    return f"{count} ({rate * 100:.1f}%)"


def _table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows, strict=True)]
    lines = ["  ".join(str(h).ljust(w) for h, w in zip(headers, widths, strict=True)).rstrip()]
    lines.append("  ".join("-" * w for w in widths))
    for row in rows:
        lines.append("  ".join(str(c).ljust(w) for c, w in zip(row, widths, strict=True)).rstrip())
    return lines


def render_text(report: dict[str, Any]) -> str:
    """Human-readable report: tail latency first, then reliability, tokens/cost, rooms."""
    groups = report["groups"]
    out: list[str] = [
        f"BENCHMARK SUMMARY: {report['total_results']} results, "
        f"{len(groups)} provider/model group(s)",
        report["percentile_convention"],
        "n/a = not reported by the provider; 'k/n' = k of n requests reported the metric.",
    ]
    if report["sources"]:
        out.append("Sources: " + ", ".join(report["sources"]))
    for warning in report["warnings"]:
        out.append(f"WARNING: {warning}")

    out += ["", "END-TO-END LATENCY (ms), tail first"]
    out += _table(
        ["provider", "model", "n", "p99", "p95", "p90", "p50", "mean", "max"],
        [
            [
                g["provider"],
                g["model"],
                f"{g['latency_ms']['reported']}/{g['requests']}",
                _fmt(g["latency_ms"]["p99"]),
                _fmt(g["latency_ms"]["p95"]),
                _fmt(g["latency_ms"]["p90"]),
                _fmt(g["latency_ms"]["p50"]),
                _fmt(g["latency_ms"]["mean"]),
                _fmt(g["latency_ms"]["max"]),
            ]
            for g in groups
        ],
    )

    out += ["", "RELIABILITY (counts and share of all requests)"]
    out += _table(
        [
            "provider",
            "model",
            "requests",
            "success",
            "timeout",
            "schema fail",
            "other error",
            "retried",
        ],
        [
            [
                g["provider"],
                g["model"],
                str(g["requests"]),
                _fmt_count_rate(g["outcomes"]["successes"], g["outcomes"]["success_rate"]),
                _fmt_count_rate(g["outcomes"]["timeouts"], g["outcomes"]["timeout_rate"]),
                _fmt_count_rate(
                    g["outcomes"]["schema_failures"], g["outcomes"]["schema_failure_rate"]
                ),
                _fmt_count_rate(g["outcomes"]["other_errors"], g["outcomes"]["other_error_rate"]),
                _retry_cell(g),
            ]
            for g in groups
        ],
    )

    out += ["", "TOKENS (min / avg / max, coverage) AND COST"]
    out += _table(
        ["provider", "model", "input", "output", "total", "cost per 1000 decisions"],
        [
            [
                g["provider"],
                g["model"],
                _token_cell(g["tokens"]["input"], g["requests"]),
                _token_cell(g["tokens"]["output"], g["requests"]),
                _token_cell(g["tokens"]["total"], g["requests"]),
                _cost_cell(g["cost"], g["requests"]),
            ]
            for g in groups
        ],
    )

    out += ["", "ROOM DECISIONS (successful rooms only)"]
    for g in groups:
        out.append(
            f"{g['provider']} / {g['model']}: "
            f"room type [{_distribution_text(g['room_types'], g['outcomes']['successes'])}]; "
            f"danger [{_distribution_text(g['danger'], g['outcomes']['successes'], 'level ')}]"
        )
    return "\n".join(out) + "\n"


def _retry_cell(group: dict[str, Any]) -> str:
    retries = group["retries"]
    if retries["reported"] == 0:
        return f"n/a (0/{group['requests']})"
    return (
        f"{_fmt_count_rate(retries['requests_retried'], retries['rate'])} "
        f"[{retries['reported']}/{group['requests']}]"
    )


def _token_cell(stats: dict[str, Any], requests: int) -> str:
    if stats["reported"] == 0:
        return f"n/a (0/{requests})"
    return f"{stats['min']}/{_fmt(stats['avg'])}/{stats['max']} ({stats['reported']}/{requests})"


def _cost_cell(cost: dict[str, Any], requests: int) -> str:
    if cost["reported"] == 0:
        return f"n/a (0/{requests})"
    return f"${cost['per_1000_decisions_usd']:.4f} ({cost['reported']}/{requests})"


def _distribution_text(distribution: dict[str, Any], successes: int, prefix: str = "") -> str:
    if distribution["reported"] == 0:
        return f"n/a (0/{successes})"
    parts = [
        f"{prefix}{key} {count} ({distribution['rates'][key] * 100:.1f}%)"
        for key, count in distribution["counts"].items()
    ]
    return ", ".join(parts)


RENDERERS = {"text": render_text, "json": render_json, "csv": render_csv}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="benchmarks.summarize",
        description=(
            "Summarize benchmarks.replay results per provider and model: tail latency, "
            "success/timeout/schema-failure rates, retries, tokens, cost and room distributions."
        ),
    )
    parser.add_argument(
        "--input",
        "-i",
        nargs="+",
        required=True,
        help="One or more replay outputs (.json report or .jsonl stream); results are merged.",
    )
    parser.add_argument(
        "--format",
        "-f",
        choices=sorted(RENDERERS),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--output",
        "-o",
        help="Write the summary to this file instead of stdout.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        results = load_results(args.input)
    except SummaryInputError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    rendered = RENDERERS[args.format](summarize_results(results, sources=args.input))
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(f"Summary written to {out_path}", file=sys.stderr)
    else:
        sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    sys.exit(main())
