# Remote telemetry export (issue #23)

How the director exports traces, metrics and structured logs to a remote
OpenLIT (or any OTLP/HTTP) collector, how to configure it, and what it
guarantees not to do. The span/metric vocabulary itself is defined by the
telemetry contract (issue #22, `telemetry_schema.py` +
`observability/telemetry-schema.md`); this document covers the transport.

## Quick start

```
# local OpenLIT stack (see observability/docker-compose.yml)
docker compose -f observability/docker-compose.yml up -d
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318
make run-director
curl localhost:8000/v1/telemetry/health
```

For the self-hosted deployment from the epic (issue #21), point the endpoint
at that host's OTLP receiver, e.g. `http://192.168.50.195:4318`. The OpenLIT
UI address (`http://192.168.50.195/`) is only for humans — nothing in the
runtime uses it, and the two addresses are separate settings.

## Configuration

All settings are read once at startup from the process environment
(`TelemetrySettings.from_env`); `.env.example` documents every variable.

### Master switch

| Variable | Effect |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | OTLP/HTTP base URL; setting it enables export. The director appends the standard signal paths (`/v1/traces`, `/v1/metrics`, `/v1/logs`). |
| `DIRECTOR_OTEL_ENABLED` | `1/true/yes/on` forces export on (default endpoint `http://localhost:4318`); `0/false/no/off` forces it off; unset follows the endpoint variable. Any other value counts as off. |

### Signals

Traces, metrics and logs are wired independently. Each follows the master
switch unless its own flag is set:

- `DIRECTOR_OTEL_TRACES_ENABLED`, `DIRECTOR_OTEL_METRICS_ENABLED`,
  `DIRECTOR_OTEL_LOGS_ENABLED` — same truthy/falsy conventions as the master
  switch; a malformed value turns that signal **off**.

Endpoint precedence per signal (standard OTel): a signal-specific full URL
wins over the base URL; otherwise base + default path.

- `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT`,
  `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` — full URLs (any path is respected).
  A malformed signal endpoint disables **only that signal** (startup logs a
  warning naming the variable, never the value); a malformed base endpoint
  disables telemetry entirely.

### Headers (optional)

- `OTEL_EXPORTER_OTLP_HEADERS` — comma-separated `key=value`, sent with every
  OTLP request.
- `OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_HEADERS` — per-signal headers;
  merged over the shared set (signal wins on key conflicts).

Header values are secrets by nature. They are passed to the exporters and
nowhere else: never logged, never in `GET /v1/config`, never in
`GET /v1/telemetry/health`, never in startup output (which reports only the
number of configured headers). Malformed entries (missing `=`, empty or
oversized value, bad key) are counted and dropped silently.

### Resource identity

Shared by all three signals, per the #22 contract:

| Attribute | Source | Purpose |
|---|---|---|
| `service.name` | `OTEL_SERVICE_NAME` (default `dungeon-director`) | Per-component identity: the game-telemetry bridge uses `dungeon-director-game-bridge`, benchmark/replay tooling `dungeon-director-benchmark`. |
| `service.namespace` | `DIRECTOR_OTEL_NAMESPACE` (default `dungeon-director`) | Groups the components as one system in OpenLIT's service map. |
| `service.version` | `DIRECTOR_OTEL_SERVICE_VERSION`, default `contracts.CONTRACT_VERSION` | Pinned to the shared contract version so dashboards can detect incompatible telemetry. |
| `deployment.environment` | `DIRECTOR_OTEL_ENVIRONMENT` (default `dev`) | Distinguishes dev/staging/prod deployments. |

Extra resource attributes: `OTEL_RESOURCE_ATTRIBUTES` (`k=v,k=v`; malformed
entries counted and dropped). Core identity above wins over same-named keys.

### Export timing

- `OTEL_EXPORTER_OTLP_TIMEOUT` (ms, default 10000; out-of-range values use
  the default, so the effective value is always 500–30000): per-request
  exporter timeout. Signal-specific
  `OTEL_EXPORTER_OTLP_{TRACES,METRICS,LOGS}_TIMEOUT` override it.
- `OTEL_EXPORTER_OTLP_METRIC_EXPORT_INTERVAL` (ms, default 5000; effectively
  1000–60000): metric push interval.

The bounds keep flush and shutdown bounded: with an unreachable collector,
`DirectorTelemetry.shutdown()` cannot block longer than the (bounded)
exporter timeouts, and the app lifespan additionally runs it on a daemon
thread joined with a hard 30 s cap.

## Structured logs (`emit_log`)

`DirectorTelemetry.emit_log(body, attributes=None, context=None,
severity="INFO")` emits one OTLP log record and never raises. It exists for
provider/game lifecycle logs (the #22 `GameEvent` vocabulary); arbitrary
Python logging is **not** exported — there is no log-handler bridge.

Conventions (from the contract):

- `body` is a fixed template per event (e.g. `room.committed`), never raw
  exception text, prompts or payloads.
- `attributes` carry the structured, sanitized data plus correlation IDs
  (`run_id`, `request_id`, `frontier_id`, `room_id`); correlation IDs are
  fine on logs and traces, never as metric dimensions.
- Severity: `INFO` for healthy lifecycle events, `WARN` for rejections and
  fallbacks, `ERROR` for provider/internal errors; unknown severity strings
  degrade to `INFO`. (`WARNING` is normalized to `WARN`.)
- Defensive bounds: body ≤ 4096 chars, ≤ 32 attributes, string values ≤ 1024
  chars, objects stringified. `context` is an `opentelemetry.context.Context`;
  the record is correlated with the span it carries.

## Diagnostics

`GET /v1/telemetry/health` returns a safe summary (also available as
`DirectorTelemetry.describe()`):

```json
{
  "enabled": true,
  "signals": {
    "traces":  {"enabled": true, "state": "ok",      "endpoint": "http://192.168.50.195:4318"},
    "metrics": {"enabled": true, "state": "failing", "endpoint": "http://192.168.50.195:4318"},
    "logs":    {"enabled": false, "state": "disabled"}
  },
  "resource": {"service.name": "dungeon-director", "service.namespace": "dungeon-director",
               "service.version": "1.0.0", "deployment.environment": "dev"},
  "headers": {"traces": 1, "metrics": 1}
}
```

- `state` is per-signal export health updated on every export attempt:
  `pending` (nothing exported yet), `ok`, `failing`, or `disabled`.
- Endpoints are reduced to `scheme://host[:port]` origins — no paths, no
  query strings, no userinfo.
- Header *values* never appear; only counts.
- `GET /health` (the liveness probe) is unchanged.

Startup logs one INFO summary with the same content (plus a warning counting
rejected header/resource entries, values withheld).

## Failure-safety guarantees

Covered by `tests/test_remote_telemetry.py`:

1. **Generation is untouched.** With the collector slow (per-request delay),
   failing (HTTP 500), or unreachable, `/v1/generate` responses are
   byte-identical (modulo wall-clock fields) to a director with telemetry
   off. Export happens on background SDK threads.
2. **No-op default.** No endpoint configured → no providers, no threads, no
   export attempts.
3. **Setup can't fail startup.** Malformed base endpoint, malformed signal
   endpoint, bad headers, broken SDK — every path degrades (all off, signal
   off, drop entry, no-op telemetry) with warnings that name at most the
   variable, never the value.
4. **Bounded diagnostics.** SDK exporter log output is filtered: exception
   text/stack traces stripped, URLs reduced to origins (endpoints may embed
   credentials), messages capped at 300 chars. Export state is tracked as
   fixed words per signal.
5. **Bounded teardown.** Exporter timeouts are effectively bounded; `flush`/`shutdown`
   return within them; the app lifespan hard-caps export shutdown at 30 s on
   a daemon thread.
6. **No secrets in flight beyond the collector.** OTLP header values are sent
   to the collector (that is their job) and appear nowhere else; span/metric/
   log payloads contain only the bounded vocabulary from the contract.

## Troubleshooting

| Symptom | Check |
|---|---|
| `state: pending` forever | No traffic: exports only happen after spans/metrics/logs exist. Trigger a generate request, then re-check. |
| `state: failing` | Collector down, wrong port, or rejecting: `curl -v $OTEL_EXPORTER_OTLP_ENDPOINT/v1/traces -X POST -H 'content-type: application/x-protobuf'` should not connection-refuse. |
| Signal `disabled` unexpectedly | Its `DIRECTOR_OTEL_*_ENABLED` is falsy/malformed, or its signal-specific endpoint URL is malformed (see the startup warning naming the variable). |
| Nothing in OpenLIT | Confirm the UI and the OTLP receiver are different ports on the deployment host; the endpoint must be the receiver (`:4318` in the example), not the UI. |
| Shutdown slow | Lower `OTEL_EXPORTER_OTLP_TIMEOUT`; shutdown waits at most the bounded exporter timeouts. |

## Test isolation note

The offline test guard (director `tests/conftest.py`) deletes all
`OTEL_*`/`DIRECTOR_OTEL_*` exporter variables per test and allows sockets to
loopback only, so tests exercise real OTLP/HTTP against a loopback fake
collector (`FakeCollector` in `tests/test_remote_telemetry.py`) without
reaching any real deployment.
