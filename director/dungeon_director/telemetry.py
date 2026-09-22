"""OpenTelemetry traces, metrics and logs for director generation decisions.

Every ``POST /v1/generate`` produces exactly one ``director.generate`` span and
one ``director.generation.requests`` count, whether it ends in a room, a
provider failure, a timeout, a schema failure, a cancellation, or a FastAPI 422
before the service is reached.

Design rules, all covered by ``tests/test_telemetry.py`` and
``tests/test_remote_telemetry.py``:

* **Telemetry never changes generation.** Every OpenTelemetry call (instrument
  creation, span start, attribute writes, metric recording, log emission,
  ``span.end``, flush, shutdown) runs behind a guard that logs the exception
  *type* and carries on. The canonical response and HTTP status are identical
  whether telemetry is disabled, healthy, or broken.
* **Metric dimensions are bounded.** Only ``provider``, ``model``, ``status``,
  ``error_code`` and ``execution_mode`` (plus ``token_type`` on the token
  counter) are ever used. Provider and model come from the registry, never
  from the request, so a client cannot mint new time series. Request and run
  IDs are span attributes only.
* **Nothing sensitive is recorded.** No request or response bodies, prompt
  hints, exception text, adapter error messages, headers or credentials: spans
  and logs carry the machine-readable error *code*, never the message, and
  OTLP header values are never logged, echoed by ``describe()`` or returned by
  ``/v1/config`` or ``/health``.
* **Tokens and cost only when the provider reported them.**
* **Active vs shadow** is the ``execution_mode`` dimension (and the
  ``director.is_shadow`` span attribute).

Remote export (issue #23) follows the standard OpenTelemetry environment
variables: a base ``OTEL_EXPORTER_OTLP_ENDPOINT`` plus optional signal-specific
``OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_ENDPOINT`` full URLs (which win over
the base URL), optional ``OTEL_EXPORTER_OTLP_HEADERS`` (and per-signal
variants), ``OTEL_SERVICE_NAME``, export interval/timeouts, and
``OTEL_RESOURCE_ATTRIBUTES``. Traces, metrics and logs can be enabled
independently via ``DIRECTOR_OTEL_{TRACES,METRICS,LOGS}_ENABLED``. See
:class:`TelemetrySettings`, :func:`setup_telemetry` and
``docs/remote-telemetry.md``.

Resource identity follows the #22 telemetry contract: ``service.name`` (default
``dungeon-director``; the game-telemetry bridge uses
``dungeon-director-game-bridge`` and benchmark/replay tooling
``dungeon-director-benchmark``), ``service.namespace=dungeon-director`` to
group them as one system, ``service.version`` pinned to the shared
``CONTRACT_VERSION`` from ``contracts.py`` (so dashboards can detect
incompatible telemetry), and ``deployment.environment``. Correlation IDs
(``run_id``, ``request_id`` and friends) live on traces and logs only — never
as metric dimensions.
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

from opentelemetry._logs import NoOpLoggerProvider, SeverityNumber
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
    "DEFAULT_ENVIRONMENT",
    "DEFAULT_EXPORT_TIMEOUT_MILLIS",
    "DEFAULT_METRIC_EXPORT_INTERVAL_MILLIS",
    "DEFAULT_OTLP_ENDPOINT",
    "DEFAULT_SERVICE_NAMESPACE",
    "ExportHealth",
    "LATENCY_BUCKET_BOUNDARIES",
    "LOG_ATTRIBUTE_MAX_CHARS",
    "LOG_ATTRIBUTE_MAX_ENTRIES",
    "LOG_BODY_MAX_CHARS",
    "METRIC_DIMENSIONS",
    "SIGNAL_NAMES",
    "DirectorTelemetry",
    "GenerationObservation",
    "Outcome",
    "TelemetrySettings",
    "setup_telemetry",
]

logger = logging.getLogger(__name__)

TRACER_NAME = "dungeon-director"
METER_NAME = "dungeon-director"
#: Logger *scope* name for OTLP log records (matches TRACER_NAME/METER_NAME).
LOGGER_NAME = "dungeon-director"
SPAN_NAME = "director.generate"

DEFAULT_OTLP_ENDPOINT = "http://localhost:4318"
DEFAULT_SERVICE_NAMESPACE = "dungeon-director"
DEFAULT_ENVIRONMENT = "dev"

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
#: ``telemetry_schema.py`` (#22) imports this read-only to keep the dimension
#: allowlists reconciled; correlation IDs are never metric dimensions.
METRIC_DIMENSIONS = frozenset(
    {"provider", "model", "status", "error_code", "execution_mode", "token_type"}
)

UNKNOWN = "unknown"
NONE = "none"

SIGNAL_NAMES = ("traces", "metrics", "logs")

#: OTLP/HTTP path appended to a base endpoint per signal (full URLs configured
#: via the signal-specific env vars are used as-is).
SIGNAL_PATHS = {
    "traces": "/v1/traces",
    "metrics": "/v1/metrics",
    "logs": "/v1/logs",
}

SIGNAL_ENDPOINT_VARS = {
    "traces": "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
    "metrics": "OTEL_EXPORTER_OTLP_METRICS_ENDPOINT",
    "logs": "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
}

SIGNAL_ENABLED_VARS = {
    "traces": "DIRECTOR_OTEL_TRACES_ENABLED",
    "metrics": "DIRECTOR_OTEL_METRICS_ENABLED",
    "logs": "DIRECTOR_OTEL_LOGS_ENABLED",
}

SIGNAL_HEADER_VARS = {
    "traces": "OTEL_EXPORTER_OTLP_TRACES_HEADERS",
    "metrics": "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
    "logs": "OTEL_EXPORTER_OTLP_LOGS_HEADERS",
}

SIGNAL_TIMEOUT_VARS = {
    "traces": "OTEL_EXPORTER_OTLP_TRACES_TIMEOUT",
    "metrics": "OTEL_EXPORTER_OTLP_METRICS_TIMEOUT",
    "logs": "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT",
}

DEFAULT_METRIC_EXPORT_INTERVAL_MILLIS = 5000
MIN_METRIC_EXPORT_INTERVAL_MILLIS = 1000
MAX_METRIC_EXPORT_INTERVAL_MILLIS = 60000

#: Per-request OTLP exporter timeout (``OTEL_EXPORTER_OTLP_TIMEOUT``, ms).
#: Clamped so flush/shutdown stay bounded even with an unreachable collector.
DEFAULT_EXPORT_TIMEOUT_MILLIS = 10_000
MIN_EXPORT_TIMEOUT_MILLIS = 500
MAX_EXPORT_TIMEOUT_MILLIS = 30_000

#: Log hygiene bounds for :meth:`DirectorTelemetry.emit_log`: bodies and
#: attribute values are truncated, exception text never reaches log records.
LOG_BODY_MAX_CHARS = 4096
LOG_ATTRIBUTE_MAX_CHARS = 1024
LOG_ATTRIBUTE_MAX_ENTRIES = 32

# Same shape the registry enforces for model ids; anything else is not a label.
_LABEL_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9_.:/@-]{0,127}$")
# Same shape as the contract's BoundedId.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
# Resource-attribute / header keys: bounded, no whitespace.
_KEY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,127}$")
# Service versions like 1.0.0, 1.2.3-rc.1+build.
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,63}$")
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

_SEVERITIES = {
    "DEBUG": SeverityNumber.DEBUG,
    "INFO": SeverityNumber.INFO,
    "WARN": SeverityNumber.WARN,
    "WARNING": SeverityNumber.WARN,
    "ERROR": SeverityNumber.ERROR,
}

# Anything that looks like a URL in SDK diagnostics is reduced to its origin:
# endpoints may embed credentials in the userinfo part.
_URL_RE = re.compile(r"https?://[^\s'\"<>]+")

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


# --------------------------------------------------------------------------- #
# Configuration parsing helpers
# --------------------------------------------------------------------------- #


def _read(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _parse_flag(value: str | None) -> bool | None:
    """``1/true/yes/on`` -> True, ``0/false/no/off`` -> False, unset -> None.

    Anything else counts as false: telemetry is opt-in, so a typo must not
    silently turn export on. The bad text is never logged.
    """
    if value is None:
        return None
    flag = value.strip().lower()
    if not flag:
        return None
    return flag in _TRUE


def _parse_kv_pairs(
    raw: str | None, *, max_value_chars: int
) -> tuple[tuple[tuple[str, str], ...], int]:
    """Parse ``k=v,k=v`` (OTel headers / OTEL_RESOURCE_ATTRIBUTES style).

    Entries with an empty/oversized value, a malformed key, or a missing ``=``
    are counted and dropped — never logged, because a pasted credential must
    not be echoed back.
    """
    if raw is None:
        return (), 0
    pairs: list[tuple[str, str]] = []
    rejected = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue  # tolerate stray commas
        key, sep, value = entry.partition("=")
        key, value = key.strip(), value.strip()
        if sep and _KEY_RE.match(key) is not None and 0 < len(value) <= max_value_chars:
            pairs.append((key, value))
        else:
            rejected += 1
    return tuple(pairs), rejected


def _lenient_millis(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    """Milliseconds from the environment; out-of-range values use ``default``.

    So the effective value is always inside ``[low, high]`` (bounded flush and
    shutdown). Malformed values fall back to ``default`` with a warning that
    names the variable but never the value.
    """
    raw = _read(env, name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = -1
    if not low <= value <= high:
        logger.warning(
            "%s must be an integer from %d to %d (milliseconds); using the default",
            name,
            low,
            high,
        )
        return default
    return value


def _validated_endpoint(url: str) -> str | None:
    """An http(s) URL with a host, or ``None``. Userinfo is not rejected here:
    exporter construction and the guarded fallback own that failure mode."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    return url


def _origin(url: str) -> str:
    """``scheme://host[:port]`` — safe to show: no path, query or userinfo."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        if not host:
            return "invalid"
        netloc = f"{host}:{parts.port}" if parts.port else host
        return f"{parts.scheme}://{netloc}"
    except ValueError:
        return "invalid"


def _bounded(value: str | None, default: str, pattern: re.Pattern[str], name: str) -> str:
    if value is None:
        return default
    if pattern.match(value) is None:
        logger.warning("%s is not a valid value; using %s instead", name, default)
        return default
    return value


def _service_version_from_contracts() -> str:
    """Pin ``service.version`` to the shared contract version (#22 rule)."""
    try:
        from dungeon_director.contracts import CONTRACT_VERSION  # noqa: PLC0415

        return CONTRACT_VERSION
    except Exception:
        return "unknown"


# --------------------------------------------------------------------------- #
# Export health (bounded, content-free diagnostics)
# --------------------------------------------------------------------------- #


class ExportHealth:
    """Thread-safe, per-signal export state for diagnostics.

    Values are the fixed words ``pending`` / ``ok`` / ``failing`` — never
    endpoint text, header values, or exception content. ``/health`` and
    :meth:`DirectorTelemetry.describe` expose the snapshot.
    """

    PENDING = "pending"
    OK = "ok"
    FAILING = "failing"

    def __init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._states: dict[str, str] = {}

    def record(self, signal: str, *, ok: bool) -> None:
        state = self.OK if ok else self.FAILING
        with self._lock:
            self._states[signal] = state

    def snapshot(self) -> dict[str, str]:
        with self._lock:
            return dict(self._states)


def _export_succeeded(result: Any) -> bool:
    """True when an exporter result means success.

    Compared by member *name*, not identity: the SDK's logs API has two
    parallel result enums (``LogExportResult`` / ``LogRecordExportResult``),
    and the OTLP log exporter returns the one our type hints don't use.
    """
    return getattr(result, "name", None) == "SUCCESS"


def _delegate_to_inner(wrapper: Any, name: str) -> Any:
    """Forward unknown attributes to the wrapped exporter.

    SDK internals reach for exporter-private attributes (e.g.
    ``_preferred_temporality`` on metric exporters read by
    ``PeriodicExportingMetricReader``); delegation keeps the wrapper
    transparent. Never recurses into an unset ``_inner``.
    """
    if name.startswith("__") or name in ("_inner", "_health", "_signal"):
        raise AttributeError(name)
    inner = wrapper.__dict__.get("_inner")
    if inner is None:
        raise AttributeError(name)
    return getattr(inner, name)


class _TrackedSpanExporter:
    """Delegate to a span exporter, recording success/failure per batch."""

    def __init__(self, inner: Any, health: ExportHealth, signal: str) -> None:
        self._inner = inner
        self._health = health
        self._signal = signal

    def __getattr__(self, name: str) -> Any:
        return _delegate_to_inner(self, name)

    def export(self, spans: Any, **kwargs: Any) -> Any:
        from opentelemetry.sdk.trace.export import SpanExportResult

        try:
            result = self._inner.export(spans, **kwargs)
        except Exception:
            self._health.record(self._signal, ok=False)
            return SpanExportResult.FAILURE
        self._health.record(self._signal, ok=_export_succeeded(result))
        return result

    def shutdown(self, **kwargs: Any) -> Any:
        return self._inner.shutdown(**kwargs)

    def force_flush(self, timeout_millis: int | None = None, **kwargs: Any) -> Any:
        flush = getattr(self._inner, "force_flush", None)
        if not callable(flush):
            return None
        return flush(timeout_millis, **kwargs)


class _TrackedMetricExporter:
    """Delegate to a metric exporter, recording success/failure per export."""

    def __init__(self, inner: Any, health: ExportHealth, signal: str) -> None:
        self._inner = inner
        self._health = health
        self._signal = signal

    def __getattr__(self, name: str) -> Any:
        return _delegate_to_inner(self, name)

    def export(self, metrics_data: Any, timeout_millis: float | None = None, **kwargs: Any) -> Any:
        from opentelemetry.sdk.metrics.export import MetricExportResult

        try:
            result = self._inner.export(metrics_data, timeout_millis=timeout_millis, **kwargs)
        except Exception:
            self._health.record(self._signal, ok=False)
            return MetricExportResult.FAILURE
        self._health.record(self._signal, ok=_export_succeeded(result))
        return result

    def shutdown(self, timeout_millis: float | None = None, **kwargs: Any) -> Any:
        return self._inner.shutdown(timeout_millis=timeout_millis, **kwargs)

    def force_flush(self, timeout_millis: float | None = None, **kwargs: Any) -> Any:
        flush = getattr(self._inner, "force_flush", None)
        if not callable(flush):
            return None
        return flush(timeout_millis=timeout_millis, **kwargs)


class _TrackedLogExporter:
    """Delegate to a log exporter, recording success/failure per batch."""

    def __init__(self, inner: Any, health: ExportHealth, signal: str) -> None:
        self._inner = inner
        self._health = health
        self._signal = signal

    def __getattr__(self, name: str) -> Any:
        return _delegate_to_inner(self, name)

    def export(self, batch: Any, **kwargs: Any) -> Any:
        from opentelemetry.sdk._logs.export import LogExportResult

        try:
            result = self._inner.export(batch, **kwargs)
        except Exception:
            self._health.record(self._signal, ok=False)
            return LogExportResult.FAILURE
        self._health.record(self._signal, ok=_export_succeeded(result))
        return result

    def shutdown(self, **kwargs: Any) -> Any:
        return self._inner.shutdown(**kwargs)

    def force_flush(self, timeout_millis: int | None = None, **kwargs: Any) -> Any:
        flush = getattr(self._inner, "force_flush", None)
        if not callable(flush):
            return None
        return flush(timeout_millis, **kwargs)


def _redact_sdk_log_text(text: str) -> str:
    """Reduce any URL in SDK exporter diagnostics to its origin and cap length."""
    redacted = _URL_RE.sub(lambda m: _origin(m.group(0)), text)
    return redacted[:300]


class _BoundedExporterLogFilter(logging.Filter):
    """Keep OTLP SDK exporter logs bounded and credential-free.

    The SDK logs retry/failure details that can embed endpoint URLs (possibly
    with userinfo credentials) and exception text. The filter drops exception
    details, redacts URLs to origins and caps the message length; the state of
    the export itself is already visible via :class:`ExportHealth`.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        try:
            message = record.getMessage()
        except Exception:
            message = record.name
        record.msg = _redact_sdk_log_text(message)
        record.args = None
        return True


_OTLP_EXPORTER_LOG_FILTER = _BoundedExporterLogFilter()
_QUIETED_SDK_LOGGERS = False

#: The loggers the OTLP/HTTP exporter modules themselves log through. Filters
#: on an ancestor do *not* apply to records logged by a child logger, so the
#: filter must be attached where the records originate.
_OTLP_EXPORTER_LOGGER_NAMES = (
    "opentelemetry.exporter.otlp",
    "opentelemetry.exporter.otlp.proto.http",
    "opentelemetry.exporter.otlp.proto.http.trace_exporter",
    "opentelemetry.exporter.otlp.proto.http.metric_exporter",
    "opentelemetry.exporter.otlp.proto.http._log_exporter",
    "opentelemetry.exporter.otlp.proto.http._common",
)


def _quiet_otlp_exporter_logging() -> None:
    """Attach the bounded-diagnostics filter once, guarded."""
    global _QUIETED_SDK_LOGGERS
    if _QUIETED_SDK_LOGGERS:
        return
    _QUIETED_SDK_LOGGERS = True
    for name in _OTLP_EXPORTER_LOGGER_NAMES:
        try:
            logging.getLogger(name).addFilter(_OTLP_EXPORTER_LOG_FILTER)
        except Exception as exc:
            logger.warning("could not bound OTLP SDK log output (%s)", type(exc).__name__)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class TelemetrySettings:
    """Telemetry configuration read from the environment.

    Master switch:

    ``OTEL_EXPORTER_OTLP_ENDPOINT``      OTLP/HTTP base URL; setting it enables export
    ``DIRECTOR_OTEL_ENABLED``            ``1/true/yes/on`` forces export on (default
                                         endpoint ``http://localhost:4318``);
                                         ``0/false/no/off`` forces it off; unset follows
                                         the endpoint; anything else is treated as off

    Identity (resource attributes, see the #22 contract):

    ``OTEL_SERVICE_NAME``                ``service.name`` (default ``dungeon-director``;
                                         the game bridge uses
                                         ``dungeon-director-game-bridge``, benchmark/
                                         replay tooling ``dungeon-director-benchmark``)
    ``DIRECTOR_OTEL_NAMESPACE``          ``service.namespace`` (default
                                         ``dungeon-director``; groups the components)
    ``DIRECTOR_OTEL_SERVICE_VERSION``    ``service.version`` override (default: the
                                         ``CONTRACT_VERSION`` from ``contracts.py``)
    ``DIRECTOR_OTEL_ENVIRONMENT``        ``deployment.environment`` (default ``dev``)
    ``OTEL_RESOURCE_ATTRIBUTES``         extra ``k=v,k=v`` resource attributes

    Endpoints and transport (per signal ``traces``/``metrics``/``logs``):

    ``OTEL_EXPORTER_OTLP_{SIGNAL}_ENDPOINT``  full signal URL; wins over base + path
    ``DIRECTOR_OTEL_{SIGNAL}_ENABLED``        enable just that signal (``1/true/...``
                                              on, ``0/false/...`` or junk off, unset
                                              follows the master switch)
    ``OTEL_EXPORTER_OTLP_HEADERS``            shared ``k=v,k=v`` request headers
    ``OTEL_EXPORTER_OTLP_{SIGNAL}_HEADERS``   per-signal headers (merged over shared)
    ``OTEL_EXPORTER_OTLP_TIMEOUT``            per-request timeout, ms (default 10000;
                                              out-of-range values use the default, so
                                              the effective value is always 500-30000)
    ``OTEL_EXPORTER_OTLP_{SIGNAL}_TIMEOUT``   per-signal timeout override
    ``OTEL_EXPORTER_OTLP_METRIC_EXPORT_INTERVAL``  metric export interval, ms
                                              (default 5000, effectively 1000-60000)

    Header *values* are never logged, echoed by ``describe()``/``/v1/config``/
    ``/health``, or included in diagnostics; malformed entries are counted in
    ``rejected_header_entries`` and dropped silently.
    """

    enabled: bool = False
    endpoint: str | None = None
    service_name: str = "dungeon-director"
    service_namespace: str = DEFAULT_SERVICE_NAMESPACE
    service_version: str | None = None
    environment: str = DEFAULT_ENVIRONMENT
    resource_attributes: tuple[tuple[str, str], ...] = ()
    traces_enabled: bool = True
    metrics_enabled: bool = True
    logs_enabled: bool = True
    traces_endpoint: str | None = None
    metrics_endpoint: str | None = None
    logs_endpoint: str | None = None
    headers: tuple[tuple[str, str], ...] = ()
    traces_headers: tuple[tuple[str, str], ...] = ()
    metrics_headers: tuple[tuple[str, str], ...] = ()
    logs_headers: tuple[tuple[str, str], ...] = ()
    rejected_header_entries: int = 0
    rejected_resource_entries: int = 0
    export_timeout_seconds: float = DEFAULT_EXPORT_TIMEOUT_MILLIS / 1000.0
    traces_timeout_seconds: float | None = None
    metrics_timeout_seconds: float | None = None
    logs_timeout_seconds: float | None = None
    metric_export_interval_seconds: float = DEFAULT_METRIC_EXPORT_INTERVAL_MILLIS / 1000.0

    def signal_endpoint(self, signal: str) -> str | None:
        """The configured full URL for a signal, or ``None`` to use base + path."""
        return getattr(self, f"{signal}_endpoint", None)

    def signal_enabled(self, signal: str) -> bool:
        return bool(getattr(self, f"{signal}_enabled", False))

    def signal_headers(self, signal: str) -> tuple[tuple[str, str], ...]:
        """Per-signal headers: signal-specific entries win over shared ones."""
        merged = dict(self.headers)
        merged.update(dict(getattr(self, f"{signal}_headers", ())))
        return tuple(merged.items())

    def signal_timeout_seconds(self, signal: str) -> float:
        specific = getattr(self, f"{signal}_timeout_seconds", None)
        return specific if specific is not None else self.export_timeout_seconds

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

        headers, rejected_headers = _parse_kv_pairs(
            env.get("OTEL_EXPORTER_OTLP_HEADERS"), max_value_chars=1024
        )
        resource_attributes, rejected_resource = _parse_kv_pairs(
            env.get("OTEL_RESOURCE_ATTRIBUTES"), max_value_chars=256
        )
        raw_service_version = _read(env, "DIRECTOR_OTEL_SERVICE_VERSION")

        signal_endpoints = {signal: _read(env, var) for signal, var in SIGNAL_ENDPOINT_VARS.items()}
        signal_headers: dict[str, tuple[tuple[str, str], ...]] = {}
        for signal, var in SIGNAL_HEADER_VARS.items():
            pairs, rejected = _parse_kv_pairs(env.get(var), max_value_chars=1024)
            signal_headers[signal] = pairs
            rejected_headers += rejected

        def signal_flag(signal: str) -> bool:
            parsed = _parse_flag(_read(env, SIGNAL_ENABLED_VARS[signal]))
            return True if parsed is None else parsed

        timeout_ms = _lenient_millis(
            env,
            "OTEL_EXPORTER_OTLP_TIMEOUT",
            DEFAULT_EXPORT_TIMEOUT_MILLIS,
            MIN_EXPORT_TIMEOUT_MILLIS,
            MAX_EXPORT_TIMEOUT_MILLIS,
        )
        signal_timeouts = {
            signal: (
                _lenient_millis(
                    env,
                    var,
                    timeout_ms,
                    MIN_EXPORT_TIMEOUT_MILLIS,
                    MAX_EXPORT_TIMEOUT_MILLIS,
                )
                / 1000.0
            )
            for signal, var in SIGNAL_TIMEOUT_VARS.items()
        }

        return cls(
            enabled=enabled,
            endpoint=endpoint,
            service_name=_bounded(
                service_name,
                "dungeon-director",
                _LABEL_RE,
                "OTEL_SERVICE_NAME",
            ),
            service_namespace=_bounded(
                _read(env, "DIRECTOR_OTEL_NAMESPACE"),
                DEFAULT_SERVICE_NAMESPACE,
                _LABEL_RE,
                "DIRECTOR_OTEL_NAMESPACE",
            ),
            service_version=(
                _bounded(
                    raw_service_version,
                    _service_version_from_contracts(),
                    _VERSION_RE,
                    "DIRECTOR_OTEL_SERVICE_VERSION",
                )
                if raw_service_version is not None
                else None
            ),
            environment=_bounded(
                _read(env, "DIRECTOR_OTEL_ENVIRONMENT"),
                DEFAULT_ENVIRONMENT,
                _LABEL_RE,
                "DIRECTOR_OTEL_ENVIRONMENT",
            ),
            resource_attributes=resource_attributes,
            traces_enabled=signal_flag("traces"),
            metrics_enabled=signal_flag("metrics"),
            logs_enabled=signal_flag("logs"),
            traces_endpoint=signal_endpoints["traces"],
            metrics_endpoint=signal_endpoints["metrics"],
            logs_endpoint=signal_endpoints["logs"],
            headers=headers,
            traces_headers=signal_headers["traces"],
            metrics_headers=signal_headers["metrics"],
            logs_headers=signal_headers["logs"],
            rejected_header_entries=rejected_headers,
            rejected_resource_entries=rejected_resource,
            export_timeout_seconds=timeout_ms / 1000.0,
            traces_timeout_seconds=signal_timeouts["traces"],
            metrics_timeout_seconds=signal_timeouts["metrics"],
            logs_timeout_seconds=signal_timeouts["logs"],
            metric_export_interval_seconds=(
                _lenient_millis(
                    env,
                    "OTEL_EXPORTER_OTLP_METRIC_EXPORT_INTERVAL",
                    DEFAULT_METRIC_EXPORT_INTERVAL_MILLIS,
                    MIN_METRIC_EXPORT_INTERVAL_MILLIS,
                    MAX_METRIC_EXPORT_INTERVAL_MILLIS,
                )
                / 1000.0
            ),
        )


# --------------------------------------------------------------------------- #
# Log hygiene
# --------------------------------------------------------------------------- #


def _sanitize_log_attributes(
    attributes: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Bound log-record attributes: count, key length, value length, no objects.

    Strings are truncated; other scalars pass through; anything else is
    stringified (and truncated). ``None`` values are dropped. This is the
    defensive backstop — callers following the #22 contract pass pre-sanitized
    attributes.
    """
    if not attributes:
        return None
    sanitized: dict[str, Any] = {}
    for key, value in list(attributes.items())[:LOG_ATTRIBUTE_MAX_ENTRIES]:
        if not isinstance(key, str) or not key:
            continue
        if value is None:
            continue
        if isinstance(value, bool | int | float):
            sanitized[key[:128]] = value
        else:
            sanitized[key[:128]] = str(value)[:LOG_ATTRIBUTE_MAX_CHARS]
    return sanitized or None


# --------------------------------------------------------------------------- #
# Telemetry facade
# --------------------------------------------------------------------------- #


class DirectorTelemetry:
    """Owns the tracer, the meter, the logger and the instruments.

    Without providers (the default) everything is a no-op. All public methods
    are safe to call from the request path: they never raise ``Exception``.

    ``signal_endpoints``/``header_counts``/``resource_view`` carry the safe
    (origin-only, value-free) configuration that :meth:`describe` exposes;
    ``export_health`` carries the bounded per-signal export state.
    """

    def __init__(
        self,
        tracer_provider: TracerProvider | None = None,
        meter_provider: MeterProvider | None = None,
        *,
        enabled: bool = True,
        logger_provider: Any = None,
        signal_endpoints: Mapping[str, str] | None = None,
        header_counts: Mapping[str, int] | None = None,
        resource_view: Mapping[str, str] | None = None,
        export_health: ExportHealth | None = None,
    ) -> None:
        if not enabled:
            tracer_provider = meter_provider = logger_provider = None
        self.tracer_provider: TracerProvider = tracer_provider or NoOpTracerProvider()
        self.meter_provider: MeterProvider = meter_provider or NoOpMeterProvider()
        self.logger_provider = logger_provider or NoOpLoggerProvider()
        self.enabled = enabled and (
            tracer_provider is not None or meter_provider is not None or logger_provider is not None
        )
        self._signal_endpoints = dict(signal_endpoints or {})
        self._header_counts = dict(header_counts or {})
        self._resource_view = dict(resource_view or {})
        self._export_health = export_health if export_health is not None else ExportHealth()
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
        try:
            self._otel_logger = self.logger_provider.get_logger(LOGGER_NAME)
        except Exception as exc:
            logger.warning(
                "telemetry logger creation failed (%s); log export disabled", type(exc).__name__
            )
            self._otel_logger = NoOpLoggerProvider().get_logger(LOGGER_NAME)

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

    # -- structured logs ------------------------------------------------------

    def emit_log(
        self,
        body: str,
        attributes: Mapping[str, Any] | None = None,
        context: Any = None,
        *,
        severity: str = "INFO",
    ) -> None:
        """Emit one structured log record over OTLP; never raises.

        ``body`` is a fixed template string (never raw exception text or
        payloads); ``attributes`` carry the structured, already-sanitized data
        (the #22 contract's ``GameEvent.attributes`` plus correlation IDs such
        as ``run_id``/``request_id`` — fine on logs, never metric dimensions).
        ``context`` is an optional :class:`opentelemetry.context.Context`; the
        log record is correlated with the span it carries.

        ``severity`` is one of ``DEBUG/INFO/WARN/WARNING/ERROR`` (unknown
        values degrade to ``INFO``), following the #22 conventions: INFO for
        healthy lifecycle events, WARN for rejections/fallbacks, ERROR for
        provider/internal errors.
        """
        self._emit_log(body, attributes, context, severity)

    @_never_raises
    def _emit_log(
        self,
        body: str,
        attributes: Mapping[str, Any] | None,
        context: Any,
        severity: str,
    ) -> None:
        if not self.enabled:
            return
        text = body if isinstance(body, str) else str(body)
        level = severity.strip().upper() if isinstance(severity, str) else "INFO"
        severity_number = _SEVERITIES.get(level, SeverityNumber.INFO)
        if level not in _SEVERITIES:
            level = "INFO"
        elif level == "WARNING":
            level = "WARN"
        self._otel_logger.emit(
            body=text[:LOG_BODY_MAX_CHARS],
            attributes=_sanitize_log_attributes(attributes),
            severity_text=level,
            severity_number=severity_number,
            context=context,
        )

    # -- diagnostics ----------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        """A safe configuration/export summary: no header values, no paths.

        Endpoints are reduced to ``scheme://host[:port]`` origins; export state
        is one of the fixed words ``pending``/``ok``/``failing`` (plus
        ``disabled`` for signals that are off). Suitable for ``/health``.
        """
        health = self._export_health.snapshot()
        signals: dict[str, Any] = {}
        for signal in SIGNAL_NAMES:
            endpoint = self._signal_endpoints.get(signal)
            state = health.get(signal, ExportHealth.PENDING) if endpoint is not None else "disabled"
            signals[signal] = {"enabled": endpoint is not None, "state": state}
            if endpoint is not None:
                signals[signal]["endpoint"] = endpoint
        described: dict[str, Any] = {
            "enabled": self.enabled,
            "signals": signals,
            "resource": dict(self._resource_view),
        }
        if self._header_counts:
            described["headers"] = {
                signal: count for signal, count in self._header_counts.items() if count
            }
        return described

    # -- lifecycle ----------------------------------------------------------

    def flush(self, timeout_millis: int = 5000) -> None:
        """Push buffered spans, metrics and logs to the exporters (best effort)."""
        self._flush(timeout_millis)

    def shutdown(self) -> None:
        """Flush and stop the exporters once; later calls do nothing.

        May block for as long as the exporters' own (bounded) timeouts when the
        collector is unreachable; async callers should run it in a worker
        thread under their own deadline (the app lifespan does).
        """
        if self._closed:
            return
        self._closed = True
        self._shutdown_provider(self.tracer_provider)
        self._shutdown_provider(self.meter_provider)
        self._shutdown_provider(self.logger_provider)

    @_never_raises
    def _flush(self, timeout_millis: int) -> None:
        for provider in (self.tracer_provider, self.meter_provider, self.logger_provider):
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


# --------------------------------------------------------------------------- #
# Setup
# --------------------------------------------------------------------------- #


def setup_telemetry(settings: TelemetrySettings) -> DirectorTelemetry:
    """Build the exporting telemetry, or a no-op one; never raises.

    Disabled settings, an unusable base endpoint, or any SDK/exporter
    construction failure all yield no-op telemetry, so a bad observability
    config cannot stop the director from serving. A malformed *signal-specific*
    endpoint disables only that signal. The exporters are lazy: an unreachable
    collector shows up later as background export failures in
    :class:`ExportHealth`, not here.

    Endpoint precedence per signal (standard OTel): a signal-specific full URL
    (``OTEL_EXPORTER_OTLP_{SIGNAL}_ENDPOINT``) wins; otherwise the base
    ``OTEL_EXPORTER_OTLP_ENDPOINT`` gets the signal's default path appended
    (``/v1/traces`` etc.).
    """
    if not settings.enabled or not settings.endpoint:
        return DirectorTelemetry(enabled=False)

    parts = urlsplit(settings.endpoint)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        # The value itself is not logged: a URL can embed credentials.
        logger.warning("OTEL_EXPORTER_OTLP_ENDPOINT is not an http(s) URL; telemetry disabled")
        return DirectorTelemetry(enabled=False)

    _quiet_otlp_exporter_logging()

    # Resolve per-signal endpoints first: an invalid signal endpoint disables
    # just that signal (the warning names the variable, never the value).
    endpoints: dict[str, str] = {}
    base = settings.endpoint.rstrip("/")
    for signal in SIGNAL_NAMES:
        if not settings.signal_enabled(signal):
            continue
        specific = settings.signal_endpoint(signal)
        if specific is None:
            endpoints[signal] = f"{base}{SIGNAL_PATHS[signal]}"
            continue
        if _validated_endpoint(specific) is None:
            logger.warning(
                "%s is not a valid http(s) URL; %s export disabled",
                SIGNAL_ENDPOINT_VARS[signal],
                signal,
            )
            continue
        endpoints[signal] = specific
    if not endpoints:
        logger.info("telemetry enabled but every signal is disabled by configuration")
        return DirectorTelemetry(enabled=False)

    try:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk._logs import LoggerProvider as SdkLoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.metrics import MeterProvider as SdkMeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider as SdkTracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        health = ExportHealth()
        version = settings.service_version or _service_version_from_contracts()
        # Our core identity wins over anything supplied via
        # OTEL_RESOURCE_ATTRIBUTES, and `service.name` stays configurable.
        core = {
            "service.name": settings.service_name,
            "service.namespace": settings.service_namespace,
            "service.version": version,
            "deployment.environment": settings.environment,
        }
        resource = Resource.create({**dict(settings.resource_attributes), **core})
        resource_view = dict(core)

        tracer_provider: Any = None
        meter_provider: Any = None
        logger_provider: Any = None

        if "traces" in endpoints:
            exporter = _TrackedSpanExporter(
                OTLPSpanExporter(
                    endpoint=endpoints["traces"],
                    headers=dict(settings.signal_headers("traces")) or None,
                    timeout=settings.signal_timeout_seconds("traces"),
                ),
                health,
                "traces",
            )
            tracer_provider = SdkTracerProvider(resource=resource)
            tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

        if "metrics" in endpoints:
            exporter = _TrackedMetricExporter(
                OTLPMetricExporter(
                    endpoint=endpoints["metrics"],
                    headers=dict(settings.signal_headers("metrics")) or None,
                    timeout=settings.signal_timeout_seconds("metrics"),
                ),
                health,
                "metrics",
            )
            reader = PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=int(settings.metric_export_interval_seconds * 1000),
                export_timeout_millis=int(
                    min(
                        settings.signal_timeout_seconds("metrics") * 1000,
                        MAX_EXPORT_TIMEOUT_MILLIS,
                    )
                ),
            )
            meter_provider = SdkMeterProvider(resource=resource, metric_readers=[reader])

        if "logs" in endpoints:
            exporter = _TrackedLogExporter(
                OTLPLogExporter(
                    endpoint=endpoints["logs"],
                    headers=dict(settings.signal_headers("logs")) or None,
                    timeout=settings.signal_timeout_seconds("logs"),
                ),
                health,
                "logs",
            )
            logger_provider = SdkLoggerProvider(resource=resource)
            logger_provider.add_log_record_processor(BatchLogRecordProcessor(exporter))

        telemetry = DirectorTelemetry(
            tracer_provider,
            meter_provider,
            logger_provider=logger_provider,
            signal_endpoints={signal: _origin(url) for signal, url in endpoints.items()},
            header_counts={signal: len(settings.signal_headers(signal)) for signal in endpoints},
            resource_view=resource_view,
            export_health=health,
        )

        origins = ", ".join(f"{s}={_origin(url)}" for s, url in sorted(endpoints.items()))
        header_note = (
            f"; headers per signal: "
            f"{', '.join(f'{s}={len(settings.signal_headers(s))}' for s in sorted(endpoints))}"
            "(values withheld)"
            if any(settings.signal_headers(s) for s in endpoints)
            else ""
        )
        logger.info(
            "telemetry enabled: service.name=%s service.namespace=%s service.version=%s "
            "deployment.environment=%s; %s%s",
            settings.service_name,
            settings.service_namespace,
            version,
            settings.environment,
            origins,
            header_note,
        )
        if settings.rejected_header_entries or settings.rejected_resource_entries:
            logger.warning(
                "OTLP header/resource configuration: %d header and %d resource entries "
                "rejected (malformed or oversized; values withheld)",
                settings.rejected_header_entries,
                settings.rejected_resource_entries,
            )
        return telemetry
    except Exception as exc:
        logger.warning("OpenTelemetry setup failed (%s); telemetry disabled", type(exc).__name__)
        return DirectorTelemetry(enabled=False)
