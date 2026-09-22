# Model performance and dungeon behavior in OpenLIT

This recipe extends the telemetry foundation from #11 and implements the views
in #27. Use the OpenLIT telemetry explorer and the parameterized read-only SQL
files in `queries/`. No OpenLIT database credentials are checked in.

## Select one comparable population

Open **Telemetry → Traces**, select a time range, and use **Custom Attributes →
Add** to filter on resource `service.name` (`dungeon-director` by default) and
`deployment.environment` (`dev` by default). Set `DIRECTOR_OTEL_SERVICE_VERSION=<build-or-git-sha>` before using
`service.version` to isolate a build: its default is the contract version, shared
by multiple builds. The game lifecycle span attribute
`telemetry.schema.version=1` identifies the game event contract; it is not a
common resource filter for generation spans. Resource names come from
`TelemetrySettings` and can be overridden in the environment.

To inspect a session, add span attributes `director.run_id` and optionally
`director.request_id`. IDs belong on traces/logs, never metric labels. The bridge
uses the director's exporters and resource identity, with instrumentation scope
`dungeon-game-bridge`; it does not require a second OpenLIT service configuration.

Keep live active, shadow, and replay executions separate. Compare the same build,
environment, time interval, and game-state population. The provider ID in this
repository is `groq` (Groq hosting), not `grok`; provider/model IDs are taken from
`GET /v1/config`. Include `rules-baseline / builtin-v1` as a local control.

## Six views

| View | Trace selection / metric explorer | Read-only query | Interpretation |
|---|---|---|---|
| Latency | `director.generate`; metrics `director.generation.duration`, `director.provider.duration` | `model-latency.sql` | p50/p90/p95/p99 in milliseconds, per provider/model/mode; upstream coverage is explicit |
| Reliability | `director.generate`, group by `director.status`; game events for downstream rejection/fallback | `model-reliability.sql`, `game-outcomes.sql` | Provider success is distinct from game acceptance and room commit |
| Tokens and cost | `gen_ai.usage.*`; metrics `director.generation.tokens`, `director.generation.cost` | `model-usage.sql` | Coverage columns show missing reports; averages include only reported values |
| Model behavior | Successful `director.generate` room attributes; `game.rooms.committed`, `game.room.density` for realized rooms | `model-behavior.sql` | Proposed room type/size/danger/exits/secrets/densities, compared with committed rooms |
| Outcome stages | `game.lifecycle`, filter `game.event.name`; `game.lifecycle.events`, `game.lifecycle.duration` | `game-outcomes.sql`, `request-journey.sql` | Discovery → queue → send → response → acceptance/rejection → fallback/commit → reveal → entry |
| Matched comparison | `director.shadow.comparison`, filter `shadow_comparison_id`; replay filter `replay_id` / `case_id` | `matched-comparisons.sql` | Active and shadow answer the same state; missing cost/latency is unknown |

Use the **Logs** tab with the same run/request filters to inspect structured
lifecycle records. A trace link connects asynchronous events when a producer
supplies W3C trace context; run/request attributes also correlate batches that
arrive after the generation span has ended.

For Jev, inspect the provider child span for allowlisted confidence summaries.
Confidence is provider-specific and is not a calibrated probability that the
gameplay outcome will be good. No raw prompts or provider responses are required
for these views.

## Run the queries

The SQL targets the ClickHouse `otel_traces` table created by the repository's
collector configuration. It works independently of OpenLIT dashboard-builder
version. Use an authorized read-only ClickHouse client connection to the same
database configured in OpenLIT. Supply the following typed parameters:

```sh
clickhouse-client --host YOUR_CLICKHOUSE_HOST --database YOUR_DATABASE \
  --param_service=dungeon-director --param_environment=dev \
  --param_start=2026-09-22T00:00:00Z --param_end=2026-09-23T00:00:00Z \
  --queries-file observability/queries/model-latency.sql
```

Use the client's protected authentication configuration; do not put passwords in
shell history. `request-journey.sql` additionally requires `--param_run` and
`--param_request`. Each query has the same service/environment/time filters;
add `ResourceAttributes['service.version'] = {version:String}` when comparing
one build, or `SpanAttributes['director.run_id'] = {run:String}` for a run.
Save the query and its parameter values with an evaluation report so it is
reproducible. Never treat a missing row as a zero-latency or zero-cost result.

## Reading results accurately

- Display the sample count beside every percentile. With fewer than 100 requests,
  p99 is effectively a maximum; even 100 is only a preliminary tail estimate.
  Use at least 1,000 matched requests for a more useful tail comparison and repeat
  across time windows. This is guidance, not a statistical guarantee.
- Trace percentiles describe the recorded sample; trace sampling changes that
  population. Metric histograms cover all recorded observations but have bucket
  approximation. Neither represents time to player entry; use the game timing
  stages for that.
- A zero upstream sample count means upstream timing is unavailable. Ignore the
  empty aggregate's numeric representation. Cost/usage sums are interpretable
  only with their coverage counts. Cost per successful provider response is not
  total spend per committed room when failed/shadow calls also consume tokens.
- `game-outcomes.sql` is a stage-count view, not automatically a conversion rate:
  discoveries can precede a request ID, events can be dropped under backpressure,
  and entry can occur outside the selected window. Use a single run/request
  journey to diagnose a missing stage. Compare actual fallback model attribution
  against the original failed request.
- Room plans are semantic decisions. The deterministic materializer can prune
  exits or use a fallback; inspect `generation.normalized`, `generation.rejected`,
  and committed-room measurements before attributing geometry to a model.
- Shadow differences reveal behavioral disagreement, not a quality ranking.
  Gameplay quality needs the existing evaluation protocol in
  `docs/evaluation-methodology.md`.

## Validation

The SQL can be checked against an isolated ClickHouse instance using
`observability/assets/clickhouse-init.sh` and a small tagged trace fixture. Remote
acceptance additionally requires fresh data from `make openlit-smoke` and a game
run; collector HTTP success alone is insufficient. Record the build, timestamp,
service/environment, sample sizes, available providers, and missing signals in
the validation evidence. See `openlit-runbook.md` for the remote check.

Local validation on 2026-09-22 executed all seven queries against ClickHouse
24.4.1 with the repository table schema and synthetic generation, game, and
comparison spans. Assertions checked 100 ms latency percentiles, a 0.5 success
rate, and one shadow latency winner. This validates query behavior; it does not
claim remote ingestion or real model performance.
