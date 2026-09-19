"""OpenTelemetry traces and metrics for director generation decisions.

Every ``POST /v1/generate`` produces exactly one ``director.generate`` span and
one ``director.generation.requests`` count, whether it ends in a room, a
provider failure, a timeout, a schema failure, a cancellation, or a FastAPI 422
before the service is reached.

Design rules, all covered by ``tests/test_telemetry.py``:

* **Telemetry never changes generation.** Every OpenTelemetry call (instrument
  creation, span start, attribute writes, metric recording, ``span.end``,
  flush, shutdown) runs behind a guard that logs the exception *type* and
  carries on. The canonical response and HTTP status are identical whether
  telemetry is disabled, healthy, or broken.
* **Metric dimensions are bounded.** Only ``provider``, ``model``, ``status``,
  ``error_code`` and ``execution_mode`` (plus ``token_type`` on the token
  counter) are ever used. Provider and model come from the registry, never
  from the request, so a client cannot mint new time series. Request and run
  IDs are span attributes only.
* **Nothing sensitive is recorded.** No request or response bodies, prompt
  hints, exception text, adapter error messages, headers or credentials: spans
  carry the machine-readable error *code*, never the message.
* **Tokens and cost only when the provider reported them.**
* **Active vs shadow** is the ``execution_mode`` dimension (and the
  ``director.is_shadow`` span attribute), ready for the shadow runner in #13.

Telemetry is off unless ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set or
``DIRECTOR_OTEL_ENABLED`` is truthy; see :class:`TelemetrySettings`.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ParamSpec, TypeVar
from urllib.parse import urlsplit

from opentelemetry.metrics import MeterProvider, NoOpMeterProvider
from opentelemetry.trace import (
    INVALID_SPAN,
    NoOpTracerProvider,
    Span,
    SpanKind,
    Status,
    StatusCode,
    TracerProvider,
)

from dungeon_director.contracts import (
    ErrorKind,
    GenerationRequest,
    GenerationResponse,
    UsageStats,
)
from dungeon_director.errors import SelectionReason

__all__ = [
    "DEFAULT_OTLP_ENDPOINT",
    "LATENCY_BUCKET_BOUNDARIES",
    "METRIC_DIMENSIONS",
    "DirectorTelemetry",
    "GenerationObservation",
    "Outcome",
    "TelemetrySettings",
    "setup_telemetry",
]

logger = logging.getLogger(__name__)

TRACER_NAME = "dungeon-director"
METER_NAME = "dungeon-director"
SPAN_NAME = "director.generate"

DEFAULT_OTLP_ENDPOINT = "http://localhost:4318"

#: Seconds. Covers a fast offline rules answer (ms) up to the 300 s timeout cap.
LATENCY_BUCKET_BOUNDARIES = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)

#: The complete set of metric attribute keys (``token_type`` only on tokens).
METRIC_DIMENSIONS = frozenset(
    {"provider", "model", "status", "error_code", "execution_mode", "token_type"}
)

UNKNOWN = "unknown"
NONE = "none"

# Same shape the registry enforces for model ids; anything else is not a label.
_LABEL_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.:/@-]{0,127}$")
# Same shape as the contract's BoundedId.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TIMEOUT_ORIGINS = frozenset({"director_deadline", "provider"})
_SELECTION_REASONS = frozenset(reason.value for reason in SelectionReason)
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})

_SCHEMA_CODES = frozenset(
    {
        ErrorKind.SCHEMA_VIOLATION,
        ErrorKind.INVALID_JSON,
        ErrorKind.UNSUPPORTED_CONTRACT_VERSION,
        ErrorKind.EMPTY_RESPONSE,
    }
)

_P = ParamSpec("_P")
_R = TypeVar("_R")


class Outcome(StrEnum):
    """The ``status`` dimension: a small closed set, never free text."""

    SUCCESS = "success"
    SELECTION_ERROR = "selection_error"
    PROVIDER_ERROR = "provider_error"
    TIMEOUT = "timeout"
    SCHEMA_ERROR = "schema_error"
    INVALID_REQUEST = "invalid_request"
    CANCELLED = "cancelled"
    INTERNAL_ERROR = "internal_error"


def _never_raises(fn: Callable[_P, _R]) -> Callable[_P, _R | None]:
    """Swallow ``Exception`` from a telemetry step; log only its type.

    Exception text is never logged: exporter and SDK errors can embed endpoint
    URLs or collector response bodies. ``BaseException`` (cancellation,
    interrupts) still propagates.
    """

    @functools.wraps(fn)
    def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R | None:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            logger.warning("telemetry step %s failed (%s)", fn.__name__, type(exc).__name__)
            return None

    return guarded


def _label(value: object) -> str:
    return value if isinstance(value, str) and _LABEL_RE.match(value) else UNKNOWN


def _safe_id(value: object) -> str | None:
    return value if isinstance(value, str) and _ID_RE.match(value) else None


def classify(response: GenerationResponse, *, selection_failed: bool = False) -> Outcome:
    """Map a canonical response onto the bounded ``status`` dimension."""
    if response.success:
        return Outcome.SUCCESS
    if selection_failed:
        return Outcome.SELECTION_ERROR
    error = response.metadata.error
    code = error.code if error is not None else None
    if code is ErrorKind.PROVIDER_TIMEOUT:
        return Outcome.TIMEOUT
    if code in _SCHEMA_CODES:
        return Outcome.SCHEMA_ERROR
    return Outcome.PROVIDER_ERROR


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    """Telemetry configuration read from the environment.

    ``OTEL_EXPORTER_OTLP_ENDPOINT``  OTLP/HTTP base URL; setting it enables export
    ``DIRECTOR_OTEL_ENABLED``        ``1/true/yes/on`` forces export on (default
                                     endpoint ``http://localhost:4318``);
                                     ``0/false/no/off`` forces it off; unset follows
                                     the endpoint; anything else is treated as off
    ``OTEL_SERVICE_NAME``            resource ``service.name`` (default ``dungeon-director``)
    """

    enabled: bool = False
    endpoint: str | None = None
    service_name: str = "dungeon-director"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> TelemetrySettings:
        env = os.environ if environ is None else environ
        endpoint = (env.get("OTEL_EXPORTER_OTLP_ENDPOINT") or "").strip() or None
        service_name = (env.get("OTEL_SERVICE_NAME") or "").strip() or "dungeon-director"

        flag = (env.get("DIRECTOR_OTEL_ENABLED") or "").strip().lower()
        if flag:
            enabled = flag in _TRUE
        else:
            enabled = endpoint is not None
        if enabled and endpoint is None:
            endpoint = DEFAULT_OTLP_ENDPOINT
        return cls(enabled=enabled, endpoint=endpoint, service_name=service_name)


class DirectorTelemetry:
    """Owns the tracer, the meter and the instruments; hands out observations.

    Without providers (the default) everything is a no-op. All public methods
    are safe to call from the request path: they never raise ``Exception``.
    """

    def __init__(
        self,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        *,
        enabled: bool = True,
    ) -> None:
        if not enabled:
            tracer_provider = meter_provider = None
        self.enabled = enabled and (tracer_provider is not None or meter_provider is not None)
        self.tracer_provider: TracerProvider = tracer_provider or NoOpTracerProvider()
        self.meter_provider: MeterProvider = meter_provider or NoOpMeterProvider()
        self._closed = False
        try:
            self._create_instruments(self.meter_provider)
        except Exception as exc:
            logger.warning(
                "telemetry instrument creation failed (%s); metrics disabled", type(exc).__name__
            )
            self._create_instruments(NoOpMeterProvider())
        try:
            self._tracer = self.tracer_provider.get_tracer(TRACER_NAME)
        except Exception as exc:
            logger.warning(
                "telemetry tracer creation failed (%s); tracing disabled", type(exc).__name__
            )
            self._tracer = NoOpTracerProvider().get_tracer(TRACER_NAME)

    def _create_instruments(self, meter_provider: MeterProvider) -> None:
        meter = meter_provider.get_meter(METER_NAME)
        self._requests = meter.create_counter(
            "director.generation.requests",
            unit="1",
            description="Generation requests by provider, model, status and execution mode",
        )
        self._e2e_duration = meter.create_histogram(
            "director.generation.duration",
            unit="s",
            description="End-to-end latency of a generation decision",
            explicit_bucket_boundaries_advisory=list(LATENCY_BUCKET_BOUNDARIES),
        )
        self._provider_duration = meter.create_histogram(
            "director.provider.duration",
            unit="s",
            description="Latency of the provider call alone",
            explicit_bucket_boundaries_advisory=list(LATENCY_BUCKET_BOUNDARIES),
        )
        self._tokens = meter.create_counter(
            "director.generation.tokens",
            unit="1",
            description="Tokens reported by providers (only when reported)",
        )
        self._cost = meter.create_counter(
            "director.generation.cost",
            unit="USD",
            description="Estimated cost in USD (only when the provider reported one)",
        )

    # -- observations -------------------------------------------------------

    def begin(
        self, request: GenerationRequest, *, is_shadow: bool = False
    ) -> GenerationObservation:
        """Start the ``director.generate`` span for one generation."""
        mode = "shadow" if is_shadow else "active"
        span = self._start_span(
            {
                "director.request_id": request.request_id,
                "director.run_id": request.run_id,
                "director.depth": request.state.depth,
                "director.is_shadow": is_shadow,
                "director.execution_mode": mode,
                "director.retry_count": 0,
            }
        )
        return GenerationObservation(self, span, execution_mode=mode)

    def record_invalid_request(self, *, request_id: object = None, run_id: object = None) -> None:
        """Record a ``/v1/generate`` request rejected by FastAPI (HTTP 422).

        ``request_id``/``run_id`` come from the unvalidated body, so they are
        only attached when they look like valid IDs. There is no end-to-end
        latency sample: nothing was generated, and a ~0 s point would skew the
        percentiles. The span's own duration is still exported.
        """
        attributes: dict[str, Any] = {
            "director.is_shadow": False,
            "director.execution_mode": "active",
            "director.retry_count": 0,
            "director.status": Outcome.INVALID_REQUEST.value,
            "director.http_status": 422,
            "director.error_code": "invalid_request",
            "director.schema_valid": False,
        }
        if (safe := _safe_id(request_id)) is not None:
            attributes["director.request_id"] = safe
        if (safe := _safe_id(run_id)) is not None:
            attributes["director.run_id"] = safe
        span = self._start_span(attributes)
        self._set_status(span, Outcome.INVALID_REQUEST, "invalid_request")
        self._end_span(span)
        self._count(
            provider=NONE,
            model=NONE,
            outcome=Outcome.INVALID_REQUEST,
            error_code="invalid_request",
            execution_mode="active",
        )

    # -- lifecycle ----------------------------------------------------------

    def flush(self, timeout_millis: int = 5000) -> None:
        """Push buffered spans and metrics to the exporters (best effort)."""
        self._flush(timeout_millis)

    def shutdown(self) -> None:
        """Flush and stop the exporters once; later calls do nothing.

        May block for as long as the exporters' own timeouts when the collector
        is unreachable; async callers should run it in a worker thread.
        """
        if self._closed:
            return
        self._closed = True
        self._shutdown_provider(self.tracer_provider)
        self._shutdown_provider(self.meter_provider)

    @_never_raises
    def _flush(self, timeout_millis: int) -> None:
        for provider in (self.tracer_provider, self.meter_provider):
            force_flush = getattr(provider, "force_flush", None)
            if callable(force_flush):
                try:
                    force_flush(timeout_millis)
                except Exception as exc:
                    logger.warning("telemetry flush failed (%s)", type(exc).__name__)

    @staticmethod
    def _shutdown_provider(provider: object) -> None:
        stop = getattr(provider, "shutdown", None)
        if not callable(stop):
            return
        try:
            stop()
        except Exception as exc:
            logger.warning("telemetry shutdown failed (%s)", type(exc).__name__)

    # -- guarded primitives (used by observations) -------------------------

    def _start_span(self, attributes: Mapping[str, Any]) -> Span:
        try:
            return self._tracer.start_span(SPAN_NAME, kind=SpanKind.SERVER, attributes=attributes)
        except Exception as exc:
            logger.warning("telemetry span start failed (%s)", type(exc).__name__)
            return INVALID_SPAN

    @_never_raises
    def _set_attributes(self, span: Span, attributes: Mapping[str, Any]) -> None:
        span.set_attributes(attributes)

    @_never_raises
    def _set_status(self, span: Span, outcome: Outcome, error_code: str) -> None:
        # Only the fixed error code, never a message: messages can echo input.
        if outcome is Outcome.SUCCESS:
            span.set_status(Status(StatusCode.OK))
        elif outcome is not Outcome.CANCELLED:
            span.set_status(Status(StatusCode.ERROR, description=error_code))

    @_never_raises
    def _end_span(self, span: Span) -> None:
        span.end()

    @_never_raises
    def _count(
        self,
        *,
        provider: str,
        model: str,
        outcome: Outcome,
        error_code: str,
        execution_mode: str,
    ) -> None:
        self._requests.add(1, _dimensions(provider, model, outcome, error_code, execution_mode))

    @_never_raises
    def _record_e2e(self, dimensions: Mapping[str, str], seconds: float) -> None:
        self._e2e_duration.record(seconds, dimensions)

    @_never_raises
    def _record_provider_latency(self, dimensions: Mapping[str, str], seconds: float) -> None:
        self._provider_duration.record(seconds, dimensions)

    @_never_raises
    def _record_tokens(self, dimensions: Mapping[str, str], usage: UsageStats) -> None:
        if usage.input_tokens is not None:
            self._tokens.add(usage.input_tokens, {**dimensions, "token_type": "input"})
        if usage.output_tokens is not None:
            self._tokens.add(usage.output_tokens, {**dimensions, "token_type": "output"})

    @_never_raises
    def _record_cost(self, dimensions: Mapping[str, str], usd: float) -> None:
        self._cost.add(usd, dimensions)


def _dimensions(
    provider: str, model: str, outcome: Outcome, error_code: str, execution_mode: str
) -> dict[str, str]:
    return {
        "provider": _label(provider) if provider != NONE else NONE,
        "model": _label(model) if model != NONE else NONE,
        "status": outcome.value,
        "error_code": error_code,
        "execution_mode": execution_mode,
    }


class GenerationObservation:
    """One in-flight generation. Exactly one terminal call ends the span.

    ``finish``, ``cancel`` and ``abort`` are terminal and idempotent (the
    first wins); ``end`` is the safety net that closes a span no terminal call
    reached. None of them raises ``Exception``.
    """

    def __init__(self, telemetry: DirectorTelemetry, span: Span, *, execution_mode: str) -> None:
        self._telemetry = telemetry
        self._span = span
        self._execution_mode = execution_mode
        self._started = time.perf_counter()
        self._done = False

    def finish(
        self,
        response: GenerationResponse,
        *,
        http_status: int,
        provider: str,
        model: str,
        provider_latency_s: float | None = None,
        selection_failed: bool = False,
    ) -> None:
        """Record the canonical outcome the service is about to return."""
        error = response.metadata.error
        error_code = error.code.value if error is not None else NONE
        outcome = classify(response, selection_failed=selection_failed)
        extra = self._response_attributes(response, outcome)
        self._complete(
            outcome,
            error_code,
            provider=provider,
            model=model,
            http_status=http_status,
            provider_latency_s=provider_latency_s,
            extra=extra,
            response=response,
        )

    def cancel(self, *, provider: str, model: str, provider_latency_s: float | None = None) -> None:
        """The request task was cancelled (client disconnect, server shutdown)."""
        self._complete(
            Outcome.CANCELLED,
            NONE,
            provider=provider,
            model=model,
            provider_latency_s=provider_latency_s,
        )

    def abort(self, *, provider: str, model: str, provider_latency_s: float | None = None) -> None:
        """An unexpected exception is escaping the service."""
        self._complete(
            Outcome.INTERNAL_ERROR,
            ErrorKind.INTERNAL_ERROR.value,
            provider=provider,
            model=model,
            provider_latency_s=provider_latency_s,
        )

    def end(self) -> None:
        """Close the span if no terminal call did."""
        if not self._done:
            self._done = True
            self._telemetry._end_span(self._span)

    # ----------------------------------------------------------------------

    @_never_raises
    def _response_attributes(
        self, response: GenerationResponse, outcome: Outcome
    ) -> dict[str, Any]:
        attributes: dict[str, Any] = {}
        error = response.metadata.error
        metadata = response.metadata.provider_metadata
        if isinstance(metadata, dict):
            if metadata.get("timeout_origin") in _TIMEOUT_ORIGINS:
                attributes["director.timeout_origin"] = metadata["timeout_origin"]
            if metadata.get("selection_error") in _SELECTION_REASONS:
                attributes["director.selection_error"] = metadata["selection_error"]
        if outcome is Outcome.SUCCESS:
            attributes["director.schema_valid"] = True
        elif outcome is Outcome.SCHEMA_ERROR:
            attributes["director.schema_valid"] = False
        if error is None and response.room is not None:
            room = response.room
            attributes.update(
                {
                    "director.room.type": room.room_type.value,
                    "director.room.size": room.size.value,
                    "director.room.danger": room.danger,
                    "director.room.exit_count": len(room.exits),
                    "director.room.has_secret": bool(room.has_secret),
                    "director.room.secret_probability": room.secret_probability,
                    "director.room.enemy_density": room.enemy_density,
                    "director.room.loot_density": room.loot_density,
                }
            )
        usage = response.metadata.usage
        if usage is not None:
            if usage.input_tokens is not None:
                attributes["gen_ai.usage.input_tokens"] = usage.input_tokens
            if usage.output_tokens is not None:
                attributes["gen_ai.usage.output_tokens"] = usage.output_tokens
            if usage.input_tokens is not None and usage.output_tokens is not None:
                attributes["gen_ai.usage.total_tokens"] = usage.input_tokens + usage.output_tokens
            if usage.estimated_cost_usd is not None:
                attributes["gen_ai.usage.cost"] = usage.estimated_cost_usd
        return attributes

    def _complete(
        self,
        outcome: Outcome,
        error_code: str,
        *,
        provider: str,
        model: str,
        http_status: int | None = None,
        provider_latency_s: float | None = None,
        extra: Mapping[str, Any] | None = None,
        response: GenerationResponse | None = None,
    ) -> None:
        if self._done:
            return
        self._done = True
        telemetry = self._telemetry
        e2e_s = time.perf_counter() - self._started
        dimensions = _dimensions(provider, model, outcome, error_code, self._execution_mode)

        attributes: dict[str, Any] = {
            "director.status": outcome.value,
            "director.provider": dimensions["provider"],
            "director.model": dimensions["model"],
            "director.latency_ms": e2e_s * 1000.0,
            "gen_ai.system": dimensions["provider"],
            "gen_ai.request.model": dimensions["model"],
            "gen_ai.response.model": dimensions["model"],
        }
        if outcome is not Outcome.SUCCESS and error_code != NONE:
            attributes["director.error_code"] = error_code
        if http_status is not None:
            attributes["director.http_status"] = http_status
        if provider_latency_s is not None:
            attributes["director.provider_latency_ms"] = provider_latency_s * 1000.0
        if extra:
            attributes.update(extra)

        telemetry._set_attributes(self._span, attributes)
        telemetry._set_status(self._span, outcome, error_code)
        telemetry._end_span(self._span)

        telemetry._count(
            provider=provider,
            model=model,
            outcome=outcome,
            error_code=error_code,
            execution_mode=self._execution_mode,
        )
        telemetry._record_e2e(dimensions, e2e_s)
        if provider_latency_s is not None:
            telemetry._record_provider_latency(dimensions, provider_latency_s)
        usage = response.metadata.usage if response is not None else None
        if usage is not None:
            telemetry._record_tokens(dimensions, usage)
            if usage.estimated_cost_usd is not None:
                telemetry._record_cost(dimensions, usage.estimated_cost_usd)


def setup_telemetry(settings: TelemetrySettings) -> DirectorTelemetry:
    """Build the exporting telemetry, or a no-op one; never raises.

    Disabled settings, an unusable endpoint, or any SDK/exporter construction
    failure all yield no-op telemetry, so a bad observability config cannot
    stop the director from serving. The exporters are lazy: an unreachable
    collector shows up later as background export warnings, not here.
    """
    if not settings.enabled or not settings.endpoint:
        return DirectorTelemetry(enabled=False)

    parts = urlsplit(settings.endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        # The value itself is not logged: a URL can embed credentials.
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT is not an http(s) URL; telemetry disabled")
        return DirectorTelemetry(enabled=False)

    try:
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": settings.service_name})
        base = settings.endpoint.rstrip("/")

        tracer_provider = SdkTracerProvider(resource=resource)
        tracer_provider.add_span_processor(
            BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{base}/v1/traces"))
        )
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=f"{base}/v1/metrics"), export_interval_millis=5000
        )
        meter_provider = SdkMeterProvider(resource=resource, metric_readers=[reader])
        return DirectorTelemetry(tracer_provider, meter_provider)
    except Exception as exc:
        logger.warning("OpenTelemetry setup failed (%s); telemetry disabled", type(exc).__name__)
        return DirectorTelemetry(enabled=False)
