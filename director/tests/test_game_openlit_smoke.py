"""Real headless Godot → isolated director → decoded OTLP lifecycle acceptance."""

import os
import shutil
from pathlib import Path

import pytest
from benchmarks.game_openlit_smoke import run
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from test_remote_telemetry import FakeCollector


def test_real_game_lifecycle_reaches_all_collector_signals(monkeypatch):
    project = Path(
        os.environ.get("OPENLIT_GAME_TEST_PROJECT", Path(__file__).resolve().parents[2] / "game")
    )
    godot = shutil.which("godot")
    if godot is None or not (project / "world/game_telemetry_sink.gd").exists():
        pytest.skip("requires Godot and merged game lifecycle instrumentation")
    for key in list(os.environ):
        if key.startswith(("OTEL_", "DIRECTOR_OTEL_")):
            monkeypatch.delenv(key)
    collector = FakeCollector()
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", collector.endpoint)
    monkeypatch.setenv("OTEL_SERVICE_NAME", "dungeon-director")
    try:
        code, evidence = run(project, godot, 0)
        assert code == 2, evidence
        assert evidence["status"] == "game_delivered_unverified"
        assert evidence["ingestion_verified"] is False
        assert evidence["events_sent"] >= 5
        assert evidence["provider"] == "rules-baseline"
        assert set(collector.paths()) == {"/v1/traces", "/v1/metrics", "/v1/logs"}
        spans = [
            span
            for request in collector.requests_for("/v1/traces")
            for resource in ExportTraceServiceRequest.FromString(request["body"]).resource_spans
            for scope in resource.scope_spans
            for span in scope.spans
        ]
        records = [
            record
            for request in collector.requests_for("/v1/logs")
            for resource in ExportLogsServiceRequest.FromString(request["body"]).resource_logs
            for scope in resource.scope_logs
            for record in scope.log_records
        ]
        required = {
            "generation.sent",
            "generation.response_received",
            "room.committed",
            "door.revealed",
            "room.entered",
        }
        assert required <= {record.body.string_value for record in records}
        generation = next(span for span in spans if span.name == "director.generate")
        attrs = {item.key: item.value.string_value for item in generation.attributes}
        assert attrs["director.run_id"] == evidence["run_id"]
        request_id = attrs["director.request_id"]
        # Final room lifecycle spans must link the actual director span, not
        # merely reuse request-ID attributes or invent a synthetic parent.
        linked_events = set()
        for span in spans:
            span_attrs = {item.key: item.value.string_value for item in span.attributes}
            if span_attrs.get("director.request_id") != request_id:
                continue
            name = span_attrs.get("game.event.name")
            if name in {"room.committed", "room.entered"}:
                assert any(
                    link.trace_id == generation.trace_id and link.span_id == generation.span_id
                    for link in span.links
                ), f"{name} must link the director.generate span"
                linked_events.add(name)
        assert linked_events == {"room.committed", "room.entered"}
        entered = next(record for record in records if record.body.string_value == "room.entered")
        attrs = {item.key: item.value.string_value for item in entered.attributes}
        assert attrs["director.run_id"] == evidence["run_id"]
        assert attrs["director.request_id"] == request_id
    finally:
        collector.close()
