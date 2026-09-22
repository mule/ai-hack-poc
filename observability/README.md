# Observability

Status: **Implemented** (issues #11, #24, #26). OpenTelemetry traces, metrics and
comparison logs for director decisions and provider calls, plus a local OpenLIT stack.

For model latency, reliability, usage, room behavior, and matched comparisons, see
[the model-view recipes](model-views.md) and their parameterized SQL queries.

## What is emitted

Every `POST /v1/generate` produces an **active director span** named `director.generate` and
**one count** on `director.generation.requests`, however it ends: a room, a
provider failure, a timeout, a schema failure, a cancelled request, an unknown
provider/model, or a FastAPI `422` that never reached the service. Nothing in
the request/response JSON contract changes. Each actual adapter invocation adds
one `director.provider.invoke` child span, including local rules; selection
failures have no provider child because no call occurred. Configured shadows
produce their own `director.generate` and provider child spans.

The span name is fixed on purpose. Provider, model and status are attributes,
so the name never becomes a high-cardinality grouping key.

### Provider children and correlation

`director.provider.invoke` records `gen_ai.provider.name` (plus `gen_ai.system`),
`gen_ai.operation.name`, `gen_ai.request.model`, and `gen_ai.response.model`.
The response model reflects the provider's reported deployment when available.
`director.provider.call_duration_ms` measures the adapter call separately from
the parent duration; outcome, schema validity and timeout origin distinguish
provider errors, validation failures and deadlines.

Provider metadata contains only bounded, typed summaries, such as Jev confidence
scores and safe model identifiers. Raw probability maps and credential-shaped
strings are excluded. Usage and cost appear only when reported: an absent value
means unknown, while an explicitly reported zero remains zero.

The HTTP endpoint accepts W3C `traceparent`/`tracestate` and returns the active
director span's `traceparent` header when tracing is available. Invalid incoming
context is ignored. Explicit task-local parent contexts keep concurrent active,
shadow and replay executions separate. Generation mode is
`director.execution_mode=active|shadow|replay`; provider children carry
`director.provider.execution_mode` with the same value.

Fixed `director.shadow.execution` and `director.shadow.comparison` spans/logs
link outcomes through `shadow_comparison_id`. `director.replay.case` groups a
benchmark execution with its evaluation/dataset/case IDs. These IDs belong on
spans/logs, never metric labels. See [model comparison telemetry](../docs/model-comparison-telemetry.md)
and [provider telemetry](../director/docs/provider-telemetry.md) for details.

### Span attributes

| Attribute | Notes |
|---|---|
| `director.request_id`, `director.run_id` | Span-only, never metric dimensions. On a `422` they are attached only if the raw body holds a well-formed ID. |
| `director.provider`, `director.model` | Registry values; `unknown` when selection failed, `none` on a `422`. |
| `director.status` | See [status values](#status-values). |
| `director.error_code` | The canonical `ErrorKind` (e.g. `rate_limited`), `invalid_request` for a `422`. Never the message. |
| `director.http_status` | Status the game receives. |
| `director.latency_ms`, `director.provider_latency_ms` | End to end / provider call only (absent if no provider was called). |
| `director.timeout_origin` | `director_deadline` or `provider`. |
| `director.selection_error` | `unknown_provider`, `unknown_model`, `provider_unavailable`. |
| `director.schema_valid` | `true` on success, `false` on schema/JSON/version failures, absent when unknown. |
| `director.retry_count` | Always `0`: the director never retries. |
| `director.is_shadow`, `director.execution_mode` | `false` with `active` or `replay`, or `true` with `shadow`; nested replay shadows remain `shadow`. |
| `director.room.*` | Type, size, danger, exit count, secrets, densities of the chosen room (success only). |
| `gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model` | For OpenLIT's GenAI views. |
| `gen_ai.usage.input_tokens`, `.output_tokens`, `.total_tokens`, `.cost` | Only when the provider reported them. |

Span status is `OK` on success and `ERROR` (description = the error code) on
failures. A cancelled request is recorded with `director.status=cancelled` and
left `UNSET`: it is not a director fault.

### Metrics

| Metric | Type | Unit |
|---|---|---|
| `director.generation.requests` | counter | `1` |
| `director.generation.duration` | histogram (end to end) | `s` |
| `director.provider.duration` | histogram (provider call only) | `s` |
| `director.generation.tokens` | counter (only when reported) | `1` |
| `director.generation.cost` | counter (only when reported) | `USD` |

Both histograms carry the explicit bucket boundaries
`0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120, 300`
seconds, so p50/p90/p95/p99 can be estimated per provider/model/status. A `422`
adds no duration sample (nothing was generated; a ~0 s point would skew the
percentiles), and a request that failed selection adds no provider-duration
sample.

**The generation metric dimensions** are `provider`, `model`, `status`, `error_code`
and `execution_mode` (plus `token_type` = `input`/`output` on the token
counter). Provider and model are taken from the registry, never from the query
string, so a client cannot create new series by sending `?provider=anything`.
Request and run IDs are never dimensions.

### Status values

| `status` | Meaning |
|---|---|
| `success` | A validated room was returned. |
| `selection_error` | Unknown provider/model, or provider unavailable (404/503). |
| `provider_error` | Rate limit, budget, refusal, empty/other provider failure, unexpected exception. |
| `timeout` | Director deadline missed, or the provider reported its own timeout (504). |
| `schema_error` | Provider output failed contract validation (invalid JSON, schema, version, empty). |
| `invalid_request` | FastAPI rejected the request body/query (422). |
| `cancelled` | The request task was cancelled (client disconnect, shutdown). |
| `internal_error` | An unexpected exception escaped the service (a director bug). |

### What is never recorded

Request or response bodies, `prompt_hint`, exception text, adapter error
messages, headers, or credentials. Errors are reduced to their code before they
reach a span or a log line; the director's own log lines from this feature
carry only the exception *type*.

### Fail-open

Telemetry cannot change what the game receives. Setup, instrument creation,
span start, attribute writes, metric recording, `span.end()`, flush and
shutdown all run behind guards that log the exception type and continue; each
step is guarded separately, so a broken span does not lose the metrics and vice
versa. A bad endpoint or SDK failure at startup yields no-op telemetry. Shutdown
flushes exporters off the event loop.

## Configuration

Read by the director at startup (see `.env.example`):

| Variable | Default | Meaning |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/HTTP **base** URL, e.g. `http://localhost:4318`. Setting it turns export on. `/v1/traces` and `/v1/metrics` are appended. |
| `DIRECTOR_OTEL_ENABLED` | follows the endpoint | `1/true/yes/on` forces export on (endpoint defaults to `http://localhost:4318`); `0/false/no/off` forces it off; any other value counts as off. |
| `OTEL_SERVICE_NAME` | `dungeon-director` | `service.name` resource attribute. |

With none of these set, telemetry is a no-op. The exporters are lazy: an
unreachable collector never blocks startup or requests; it only produces
background export warnings.

## Run OpenLIT locally

Requires Docker with Compose v2. From the repository root:

```bash
docker compose -f observability/docker-compose.yml up -d
docker compose -f observability/docker-compose.yml ps   # wait for both "healthy"
```

The stack follows the official OpenLIT 2.1.0 layout:

* `clickhouse` (`clickhouse/clickhouse-server:24.4.1`) stores traces and metrics.
  `assets/clickhouse-init.sh` creates the OpenTelemetry tables and
  `assets/clickhouse-config.xml` trims ClickHouse's own logging.
* `openlit` (`ghcr.io/openlit/openlit:2.1.0`) serves the UI and embeds the
  OpenTelemetry collector configured by `assets/otel-collector-config.yaml`
  (OTLP in, ClickHouse out).

The three files in `assets/` are the upstream 2.1.0 files, unmodified. All
ports bind to `127.0.0.1`. The ClickHouse password defaults to `OPENLIT`, a
well-known local value; override it with `OPENLIT_DB_USER`, `OPENLIT_DB_PASSWORD`
and `OPENLIT_DB_NAME` (set them for both services by exporting them before
`up`). OpenLIT's own anonymous usage analytics (`TELEMETRY_ENABLED`) are **off**
by default; set `OPENLIT_TELEMETRY_ENABLED=true` to opt in. That is unrelated to
the director's OTLP export.

| Endpoint | URL |
|---|---|
| OpenLIT UI | <http://127.0.0.1:3000> |
| OTLP HTTP receiver | `http://127.0.0.1:4318` |
| OTLP gRPC receiver | `127.0.0.1:4317` (the director uses HTTP) |

## Point the director at it

```bash
export OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
make run-director
```

Send a request with the canonical fixture (the real `GenerationRequest` shape):

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/generate" \
  -H "Content-Type: application/json" \
  --data @contracts/fixtures/generation_request.json
```

Select a provider with the query string. Without Cloudflare credentials this one
is reported unavailable (503), which shows up as a `selection_error` span:

```bash
curl -s -X POST "http://127.0.0.1:8000/v1/generate?provider=cloudflare-jev" \
  -H "Content-Type: application/json" \
  --data @contracts/fixtures/generation_request.json
```

Spans export within a few seconds; metrics every 5 seconds.

## Inspect

* **OpenLIT UI** (<http://127.0.0.1:3000>): the first visit asks you to sign up
  or log in; use the credentials from the OpenLIT docs for a fresh install.
  Open *Requests*/traces and filter on `director.provider`, `director.model`,
  `director.status`.
* **ClickHouse directly** (works without the UI). Histograms are exported as
  cumulative totals every 5 s, so take the latest (`max`) value per series and
  process start (`StartTimeUnix`) before summing. Request count and mean
  end-to-end latency per provider and status:

  ```bash
  docker compose -f observability/docker-compose.yml exec clickhouse \
    clickhouse-client --user default --password OPENLIT --database openlit --query "
    SELECT provider, status, sum(n) AS requests, round(sum(total_s) / sum(n), 6) AS mean_s
    FROM (
      SELECT Attributes['provider'] AS provider, Attributes['status'] AS status,
             StartTimeUnix, max(Count) AS n, max(Sum) AS total_s
      FROM otel_metrics_histogram
      WHERE MetricName = 'director.generation.duration' AND ServiceName = 'dungeon-director'
      GROUP BY provider, status, StartTimeUnix)
    GROUP BY provider, status ORDER BY provider, status"
  ```

  Recent spans:

  ```bash
  docker compose -f observability/docker-compose.yml exec clickhouse \
    clickhouse-client --user default --password OPENLIT --database openlit --query "
    SELECT Timestamp, SpanName, StatusCode,
           SpanAttributes['director.status'] AS status,
           SpanAttributes['director.provider'] AS provider,
           SpanAttributes['director.request_id'] AS request_id
    FROM otel_traces WHERE ServiceName = 'dungeon-director'
    ORDER BY Timestamp DESC LIMIT 20"
  ```

  The histogram rows keep `BucketCounts` and `ExplicitBounds` for p50/p90/p95/p99
  estimates. The offline `rules-baseline` provider reports 0 tokens and $0, so
  its token and cost series exist with value 0; providers that report nothing
  produce no series.

## Stop

```bash
docker compose -f observability/docker-compose.yml down       # keep data
docker compose -f observability/docker-compose.yml down -v    # also delete stored telemetry
```

## Tests

`director/tests/test_telemetry.py` reads real SDK output back from in-memory
span and metric exporters. It covers every outcome above, the bounded
dimensions (including a client hammering random `?provider=` values), token and
cost omission, shadow labelling, secret hygiene, and each fail-open path
(broken tracer, span, instrument, exporter setup, flush, shutdown).

## Known limits

* Metric label values come from the registry, so anything an operator
  registers is a label value. That set is small and operator-controlled.
* OTLP exporter diagnostics are redacted; inspect the safe telemetry health
  endpoint for export state rather than expecting raw collector error text.
* `director.generation.duration` measures the service call, not HTTP parsing or
  serialization.
* Cross-process parenting requires W3C context from the caller; without it, the
  director starts a new trace and returns its context for subsequent correlation.

## Shadow evaluation (issue #13)

The director records every active and shadow execution through the
`ShadowObserver` interface in `director/dungeon_director/shadow.py`; the full
record contract is documented in `director/docs/shadow-mode.md`. OpenTelemetry
also instruments each actual provider execution through the shared service
pipeline, with `director.execution_mode=active`, `shadow`, or `replay`.

Shadow record metric labels come from `ExecutionRecord.metric_labels()` and are
bounded to role, registered provider/model, status and reason. The
`comparison_id`, `request_id` and `run_id` values are unbounded and belong only
in stored records or span attributes, never metric labels.
