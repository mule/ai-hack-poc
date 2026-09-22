"""Remote OTLP export (issue #23): endpoints, headers, logs, diagnostics.

Everything about *what* is emitted stays covered by ``test_telemetry.py``;
these tests cover the remote-export layer: signal-specific endpoint
precedence, independent signal enablement, header handling and redaction,
``emit_log`` structured logs, per-signal export health, the
``/v1/telemetry/health`` diagnostics endpoint, and failure-safety against a
loopback fake collector that can fail, hang, or be unreachable.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import logging
import threading
import time
import warnings
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest
from fakes import FakeProvider, make_request, request_payload
from fastapi.testclient import TestClient
from opentelemetry import context as context_api
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import InMemoryLogExporter, SimpleLogRecordProcessor
from opentelemetry.sdk.trace.export import SpanExportResult

from dungeon_director.app import create_app
from dungeon_director.contracts import CONTRACT_VERSION
from dungeon_director.registry import ProviderRegistry
from dungeon_director.service import DirectorService
from dungeon_director.settings import DirectorSettings
from dungeon_director.telemetry import (
    LOG_ATTRIBUTE_MAX_CHARS,
    LOG_ATTRIBUTE_MAX_ENTRIES,
    LOG_BODY_MAX_CHARS,
    SIGNAL_PATHS,
    DirectorTelemetry,
    TelemetrySettings,
    setup_telemetry,
)

warnings.filterwarnings("ignore", category=DeprecationWarning)

SECRET = "sk-live-SECRET-0123456789"

TRACE_EXPORTER_MOD = "opentelemetry.exporter.otlp.proto.http.trace_exporter"
METRIC_EXPORTER_MOD = "opentelemetry.exporter.otlp.proto.http.metric_exporter"
LOG_EXPORTER_MOD = "opentelemetry.exporter.otlp.proto.http._log_exporter"


# --------------------------------------------------------------------------- #
# Exporter capture: replace the OTLP exporter classes with recording stubs so
# endpoint/header wiring is asserted without any network.
# --------------------------------------------------------------------------- #


class StubExporter:
    """Records constructor kwargs; every method succeeds or fails on demand.

    Mirrors the private attributes the SDK reads off real OTLPMetricExporter
    instances (``_preferred_temporality``/``_preferred_aggregation``), which
    :class:`_TrackedMetricExporter` delegates to the wrapped exporter.
    """

    _preferred_temporality = None
    _preferred_aggregation = None

    instances: list[StubExporter] = []

    def __init__(
        self,
        endpoint: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
        **kwargs: Any,
    ) -> None:
        self.endpoint = endpoint
        self.headers = dict(headers or {})
        self.timeout = timeout
        self.kwargs = kwargs
        self.exported = 0
        self.shutdown_called = False
        StubExporter.instances.append(self)

    def export(self, batch: Any, **kwargs: Any) -> Any:
        self.exported += 1
        return SUCCESS_RESULT

    def shutdown(self, **kwargs: Any) -> Any:
        self.shutdown_called = True
        return None

    def force_flush(self, timeout_millis: int | None = None, **kwargs: Any) -> Any:
        return None


SUCCESS_RESULT = SpanExportResult.SUCCESS


@pytest.fixture
def stub_exporters(monkeypatch: pytest.MonkeyPatch) -> list[StubExporter]:
    """Patch the three OTLP exporter classes; yields the constructed stubs."""
    StubExporter.instances = []
    import opentelemetry.exporter.otlp.proto.http._log_exporter as log_mod
    import opentelemetry.exporter.otlp.proto.http.metric_exporter as metric_mod
    import opentelemetry.exporter.otlp.proto.http.trace_exporter as trace_mod

    monkeypatch.setattr(trace_mod, "OTLPSpanExporter", StubExporter)
    monkeypatch.setattr(metric_mod, "OTLPMetricExporter", StubExporter)
    monkeypatch.setattr(log_mod, "OTLPLogExporter", StubExporter)
    yield StubExporter.instances
    StubExporter.instances = []


def build(env: dict[str, str]) -> DirectorTelemetry:
    telemetry = setup_telemetry(TelemetrySettings.from_env(env))
    assert telemetry is not None
    return telemetry


def endpoints_of(stubs: list[StubExporter]) -> dict[str, str]:
    found = {}
    for stub in stubs:
        path = urlsplit(stub.endpoint or "").path
        for signal, suffix in SIGNAL_PATHS.items():
            if path == suffix or path.endswith(suffix):
                found.setdefault(signal, stub.endpoint or "")
    return found


def stub_for(stubs: list[StubExporter], signal: str) -> StubExporter | None:
    matches = [s for s in stubs if (s.endpoint or "").endswith(SIGNAL_PATHS[signal])]
    return matches[0] if matches else None


# --------------------------------------------------------------------------- #
# Endpoint precedence and independent signals
# --------------------------------------------------------------------------- #


def test_base_endpoint_gets_each_signal_path(stub_exporters):
    telemetry = build({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318"})
    try:
        assert telemetry.enabled
        resolved = endpoints_of(stub_exporters)
        assert resolved == {
            "traces": "http://collector:4318/v1/traces",
            "metrics": "http://collector:4318/v1/metrics",
            "logs": "http://collector:4318/v1/logs",
        }
    finally:
        telemetry.shutdown()


def test_signal_specific_endpoint_overrides_base_but_keeps_other_signals(stub_exporters):
    telemetry = build(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "https://traces.example/somewhere/v1/traces",
        }
    )
    try:
        resolved = endpoints_of(stub_exporters)
        assert resolved["traces"] == "https://traces.example/somewhere/v1/traces"
        assert resolved["metrics"] == "http://collector:4318/v1/metrics"
        assert resolved["logs"] == "http://collector:4318/v1/logs"
    finally:
        telemetry.shutdown()


@pytest.mark.parametrize(
    ("flag", "signal"),
    [
        ("DIRECTOR_OTEL_TRACES_ENABLED", "traces"),
        ("DIRECTOR_OTEL_METRICS_ENABLED", "metrics"),
        ("DIRECTOR_OTEL_LOGS_ENABLED", "logs"),
    ],
)
@pytest.mark.parametrize("value", ["0", "false", "junk"])
def test_each_signal_can_be_disabled_independently(stub_exporters, flag, signal, value):
    telemetry = build({"OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318", flag: value})
    try:
        enabled = set(endpoints_of(stub_exporters))
        assert signal not in enabled
        assert enabled == {"traces", "metrics", "logs"} - {signal}
        described = telemetry.describe()
        assert described["signals"][signal] == {"enabled": False, "state": "disabled"}
    finally:
        telemetry.shutdown()


def test_all_signals_disabled_yields_noop(stub_exporters):
    telemetry = build(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
            "DIRECTOR_OTEL_TRACES_ENABLED": "false",
            "DIRECTOR_OTEL_METRICS_ENABLED": "false",
            "DIRECTOR_OTEL_LOGS_ENABLED": "false",
        }
    )
    assert not telemetry.enabled
    assert stub_exporters == []


def test_invalid_signal_endpoint_disables_only_that_signal(stub_exporters, caplog):
    with caplog.at_level(logging.WARNING):
        telemetry = build(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector:4318",
                "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT": "definitely not a url",
            }
        )
    try:
        assert telemetry.enabled
        enabled = set(endpoints_of(stub_exporters))
        assert enabled == {"traces", "logs"}
        assert "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT" in caplog.text
        # The malformed value is never echoed.
        assert "definitely not a url" not in caplog.text
    finally:
        telemetry.shutdown()


def test_invalid_base_endpoint_disables_everything(stub_exporters, caplog):
    with caplog.at_level(logging.WARNING):
        telemetry = build({"OTEL_EXPORTER_OTLP_ENDPOINT": "localhost:4318"})
    assert not telemetry.enabled
    assert stub_exporters == []
    assert "localhost:4318" not in caplog.text


# --------------------------------------------------------------------------- #
# Headers: parsing, precedence, and total redaction
# --------------------------------------------------------------------------- #


def test_headers_merge_with_signal_specific_winning(stub_exporters):
    telemetry = build(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
            "OTEL_EXPORTER_OTLP_HEADERS": f"api-key={SECRET},shared=yes",
            "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "api-key=traces-override",
        }
    )
    try:
        traces = stub_for(stub_exporters, "traces")
        metrics = stub_for(stub_exporters, "metrics")
        assert traces is not None and metrics is not None
        assert traces.headers["api-key"] == "traces-override"
        assert traces.headers["shared"] == "yes"
        assert metrics.headers["api-key"] == SECRET  # on the wire, by design
        assert metrics.headers["shared"] == "yes"
    finally:
        telemetry.shutdown()


def test_header_and_resource_values_never_appear_in_diagnostics_or_logs(stub_exporters, caplog):
    caplog.set_level(logging.INFO)
    telemetry = build(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://collector.internal:4318",
            "OTEL_EXPORTER_OTLP_HEADERS": f"authorization=Bearer {SECRET},=novalue,junk",
            "OTEL_RESOURCE_ATTRIBUTES": f"owner={SECRET},=bad",
        }
    )
    try:
        described = json.dumps(telemetry.describe())
        assert SECRET not in described
        assert "Bearer" not in described
        # Counts only: 1 accepted header, 2 rejected; 1 rejected resource entry.
        assert telemetry.describe()["headers"] == {"traces": 1, "metrics": 1, "logs": 1}
        assert "values withheld" in caplog.text
        assert SECRET not in caplog.text
    finally:
        telemetry.shutdown()


def test_header_entries_with_missing_value_are_rejected_and_counted():
    settings = TelemetrySettings.from_env(
        {"OTEL_EXPORTER_OTLP_HEADERS": f"good=1,empty=,junk-entry,{SECRET}"}
    )
    assert settings.headers == (("good", "1"),)
    assert settings.rejected_header_entries == 3


# --------------------------------------------------------------------------- #
# Resource identity (#22 contract)
# --------------------------------------------------------------------------- #


def test_resource_identity_defaults_and_overrides(stub_exporters):
    telemetry = build(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
            "DIRECTOR_OTEL_ENVIRONMENT": "prod",
        }
    )
    try:
        resource = telemetry.describe()["resource"]
        assert resource == {
            "service.name": "dungeon-director",
            "service.namespace": "dungeon-director",
            "service.version": CONTRACT_VERSION,
            "deployment.environment": "prod",
        }
    finally:
        telemetry.shutdown()


def test_service_name_and_version_are_configurable(stub_exporters):
    telemetry = build(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
            "OTEL_SERVICE_NAME": "dungeon-director-benchmark",
            "DIRECTOR_OTEL_SERVICE_VERSION": "9.9.9",
        }
    )
    try:
        resource = telemetry.describe()["resource"]
        assert resource["service.name"] == "dungeon-director-benchmark"
        assert resource["service.version"] == "9.9.9"
    finally:
        telemetry.shutdown()


def test_timeouts_and_interval_are_clamped(stub_exporters):
    telemetry = build(
        {
            "OTEL_EXPORTER_OTLP_ENDPOINT": "http://c:4318",
            "OTEL_EXPORTER_OTLP_TIMEOUT": "999999",  # -> 30000 ms
            "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT": "42",  # too small -> default clamp
            "OTEL_EXPORTER_OTLP_METRIC_EXPORT_INTERVAL": "10",  # -> 1000 ms
            "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT": "2000",
        }
    )
    try:
        traces = stub_for(stub_exporters, "traces")
        metrics = stub_for(stub_exporters, "metrics")
        assert traces is not None and metrics is not None
        assert traces.timeout == pytest.approx(10.0)  # invalid -> generic default
        assert metrics.timeout == pytest.approx(2.0)
    finally:
        telemetry.shutdown()


# --------------------------------------------------------------------------- #
# emit_log
# --------------------------------------------------------------------------- #


class LogHarness:
    def __init__(self) -> None:
        self.exporter = InMemoryLogExporter()
        self.provider = LoggerProvider()
        self.provider.add_log_record_processor(SimpleLogRecordProcessor(self.exporter))
        self.telemetry = DirectorTelemetry(logger_provider=self.provider)

    def records(self) -> list[Any]:
        return list(self.exporter.get_finished_logs())

    def only(self) -> Any:
        records = self.records()
        assert len(records) == 1, [r.log_record.body for r in records]
        return records[0]


@pytest.fixture
def logs() -> LogHarness:
    harness = LogHarness()
    yield harness
    harness.provider.shutdown()


def test_emit_log_exports_body_attributes_and_severity(logs):
    logs.telemetry.emit_log(
        "room.committed",
        {"run_id": "run-1", "request_id": "req-1", "room_id": "room-9", "depth": 3},
        None,
        severity="INFO",
    )
    record = logs.only().log_record
    assert record.body == "room.committed"
    assert record.severity_text == "INFO"
    assert record.attributes["run_id"] == "run-1"
    assert record.attributes["depth"] == 3


def test_emit_log_maps_warning_and_never_exports_exception_text(logs):
    logs.telemetry.emit_log(
        "generation.rejected", {"error_code": "schema_violation"}, None, severity="WARN"
    )
    logs.telemetry.emit_log("generation.fallback_applied", {}, None, severity="WARNING")
    logs.telemetry.emit_log("provider failed", {}, None, severity="ERROR")
    kinds = [(r.log_record.severity_text, r.log_record.body) for r in logs.records()]
    assert kinds == [
        ("WARN", "generation.rejected"),
        ("WARN", "generation.fallback_applied"),
        ("ERROR", "provider failed"),
    ]


def test_emit_log_unknown_severity_degrades_to_info(logs):
    logs.telemetry.emit_log("frontier.discovered", {}, None, severity="catastrophic")
    record = logs.only().log_record
    assert record.severity_text == "INFO"


def test_emit_log_truncates_body_and_values_and_caps_attribute_count(logs):
    body = "x" * (LOG_BODY_MAX_CHARS + 100)
    attributes = {f"k{i}": "y" * (LOG_ATTRIBUTE_MAX_CHARS + 50) for i in range(80)}
    logs.telemetry.emit_log(body, attributes, None)
    record = logs.only().log_record
    assert len(record.body) == LOG_BODY_MAX_CHARS
    assert len(record.attributes) == LOG_ATTRIBUTE_MAX_ENTRIES
    for value in record.attributes.values():
        assert len(value) == LOG_ATTRIBUTE_MAX_CHARS


def test_emit_log_stringifies_objects_and_drops_none(logs):
    logs.telemetry.emit_log("event", {"nested": {"a": 1}, "skip": None, "flag": True})
    record = logs.only().log_record
    assert record.attributes == {"nested": "{'a': 1}", "flag": True}


def test_emit_log_correlates_with_the_span_in_context(logs):
    from opentelemetry.sdk.trace import TracerProvider

    tracer = TracerProvider().get_tracer("t")
    with tracer.start_as_current_span("director.generate") as span:
        span_context = span.get_span_context()
        logs.telemetry.emit_log("room.committed", {}, context_api.get_current())
    record = logs.only().log_record
    assert record.trace_id == span_context.trace_id
    assert record.span_id == span_context.span_id


def test_emit_log_is_noop_and_never_raises_when_disabled():
    telemetry = DirectorTelemetry(enabled=False)
    telemetry.emit_log("room.committed", {"run_id": "run-1"})  # must not raise
    telemetry.emit_log(None, {"a": object()}, 1)  # type: ignore[arg-type]


def test_emit_log_survives_a_broken_logger_provider():
    class Exploding:
        def __getattr__(self, name: str) -> Any:
            def raiser(*_a: Any, **_k: Any) -> Any:
                raise RuntimeError(f"{name} {SECRET}")

            return raiser

    telemetry = DirectorTelemetry(logger_provider=Exploding())  # type: ignore[arg-type]
    telemetry.emit_log("room.committed", {"run_id": "run-1"})
    assert telemetry.enabled


# --------------------------------------------------------------------------- #
# Loopback fake collector
# --------------------------------------------------------------------------- #


class FakeCollector:
    """A loopback OTLP receiver that records requests and can misbehave."""

    def __init__(self, *, status: int = 200, delay: float = 0.0) -> None:
        self.requests: list[dict[str, Any]] = []
        self.status = status
        self.delay = delay
        handler = self._make_handler()
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _make_handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        collector = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length") or 0)
                body = self.rfile.read(length) if length else b""
                collector.requests.append(
                    {
                        "path": self.path,
                        "headers": {k.lower(): v for k, v in self.headers.items()},
                        "body": body,
                    }
                )
                if collector.delay:
                    time.sleep(collector.delay)
                self.send_response(collector.status)
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, *_a: Any) -> None:
                pass

        return Handler

    def paths(self) -> list[str]:
        return [request["path"] for request in self.requests]

    def requests_for(self, path: str) -> list[dict[str, Any]]:
        return [request for request in self.requests if request["path"] == path]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def collector() -> Iterator[FakeCollector]:
    fake = FakeCollector()
    yield fake
    fake.close()


def make_app(telemetry: DirectorTelemetry) -> TestClient:
    registry = ProviderRegistry()
    provider = FakeProvider("p")
    registry.register(provider)
    settings = DirectorSettings(default_provider="p", default_model=provider.default_model)
    return TestClient(create_app(settings, registry, telemetry))


def test_export_reaches_fake_collector_with_headers_and_stays_ok(collector):
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint,
        "OTEL_EXPORTER_OTLP_HEADERS": f"x-api-key={SECRET}",
        "OTEL_EXPORTER_OTLP_METRIC_EXPORT_INTERVAL": "1000",
    }
    telemetry = setup_telemetry(TelemetrySettings.from_env(env))
    client = make_app(telemetry)
    with client:
        assert client.post("/v1/generate", json=request_payload()).status_code == 200
        telemetry.emit_log("room.committed", {"run_id": "run-1"})
        telemetry.flush(5000)

    assert set(collector.paths()) == {"/v1/traces", "/v1/metrics", "/v1/logs"}
    for path in ("/v1/traces", "/v1/metrics", "/v1/logs"):
        requests = collector.requests_for(path)
        assert requests  # at least one export per signal (metrics may repeat)
        request = requests[-1]
        assert request["headers"]["x-api-key"] == SECRET  # sent, by design
        assert request["headers"]["content-type"] == "application/x-protobuf"
        # Nothing on the wire carries the secret outside the auth header.
        assert SECRET not in request["body"].decode("latin-1")
    described = telemetry.describe()
    states = {signal: info["state"] for signal, info in described["signals"].items()}
    assert states == {"traces": "ok", "metrics": "ok", "logs": "ok"}


def test_collector_rejections_do_not_change_responses_and_show_as_failing(caplog):
    failing = FakeCollector(status=500)
    try:
        caplog.set_level(logging.INFO)
        env = {
            "OTEL_EXPORTER_OTLP_ENDPOINT": failing.endpoint,
            "OTEL_EXPORTER_OTLP_TIMEOUT": "1000",
            "OTEL_EXPORTER_OTLP_METRIC_EXPORT_INTERVAL": "1000",
        }
        telemetry = setup_telemetry(TelemetrySettings.from_env(env))
        with make_app(telemetry) as client:
            response = client.post("/v1/generate", json=request_payload())
            assert response.status_code == 200
            assert response.json()["room"] is not None
            telemetry.emit_log("generation.rejected", {}, severity="WARN")
            telemetry.flush(5000)
    finally:
        failing.close()

    assert failing.paths()  # it did receive (and reject) exports
    states = {s: i["state"] for s, i in telemetry.describe()["signals"].items()}
    assert states["traces"] == "failing"
    assert states["logs"] == "failing"
    assert SECRET not in caplog.text


def test_unreachable_collector_keeps_director_healthy_and_shutdown_bounded():
    env = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",  # nothing listens
        "OTEL_EXPORTER_OTLP_TIMEOUT": "500",
    }
    telemetry = setup_telemetry(TelemetrySettings.from_env(env))
    try:
        with make_app(telemetry) as client:
            assert client.get("/health").status_code == 200
            assert client.post("/v1/generate", json=request_payload()).status_code == 200
            telemetry.emit_log("frontier.discovered", {"run_id": "run-1"})
        started = time.perf_counter()
        telemetry.flush(2000)
        elapsed = time.perf_counter() - started
        assert elapsed < 10  # bounded by the clamped exporter timeouts
        states = {s: i["state"] for s, i in telemetry.describe()["signals"].items()}
        assert "failing" in states.values()
    finally:
        started = time.perf_counter()
        telemetry.shutdown()
        assert time.perf_counter() - started < 15


def test_slow_collector_does_not_delay_generation():
    slow = FakeCollector(delay=1.5)
    try:
        telemetry = setup_telemetry(
            TelemetrySettings.from_env(
                {
                    "OTEL_EXPORTER_OTLP_ENDPOINT": slow.endpoint,
                    "OTEL_EXPORTER_OTLP_TIMEOUT": "500",
                }
            )
        )
        with make_app(telemetry) as client:
            started = time.perf_counter()
            assert client.post("/v1/generate", json=request_payload()).status_code == 200
            assert time.perf_counter() - started < 1.0  # export is background-only
    finally:
        slow.close()


# --------------------------------------------------------------------------- #
# Diagnostics endpoint
# --------------------------------------------------------------------------- #


def test_telemetry_health_endpoint_reports_safe_diagnostics(collector):
    telemetry = setup_telemetry(
        TelemetrySettings.from_env(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint,
                "OTEL_EXPORTER_OTLP_HEADERS": f"x-api-key={SECRET}",
            }
        )
    )
    with make_app(telemetry) as client:
        body = client.get("/v1/telemetry/health").json()
    assert body["enabled"] is True
    signals = body["signals"]
    assert set(signals) == {"traces", "metrics", "logs"}
    assert all(info["enabled"] is True for info in signals.values())
    assert signals["traces"]["endpoint"] == collector.endpoint
    dumped = json.dumps(body)
    assert SECRET not in dumped and "x-api-key" not in dumped


def test_telemetry_health_endpoint_when_disabled(monkeypatch):
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    with make_app(DirectorTelemetry(enabled=False)) as client:
        body = client.get("/v1/telemetry/health").json()
    assert body["enabled"] is False
    assert all(info == {"enabled": False, "state": "disabled"} for info in body["signals"].values())


def test_health_endpoint_shape_is_unchanged_even_with_telemetry_enabled(collector):
    telemetry = setup_telemetry(
        TelemetrySettings.from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
    )
    with make_app(telemetry) as client:
        assert client.get("/health").json() == {"status": "ok", "service": "dungeon-director"}


def test_config_endpoint_never_mentions_telemetry_headers(collector):
    telemetry = setup_telemetry(
        TelemetrySettings.from_env(
            {
                "OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint,
                "OTEL_EXPORTER_OTLP_HEADERS": f"x-api-key={SECRET}",
            }
        )
    )
    with make_app(telemetry) as client:
        dumped = json.dumps(client.get("/v1/config").json())
    assert "x-api-key" not in dumped and SECRET not in dumped


# --------------------------------------------------------------------------- #
# App lifecycle
# --------------------------------------------------------------------------- #


def test_lifespan_bounds_telemetry_shutdown(monkeypatch):
    import dungeon_director.app as app_module

    telemetry = DirectorTelemetry(enabled=False)

    def stuck_shutdown() -> None:
        time.sleep(5.0)

    telemetry.shutdown = stuck_shutdown  # type: ignore[method-assign]
    monkeypatch.setattr(app_module, "TELEMETRY_SHUTDOWN_TIMEOUT_SECONDS", 0.2)

    started = time.perf_counter()
    with make_app(telemetry) as client:
        assert client.get("/health").status_code == 200
    assert time.perf_counter() - started < 3.0


def test_lifespan_still_shuts_down_working_telemetry(collector):
    telemetry = setup_telemetry(
        TelemetrySettings.from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
    )
    with make_app(telemetry) as client:
        client.post("/v1/generate", json=request_payload())
    assert telemetry._closed


# --------------------------------------------------------------------------- #
# Generation equivalence (remote telemetry must not change answers)
# --------------------------------------------------------------------------- #


def test_canonical_response_is_identical_with_and_without_export(collector):
    payload = request_payload()
    telemetry = setup_telemetry(
        TelemetrySettings.from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
    )
    volatile = {"started_at", "completed_at", "latency_ms", "occurred_at"}

    def strip(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: strip(v) for k, v in value.items() if k not in volatile}
        if isinstance(value, list):
            return [strip(v) for v in value]
        return value

    with make_app(telemetry) as client:
        exported = client.post("/v1/generate", json=payload).json()
    with make_app(DirectorTelemetry(enabled=False)) as client:
        plain = client.post("/v1/generate", json=payload).json()

    assert strip(exported) == strip(plain)


def test_service_generate_works_with_exporting_telemetry(collector):
    telemetry = setup_telemetry(
        TelemetrySettings.from_env({"OTEL_EXPORTER_OTLP_ENDPOINT": collector.endpoint})
    )
    registry = ProviderRegistry()
    provider = FakeProvider("p")
    registry.register(provider)
    service = DirectorService(
        registry,
        DirectorSettings(default_provider="p", default_model=provider.default_model),
        telemetry=telemetry,
    )
    outcome = asyncio.run(service.generate(make_request()))
    assert outcome.status_code == 200
