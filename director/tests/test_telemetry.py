"""OpenTelemetry instrumentation of director decisions (issue #11).

Everything runs against in-memory exporters: a real SDK ``TracerProvider`` and
``MeterProvider`` whose output the tests read back, so a regression in what is
actually emitted (names, attributes, dimensions, buckets) fails a test.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from fakes import (
    ClassifiedFailureProvider,
    FakeProvider,
    MalformedProvider,
    RaisingProvider,
    SlowProvider,
    make_request,
    request_payload,
    valid_room_dict,
)
from fastapi.testclient import TestClient
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from dungeon_director.app import create_app
from dungeon_director.contracts import ErrorKind, RoomPlan, UsageStats
from dungeon_director.errors import ProviderError
from dungeon_director.providers import ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService, GenerationOutcome
from dungeon_director.settings import DirectorSettings, ShadowSettings, ShadowTarget
from dungeon_director.telemetry import (
    DEFAULT_OTLP_ENDPOINT,
    LATENCY_BUCKET_BOUNDARIES,
    METRIC_DIMENSIONS,
    DirectorTelemetry,
    TelemetrySettings,
    setup_telemetry,
)

SECRET = "sk-live-SECRET-0123456789"


# --------------------------------------------------------------------------- harness


class Harness:
    """A real SDK telemetry stack that keeps everything in memory."""

    def __init__(self) -> None:
        self.spans = InMemorySpanExporter()
        tracer_provider = TracerProvider()
        tracer_provider.add_span_processor(SimpleSpanProcessor(self.spans))
        self.reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[self.reader])
        self.telemetry = DirectorTelemetry(tracer_provider, meter_provider)

    def finished_spans(self) -> list[ReadableSpan]:
        return list(self.spans.get_finished_spans())

    def only_span(self) -> ReadableSpan:
        spans = self.finished_spans()
        assert len(spans) == 1, f"expected exactly one span, got {[s.name for s in spans]}"
        return spans[0]

    def metrics(self) -> dict[str, list[Any]]:
        data = self.reader.get_metrics_data()
        found: dict[str, list[Any]] = {}
        if data is None:
            return found
        for resource_metrics in data.resource_metrics:
            for scope in resource_metrics.scope_metrics:
                for metric in scope.metrics:
                    found.setdefault(metric.name, []).extend(metric.data.data_points)
        return found

    def request_points(self) -> list[Any]:
        return self.metrics().get("director.generation.requests", [])

    def only_request_point(self) -> Any:
        points = self.request_points()
        assert len(points) == 1, [dict(p.attributes) for p in points]
        return points[0]


@pytest.fixture
def harness() -> Harness:
    return Harness()


class UsageProvider(FakeProvider):
    def __init__(self, usage: UsageStats | None, provider_id: str = "fake") -> None:
        super().__init__(provider_id)
        self._usage = usage

    async def decide(self, request):
        return ProviderResult(
            payload=RoomPlan.model_validate(valid_room_dict(request)), usage=self._usage
        )


def make_service(
    provider: FakeProvider, telemetry: DirectorTelemetry | None, timeout: float = 5.0
) -> DirectorService:
    registry = ProviderRegistry()
    registry.register(provider)
    settings = DirectorSettings(
        default_provider=provider.provider_id,
        default_model=provider.default_model,
        timeout_seconds=timeout,
    )
    return DirectorService(registry, settings, telemetry=telemetry)


def run_generate(service: DirectorService, **kwargs: Any) -> GenerationOutcome:
    return asyncio.run(service.generate(make_request(), **kwargs))


_VOLATILE = {"started_at", "completed_at", "latency_ms", "occurred_at"}


def canonical(outcome: GenerationOutcome) -> tuple[int, dict[str, Any]]:
    """The wire-visible result minus wall-clock values."""

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in _VOLATILE}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return outcome.status_code, strip(outcome.response.model_dump(mode="json"))


def every_string(span: ReadableSpan) -> Iterator[str]:
    yield span.name
    for key, value in span.attributes.items():
        yield key
        yield str(value)
    if span.status.description:
        yield span.status.description
    for event in span.events:
        yield event.name
        for value in (event.attributes or {}).values():
            yield str(value)


# --------------------------------------------------------------------------- success


def test_success_emits_one_stable_span_with_shadow_ready_attributes(harness):
    usage = UsageStats(input_tokens=42, output_tokens=18, estimated_cost_usd=0.0015)
    service = make_service(UsageProvider(usage, "test-provider"), harness.telemetry)
    request = make_request()

    outcome = asyncio.run(service.generate(request))

    assert outcome.status_code == 200
    span = harness.only_span()
    # One stable, low-cardinality name; provider/model are attributes, not name parts.
    assert span.name == "director.generate"
    assert span.status.is_ok
    attrs = span.attributes
    assert attrs["director.request_id"] == request.request_id
    assert attrs["director.run_id"] == request.run_id
    assert attrs["director.provider"] == "test-provider"
    assert attrs["director.model"] == "fake-model"
    assert attrs["director.status"] == "success"
    assert attrs["director.http_status"] == 200
    assert attrs["director.is_shadow"] is False
    assert attrs["director.execution_mode"] == "active"
    assert attrs["director.schema_valid"] is True
    assert attrs["director.retry_count"] == 0
    assert attrs["director.room.type"] == "room"
    assert attrs["director.room.exit_count"] == 1
    assert attrs["director.latency_ms"] > 0
    assert attrs["director.provider_latency_ms"] > 0
    assert "director.error_code" not in attrs
    assert attrs["gen_ai.system"] == "test-provider"
    assert attrs["gen_ai.request.model"] == "fake-model"
    assert attrs["gen_ai.usage.input_tokens"] == 42
    assert attrs["gen_ai.usage.output_tokens"] == 18
    assert attrs["gen_ai.usage.total_tokens"] == 60
    assert attrs["gen_ai.usage.cost"] == 0.0015


def test_success_metrics_use_only_bounded_dimensions(harness):
    usage = UsageStats(input_tokens=42, output_tokens=18, estimated_cost_usd=0.0015)
    service = make_service(UsageProvider(usage, "test-provider"), harness.telemetry)

    asyncio.run(service.generate(make_request()))

    point = harness.only_request_point()
    assert dict(point.attributes) == {
        "provider": "test-provider",
        "model": "fake-model",
        "status": "success",
        "error_code": "none",
        "execution_mode": "active",
    }
    assert point.value == 1

    metrics = harness.metrics()
    tokens = {p.attributes["token_type"]: p.value for p in metrics["director.generation.tokens"]}
    assert tokens == {"input": 42, "output": 18}
    assert [p.value for p in metrics["director.generation.cost"]] == [0.0015]
    for points in metrics.values():
        for p in points:
            assert set(p.attributes) <= METRIC_DIMENSIONS
            assert "request_id" not in p.attributes and "run_id" not in p.attributes


def test_latency_histograms_are_queryable_with_explicit_buckets(harness):
    service = make_service(FakeProvider(), harness.telemetry)
    for _ in range(3):
        asyncio.run(service.generate(make_request()))

    metrics = harness.metrics()
    for name in ("director.generation.duration", "director.provider.duration"):
        (point,) = metrics[name]
        assert point.count == 3
        assert tuple(point.explicit_bounds) == LATENCY_BUCKET_BOUNDARIES
        assert len(point.bucket_counts) == len(LATENCY_BUCKET_BOUNDARIES) + 1
        assert sum(point.bucket_counts) == 3
        assert point.sum > 0
        assert dict(point.attributes)["status"] == "success"


def test_shadow_execution_is_distinguishable_on_span_and_metrics(harness):
    service = make_service(FakeProvider(), harness.telemetry)

    asyncio.run(service.generate(make_request()))
    asyncio.run(service.generate(make_request(), is_shadow=True))

    modes = {
        (s.attributes["director.execution_mode"], s.attributes["director.is_shadow"])
        for s in harness.finished_spans()
    }
    assert modes == {("active", False), ("shadow", True)}
    by_mode = {p.attributes["execution_mode"]: p.value for p in harness.request_points()}
    assert by_mode == {"active": 1, "shadow": 1}


def test_configured_shadow_fanout_uses_the_instrumented_execution_path(harness):
    registry = ProviderRegistry()
    registry.register(FakeProvider("active"))
    registry.register(FakeProvider("shadow"))
    settings = DirectorSettings(
        default_provider="active",
        shadow=ShadowSettings(targets=(ShadowTarget("shadow"),)),
    )
    service = DirectorService(registry, settings, telemetry=harness.telemetry)

    async def scenario() -> GenerationOutcome:
        outcome = await service.generate(make_request())
        assert outcome.comparison_id is not None
        assert service.shadow is not None
        await asyncio.wait_for(service.aclose(), timeout=2)
        return outcome

    outcome = asyncio.run(scenario())

    assert outcome.response.metadata.provider == "active"
    spans_by_mode = {
        span.attributes["director.execution_mode"]: span for span in harness.finished_spans()
    }
    assert set(spans_by_mode) == {"active", "shadow"}
    assert spans_by_mode["active"].attributes["director.provider"] == "active"
    assert spans_by_mode["shadow"].attributes["director.provider"] == "shadow"
    by_mode = {
        point.attributes["execution_mode"]: point.value for point in harness.request_points()
    }
    assert by_mode == {"active": 1, "shadow": 1}


def test_shadow_flag_does_not_change_the_canonical_answer():
    service = make_service(FakeProvider(), None)
    active = canonical(run_generate(service))
    shadow = canonical(run_generate(service, is_shadow=True))
    assert active == shadow


# --------------------------------------------------------------------------- failures


def _selection_kwargs(**kwargs: Any) -> dict[str, Any]:
    return kwargs


FAILURE_CASES = [
    pytest.param(
        ClassifiedFailureProvider(ErrorKind.RATE_LIMITED, f"limit for {SECRET}", "p"),
        {},
        429,
        "provider_error",
        "rate_limited",
        id="rate-limited",
    ),
    pytest.param(
        ClassifiedFailureProvider(ErrorKind.PROVIDER_TIMEOUT, f"slow {SECRET}", "p"),
        {},
        504,
        "timeout",
        "provider_timeout",
        id="provider-reported-timeout",
    ),
    pytest.param(
        RaisingProvider(TimeoutError(SECRET), "p"),
        {},
        504,
        "timeout",
        "provider_timeout",
        id="provider-timeouterror",
    ),
    pytest.param(
        ClassifiedFailureProvider(ErrorKind.SCHEMA_VIOLATION, f"bad {SECRET}", "p"),
        {},
        502,
        "schema_error",
        "schema_violation",
        id="provider-reported-schema",
    ),
    pytest.param(
        MalformedProvider({"bad": SECRET}, "p"),
        {},
        502,
        "schema_error",
        "schema_violation",
        id="malformed-payload",
    ),
    pytest.param(
        MalformedProvider("{not json " + SECRET, "p"),
        {},
        502,
        "schema_error",
        "invalid_json",
        id="invalid-json",
    ),
    pytest.param(
        RaisingProvider(RuntimeError(f"boom {SECRET}"), "p"),
        {},
        502,
        "provider_error",
        "provider_error",
        id="unexpected-exception",
    ),
    pytest.param(
        ClassifiedFailureProvider(ErrorKind.SAFETY_REFUSAL, "no", "p"),
        {},
        502,
        "provider_error",
        "safety_refusal",
        id="safety-refusal",
    ),
]


@pytest.mark.parametrize(("provider", "kwargs", "http", "status", "code"), FAILURE_CASES)
def test_failures_record_specific_status_and_code(harness, provider, kwargs, http, status, code):
    service = make_service(provider, harness.telemetry)

    outcome = run_generate(service, **kwargs)

    assert outcome.status_code == http
    span = harness.only_span()
    assert not span.status.is_ok and span.status.status_code.name == "ERROR"
    assert span.status.description == code
    assert span.attributes["director.status"] == status
    assert span.attributes["director.error_code"] == code
    assert span.attributes["director.http_status"] == http
    assert "director.room.type" not in span.attributes
    point = harness.only_request_point()
    assert point.attributes["status"] == status
    assert point.attributes["error_code"] == code
    assert point.attributes["provider"] == "p"
    assert set(point.attributes) <= METRIC_DIMENSIONS
    # The provider ran, so its latency is recorded next to the end-to-end latency.
    assert "director.provider_latency_ms" in span.attributes
    assert len(harness.metrics()["director.provider.duration"]) == 1
    # No token or cost series when the provider reported nothing.
    assert "director.generation.tokens" not in harness.metrics()
    assert "director.generation.cost" not in harness.metrics()


def test_schema_failures_mark_schema_invalid_others_leave_it_unset(harness):
    asyncio.run(
        make_service(MalformedProvider({"bad": 1}, "m"), harness.telemetry).generate(make_request())
    )
    asyncio.run(
        make_service(RaisingProvider(RuntimeError("x"), "r"), harness.telemetry).generate(
            make_request()
        )
    )

    by_provider = {s.attributes["director.provider"]: s for s in harness.finished_spans()}
    assert by_provider["m"].attributes["director.schema_valid"] is False
    assert "director.schema_valid" not in by_provider["r"].attributes


def test_director_deadline_timeout_records_origin_and_provider_latency(harness):
    slow = SlowProvider("slow")
    service = make_service(slow, harness.telemetry, timeout=0.05)

    outcome = run_generate(service)

    assert outcome.status_code == 504
    span = harness.only_span()
    assert span.attributes["director.status"] == "timeout"
    assert span.attributes["director.error_code"] == "provider_timeout"
    assert span.attributes["director.timeout_origin"] == "director_deadline"
    assert span.attributes["director.provider_latency_ms"] >= 40
    assert harness.only_request_point().attributes["status"] == "timeout"
    assert slow.cancelled


def test_provider_timeouterror_is_attributed_to_the_provider(harness):
    service = make_service(RaisingProvider(TimeoutError(), "p"), harness.telemetry)
    run_generate(service)
    assert harness.only_span().attributes["director.timeout_origin"] == "provider"


# --------------------------------------------------------------------------- selection


def test_unknown_provider_is_a_selection_error_with_unknown_labels(harness):
    service = make_service(FakeProvider("p"), harness.telemetry)

    outcome = run_generate(service, provider="nope")

    assert outcome.status_code == 404
    span = harness.only_span()
    assert span.attributes["director.status"] == "selection_error"
    assert span.attributes["director.selection_error"] == "unknown_provider"
    assert span.attributes["director.provider"] == "unknown"
    assert "nope" not in "".join(every_string(span))
    assert dict(harness.only_request_point().attributes) == {
        "provider": "unknown",
        "model": "unknown",
        "status": "selection_error",
        "error_code": "provider_error",
        "execution_mode": "active",
    }
    # Nothing was called, so there is no provider latency sample.
    assert "director.provider.duration" not in harness.metrics()
    assert "director.provider_latency_ms" not in span.attributes


def test_unknown_model_keeps_the_registered_provider_but_not_the_model(harness):
    service = make_service(FakeProvider("p"), harness.telemetry)

    outcome = run_generate(service, provider="p", model="made-up-model")

    assert outcome.status_code == 404
    point = harness.only_request_point()
    assert point.attributes["provider"] == "p"
    assert point.attributes["model"] == "unknown"
    assert point.attributes["status"] == "selection_error"
    assert "made-up-model" not in "".join(every_string(harness.only_span()))


def test_unavailable_provider_is_a_selection_error(harness):
    service = make_service(FakeProvider("p"), harness.telemetry)
    registry = service._registry
    registry.register(FakeProvider("off", available=False))

    outcome = run_generate(service, provider="off")

    assert outcome.status_code == 503
    span = harness.only_span()
    assert span.attributes["director.status"] == "selection_error"
    assert span.attributes["director.selection_error"] == "provider_unavailable"
    assert harness.only_request_point().attributes["provider"] == "off"


def test_client_chosen_selectors_cannot_mint_metric_series(harness):
    service = make_service(FakeProvider("p"), harness.telemetry)

    async def hammer() -> None:
        for _ in range(25):
            await service.generate(make_request(), provider=f"p-{uuid.uuid4().hex}")
            await service.generate(make_request(), provider="p", model=f"m-{uuid.uuid4().hex}")

    asyncio.run(hammer())

    series = {tuple(sorted(p.attributes.items())) for p in harness.request_points()}
    assert len(series) == 2, series
    assert len(harness.finished_spans()) == 50


# --------------------------------------------------------------------------- tokens


def test_tokens_and_cost_are_omitted_when_not_reported(harness):
    service = make_service(UsageProvider(None), harness.telemetry)

    assert run_generate(service).status_code == 200

    span = harness.only_span()
    assert not [k for k in span.attributes if k.startswith("gen_ai.usage")]
    metrics = harness.metrics()
    assert "director.generation.tokens" not in metrics
    assert "director.generation.cost" not in metrics


def test_partial_usage_records_only_what_was_reported(harness):
    usage = UsageStats(input_tokens=7)
    service = make_service(UsageProvider(usage), harness.telemetry)

    run_generate(service)

    span = harness.only_span()
    assert span.attributes["gen_ai.usage.input_tokens"] == 7
    assert "gen_ai.usage.output_tokens" not in span.attributes
    assert "gen_ai.usage.total_tokens" not in span.attributes
    assert "gen_ai.usage.cost" not in span.attributes
    tokens = harness.metrics()["director.generation.tokens"]
    assert [(p.attributes["token_type"], p.value) for p in tokens] == [("input", 7)]
    assert "director.generation.cost" not in harness.metrics()


# --------------------------------------------------------------------------- cancellation


def test_cancellation_is_recorded_and_still_propagates(harness):
    slow = SlowProvider("slow")
    service = make_service(slow, harness.telemetry, timeout=30)

    async def scenario() -> None:
        task = asyncio.create_task(service.generate(make_request()))
        await asyncio.wait_for(slow.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    span = harness.only_span()
    assert span.end_time is not None
    assert span.attributes["director.status"] == "cancelled"
    assert span.attributes["director.provider"] == "slow"
    assert span.attributes["director.provider_latency_ms"] >= 0
    assert span.status.status_code.name != "ERROR"
    point = harness.only_request_point()
    assert point.attributes["status"] == "cancelled"
    assert point.attributes["error_code"] == "none"
    assert len(harness.metrics()["director.generation.duration"]) == 1
    assert slow.cancelled


def test_unexpected_service_exception_still_ends_the_span_and_propagates(harness, monkeypatch):
    service = make_service(FakeProvider("p"), harness.telemetry)

    def explode(*_a: Any, **_k: Any) -> Any:
        raise ZeroDivisionError

    monkeypatch.setattr("dungeon_director.service.GenerationResponse.success_from_request", explode)
    # success_from_request raising is handled as INTERNAL_ERROR; make failure() raise instead.
    monkeypatch.setattr("dungeon_director.service.GenerationResponse.failure", explode)

    with pytest.raises(ZeroDivisionError):
        run_generate(service)

    span = harness.only_span()
    assert span.attributes["director.status"] == "internal_error"
    assert harness.only_request_point().attributes["status"] == "internal_error"


# --------------------------------------------------------------------------- FastAPI 422


def _client(harness: Harness, provider: FakeProvider | None = None) -> TestClient:
    registry = ProviderRegistry()
    provider = provider or FakeProvider("p")
    registry.register(provider)
    settings = DirectorSettings(default_provider="p", default_model=provider.default_model)
    return TestClient(create_app(settings, registry, harness.telemetry))


def test_valid_http_request_emits_exactly_one_span(harness):
    response = _client(harness).post("/v1/generate", json=request_payload())
    assert response.status_code == 200
    assert harness.only_span().attributes["director.http_status"] == 200


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda p: p.pop("state"), id="missing-field"),
        pytest.param(lambda p: p.update(contract_version="99.0"), id="bad-version"),
        pytest.param(lambda p: p.update(extra_field=SECRET), id="unknown-field"),
    ],
)
def test_fastapi_422_emits_span_and_count(harness, mutate):
    payload = request_payload(request_id="req-422", run_id="run-422")
    mutate(payload)

    response = _client(harness).post("/v1/generate", json=payload)

    assert response.status_code == 422
    span = harness.only_span()
    assert span.name == "director.generate"
    assert span.attributes["director.request_id"] == "req-422"
    assert span.attributes["director.run_id"] == "run-422"
    assert span.attributes["director.http_status"] == 422
    assert span.attributes["director.status"] == "invalid_request"
    assert span.attributes["director.schema_valid"] is False
    assert span.status.status_code.name == "ERROR"
    assert SECRET not in "".join(every_string(span))
    point = harness.only_request_point()
    assert dict(point.attributes) == {
        "provider": "none",
        "model": "none",
        "status": "invalid_request",
        "error_code": "invalid_request",
        "execution_mode": "active",
    }
    # No fake ~0 s latency sample for a request that never reached the service.
    assert "director.generation.duration" not in harness.metrics()


def test_fastapi_422_for_unparseable_json_and_bad_query_still_emit(harness):
    client = _client(harness)
    assert (
        client.post(
            "/v1/generate", content=b"{nope", headers={"content-type": "application/json"}
        ).status_code
        == 422
    )
    assert (
        client.post("/v1/generate?provider=" + "x" * 500, json=request_payload()).status_code == 422
    )

    spans = harness.finished_spans()
    assert len(spans) == 2
    assert all(s.attributes["director.status"] == "invalid_request" for s in spans)
    assert "director.request_id" not in spans[0].attributes
    assert spans[1].attributes["director.request_id"] == request_payload()["request_id"]
    assert harness.only_request_point().value == 2


def test_fastapi_422_never_attaches_untrusted_ids(harness):
    payload = {"request_id": "x" * 5000, "run_id": {"nested": SECRET}}

    assert _client(harness).post("/v1/generate", json=payload).status_code == 422

    span = harness.only_span()
    assert "director.request_id" not in span.attributes
    assert "director.run_id" not in span.attributes
    assert SECRET not in "".join(every_string(span))


def test_non_generate_validation_errors_are_not_counted(harness):
    client = _client(harness)
    assert client.get("/health").status_code == 200
    assert client.get("/v1/config").status_code == 200
    assert harness.finished_spans() == []


def test_http_end_to_end_matches_disabled_telemetry_bytes(harness):
    payload = request_payload()
    with_telemetry = _client(harness).post("/v1/generate", json=payload)
    without = TestClient(create_app(*_settings_registry())).post("/v1/generate", json=payload)

    assert with_telemetry.status_code == without.status_code == 200
    assert canonical_json(with_telemetry.json()) == canonical_json(without.json())


def _settings_registry() -> tuple[DirectorSettings, ProviderRegistry]:
    registry = ProviderRegistry()
    registry.register(FakeProvider("p"))
    return DirectorSettings(default_provider="p", default_model="fake-model"), registry


def canonical_json(body: dict[str, Any]) -> dict[str, Any]:
    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in _VOLATILE}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    return strip(body)


# --------------------------------------------------------------------------- secret hygiene


def test_no_secret_or_input_text_reaches_spans_metrics_or_logs(harness, caplog):
    caplog.set_level(logging.DEBUG)
    provider_cases = [
        ClassifiedFailureProvider(ErrorKind.PROVIDER_ERROR, f"Bearer {SECRET}", "a"),
        RaisingProvider(RuntimeError(f"https://user:{SECRET}@host"), "b"),
        MalformedProvider({"leak": SECRET}, "c"),
        MalformedProvider(f"not json {SECRET}", "d"),
    ]
    request = make_request(prompt_hint=f"my key is {SECRET}")
    for provider in provider_cases:
        asyncio.run(make_service(provider, harness.telemetry).generate(request))

    assert len(harness.finished_spans()) == len(provider_cases)
    for span in harness.finished_spans():
        assert SECRET not in "".join(every_string(span))
        assert "prompt" not in "".join(k for k in span.attributes)
    for points in harness.metrics().values():
        for point in points:
            assert SECRET not in str(dict(point.attributes))
    assert "my key is" not in caplog.text
    # The *service's own* log lines already withheld adapter text; telemetry adds none.
    assert SECRET not in "".join(
        r.getMessage() for r in caplog.records if r.name.endswith("telemetry")
    )


# --------------------------------------------------------------------------- fail-open


class _Boom(Exception):
    pass


class _Exploding:
    """Every attribute access is a callable that raises with secret-laden text."""

    def __getattr__(self, name: str):
        def raiser(*_a: Any, **_k: Any) -> Any:
            raise _Boom(f"{name} failed with {SECRET}")

        return raiser


class _SpanFailing:
    def __init__(self, failing: str) -> None:
        self._failing = failing

    def _maybe(self, name: str) -> None:
        if name == self._failing or self._failing == "all":
            raise _Boom(f"{name} {SECRET}")

    def set_attributes(self, *_a: Any, **_k: Any) -> None:
        self._maybe("set_attributes")

    def set_attribute(self, *_a: Any, **_k: Any) -> None:
        self._maybe("set_attributes")

    def set_status(self, *_a: Any, **_k: Any) -> None:
        self._maybe("set_status")

    def end(self, *_a: Any, **_k: Any) -> None:
        self._maybe("end")


class _Tracer:
    def __init__(self, failing: str) -> None:
        self._failing = failing

    def start_span(self, *_a: Any, **_k: Any) -> Any:
        if self._failing == "start_span":
            raise _Boom(f"start_span {SECRET}")
        return _SpanFailing(self._failing)


class _TracerProvider:
    def __init__(self, failing: str) -> None:
        self._failing = failing

    def get_tracer(self, *_a: Any, **_k: Any) -> Any:
        if self._failing == "get_tracer":
            raise _Boom(SECRET)
        return _Tracer(self._failing)

    def force_flush(self, *_a: Any) -> None:
        raise _Boom(SECRET)

    def shutdown(self) -> None:
        raise _Boom(SECRET)


class _Instrument:
    def add(self, *_a: Any, **_k: Any) -> None:
        raise _Boom(f"add {SECRET}")

    def record(self, *_a: Any, **_k: Any) -> None:
        raise _Boom(f"record {SECRET}")


class _Meter:
    def create_counter(self, *_a: Any, **_k: Any) -> Any:
        return _Instrument()

    def create_histogram(self, *_a: Any, **_k: Any) -> Any:
        return _Instrument()


class _MeterProvider:
    def __init__(self, *, fail_get_meter: bool = False) -> None:
        self._fail = fail_get_meter

    def get_meter(self, *_a: Any, **_k: Any) -> Any:
        if self._fail:
            raise _Boom(SECRET)
        return _Meter()

    def force_flush(self, *_a: Any) -> None:
        raise _Boom(SECRET)

    def shutdown(self) -> None:
        raise _Boom(SECRET)


def _broken_telemetry(kind: str) -> DirectorTelemetry:
    if kind == "get_meter":
        return DirectorTelemetry(_TracerProvider("none"), _MeterProvider(fail_get_meter=True))
    if kind == "instruments":
        return DirectorTelemetry(_TracerProvider("none"), _MeterProvider())
    return DirectorTelemetry(_TracerProvider(kind), _MeterProvider())


BROKEN_KINDS = [
    "get_tracer",
    "start_span",
    "set_attributes",
    "set_status",
    "end",
    "all",
    "get_meter",
    "instruments",
]


@pytest.mark.parametrize("kind", BROKEN_KINDS)
@pytest.mark.parametrize(
    "provider_factory",
    [
        pytest.param(lambda: FakeProvider("p"), id="success"),
        pytest.param(lambda: RaisingProvider(RuntimeError(SECRET), "p"), id="provider-error"),
        pytest.param(lambda: MalformedProvider({"bad": 1}, "p"), id="schema"),
    ],
)
def test_broken_telemetry_never_changes_the_canonical_outcome(kind, provider_factory, caplog):
    caplog.set_level(logging.DEBUG)
    baseline = canonical(run_generate(make_service(provider_factory(), None)))

    broken = canonical(run_generate(make_service(provider_factory(), _broken_telemetry(kind))))

    assert broken == baseline
    assert SECRET not in caplog.text


def test_broken_telemetry_on_timeout_and_selection_error_changes_nothing():
    def slow_case(telemetry):
        return run_generate(make_service(SlowProvider("p"), telemetry, timeout=0.05))

    assert canonical(slow_case(_broken_telemetry("all"))) == canonical(slow_case(None))

    def unknown(telemetry):
        return run_generate(make_service(FakeProvider("p"), telemetry), provider="nope")

    assert canonical(unknown(_broken_telemetry("all"))) == canonical(unknown(None))


def test_broken_telemetry_does_not_swallow_or_delay_cancellation():
    slow = SlowProvider("slow")
    service = make_service(slow, _broken_telemetry("all"), timeout=30)

    async def scenario() -> None:
        task = asyncio.create_task(service.generate(make_request()))
        await asyncio.wait_for(slow.started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)

    asyncio.run(scenario())
    assert slow.cancelled


def test_telemetry_object_that_raises_from_begin_and_finish_is_contained(monkeypatch):
    telemetry = DirectorTelemetry()
    monkeypatch.setattr(telemetry, "begin", _Exploding().begin)
    baseline = canonical(run_generate(make_service(FakeProvider("p"), None)))
    assert canonical(run_generate(make_service(FakeProvider("p"), telemetry))) == baseline

    telemetry = DirectorTelemetry()
    real_begin = telemetry.begin

    def begin_with_broken_finish(*args: Any, **kwargs: Any) -> Any:
        observation = real_begin(*args, **kwargs)
        for terminal in ("finish", "cancel", "abort", "end"):
            monkeypatch.setattr(observation, terminal, _Exploding().x)
        return observation

    monkeypatch.setattr(telemetry, "begin", begin_with_broken_finish)
    assert canonical(run_generate(make_service(FakeProvider("p"), telemetry))) == baseline
    failing = canonical(
        run_generate(make_service(RaisingProvider(RuntimeError("x"), "p"), telemetry))
    )
    assert failing[0] == 502


def test_a_failing_span_does_not_lose_the_metrics(harness):
    """Steps are guarded independently: a bad span must not drop the counters."""
    reader = InMemoryMetricReader()
    telemetry = DirectorTelemetry(_TracerProvider("all"), MeterProvider(metric_readers=[reader]))

    run_generate(make_service(FakeProvider("p"), telemetry))

    data = reader.get_metrics_data()
    names = {m.name for rm in data.resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert {"director.generation.requests", "director.generation.duration"} <= names


@pytest.mark.parametrize("broken", ["_requests", "_e2e_duration", "_provider_duration", "_tokens"])
def test_each_instrument_fails_independently(harness, broken):
    """One broken instrument must not cost the span or the other instruments."""
    setattr(harness.telemetry, broken, _Instrument())
    usage = UsageStats(input_tokens=1, output_tokens=2, estimated_cost_usd=0.5)

    run_generate(make_service(UsageProvider(usage), harness.telemetry))

    assert harness.only_span().attributes["director.status"] == "success"
    expected = {
        "_requests": "director.generation.requests",
        "_e2e_duration": "director.generation.duration",
        "_provider_duration": "director.provider.duration",
        "_tokens": "director.generation.tokens",
    }
    recorded = set(harness.metrics())
    assert expected[broken] not in recorded
    assert (set(expected.values()) | {"director.generation.cost"}) - {expected[broken]} <= recorded


def test_a_failing_metric_does_not_lose_the_span():
    exporter = InMemorySpanExporter()
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(SimpleSpanProcessor(exporter))
    telemetry = DirectorTelemetry(tracer_provider, _MeterProvider())

    run_generate(make_service(FakeProvider("p"), telemetry))

    (span,) = exporter.get_finished_spans()
    assert span.attributes["director.status"] == "success"


def test_flush_and_shutdown_failures_are_contained(caplog):
    telemetry = DirectorTelemetry(_TracerProvider("none"), _MeterProvider())
    telemetry.flush()
    telemetry.shutdown()
    telemetry.shutdown()  # idempotent: does not retry a provider that already failed
    assert "_Boom" in caplog.text
    assert SECRET not in caplog.text


def test_shutdown_flushes_pending_spans_and_metrics(harness):
    run_generate(make_service(FakeProvider("p"), harness.telemetry))
    harness.telemetry.flush()
    harness.telemetry.shutdown()
    assert len(harness.finished_spans()) == 1


def test_app_lifespan_shuts_telemetry_down_even_if_it_and_the_registry_fail(caplog):
    class ExplodingClose(FakeProvider):
        async def aclose(self) -> None:
            raise _Boom(SECRET)

    telemetry = DirectorTelemetry(_TracerProvider("none"), _MeterProvider())
    registry = ProviderRegistry()
    registry.register(ExplodingClose("p"))
    settings = DirectorSettings(default_provider="p", default_model="fake-model")

    with TestClient(create_app(settings, registry, telemetry)) as client:
        assert client.get("/health").status_code == 200

    assert telemetry._closed
    assert SECRET not in caplog.text


def test_app_lifespan_calls_shutdown_once(harness):
    calls: list[int] = []
    original = harness.telemetry.shutdown
    harness.telemetry.shutdown = lambda: (calls.append(1), original())[1]  # type: ignore[method-assign]

    with _client(harness) as client:
        client.post("/v1/generate", json=request_payload())

    assert calls == [1]


# --------------------------------------------------------------------------- setup


def test_disabled_settings_give_noop_telemetry_that_still_serves():
    telemetry = setup_telemetry(TelemetrySettings(enabled=False))
    assert not telemetry.enabled
    outcome = run_generate(make_service(FakeProvider("p"), telemetry))
    assert outcome.status_code == 200
    telemetry.shutdown()


def test_setup_failure_falls_back_and_logs_only_the_exception_type(monkeypatch, caplog):
    endpoint = f"http://user:{SECRET}@collector.internal:4318"
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as trace_exporter

    def exploding(*_a: Any, **_k: Any) -> Any:
        raise ValueError(f"cannot reach {endpoint}")

    monkeypatch.setattr(trace_exporter, "OTLPSpanExporter", exploding)
    with caplog.at_level(logging.WARNING):
        telemetry = setup_telemetry(TelemetrySettings(enabled=True, endpoint=endpoint))

    assert not telemetry.enabled
    assert "ValueError" in caplog.text
    assert SECRET not in caplog.text and "collector.internal" not in caplog.text
    assert run_generate(make_service(FakeProvider("p"), telemetry)).status_code == 200


@pytest.mark.parametrize("endpoint", ["not a url", "ftp://host", "http://", "localhost:4318"])
def test_unusable_endpoint_disables_telemetry_without_echoing_it(endpoint, caplog):
    with caplog.at_level(logging.WARNING):
        telemetry = setup_telemetry(TelemetrySettings(enabled=True, endpoint=endpoint))
    assert not telemetry.enabled
    assert endpoint not in caplog.text


def test_valid_endpoint_builds_exporting_telemetry_without_connecting():
    telemetry = setup_telemetry(TelemetrySettings(enabled=True, endpoint="http://127.0.0.1:9"))
    try:
        assert telemetry.enabled
    finally:
        telemetry.shutdown()


def test_create_app_survives_broken_telemetry_environment(monkeypatch):
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "definitely not a url")

    settings, registry = _settings_registry()
    with TestClient(create_app(settings, registry)) as client:
        assert client.post("/v1/generate", json=request_payload()).status_code == 200


@pytest.mark.parametrize(
    ("env", "enabled", "endpoint", "service"),
    [
        ({}, False, None, "dungeon-director"),
        (
            {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"},
            True,
            "http://c:4318",
            "dungeon-director",
        ),
        ({"OTEL_EXPORTER_OTLP_ENDPOINT": "  "}, False, None, "dungeon-director"),
        ({"DIRECTOR_OTEL_ENABLED": "true"}, True, DEFAULT_OTLP_ENDPOINT, "dungeon-director"),
        (
            {"DIRECTOR_OTEL_ENABLED": "false", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318"},
            False,
            "http://c:4318",
            "dungeon-director",
        ),
        (
            {"DIRECTOR_OTEL_ENABLED": "maybe", "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c"},
            False,
            "http://c",
            "dungeon-director",
        ),
        ({"OTEL_SERVICE_NAME": "director-dev"}, False, None, "director-dev"),
    ],
)
def test_settings_from_env(env, enabled, endpoint, service):
    settings = TelemetrySettings.from_env(env)
    assert (settings.enabled, settings.endpoint, settings.service_name) == (
        enabled,
        endpoint,
        service,
    )


def test_provider_error_uses_only_the_code_in_span_status(harness):
    provider = RaisingProvider(ProviderError(ErrorKind.RATE_LIMITED, f"quota {SECRET}"), "p")
    run_generate(make_service(provider, harness.telemetry))
    assert harness.only_span().status.description == "rate_limited"
