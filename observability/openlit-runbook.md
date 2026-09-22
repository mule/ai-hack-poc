# OpenLIT ingestion smoke and operator runbook

`make openlit-smoke` exercises the production director service with one offline
`rules-baseline` generation, exports a correlated structured log, flushes OTLP,
and queries persisted ClickHouse rows for traces, logs, a request counter, and
a latency histogram. A receiver HTTP 200 is not sufficient to pass.

## Configuration

Keep the UI and collector addresses separate. For the remote deployment:

```sh
export OTEL_EXPORTER_OTLP_ENDPOINT=http://192.168.50.195:4318
export DIRECTOR_OTEL_ENABLED=1
export DIRECTOR_OTEL_TRACES_ENABLED=1
export DIRECTOR_OTEL_METRICS_ENABLED=1
export DIRECTOR_OTEL_LOGS_ENABLED=1
```

The OpenLIT UI is at `http://192.168.50.195:3000`. The base collector endpoint
appends `/v1/traces`, `/v1/metrics`, and `/v1/logs`; signal-specific endpoints
remain supported. See [remote export configuration](../director/docs/remote-telemetry.md).
The command reads exported environment variables; it does not load `.env` itself.

Full automatic verification additionally requires an operator-supplied ClickHouse
HTTP URL and a read-only account with SELECT on the four OTEL tables:

- `OPENLIT_SMOKE_CLICKHOUSE_URL` — HTTP(S) URL, normally port 8123; no embedded credentials.
- `OPENLIT_SMOKE_CLICKHOUSE_DATABASE` — actual OpenLIT database name.
- `OPENLIT_SMOKE_CLICKHOUSE_USER` — explicitly supplied read-only username.
- `OPENLIT_SMOKE_CLICKHOUSE_PASSWORD` — password from the environment (an explicitly empty password is supported).

These are distinct from OpenLIT login credentials and OTLP authorization headers.
There are no guessed credential or database defaults. A private ClickHouse server
can be reached through an operator-managed tunnel; exposing it publicly is not
required. The verifier uses parameterized SELECT queries with `readonly=1` and
bounded query/HTTP timeouts. Credentials, collector URLs, response bodies, room
payloads, and prompts are omitted from evidence.

```sh
make openlit-smoke
# Optional bounded ingestion deadline, default 60 seconds:
make openlit-smoke SMOKE_ARGS='--deadline 120'
```

The underlying Python CLI returns exit 0 when all requested generations succeeded and all four persisted signal
groups were found for this invocation. Exit 1 means configuration, generation,
export setup, verification query/authentication, or ingestion failed. JSON
`missing` lists exactly which signal groups were absent. The deadline bounds
polling; in-flight HTTP queries and exporter cleanup have their own short bounds.

GNU Make returns 2 for any failed recipe, so `make openlit-smoke` does not
preserve the CLI distinction between exit 1 and exit 2. Read the JSON `status`
and `error`, or invoke `PYTHONPATH=director director/.venv/bin/python -m
benchmarks.openlit_smoke` directly when exact exit codes are needed. The Make
recipe suppresses command echo so redirected stdout contains only JSON.

Every invocation gets a fresh `service.instance.id` resource value. The
request/run IDs are attributes on spans/logs only. The log must share the
`director.generate` trace ID. Metric checks require a positive successful request
counter and histogram count for the requested provider. Old records cannot
satisfy these checks, and cumulative samples are not summed as separate requests.

## Emit for manual UI verification

If ClickHouse read access has not been supplied:

```sh
make openlit-smoke SMOKE_ARGS='--emit-only' > /tmp/openlit-smoke.json
```

**Exit 2 is intentional:** status is `emitted_unverified` and
`ingestion_verified` is false. This is not end-to-end acceptance. The JSON includes
a fresh instance ID, start time, request IDs, actual selected provider/model,
service version, deployment environment, Git revision, and dirty-checkout flag.
Only these bounded identity fields are included; arbitrary resource attributes,
headers, and environment variables are not copied into evidence. A packaged
checkout without Git metadata reports an unknown revision. In the authenticated OpenLIT
Telemetry explorer, filter **ResourceAttributes → service.instance.id** to that
value and use a time window covering the emitted timestamp:

1. Traces: find `director.generate` with the exact request ID.
2. Logs: find `director.smoke.generation` and verify its trace ID matches.
3. Metrics: find positive `director.generation.requests` and
   `director.generation.duration` samples for `rules-baseline`.

Save screenshots or separately recorded query results with the JSON evidence.
Do not change an emit-only artifact into a machine-verified pass. A fresh remote
acceptance run remains necessary after deploying changes; repository tests alone
do not prove the remote receiver stored data.

## Optional live models

Baseline is always included. Hosted models are opt-in and may incur costs:

```sh
make openlit-smoke SMOKE_ARGS='--live --live-provider typesafe-jev --live-provider groq'
```

Supported selections: `typesafe-jev`, `cloudflare-jev`, `groq`, `cerebras`.
Their normal provider credentials/model environment settings apply. Ambient
shadow settings and the default-provider selector are deliberately ignored so
this command cannot silently make additional hosted calls. There is one call per
selected provider; unavailable/failing providers fail the smoke, without exposing
their error text. No live model calls run in ordinary tests.

## Troubleshooting

- `verification_config_missing_or_invalid` / `verification_credentials_missing`:
  provide explicit ClickHouse read access, or use emit-only for manual evidence.
- `verification_auth_failed`: verify that the supplied read-only account has
  access to the selected database. No default credentials are tried.
- `verification_query_failed`: check the database/table schema and private
  ClickHouse reachability. Queries target `otel_traces`, `otel_logs`,
  `otel_metrics_sum`, and `otel_metrics_histogram` as defined in
  [the collector schema](assets/clickhouse-init.sh).
- `all_three_export_signals_required`: enable all three export signals.
- `ingestion_missing_signals`: inspect the missing group(s), collector pipelines,
  ClickHouse export errors, and table retention. OTLP export success only proves
  receiver acceptance, so downstream storage failure can cause this result.
- `generation_failed`: check the configured provider separately; baseline needs
  no AI credentials. The smoke deliberately omits provider exception text.

The tests decode actual OTLP protobufs to check production generation/log/metric
emission and correlation. A sentinel is injected into the generation prompt and
must be absent from every exported body. Separate negative checks withhold each
persisted signal and require a nonzero outcome; backend error text containing a
sentinel must not appear in error evidence.
