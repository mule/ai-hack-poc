# Model comparison telemetry

Use the same OTLP configuration as the director. A comparison adds metadata-only
`director.shadow.execution` and `director.shadow.comparison` spans and structured
logs to the trace that launched the shadows. Search by `shadow_comparison_id`,
`director.request_id`, or `director.run_id`; these identifiers never appear on
metric dimensions. The existing ShadowStore and recording artifacts retain
canonical outcomes, including details deliberately omitted from telemetry.

The app registers the comparison observer through DirectorService's
`shadow_observers` interface; library callers can use the same interface.
It retains at most 256 comparisons, each with at most the active result
and four shadow results. It emits each active/shadow pair when both are available,
regardless of completion order. Evicted work cannot recreate old state; its later
execution status is still exported. SDK batch processors perform remote export in
the background. Observer errors are isolated by ShadowEvaluator.

`director.shadow.executions` counts active/shadow status and reason, including
skipped, overloaded, cancelled, failed and successful executions. Unregistered
provider/model labels become `other`. `director.shadow.comparisons.evicted`
indicates an incomplete comparison lost to bounded retention.

`director.shadow.comparisons` uses provider/model pairs plus **one** `category` and
`result` dimension per observation, avoiding the Cartesian product of every room
property. Consequently, summing every category would count each pair repeatedly;
filter one category (for example `success_mismatch`) to count comparisons.

| Category | Results |
| --- | --- |
| success_mismatch, schema_mismatch | true, false, unknown |
| room_type_mismatch, size_mismatch, has_secret_mismatch | true, false, unknown |
| danger_delta, enemy_density_delta, loot_density_delta, secret_probability_delta, exit_count_delta | lower, equal, higher, unknown |
| latency_winner, cost_winner | active, shadow, tie, unknown |

Delta direction is **shadow minus active**. Schema failures include invalid JSON,
schema violations, unsupported contract versions and empty responses. Unavailable
outcomes, unresolved secret decisions and absent costs are unknown, never zero.
An explicitly reported zero cost remains a valid measurement. Latency compares
wall-clock execution durations when both executions produced outcomes; a fast
failure can win latency, so inspect `success_mismatch` and execution statuses too.

## Replay and evaluation

The replay CLI initializes production telemetry from the environment and drains
it after service/provider shutdown, including failure paths. Export shutdown is
bounded to five seconds. Library calls with an injected service keep ownership of
that service's telemetry with the caller; internally constructed services own and
close their telemetry.

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://192.168.50.195:4318 \
PYTHONPATH=director python -m benchmarks.replay \
  --input benchmarks/fixtures/sample_run.jsonl --providers rules-baseline \
  --evaluation-id eval-local-001 --dataset-version corpus-v1
```

Every provider/model execution gets a `director.replay.case` span with replay,
evaluation, dataset, version and case IDs. The same canonical request and iteration
share a case ID across providers; dataset IDs derive from canonical dataset
content, not potentially sensitive file paths. Iterations have distinct case IDs.
Execution context is `replay` (nested shadow calls remain `shadow`). IDs and the
explicit parent span context propagate to service/provider spans through shared
task-local context variables, without attaching a generation span as ambient state.

Each result artifact adds `telemetry_ids`, preserving the existing canonical room,
usage, response metadata, and error fields. Filter OpenLIT by these IDs to compare
the case across models. IDs and versions are bounded label-shaped strings; invalid
free text is omitted from telemetry. No request state, prompts, response payloads,
or provider error messages are copied to telemetry.
