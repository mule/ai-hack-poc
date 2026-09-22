"""Production app integration: W3C, comparisons, replay, cancellation and privacy."""

import asyncio
import json

import httpx
import pytest
from fakes import FakeProvider, make_request, valid_room_dict
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from shadow_fakes import GatedProvider
from test_telemetry import Harness, make_service

from dungeon_director.app import create_app
from dungeon_director.contracts import UsageStats
from dungeon_director.providers import ProviderResult
from dungeon_director.registry import ProviderRegistry
from dungeon_director.settings import DirectorSettings, ShadowSettings, ShadowTarget
from dungeon_director.telemetry import DirectorTelemetry


def build(active_gate=None, shadow_gate=None, timeout=5):
    spans = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(spans))
    metrics = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[metrics])
    logs = InMemoryLogRecordExporter()
    logger = LoggerProvider()
    logger.add_log_record_processor(SimpleLogRecordProcessor(logs))
    telemetry = DirectorTelemetry(tracer, meter, logger_provider=logger)
    registry = ProviderRegistry()
    active = GatedProvider("active", gate=active_gate)
    shadow = GatedProvider("shadow", gate=shadow_gate)
    registry.register(active)
    registry.register(shadow)
    settings = DirectorSettings(
        default_provider="active",
        default_model="fake-model",
        timeout_seconds=timeout,
        shadow=ShadowSettings(targets=(ShadowTarget("shadow", "fake-model"),), drain_seconds=0.01),
    )
    app = create_app(registry=registry, settings=settings, telemetry=telemetry)
    return app, registry, telemetry, spans, logs, active, shadow


def mode(span):
    return span.attributes.get(
        "director.execution_mode", span.attributes.get("director.provider.execution_mode")
    )


def comparison_id(span):
    return span.attributes.get(
        "shadow_comparison_id", span.attributes.get("director.shadow_comparison_id")
    )


def test_http_parent_response_header_and_comparison_registration():
    async def scenario():
        app, registry, telemetry, exporter, logs, _, _ = build()
        incoming = "00-123456789abcdef0123456789abcdef0-123456789abcdef0-01"
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                response = await client.post(
                    "/v1/generate",
                    json=make_request().model_dump(mode="json"),
                    headers={"traceparent": incoming},
                )
            assert response.status_code == 200
            await app.state.service.aclose()
            spans = list(exporter.get_finished_spans())
            generations = [s for s in spans if s.name == "director.generate"]
            providers = [s for s in spans if s.name == "director.provider.invoke"]
            assert len(generations) == len(providers) == 2
            active = next(s for s in generations if mode(s) == "active")
            shadow = next(s for s in generations if mode(s) == "shadow")
            assert active.context.trace_id == int(incoming.split("-")[1], 16)
            assert active.parent.span_id == int(incoming.split("-")[2], 16)
            assert shadow.parent.span_id == active.context.span_id
            assert {s.context.trace_id for s in spans} == {active.context.trace_id}
            header = response.headers["traceparent"].split("-")
            assert int(header[1], 16) == active.context.trace_id
            assert int(header[2], 16) == active.context.span_id
            for provider in providers:
                parent = active if mode(provider) == "active" else shadow
                assert provider.parent.span_id == parent.context.span_id
                assert provider.attributes["gen_ai.usage.input_tokens"] == 11
            ids = {comparison_id(s) for s in generations + providers}
            assert len(ids) == 1 and None not in ids
            assert len([s for s in spans if s.name == "director.shadow.comparison"]) == 1
            assert any(
                record.log_record.body == "shadow comparison completed"
                for record in logs.get_finished_logs()
            )
        finally:
            await app.state.service.aclose()
            await registry.aclose()
            telemetry.shutdown()

    asyncio.run(scenario())


def test_cancel_closes_active_and_shadow_provider_spans():
    async def scenario():
        app, registry, telemetry, exporter, _, active, shadow = build(
            asyncio.Event(), asyncio.Event()
        )
        try:
            task = asyncio.create_task(app.state.service.generate(make_request()))
            await asyncio.wait_for(asyncio.gather(active.started.wait(), shadow.started.wait()), 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            await app.state.service.aclose()
            spans = list(exporter.get_finished_spans())
            providers = [s for s in spans if s.name == "director.provider.invoke"]
            generations = [s for s in spans if s.name == "director.generate"]
            assert len(providers) == len(generations) == 2
            assert all(s.attributes["director.provider.outcome"] == "cancelled" for s in providers)
            assert all(s.end_time is not None for s in providers + generations)
            assert {s.parent.span_id for s in providers} == {s.context.span_id for s in generations}
        finally:
            await app.state.service.aclose()
            await registry.aclose()
            telemetry.shutdown()

    asyncio.run(scenario())


def test_replay_context_reaches_provider_and_shadow_comparison():
    from benchmarks.replay import run_benchmark

    async def scenario():
        app, registry, telemetry, exporter, _, _, _ = build()
        try:
            report = await run_benchmark(
                [make_request()],
                ["active"],
                {"active": "fake-model"},
                service=app.state.service,
                registry=registry,
                evaluation_id="eval-acceptance",
            )
            spans = list(exporter.get_finished_spans())
            replay = next(s for s in spans if s.name == "director.replay.case")
            generations = [s for s in spans if s.name == "director.generate"]
            active = next(s for s in generations if mode(s) == "replay")
            shadow = next(s for s in generations if mode(s) == "shadow")
            assert active.parent.span_id == replay.context.span_id
            assert shadow.parent.span_id == active.context.span_id
            for s in spans:
                assert s.context.trace_id == replay.context.trace_id
                assert s.attributes.get("evaluation_id") == "eval-acceptance"
                assert s.attributes.get("case_id") == report.results[0].telemetry_ids["case_id"]
            providers = [s for s in spans if s.name == "director.provider.invoke"]
            assert {mode(s) for s in providers} == {"replay", "shadow"}
        finally:
            telemetry.shutdown()

    asyncio.run(scenario())


def test_served_model_usage_and_sensitive_metadata_are_correct():
    class MetadataProvider(FakeProvider):
        async def decide(self, request):
            return ProviderResult(
                payload=valid_room_dict(request),
                usage=UsageStats(input_tokens=12, output_tokens=4, estimated_cost_usd=0.01),
                provider_metadata={
                    "jev_model": "actual-deployment-v2",
                    "finish_reason": "Bearer sk-review-canary",
                    "room_type_confidence": 0.75,
                    "time_queue": "raw_prompt_text",
                    "size_provider_choice": "sk-review-canary",
                    "danger_confidence": 3.5,
                    "raw_prompt": "private request body",
                    "room_type_probabilities": {"room": 0.75},
                },
            )

    h = Harness()
    try:
        outcome = asyncio.run(
            make_service(MetadataProvider("fake"), h.telemetry).generate(make_request())
        )
        assert outcome.response.success
        attrs = next(
            s.attributes for s in h.finished_spans() if s.name == "director.provider.invoke"
        )
        assert attrs["gen_ai.request.model"] == "fake-model"
        assert attrs["gen_ai.response.model"] == "actual-deployment-v2"
        assert attrs["gen_ai.usage.input_tokens"] == 12
        assert attrs["gen_ai.usage.output_tokens"] == 4
        assert attrs["gen_ai.usage.cost"] == 0.01
        assert attrs["director.provider.meta.room_type_confidence"] == 0.75
        assert not any(
            key.endswith(
                (
                    "finish_reason",
                    "time_queue",
                    "size_provider_choice",
                    "danger_confidence",
                    "raw_prompt",
                    "room_type_probabilities",
                )
            )
            for key in attrs
        )
        assert "sk-review-canary" not in str(attrs)
    finally:
        h.telemetry.shutdown()


def test_sanitizer_failure_still_ends_provider_span(monkeypatch):
    from dungeon_director import provider_telemetry

    def broken(_metadata):
        raise RuntimeError("private provider payload")

    monkeypatch.setattr(provider_telemetry, "_bounded_metadata_attributes", broken)
    h = Harness()
    try:
        outcome = asyncio.run(
            make_service(FakeProvider("fake"), h.telemetry).generate(make_request())
        )
        assert outcome.response.success
        providers = [s for s in h.finished_spans() if s.name == "director.provider.invoke"]
        assert len(providers) == 1 and providers[0].end_time is not None
    finally:
        h.telemetry.shutdown()


def test_parallel_http_requests_do_not_share_context_and_bad_header_is_ignored():
    async def scenario():
        app, registry, telemetry, exporter, _, _, _ = build()
        headers = [
            "00-123456789abcdef0123456789abcdef0-123456789abcdef0-01",
            "00-abcdef0123456789abcdef0123456789-abcdef0123456789-01",
            "invalid-secret-header",
        ]
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:

                async def send(index):
                    return await client.post(
                        "/v1/generate",
                        json=make_request(request_id=f"request-{index}").model_dump(mode="json"),
                        headers={"traceparent": headers[index]},
                    )

                responses = await asyncio.gather(*(send(i) for i in range(3)))
            await app.state.service.aclose()
            assert all(r.status_code == 200 for r in responses)
            traces = []
            for index, response in enumerate(responses):
                trace_id = int(response.headers["traceparent"].split("-")[1], 16)
                traces.append(trace_id)
                if index < 2:
                    assert trace_id == int(headers[index].split("-")[1], 16)
                spans = [
                    s
                    for s in exporter.get_finished_spans()
                    if s.attributes.get("director.request_id") == f"request-{index}"
                ]
                assert len([s for s in spans if s.name == "director.generate"]) == 2
                assert {s.context.trace_id for s in spans} == {trace_id}
            assert len(set(traces)) == 3
            assert "invalid-secret-header" not in str(
                [s.attributes for s in exporter.get_finished_spans()]
            )
        finally:
            await app.state.service.aclose()
            await registry.aclose()
            telemetry.shutdown()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "adapter", ["groq", "cerebras", "typesafe-jev", "cloudflare-jev", "rules-baseline"]
)
def test_each_real_adapter_exports_child_span_with_reported_usage(adapter):
    import test_cerebras as cerebras
    import test_cloudflare_jev as cloudflare
    import test_groq as groq
    import test_typesafe_jev as typesafe

    from dungeon_director.rules import RulesProvider

    room_json = json.dumps(valid_room_dict(make_request()))
    factories = {
        "groq": lambda: groq.provider_with(
            groq.FakeTransport([groq.http_response(groq.completion(content=room_json))])
        ),
        "cerebras": lambda: cerebras.provider_with(
            cerebras.FakeCerebrasTransport(
                [cerebras.http_response(cerebras.cerebras_chat_completion(room_content=room_json))]
            )
        ),
        "typesafe-jev": lambda: typesafe.provider_with(
            typesafe.FakeTransport([typesafe.response(typesafe.payload())])
        ),
        "cloudflare-jev": lambda: cloudflare.provider_with(
            cloudflare.FakeTransport(
                [cloudflare.http_response(cloudflare.envelope(cloudflare.jev_payload()))]
            )
        ),
        "rules-baseline": RulesProvider,
    }
    h = Harness()
    try:
        outcome = asyncio.run(
            make_service(factories[adapter](), h.telemetry).generate(make_request())
        )
        assert outcome.response.success
        child = h.only_provider_span()
        assert child.parent.span_id == h.director_span().context.span_id
        assert child.attributes["gen_ai.provider.name"] == adapter
        assert child.attributes["director.provider.outcome"] == "success"
        assert child.attributes["director.provider.call_duration_ms"] >= 0
        usage = outcome.response.metadata.usage
        if usage and usage.input_tokens is not None:
            assert child.attributes["gen_ai.usage.input_tokens"] == usage.input_tokens
        else:
            assert "gen_ai.usage.input_tokens" not in child.attributes
        assert not any("probabilities" in key for key in child.attributes)
    finally:
        h.telemetry.shutdown()
