"""Read exported SDK data, including ordering, missing data and isolation paths."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from dungeon_director.comparison_telemetry import (
    ComparisonTelemetry,
    comparison_summary,
    current_correlation,
    telemetry_context,
)
from dungeon_director.contracts import ErrorDetail, ErrorKind, UsageStats
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings, ShadowSettings, ShadowTarget
from dungeon_director.shadow import ComparisonMeta, ExecutionRecord, ExecutionRole, ExecutionStatus
from fakes import FakeProvider, make_request
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from shadow_fakes import GatedProvider, eventually
from test_telemetry import Harness

from benchmarks import replay


@pytest.fixture
def harness():
    h = Harness()
    h.logs = InMemoryLogRecordExporter()
    logger_provider = LoggerProvider()
    logger_provider.add_log_record_processor(SimpleLogRecordProcessor(h.logs))
    h.telemetry.logger_provider = logger_provider
    h.telemetry._otel_logger = logger_provider.get_logger("comparison-test")
    yield h
    h.telemetry.shutdown()


def records():
    registry = ProviderRegistry()
    registry.register(FakeProvider("active"))
    service = DirectorService(
        registry, DirectorSettings(default_provider="active", default_model="fake-model")
    )
    outcome = asyncio.run(service.generate(make_request()))
    now = datetime.now(UTC)
    active = ExecutionRecord(
        "cmp-test",
        ExecutionRole.ACTIVE,
        "active",
        "fake-model",
        True,
        ExecutionStatus.SUCCESS,
        "req-test",
        "run-test",
        now,
        now,
        20,
        outcome,
    )
    shadow_response = outcome.response.model_copy(deep=True)
    shadow_response.room.danger = min(5, shadow_response.room.danger + 1)
    shadow_response.room.enemy_density = 0.95
    shadow = replace(
        active,
        role=ExecutionRole.SHADOW,
        provider="shadow",
        duration_ms=10,
        outcome=replace(outcome, response=shadow_response),
    )
    return active, shadow


@pytest.mark.parametrize("shadow_first", [True, False])
def test_pairs_export_once_in_either_order_without_ids_on_metrics(harness, shadow_first):
    active, shadow = records()
    observer = ComparisonTelemetry(harness.telemetry)
    with harness.telemetry.tracer_provider.get_tracer("test").start_as_current_span(
        "active"
    ) as parent:
        observer.comparison_started(
            ComparisonMeta("cmp-test", "req-test", "run-test", datetime.now(UTC), 2)
        )
    for record in (shadow, active) if shadow_first else (active, shadow):
        observer.execution_finished(record)
    pairs = [s for s in harness.finished_spans() if s.name == "director.shadow.comparison"]
    assert len(pairs) == 1
    assert pairs[0].parent.span_id == parent.get_span_context().span_id
    assert pairs[0].attributes["shadow_comparison_id"] == "cmp-test"
    assert pairs[0].attributes["director.request_id"] == "req-test"
    assert pairs[0].attributes["danger_delta"] == "higher"
    assert pairs[0].attributes["latency_winner"] == "shadow"
    assert pairs[0].attributes["cost_winner"] == "unknown"
    assert not observer._pending
    metrics = harness.metrics()["director.shadow.comparisons"]
    assert any(
        p.attributes["category"] == "danger_delta" and p.attributes["result"] == "higher"
        for p in metrics
    )
    assert all(
        set(p.attributes)
        == {
            "active_provider",
            "active_model",
            "shadow_provider",
            "shadow_model",
            "category",
            "result",
        }
        for p in metrics
    )
    logs = [x.log_record for x in harness.logs.get_finished_logs()]
    assert len(logs) == 3
    assert logs[-1].attributes["shadow_comparison_id"] == "cmp-test"
    assert logs[-1].trace_id == pairs[0].context.trace_id
    assert "raw_payload" not in str(logs[-1].attributes)


@pytest.mark.parametrize(
    "status", [ExecutionStatus.SKIPPED, ExecutionStatus.CANCELLED, ExecutionStatus.ERROR]
)
def test_absent_outcomes_are_unknown_not_zero(status):
    active, shadow = records()
    missing = replace(shadow, status=status, duration_ms=0, outcome=None)
    summary = comparison_summary(active, missing)
    assert all(value == "unknown" for value in summary.values())


@pytest.mark.parametrize(
    "code",
    [
        ErrorKind.INVALID_JSON,
        ErrorKind.SCHEMA_VIOLATION,
        ErrorKind.UNSUPPORTED_CONTRACT_VERSION,
        ErrorKind.EMPTY_RESPONSE,
    ],
)
def test_every_schema_error_is_compared(code):
    active, shadow = records()
    failed = shadow.outcome.response.model_copy(deep=True)
    failed.success = False
    failed.room = None
    failed.metadata.error = ErrorDetail(code=code, message="sk-secret-provider-text")
    summary = comparison_summary(
        active, replace(shadow, outcome=replace(shadow.outcome, response=failed))
    )
    assert summary["schema_mismatch"] == "true"
    assert summary["success_mismatch"] == "true"
    assert "sk-secret" not in str(summary)


def test_zero_cost_is_real_but_missing_cost_is_unknown():
    active, shadow = records()
    active.outcome.response.metadata.usage = UsageStats(estimated_cost_usd=0)
    shadow.outcome.response.metadata.usage = UsageStats(estimated_cost_usd=0.1)
    assert comparison_summary(active, shadow)["cost_winner"] == "active"
    shadow.outcome.response.metadata.usage = None
    assert comparison_summary(active, shadow)["cost_winner"] == "unknown"


def test_bounded_pending_eviction_never_resurrects(harness):
    observer = ComparisonTelemetry(harness.telemetry, max_comparisons=1)
    now = datetime.now(UTC)
    observer.comparison_started(ComparisonMeta("cmp-test", "req-test", "run-test", now, 2))
    observer.comparison_started(ComparisonMeta("cmp-new", "req-new", "run-test", now, 2))
    for record in records():
        observer.execution_finished(record)
    assert list(observer._pending) == ["cmp-new"]
    assert not any(s.name == "director.shadow.comparison" for s in harness.finished_spans())
    assert harness.metrics()["director.shadow.comparisons.evicted"][0].value == 1


def test_slow_shadow_and_broken_exporter_do_not_change_active_result(harness):
    async def scenario():
        gate = asyncio.Event()
        active, shadow = GatedProvider("active"), GatedProvider("shadow", gate=gate)
        registry = ProviderRegistry()
        registry.register(active)
        registry.register(shadow)
        observer = ComparisonTelemetry(harness.telemetry)
        observer._executions.add = lambda *a, **kw: (_ for _ in ()).throw(
            RuntimeError("private-text")
        )
        service = DirectorService(
            registry,
            DirectorSettings(
                default_provider="active",
                default_model="fake-model",
                shadow=ShadowSettings(targets=(ShadowTarget("shadow", "fake-model"),)),
            ),
            shadow_observers=(observer,),
        )
        original = make_request()
        pristine = original.model_dump()
        outcome = await asyncio.wait_for(service.generate(original), 0.5)
        assert outcome.response.success
        assert not gate.is_set()
        assert original.model_dump() == pristine
        gate.set()
        await eventually(lambda: service.shadow.store.get(outcome.comparison_id).complete)
        await service.aclose()

    asyncio.run(scenario())


def test_replay_correlates_cases_across_models_and_restores_context(harness):
    async def scenario():
        registry = ProviderRegistry()
        registry.register(FakeProvider("active", models=("one", "two")))
        service = DirectorService(
            registry,
            DirectorSettings(default_provider="active", default_model="one"),
            telemetry=harness.telemetry,
        )
        report = await replay.run_benchmark(
            [make_request()],
            ["active"],
            {"active": ["one", "two"]},
            service=service,
            registry=registry,
            concurrency=2,
            evaluation_id="eval-test",
        )
        assert report.results[0].telemetry_ids == report.results[1].telemetry_ids
        assert report.results[0].telemetry_ids["evaluation_id"] == "eval-test"
        assert current_correlation() == {}

    asyncio.run(scenario())
    spans = [s for s in harness.finished_spans() if s.name == "director.replay.case"]
    assert len(spans) == 2
    assert len({s.attributes["case_id"] for s in spans}) == 1
    assert all(s.attributes["execution_mode"] == "replay" for s in spans)
    assert all(s.attributes["director.request_id"] == make_request().request_id for s in spans)


def test_owned_replay_initializes_and_shuts_down_telemetry(monkeypatch, harness):
    calls = []
    monkeypatch.setattr(replay, "_setup_replay_telemetry", lambda: harness.telemetry)

    async def shutdown(telemetry):
        calls.append(telemetry)

    monkeypatch.setattr(replay, "_shutdown_replay_telemetry", shutdown)
    report = asyncio.run(replay.run_benchmark([make_request()], ["rules-baseline"]))
    assert report.results[0].success
    assert calls == [harness.telemetry]
    assert any(s.name == "director.replay.case" for s in harness.finished_spans())


def test_context_rejects_free_text_and_resets_after_exception():
    with (
        pytest.raises(RuntimeError),
        telemetry_context(execution_mode="replay", evaluation_id="secret text"),
    ):
        assert current_correlation() == {"execution_mode": "replay"}
        raise RuntimeError
    assert current_correlation() == {}
