"""Smoke acceptance proves persisted identities, never collector HTTP status alone."""

import argparse
import asyncio
import json
import os
import subprocess
from pathlib import Path

import httpx
import pytest
from benchmarks import openlit_smoke as smoke
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from test_remote_telemetry import FakeCollector

from dungeon_director.telemetry import TelemetrySettings, setup_telemetry

SECRET = "secret-must-never-appear-89371"
ENV = {
    "OPENLIT_SMOKE_CLICKHOUSE_URL": "http://127.0.0.1:8123",
    "OPENLIT_SMOKE_CLICKHOUSE_DATABASE": "openlit",
    "OPENLIT_SMOKE_CLICKHOUSE_USER": "explicit-test-user",
    "OPENLIT_SMOKE_CLICKHOUSE_PASSWORD": SECRET,
}
SAMPLE = smoke.Sample("smoke-request", "rules-baseline", "rules-v1")


def response_counts(missing=None):
    return {
        "data": [
            {"signal": name, "matched": "0" if name == missing else "1"} for name in smoke.SIGNALS
        ]
    }


@pytest.mark.parametrize("missing", [*smoke.SIGNALS, None])
def test_each_missing_signal_fails_acceptance(monkeypatch, missing):
    class Verifier:
        def counts(self, *args):
            return {r["signal"]: int(r["matched"]) for r in response_counts(missing)["data"]}

    counts = smoke.verify(Verifier(), "fresh", 1000, [SAMPLE], 0)
    if missing is not None:
        assert counts[SAMPLE.request_id][missing] == 0
    monkeypatch.setattr(smoke, "ClickHouseVerifier", lambda env: Verifier())
    Verifier.close = lambda self: None

    class Telemetry:
        enabled = True

        def describe(self):
            return {"resource": {"service.version": "1.2.3", "deployment.environment": "test"}}

        def flush(self, *args):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr("dungeon_director.telemetry.setup_telemetry", lambda settings: Telemetry())

    async def emit(*args):
        return [SAMPLE]

    monkeypatch.setattr(smoke, "emit", emit)
    code, evidence = smoke.run(
        argparse.Namespace(live_provider=[], live=False, emit_only=False, deadline=0),
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"},
    )
    assert code == (1 if missing else 0)
    assert evidence["missing"] == ({SAMPLE.request_id: [missing]} if missing else {})
    assert evidence["ingestion_verified"] is (missing is None)


def test_readonly_queries_match_fresh_instance_and_correlated_trace():
    def handler(request):
        params = request.url.params
        assert params["readonly"] == "1"
        assert params["param_instance"] == "fresh-instance"
        assert params["param_started"] == "1234"
        assert params["param_request"] == SAMPLE.request_id
        sql = request.content.decode()
        assert "TraceId IN (SELECT TraceId" in sql
        assert sql.count("ResourceAttributes['service.instance.id']") == 5
        assert sql.count("fromUnixTimestamp64Milli") == 5
        assert "Attributes['request_id']" not in sql
        assert SECRET not in sql
        # Simulate old rows in storage: only exact fresh identity can match.
        return httpx.Response(200, json=response_counts("logs"))

    verifier = smoke.ClickHouseVerifier(ENV, transport=httpx.MockTransport(handler))
    try:
        assert verifier.counts("fresh-instance", 1234, SAMPLE)["logs"] == 0
    finally:
        verifier.close()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_backend_errors_never_echo_response_credentials(status):
    verifier = smoke.ClickHouseVerifier(
        ENV, transport=httpx.MockTransport(lambda request: httpx.Response(status, text=SECRET))
    )
    try:
        with pytest.raises(smoke.SmokeError) as error:
            verifier.counts("fresh", 1000, SAMPLE)
        assert SECRET not in str(error.value)
    finally:
        verifier.close()


def test_missing_config_and_live_opt_in_fail_before_emission(monkeypatch):
    async def forbidden(*args):
        pytest.fail("must not emit")

    monkeypatch.setattr(smoke, "emit", forbidden)
    args = argparse.Namespace(live_provider=[], live=False, emit_only=False, deadline=1)
    code, evidence = smoke.run(args, {})
    assert code == 1 and evidence["error"] == "verification_config_missing_or_invalid"
    args.live_provider = ["groq"]
    assert smoke.run(args, {})[1]["error"] == "live_provider_requires_live_opt_in"


def test_emit_only_exports_real_correlated_signals_without_prompt_or_secret(monkeypatch):
    collector = FakeCollector()
    try:
        settings = TelemetrySettings.from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
        from dataclasses import replace

        telemetry = setup_telemetry(
            replace(settings, resource_attributes=(("service.instance.id", "fresh"),))
        )
        # A sentinel in the actual generation prompt must never enter OTLP payloads.
        real_read = smoke.Path.read_text

        def fixture_read(path, *args, **kwargs):
            value = real_read(path, *args, **kwargs)
            if path.name == "generation_request.json":
                payload = json.loads(value)
                payload["prompt_hint"] = SECRET
                return json.dumps(payload)
            return value

        monkeypatch.setattr(smoke.Path, "read_text", fixture_read)
        samples = asyncio.run(smoke.emit(telemetry, ["rules-baseline"], "fresh"))
        assert samples[0].provider == "rules-baseline"
        assert samples[0].model != "unknown"
        telemetry.flush(5000)
        telemetry.shutdown()
        traces, logs, metrics = [], [], []
        for path, cls, target in [
            ("/v1/traces", ExportTraceServiceRequest, traces),
            ("/v1/logs", ExportLogsServiceRequest, logs),
            ("/v1/metrics", ExportMetricsServiceRequest, metrics),
        ]:
            for request in collector.requests_for(path):
                assert SECRET.encode() not in request["body"]
                decoded = cls.FromString(request["body"])
                target.append(decoded)
            assert target, path
        spans = [
            s
            for export in traces
            for r in export.resource_spans
            for scope in r.scope_spans
            for s in scope.spans
        ]
        generation = next(s for s in spans if s.name == "director.generate")
        records = [
            r
            for export in logs
            for resource in export.resource_logs
            for scope in resource.scope_logs
            for r in scope.log_records
        ]
        record = next(r for r in records if r.body.string_value == "director.smoke.generation")
        assert generation.trace_id == record.trace_id
        assert any(
            a.key == "director.request_id" and a.value.string_value == samples[0].request_id
            for a in record.attributes
        )
        names = {
            m.name
            for export in metrics
            for r in export.resource_metrics
            for scope in r.scope_metrics
            for m in scope.metrics
        }
        assert {"director.generation.requests", "director.generation.duration"} <= names
    finally:
        collector.close()


def test_emit_only_is_explicitly_nonzero(monkeypatch):
    class Telemetry:
        enabled = True

        def describe(self):
            return {"resource": {"service.version": "1.2.3", "deployment.environment": "test"}}

        def flush(self, *args):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr("dungeon_director.telemetry.setup_telemetry", lambda settings: Telemetry())

    async def emit(*args):
        return [SAMPLE]

    monkeypatch.setattr(smoke, "emit", emit)
    code, evidence = smoke.run(
        argparse.Namespace(live_provider=[], live=False, emit_only=True, deadline=1),
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318"},
    )
    assert code == 2
    assert evidence["status"] == "emitted_unverified"
    assert evidence["ingestion_verified"] is False


def test_make_smoke_stdout_is_one_json_document():
    root = Path(__file__).resolve().parents[2]
    # Point at the active test interpreter's venv without requiring a second install.
    import sys

    env = {k: v for k, v in os.environ.items() if not k.startswith("OPENLIT_SMOKE_")}
    result = subprocess.run(
        [
            "make",
            "--no-print-directory",
            "openlit-smoke",
            f"VENV={Path(sys.executable).parent.parent}",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 2  # Make's failure code, not the Python CLI's 1.
    evidence = json.loads(result.stdout)
    assert evidence["error"] == "verification_config_missing_or_invalid"


def test_evidence_records_actual_sample_and_safe_build_resource_identity(monkeypatch):
    class Telemetry:
        enabled = True

        def flush(self, *args):
            pass

        def shutdown(self):
            pass

        def describe(self):
            return {
                "resource": {
                    "service.version": "v2.3.4",
                    "deployment.environment": "staging",
                    "secret.extra": SECRET,
                }
            }

    monkeypatch.setattr("dungeon_director.telemetry.setup_telemetry", lambda settings: Telemetry())
    monkeypatch.setattr(smoke, "build_identity", lambda: {"revision": "a" * 40, "dirty": False})

    async def emit(*args):
        return [smoke.Sample("req1", "groq", "actual-model-v3")]

    monkeypatch.setattr(smoke, "emit", emit)
    code, evidence = smoke.run(
        argparse.Namespace(live_provider=[], live=False, emit_only=True, deadline=1),
        {"OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318", "SECRET": SECRET},
    )
    assert code == 2
    assert evidence["samples"] == [
        {"request_id": "req1", "provider": "groq", "model": "actual-model-v3"}
    ]
    assert evidence["service_version"] == "v2.3.4"
    assert evidence["environment"] == "staging"
    assert evidence["build"] == {"revision": "a" * 40, "dirty": False}
    assert SECRET not in json.dumps(evidence)


@pytest.mark.parametrize("value", ["https://user:secret@host", "sk-secret", "Bearer token", "x\ny"])
def test_evidence_labels_reject_sensitive_or_unbounded_values(value):
    assert smoke.evidence_label(value) == "unknown"


def test_build_identity_reports_checkout_without_exposing_status_paths():
    identity = smoke.build_identity()
    assert len(identity["revision"]) == 40
    assert isinstance(identity["dirty"], bool)
    assert set(identity) == {"revision", "dirty"}
