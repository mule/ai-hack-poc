"""Game bridge tests inspect real OTel output, not just mocked API calls."""

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogRecordExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from dungeon_director.game_telemetry import (
    MAX_GAME_BODY_BYTES,
    EventBudget,
    GameTelemetryRecorder,
    game_telemetry_router,
)
from dungeon_director.telemetry import DirectorTelemetry


def test_event_budget_is_fixed_and_refills(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr("dungeon_director.game_telemetry.time.monotonic", lambda: clock[0])
    budget = EventBudget(capacity=4, rate=2.0)
    assert budget.accept(4)
    assert not budget.accept(1)
    clock[0] += 0.5
    assert budget.accept(1)
    assert not budget.accept(1)
    clock[0] += 100
    assert not budget.accept(5)
    assert budget.accept(4)


def test_game_event_emits_correlated_span_and_bounded_metrics():
    exporter = InMemorySpanExporter()
    tracer = TracerProvider()
    tracer.add_span_processor(SimpleSpanProcessor(exporter))
    reader = InMemoryMetricReader()
    meter = MeterProvider(metric_readers=[reader])
    log_exporter = InMemoryLogRecordExporter()
    log_provider = LoggerProvider()
    log_provider.add_log_record_processor(SimpleLogRecordProcessor(log_exporter))
    telemetry = DirectorTelemetry(tracer, meter, logger_provider=log_provider)
    app = FastAPI()
    app.include_router(game_telemetry_router(telemetry, {"rules-baseline": {"builtin-v1"}}))
    response = TestClient(app).post(
        "/v1/telemetry/game",
        json={
            "schema_version": "1",
            "events": [
                {
                    "event_name": "room.committed",
                    "run_id": "run-42",
                    "request_id": "req-42",
                    "timestamp": datetime.now(UTC).isoformat(),
                    "traceparent": "00-1234567890abcdef1234567890abcdef-1234567890abcdef-01",
                    "attributes": {
                        "provider": "rules-baseline",
                        "model": "builtin-v1",
                        "materialization_ms": 250,
                        "room_type": "vault",
                        "enemy_density": 0.3,
                    },
                }
            ],
        },
    )
    assert response.status_code == 202
    spans = exporter.get_finished_spans()
    assert len(spans) == 1
    assert spans[0].attributes["director.request_id"] == "req-42"
    assert spans[0].links[0].context.trace_id == int("1234567890abcdef1234567890abcdef", 16)
    logs = log_exporter.get_finished_logs()
    assert len(logs) == 1
    assert logs[0].log_record.trace_id == spans[0].context.trace_id
    assert logs[0].log_record.body == "room.committed"
    metrics = reader.get_metrics_data()
    points = {
        m.name: list(m.data.data_points)
        for r in metrics.resource_metrics
        for s in r.scope_metrics
        for m in s.metrics
        if m.name.startswith("game.")
    }
    assert points["game.lifecycle.events"][0].value == 1
    assert points["game.lifecycle.duration"][0].sum == 0.25
    assert dict(points["game.lifecycle.events"][0].attributes) == {
        "event_name": "room.committed",
        "provider": "rules-baseline",
        "model": "builtin-v1",
    }
    assert points["game.rooms.committed"][0].attributes["room_type"] == "vault"
    assert points["game.room.density"][0].sum == 0.3
    telemetry.shutdown()


def test_recorder_failure_does_not_escape_or_log_exception_content(caplog):
    recorder = GameTelemetryRecorder(DirectorTelemetry(enabled=False), {})

    def fail(*args, **kwargs):
        raise RuntimeError("sk-secret-sentinel")

    recorder._record = fail
    recorder.record({})
    assert "RuntimeError" in caplog.text
    assert "sk-secret-sentinel" not in caplog.text


def bridge_client():
    app = FastAPI()
    app.include_router(game_telemetry_router(DirectorTelemetry(enabled=False), {}))
    return TestClient(app)


def batch():
    return {
        "schema_version": "1",
        "events": [
            {
                "event_name": "room.committed",
                "run_id": "run-42",
                "request_id": "req-42",
                "timestamp": datetime.now(UTC).isoformat(),
                "attributes": {"room_id": "room-42", "room_type": "vault", "danger": 3},
            }
        ],
    }


def test_bridge_accepts_schema_valid_event_when_export_is_disabled():
    response = bridge_client().post("/v1/telemetry/game", json=batch())
    assert response.status_code == 202
    assert response.json() == {"accepted": 1}


def test_bridge_rejects_sensitive_unknown_attributes_without_echoing_them():
    payload = batch()
    payload["events"][0]["attributes"]["raw_prompt"] = "sk-private-sentinel"
    response = bridge_client().post("/v1/telemetry/game", json=payload)
    assert response.status_code == 422
    assert response.json() == {"error": "invalid_game_telemetry"}
    assert "sk-private-sentinel" not in response.text


def test_bridge_enforces_actual_body_length_even_without_content_length():
    response = bridge_client().post(
        "/v1/telemetry/game",
        content=iter([b"x" * MAX_GAME_BODY_BYTES, b"x"]),
    )
    assert response.status_code == 413


def test_bridge_handles_bad_json_without_echoing_body():
    response = bridge_client().post("/v1/telemetry/game", content=b"sk-secret-not-json")
    assert response.status_code == 422
    assert "sk-secret" not in response.text


def test_bridge_reports_backpressure(monkeypatch):
    monkeypatch.setattr(EventBudget, "accept", lambda self, count: False)
    response = bridge_client().post("/v1/telemetry/game", json=batch())
    assert response.status_code == 429
