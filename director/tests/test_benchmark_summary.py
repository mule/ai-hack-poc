"""Tests for the provider/model benchmark summary CLI (issue #15).

Each test names the production change that would make it fail: a wrong
percentile convention, providers/models overwriting each other, a missing metric
reported as zero, a rate with the wrong denominator, a lost output format, and
so on. Inputs are built inline so the expected numbers can be checked by hand.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
from pathlib import Path
from typing import Any

import pytest
from benchmarks import summarize
from benchmarks.replay import (
    RULES_PROVIDER_ID,
    build_default_service,
    main_async,
    parse_model_selections,
    run_benchmark,
)
from benchmarks.summarize import (
    SummaryInputError,
    load_results,
    main,
    percentile,
    render_csv,
    render_json,
    render_text,
    summarize_results,
)
from fakes import make_request

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "benchmarks" / "fixtures"


def rec(
    provider: str = "groq",
    model: str = "m1",
    *,
    request_id: str = "req-1",
    iteration: int = 1,
    success: bool = True,
    latency_ms: float | None = 100.0,
    error_code: str | None = None,
    usage: dict[str, Any] | None = None,
    room: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One raw replay result as ``benchmarks.replay`` writes it."""
    record: dict[str, Any] = {
        "request_id": request_id,
        "run_id": "run-1",
        "provider": provider,
        "model": model,
        "iteration": iteration,
        "success": success,
        "status_code": 200 if success else 502,
        "error_code": error_code,
        "error_message": None if success else "boom",
        "usage": usage,
        "room": room,
        "provider_metadata": {},
    }
    if latency_ms is not None:
        record["latency_ms"] = latency_ms
    record.update(extra)
    return record


def numbered(count: int, **kwargs: Any) -> list[dict[str, Any]]:
    """``count`` records with unique request ids (so they never collide as duplicates)."""
    return [rec(request_id=f"req-{i}", **kwargs) for i in range(count)]


def group(report: dict[str, Any], provider: str, model: str) -> dict[str, Any]:
    matches = [g for g in report["groups"] if g["provider"] == provider and g["model"] == model]
    assert len(matches) == 1, f"expected one group for {provider}/{model}, got {len(matches)}"
    return matches[0]


def write_json(path: Path, payload: Any) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def write_jsonl(path: Path, lines: list[Any]) -> Path:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Percentile convention and tail latency
# ---------------------------------------------------------------------------


def test_percentile_uses_documented_linear_interpolation_convention():
    values = [float(v) for v in range(10, 101, 10)]  # 10, 20, ... 100
    # rank = p * (n - 1), 0-indexed, linear between closest ranks (numpy default).
    assert percentile(values, 0.50) == pytest.approx(55.0)
    assert percentile(values, 0.90) == pytest.approx(91.0)
    assert percentile(values, 0.95) == pytest.approx(95.5)
    assert percentile(values, 0.99) == pytest.approx(99.1)
    assert percentile(values, 0.0) == 10.0
    assert percentile(values, 1.0) == 100.0


def test_percentile_of_nothing_is_none_not_zero():
    assert percentile([], 0.99) is None


def test_percentile_of_one_sample_is_that_sample():
    assert percentile([42.0], 0.99) == 42.0


def test_percentile_ignores_input_order():
    assert percentile([50.0, 10.0, 30.0, 20.0, 40.0], 0.5) == 30.0


def test_latency_tail_percentiles_are_distinct_and_correct():
    # 1..100 ms: p50 50.5, p90 90.1, p95 95.05, p99 99.01 under the documented convention.
    records = [rec(request_id=f"r{i}", latency_ms=float(i)) for i in range(1, 101)]
    latency = group(summarize_results(records), "groq", "m1")["latency_ms"]
    assert latency["p50"] == pytest.approx(50.5)
    assert latency["p90"] == pytest.approx(90.1)
    assert latency["p95"] == pytest.approx(95.05)
    assert latency["p99"] == pytest.approx(99.01)
    assert latency["min"] == 1.0
    assert latency["max"] == 100.0
    assert latency["mean"] == pytest.approx(50.5)
    assert latency["reported"] == 100


def test_p99_exposes_a_tail_outlier_that_p95_hides():
    records = [rec(request_id=f"r{i}", latency_ms=100.0) for i in range(98)]
    records += [
        rec(request_id="slow-1", latency_ms=5000.0),
        rec(request_id="slow-2", latency_ms=9000.0),
    ]
    latency = group(summarize_results(records), "groq", "m1")["latency_ms"]
    assert latency["p50"] == 100.0
    assert latency["p95"] == 100.0
    assert latency["p99"] > 5000.0
    assert latency["max"] == 9000.0


def test_latency_includes_failed_requests_so_timeouts_show_in_the_tail():
    records = numbered(9, latency_ms=100.0)
    records.append(
        rec(request_id="timeout", success=False, error_code="provider_timeout", latency_ms=10_000.0)
    )
    latency = group(summarize_results(records), "groq", "m1")["latency_ms"]
    assert latency["max"] == 10_000.0
    assert latency["reported"] == 10


def test_latency_is_unavailable_not_zero_when_no_result_reports_it():
    records = numbered(3, latency_ms=None)
    latency = group(summarize_results(records), "groq", "m1")["latency_ms"]
    assert latency["reported"] == 0
    for key in ("p50", "p90", "p95", "p99", "min", "max", "mean"):
        assert latency[key] is None


def test_latency_coverage_counts_only_results_that_report_it():
    records = numbered(2, latency_ms=100.0) + [rec(request_id="nolat", latency_ms=None)]
    group_ = group(summarize_results(records), "groq", "m1")
    assert group_["requests"] == 3
    assert group_["latency_ms"]["reported"] == 2
    assert group_["latency_ms"]["p50"] == 100.0


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def test_multiple_models_for_one_provider_are_separate_groups():
    records = numbered(4, model="fast", latency_ms=10.0) + numbered(
        2, model="slow", latency_ms=90.0
    )
    report = summarize_results(records)
    assert [(g["provider"], g["model"]) for g in report["groups"]] == [
        ("groq", "fast"),
        ("groq", "slow"),
    ]
    assert group(report, "groq", "fast")["requests"] == 4
    assert group(report, "groq", "slow")["requests"] == 2
    assert group(report, "groq", "fast")["latency_ms"]["p50"] == 10.0
    assert group(report, "groq", "slow")["latency_ms"]["p50"] == 90.0


def test_same_model_name_under_different_providers_stays_separate():
    records = numbered(3, provider="groq", model="shared") + numbered(
        5, provider="cerebras", model="shared"
    )
    report = summarize_results(records)
    assert group(report, "groq", "shared")["requests"] == 3
    assert group(report, "cerebras", "shared")["requests"] == 5


def test_groups_are_sorted_by_provider_then_model_regardless_of_input_order():
    records = [
        rec("groq", "b", request_id="1"),
        rec("cerebras", "z", request_id="2"),
        rec("groq", "a", request_id="3"),
    ]
    report = summarize_results(records)
    assert [(g["provider"], g["model"]) for g in report["groups"]] == [
        ("cerebras", "z"),
        ("groq", "a"),
        ("groq", "b"),
    ]


def test_summary_is_deterministic_across_input_order():
    records = numbered(5, latency_ms=10.0) + numbered(5, model="m2", latency_ms=20.0)
    forward = summarize_results(records)
    backward = summarize_results(list(reversed(records)))
    assert render_json(forward) == render_json(backward)


def test_unique_requests_counts_distinct_request_ids_across_iterations():
    records = [
        rec(request_id="a", iteration=1),
        rec(request_id="a", iteration=2),
        rec(request_id="b", iteration=1),
    ]
    group_ = group(summarize_results(records), "groq", "m1")
    assert group_["requests"] == 3
    assert group_["unique_requests"] == 2


def test_groups_over_different_request_sets_produce_a_comparability_warning():
    records = [
        rec("groq", request_id="a"),
        rec("groq", request_id="b"),
        rec("cerebras", request_id="a"),
    ]
    report = summarize_results(records)
    assert any("different request sets" in warning for warning in report["warnings"])


def test_groups_over_identical_request_sets_have_no_warnings():
    records = [rec("groq", request_id="a"), rec("cerebras", request_id="a")]
    assert summarize_results(records)["warnings"] == []


# ---------------------------------------------------------------------------
# Success, error classification and rates
# ---------------------------------------------------------------------------


def test_counts_and_rates_split_timeouts_schema_failures_and_other_errors():
    records = (
        numbered(6)
        + [rec(request_id=f"t{i}", success=False, error_code="provider_timeout") for i in range(2)]
        + [rec(request_id="s", success=False, error_code="schema_violation")]
        + [rec(request_id="e", success=False, error_code="provider_error")]
    )
    outcomes = group(summarize_results(records), "groq", "m1")["outcomes"]
    assert outcomes["successes"] == 6
    assert outcomes["success_rate"] == pytest.approx(0.6)
    assert outcomes["timeouts"] == 2
    assert outcomes["timeout_rate"] == pytest.approx(0.2)
    assert outcomes["schema_failures"] == 1
    assert outcomes["schema_failure_rate"] == pytest.approx(0.1)
    assert outcomes["other_errors"] == 1
    assert outcomes["other_error_rate"] == pytest.approx(0.1)


def test_error_buckets_partition_all_failures():
    records = (
        numbered(3)
        + [rec(request_id="a", success=False, error_code="provider_timeout")]
        + [rec(request_id="b", success=False, error_code="rate_limited")]
        + [rec(request_id="c", success=False, error_code="invalid_json")]
        + [rec(request_id="d", success=False, error_code="empty_response")]
        + [rec(request_id="e", success=False, error_code=None)]
    )
    outcomes = group(summarize_results(records), "groq", "m1")["outcomes"]
    # invalid_json and empty_response are schema-shape failures; rate_limited and an
    # unclassified failure are "other errors".
    assert outcomes["successes"] == 3
    assert outcomes["timeouts"] == 1
    assert outcomes["schema_failures"] == 2
    assert outcomes["other_errors"] == 2
    buckets = ("successes", "timeouts", "schema_failures", "other_errors")
    assert sum(outcomes[bucket] for bucket in buckets) == 8


def test_all_success_group_reports_zero_error_counts_and_full_success_rate():
    outcomes = group(summarize_results(numbered(4)), "groq", "m1")["outcomes"]
    assert outcomes["success_rate"] == 1.0
    assert outcomes["timeouts"] == outcomes["schema_failures"] == outcomes["other_errors"] == 0
    assert outcomes["timeout_rate"] == outcomes["schema_failure_rate"] == 0.0


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


def test_retry_count_and_rate_use_only_results_that_report_retries():
    records = [
        rec(request_id="a", retry_count=0),
        rec(request_id="b", retry_count=2),
        rec(request_id="c", retry_count=1),
        rec(request_id="d", retry_count=0),
        rec(request_id="unreported"),
    ]
    retries = group(summarize_results(records), "groq", "m1")["retries"]
    assert retries["reported"] == 4
    assert retries["count"] == 3
    assert retries["requests_retried"] == 2
    assert retries["rate"] == pytest.approx(0.5)


def test_retries_are_unavailable_not_zero_when_never_reported():
    retries = group(summarize_results(numbered(3)), "groq", "m1")["retries"]
    assert retries == {"reported": 0, "count": None, "requests_retried": None, "rate": None}


# ---------------------------------------------------------------------------
# Tokens and cost: values, missingness and coverage
# ---------------------------------------------------------------------------


def test_token_min_avg_max_cover_input_output_and_total():
    records = [
        rec(request_id="a", usage={"input_tokens": 100, "output_tokens": 10}),
        rec(request_id="b", usage={"input_tokens": 300, "output_tokens": 50}),
        rec(request_id="c", usage={"input_tokens": 200, "output_tokens": 30}),
    ]
    tokens = group(summarize_results(records), "groq", "m1")["tokens"]
    assert tokens["input"] == {"reported": 3, "min": 100, "avg": 200.0, "max": 300}
    assert tokens["output"] == {"reported": 3, "min": 10, "avg": 30.0, "max": 50}
    assert tokens["total"] == {"reported": 3, "min": 110, "avg": 230.0, "max": 350}


def test_token_stats_ignore_results_without_usage_instead_of_counting_zero():
    records = [
        rec(request_id="a", usage={"input_tokens": 100, "output_tokens": 20}),
        rec(request_id="b", usage=None),
        rec(request_id="c", usage={"input_tokens": 300}),  # output not reported
    ]
    tokens = group(summarize_results(records), "groq", "m1")["tokens"]
    assert tokens["input"]["reported"] == 2
    assert tokens["input"]["min"] == 100
    assert tokens["input"]["avg"] == 200.0
    assert tokens["output"]["reported"] == 1
    assert tokens["output"]["min"] == tokens["output"]["max"] == 20
    # total needs both halves (or a reported total_tokens): only "a" qualifies.
    assert tokens["total"] == {"reported": 1, "min": 120, "avg": 120.0, "max": 120}


def test_reported_total_tokens_is_preferred_over_derived_sum():
    records = [rec(usage={"input_tokens": 10, "output_tokens": 5, "total_tokens": 99})]
    total = group(summarize_results(records), "groq", "m1")["tokens"]["total"]
    assert total["min"] == total["max"] == 99


def test_tokens_are_null_with_zero_coverage_when_provider_reports_none():
    tokens = group(summarize_results(numbered(3, usage=None)), "groq", "m1")["tokens"]
    for kind in ("input", "output", "total"):
        assert tokens[kind] == {"reported": 0, "min": None, "avg": None, "max": None}


def test_a_genuine_zero_token_report_is_kept_as_zero():
    records = [rec(usage={"input_tokens": 0, "output_tokens": 0})]
    tokens = group(summarize_results(records), "groq", "m1")["tokens"]
    assert tokens["input"] == {"reported": 1, "min": 0, "avg": 0.0, "max": 0}


def test_cost_per_1000_decisions_uses_reported_results_and_states_coverage():
    records = [
        rec(request_id="a", usage={"estimated_cost_usd": 0.001}),
        rec(request_id="b", usage={"estimated_cost_usd": 0.002}),
        rec(request_id="c", usage={"estimated_cost_usd": 0.003}),
        rec(request_id="d", usage=None),
    ]
    cost = group(summarize_results(records), "groq", "m1")["cost"]
    assert cost["reported"] == 3
    assert cost["total_usd"] == pytest.approx(0.006)
    assert cost["per_1000_decisions_usd"] == pytest.approx(2.0)


def test_cost_is_null_not_zero_when_no_result_reports_it():
    records = numbered(3, usage={"input_tokens": 5, "output_tokens": 5})
    cost = group(summarize_results(records), "groq", "m1")["cost"]
    assert cost == {"reported": 0, "total_usd": None, "per_1000_decisions_usd": None}


def test_free_provider_reporting_zero_cost_is_zero_not_null():
    cost = group(summarize_results([rec(usage={"estimated_cost_usd": 0.0})]), "groq", "m1")["cost"]
    assert cost["reported"] == 1
    assert cost["per_1000_decisions_usd"] == 0.0


def test_one_providers_missing_usage_does_not_affect_another():
    records = [
        rec("groq", request_id="a", usage={"input_tokens": 10, "output_tokens": 1}),
        rec("cerebras", request_id="a", usage=None),
    ]
    report = summarize_results(records)
    assert group(report, "groq", "m1")["tokens"]["input"]["reported"] == 1
    assert group(report, "cerebras", "m1")["tokens"]["input"]["reported"] == 0


# ---------------------------------------------------------------------------
# Room type and danger distributions
# ---------------------------------------------------------------------------


def _room(room_type: str, danger: int) -> dict[str, Any]:
    return {"room_type": room_type, "danger": danger}


def test_room_type_and_danger_distributions_count_successful_rooms():
    records = [
        rec(request_id="1", room=_room("chamber", 2)),
        rec(request_id="2", room=_room("chamber", 3)),
        rec(request_id="3", room=_room("vault", 3)),
        rec(request_id="4", room=_room("corridor", 3)),
    ]
    group_ = group(summarize_results(records), "groq", "m1")
    room_types = group_["room_types"]
    assert room_types["reported"] == 4
    assert room_types["counts"] == {"chamber": 2, "corridor": 1, "vault": 1}
    assert room_types["rates"] == {"chamber": 0.5, "corridor": 0.25, "vault": 0.25}
    danger = group_["danger"]
    assert danger["reported"] == 4
    assert danger["counts"] == {"2": 1, "3": 3}
    assert danger["rates"] == {"2": 0.25, "3": 0.75}


def test_failed_requests_do_not_contribute_to_distributions():
    records = [
        rec(request_id="1", room=_room("chamber", 1)),
        rec(request_id="2", success=False, error_code="provider_error", room=None),
    ]
    group_ = group(summarize_results(records), "groq", "m1")
    assert group_["requests"] == 2
    assert group_["room_types"]["reported"] == 1
    assert group_["room_types"]["rates"] == {"chamber": 1.0}


def test_distributions_are_empty_with_zero_coverage_when_no_rooms_recorded():
    group_ = group(summarize_results(numbered(2, room=None)), "groq", "m1")
    for key in ("room_types", "danger"):
        assert group_[key] == {"reported": 0, "counts": None, "rates": None}


def test_distribution_keys_are_sorted_for_stable_output():
    records = [
        rec(request_id="1", room=_room("vault", 5)),
        rec(request_id="2", room=_room("cavern", 1)),
    ]
    group_ = group(summarize_results(records), "groq", "m1")
    assert list(group_["room_types"]["counts"]) == ["cavern", "vault"]
    assert list(group_["danger"]["counts"]) == ["1", "5"]


# ---------------------------------------------------------------------------
# Input formats
# ---------------------------------------------------------------------------


def _report_document(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": "2026-09-19T10:00:00Z",
        "contract_version": "1.0.0",
        "input_file": "dataset.jsonl",
        "providers": [],
        "summary_by_provider": {},
        "results": results,
    }


def test_loads_json_report_document(tmp_path: Path):
    path = write_json(tmp_path / "report.json", _report_document(numbered(3)))
    assert len(load_results([path])) == 3


def test_loads_jsonl_report_with_summary_header(tmp_path: Path):
    header = {**_report_document([]), "type": "benchmark_summary"}
    del header["results"]
    lines = [header] + [{**r, "type": "benchmark_result"} for r in numbered(3)]
    path = write_jsonl(tmp_path / "report.jsonl", lines)
    assert len(load_results([path])) == 3


def test_loads_bare_json_array_and_bare_jsonl(tmp_path: Path):
    array_path = write_json(tmp_path / "array.json", numbered(2))
    jsonl_path = write_jsonl(tmp_path / "bare.jsonl", numbered(2))
    assert len(load_results([array_path])) == 2
    assert len(load_results([jsonl_path])) == 2


def test_json_and_jsonl_inputs_summarize_identically(tmp_path: Path):
    results = numbered(4, latency_ms=12.5, usage={"input_tokens": 3, "output_tokens": 4})
    json_path = write_json(tmp_path / "r.json", _report_document(results))
    jsonl_path = write_jsonl(
        tmp_path / "r.jsonl",
        [{"type": "benchmark_summary", "contract_version": "1.0.0"}]
        + [{**r, "type": "benchmark_result"} for r in results],
    )
    from_json = render_json(summarize_results(load_results([json_path])))
    from_jsonl = render_json(summarize_results(load_results([jsonl_path])))
    assert json.loads(from_json)["groups"] == json.loads(from_jsonl)["groups"]


def test_multiple_inputs_are_merged_into_one_summary(tmp_path: Path):
    a = write_json(tmp_path / "a.json", _report_document(numbered(2, provider="groq")))
    b = write_jsonl(tmp_path / "b.jsonl", numbered(3, provider="cerebras"))
    report = summarize_results(load_results([a, b]))
    assert group(report, "groq", "m1")["requests"] == 2
    assert group(report, "cerebras", "m1")["requests"] == 3


def test_legacy_results_without_retry_field_load_and_report_unavailable(tmp_path: Path):
    legacy = numbered(2)
    for item in legacy:
        assert "retry_count" not in item
    path = write_json(tmp_path / "legacy.json", _report_document(legacy))
    retries = group(summarize_results(load_results([path])), "groq", "m1")["retries"]
    assert retries["reported"] == 0


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_missing_file_names_the_path(tmp_path: Path):
    with pytest.raises(SummaryInputError, match="not found"):
        load_results([tmp_path / "nope.json"])


def test_empty_file_is_rejected(tmp_path: Path):
    path = tmp_path / "empty.jsonl"
    path.write_text("\n\n", encoding="utf-8")
    with pytest.raises(SummaryInputError, match="no replay results"):
        load_results([path])


def test_malformed_jsonl_line_reports_line_number(tmp_path: Path):
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(rec()) + "\n{not json}\n", encoding="utf-8")
    with pytest.raises(SummaryInputError, match=r"bad\.jsonl.*line 2"):
        load_results([path])


def test_malformed_json_document_is_rejected(tmp_path: Path):
    path = tmp_path / "bad.json"
    path.write_text('{"results": [', encoding="utf-8")
    with pytest.raises(SummaryInputError, match="malformed JSON"):
        load_results([path])


def test_json_object_without_results_says_how_to_fix_it(tmp_path: Path):
    path = write_json(tmp_path / "noresults.json", {"providers": [], "contract_version": "1.0.0"})
    with pytest.raises(SummaryInputError, match="results"):
        load_results([path])


def test_input_that_is_not_a_replay_output_is_rejected(tmp_path: Path):
    path = write_json(tmp_path / "scalar.json", 42)
    with pytest.raises(SummaryInputError, match="replay"):
        load_results([path])


def test_recorded_dataset_events_are_not_replay_results(tmp_path: Path):
    # sample_run.jsonl is the *input* to benchmarks.replay, not its output.
    with pytest.raises(SummaryInputError, match="replay"):
        load_results([FIXTURES_DIR / "sample_run.jsonl"])


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"provider": None}, "provider"),
        ({"provider": ""}, "provider"),
        ({"model": 7}, "model"),
        ({"success": "yes"}, "success"),
        ({"latency_ms": -1.0}, "latency_ms"),
        ({"latency_ms": "fast"}, "latency_ms"),
        ({"latency_ms": True}, "latency_ms"),
        ({"usage": {"input_tokens": -5}}, "input_tokens"),
        ({"usage": {"output_tokens": "many"}}, "output_tokens"),
        ({"usage": {"estimated_cost_usd": -0.1}}, "estimated_cost_usd"),
        ({"usage": "lots"}, "usage"),
        ({"room": {"room_type": "chamber", "danger": "high"}}, "danger"),
        ({"room": {"danger": 2}}, "room_type"),
        ({"error_code": 500}, "error_code"),
        ({"retry_count": -1}, "retry_count"),
        ({"retry_count": 1.5}, "retry_count"),
    ],
)
def test_invalid_field_values_are_rejected_with_the_field_name(
    tmp_path: Path, mutation: dict[str, Any], message: str
):
    record = {**rec(), **mutation}
    path = write_json(tmp_path / "bad.json", _report_document([record]))
    with pytest.raises(SummaryInputError, match=message) as excinfo:
        load_results([path])
    assert "bad.json" in str(excinfo.value)
    assert "result 1" in str(excinfo.value)


@pytest.mark.parametrize("field", ["provider", "model", "success"])
def test_missing_required_fields_are_rejected(tmp_path: Path, field: str):
    record = rec()
    del record[field]
    path = write_json(tmp_path / "bad.json", _report_document([record]))
    with pytest.raises(SummaryInputError, match=field):
        load_results([path])


def test_non_object_result_is_rejected(tmp_path: Path):
    path = write_json(tmp_path / "bad.json", _report_document([rec(), "oops"]))  # type: ignore[list-item]
    with pytest.raises(SummaryInputError, match="result 2"):
        load_results([path])


def test_unknown_jsonl_record_type_is_rejected(tmp_path: Path):
    path = write_jsonl(tmp_path / "r.jsonl", [{**rec(), "type": "something_else"}])
    with pytest.raises(SummaryInputError, match="something_else"):
        load_results([path])


def test_incompatible_contract_version_is_rejected(tmp_path: Path):
    document = {**_report_document(numbered(1)), "contract_version": "2.0.0"}
    path = write_json(tmp_path / "v2.json", document)
    with pytest.raises(SummaryInputError, match="contract version"):
        load_results([path])


def test_compatible_minor_contract_version_is_accepted(tmp_path: Path):
    document = {**_report_document(numbered(1)), "contract_version": "1.7.0"}
    path = write_json(tmp_path / "v1_7.json", document)
    assert len(load_results([path])) == 1


def test_duplicate_results_are_rejected_instead_of_double_counted(tmp_path: Path):
    path = write_json(tmp_path / "a.json", _report_document(numbered(2)))
    with pytest.raises(SummaryInputError, match="duplicate"):
        load_results([path, path])


# ---------------------------------------------------------------------------
# Output formats
# ---------------------------------------------------------------------------


def _sample_report() -> dict[str, Any]:
    records = (
        [
            rec(
                "groq",
                "fast",
                request_id=f"r{i}",
                latency_ms=float(10 * (i + 1)),
                usage={"input_tokens": 100 + i, "output_tokens": 20, "estimated_cost_usd": 0.001},
                room=_room("chamber", 2),
                retry_count=0,
            )
            for i in range(5)
        ]
        + [
            rec(
                "groq",
                "fast",
                request_id="r-timeout",
                success=False,
                error_code="provider_timeout",
                latency_ms=9000.0,
            )
        ]
        + [
            rec("groq", "slow", request_id=f"r{i}", latency_ms=500.0, room=_room("vault", 4))
            for i in range(3)
        ]
        + [
            rec(
                "rules-baseline",
                "builtin-v1",
                request_id="r0",
                latency_ms=0.5,
                room=_room("room", 1),
            )
        ]
    )
    return summarize_results(records)


def test_json_output_is_valid_json_with_explicit_nulls_and_convention():
    parsed = json.loads(render_json(_sample_report()))
    assert parsed["percentile_convention"]
    slow = next(g for g in parsed["groups"] if g["model"] == "slow")
    assert slow["tokens"]["input"]["min"] is None
    assert slow["tokens"]["input"]["reported"] == 0
    assert slow["cost"]["per_1000_decisions_usd"] is None
    assert slow["retries"]["rate"] is None
    fast = next(g for g in parsed["groups"] if g["model"] == "fast")
    assert fast["latency_ms"]["p99"] > fast["latency_ms"]["p50"]


def test_json_output_has_no_nan_or_infinity_tokens():
    text = render_json(_sample_report())
    assert "NaN" not in text
    assert "Infinity" not in text


def test_csv_output_has_one_row_per_provider_model_and_flat_columns():
    rows = list(csv.DictReader(io.StringIO(render_csv(_sample_report()))))
    assert [(r["provider"], r["model"]) for r in rows] == [
        ("groq", "fast"),
        ("groq", "slow"),
        ("rules-baseline", "builtin-v1"),
    ]
    fast = rows[0]
    assert fast["requests"] == "6"
    assert float(fast["latency_p99_ms"]) > float(fast["latency_p95_ms"]) > 0
    assert fast["timeouts"] == "1"
    assert float(fast["timeout_rate"]) == pytest.approx(1 / 6, abs=1e-4)
    assert fast["input_tokens_reported"] == "5"
    assert fast["input_tokens_min"] == "100"
    assert fast["input_tokens_max"] == "104"
    assert float(fast["cost_per_1000_decisions_usd"]) == pytest.approx(1.0)
    assert fast["cost_reported"] == "5"
    assert fast["room_type_chamber_count"] == "5"
    assert fast["danger_2_count"] == "5"


def test_csv_leaves_unavailable_metrics_empty_never_zero():
    rows = list(csv.DictReader(io.StringIO(render_csv(_sample_report()))))
    slow = rows[1]
    assert slow["input_tokens_reported"] == "0"
    for column in (
        "input_tokens_min",
        "input_tokens_avg",
        "output_tokens_max",
        "total_tokens_avg",
        "cost_per_1000_decisions_usd",
        "retry_rate",
    ):
        assert slow[column] == "", column


def test_csv_columns_are_stable_across_groups_with_different_room_types():
    header = render_csv(_sample_report()).splitlines()[0].split(",")
    assert "room_type_vault_count" in header
    assert "room_type_chamber_rate" in header
    assert all(f"danger_{level}_count" in header for level in range(1, 6))


def test_text_output_leads_with_tail_latency_and_marks_unavailable_metrics():
    text = render_text(_sample_report())
    assert "p99" in text and "p95" in text and "p90" in text and "p50" in text
    assert text.index("p99") < text.index("p50")  # tail first
    assert "n/a" in text  # slow model has no token/cost data
    assert "groq" in text and "fast" in text and "slow" in text and "builtin-v1" in text
    assert "linear interpolation" in text
    # groq/slow reported no tokens and no cost: each of the four cells in its
    # TOKENS row must show n/a *with* its 0/3 coverage.
    tokens_section = text.split("TOKENS", 1)[1].split("ROOM DECISIONS", 1)[0]
    slow_row = next(line for line in tokens_section.splitlines() if " slow " in f"{line} ")
    assert slow_row.count("n/a (0/3)") == 4


def test_text_output_reports_rates_as_percentages_with_counts():
    text = render_text(_sample_report())
    assert "1 (16.7%)" in text  # one timeout out of six


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _report_file(tmp_path: Path) -> Path:
    results = numbered(
        4, model="fast", latency_ms=10.0, usage={"input_tokens": 5, "output_tokens": 5}
    )
    results += [rec(request_id="req-0", model="slow", latency_ms=99.0, room=_room("vault", 3))]
    return write_json(tmp_path / "report.json", _report_document(results))


def test_cli_text_is_default_and_writes_only_the_report_to_stdout(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    assert main(["--input", str(_report_file(tmp_path))]) == 0
    captured = capsys.readouterr()
    assert "p99" in captured.out
    assert captured.err == ""


def test_cli_json_format_prints_parseable_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["--input", str(_report_file(tmp_path)), "--format", "json"]) == 0
    parsed = json.loads(capsys.readouterr().out)
    assert {g["model"] for g in parsed["groups"]} == {"fast", "slow"}


def test_cli_csv_format_prints_parseable_csv(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    assert main(["--input", str(_report_file(tmp_path)), "--format", "csv"]) == 0
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 2


@pytest.mark.parametrize("fmt", ["text", "json", "csv"])
def test_cli_output_flag_writes_file_and_keeps_stdout_empty(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], fmt: str
):
    out = tmp_path / "nested" / f"summary.{fmt}"
    assert (
        main(["--input", str(_report_file(tmp_path)), "--format", fmt, "--output", str(out)]) == 0
    )
    assert out.is_file() and out.read_text(encoding="utf-8").strip()
    assert capsys.readouterr().out == ""


def test_cli_accepts_multiple_inputs_of_mixed_format(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    a = _report_file(tmp_path)
    b = write_jsonl(tmp_path / "b.jsonl", numbered(2, provider="cerebras", model="x"))
    assert main(["--input", str(a), str(b), "--format", "json"]) == 0
    providers = {g["provider"] for g in json.loads(capsys.readouterr().out)["groups"]}
    assert providers == {"groq", "cerebras"}


def test_cli_invalid_input_exits_2_with_message_on_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    bad = tmp_path / "bad.jsonl"
    bad.write_text("nope\n", encoding="utf-8")
    assert main(["--input", str(bad)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "bad.jsonl" in captured.err and "line 1" in captured.err


def test_cli_rejects_unknown_format(tmp_path: Path):
    with pytest.raises(SystemExit) as excinfo:
        main(["--input", str(_report_file(tmp_path)), "--format", "yaml"])
    assert excinfo.value.code == 2


# ---------------------------------------------------------------------------
# Committed sample fixtures
# ---------------------------------------------------------------------------


def test_sample_fixtures_json_and_jsonl_agree_and_show_every_metric_class():
    json_records = load_results([FIXTURES_DIR / "sample_replay_results.json"])
    jsonl_records = load_results([FIXTURES_DIR / "sample_replay_results.jsonl"])
    from_json = summarize_results(json_records)
    from_jsonl = summarize_results(jsonl_records)
    assert from_json["groups"] == from_jsonl["groups"]

    providers_models = {(g["provider"], g["model"]) for g in from_json["groups"]}
    assert len(providers_models) >= 4
    assert len({model for provider, model in providers_models if provider == "groq"}) >= 2
    groups = from_json["groups"]
    assert any(g["outcomes"]["timeouts"] > 0 for g in groups)
    assert any(g["outcomes"]["schema_failures"] > 0 for g in groups)
    assert any(g["tokens"]["input"]["reported"] == 0 for g in groups)
    assert any(0 < g["cost"]["reported"] < g["requests"] for g in groups)


# ---------------------------------------------------------------------------
# Integration with benchmarks.replay
# ---------------------------------------------------------------------------


def test_parse_model_selections_allows_several_models_and_rejects_duplicates():
    assert parse_model_selections(["groq:a", "groq:b", "cerebras:c"]) == {
        "groq": ["a", "b"],
        "cerebras": ["c"],
    }
    with pytest.raises(ValueError, match="duplicate provider/model selection"):
        parse_model_selections(["groq:a", "groq:a"])
    assert parse_model_selections(None) == {}
    with pytest.raises(ValueError, match="invalid model override format"):
        parse_model_selections(["nocolon"])


def test_replay_report_keeps_every_model_of_a_provider_and_records_retry_field():
    service, registry = build_default_service()
    report = asyncio.run(
        run_benchmark(
            [make_request()],
            providers=[RULES_PROVIDER_ID],
            models={RULES_PROVIDER_ID: ["builtin-v1", "other-model"]},
            service=service,
            registry=registry,
        )
    )
    keys = {(p.provider, p.model) for p in report.providers}
    assert keys == {(RULES_PROVIDER_ID, "builtin-v1"), (RULES_PROVIDER_ID, "other-model")}
    assert len(report.results) == 2
    # The legacy per-provider map cannot represent two models: it must not
    # silently keep just one of them.
    assert RULES_PROVIDER_ID not in report.summary_by_provider


def test_replay_json_output_feeds_the_summarizer_end_to_end(tmp_path: Path):
    from benchmarks.replay import build_arg_parser

    fixture = FIXTURES_DIR / "sample_run.jsonl"
    out_json = tmp_path / "replay.json"
    out_jsonl = tmp_path / "replay.jsonl"
    parser = build_arg_parser()
    for out in (out_json, out_jsonl):
        args = parser.parse_args(["--input", str(fixture), "--output", str(out), "--quiet"])
        assert asyncio.run(main_async(args)) == 0

    for out in (out_json, out_jsonl):
        report = summarize_results(load_results([out]))
        summary = group(report, RULES_PROVIDER_ID, "builtin-v1")
        assert summary["requests"] == 3
        assert summary["outcomes"]["success_rate"] == 1.0
        assert summary["latency_ms"]["reported"] == 3
        assert summary["latency_ms"]["p99"] is not None
        assert summary["room_types"]["reported"] == 3
        assert summary["danger"]["reported"] == 3
        # The local rules baseline *reports* zero tokens and zero cost: a real
        # zero with full coverage, distinct from "not reported" (null).
        assert summary["tokens"]["input"] == {"reported": 3, "min": 0, "avg": 0.0, "max": 0}
        assert summary["cost"]["reported"] == 3
        assert summary["cost"]["per_1000_decisions_usd"] == 0.0
        assert summary["retries"]["reported"] == 0


def test_module_exposes_documented_percentile_convention():
    assert "linear interpolation" in summarize.PERCENTILE_CONVENTION
    assert "n - 1" in summarize.PERCENTILE_CONVENTION
