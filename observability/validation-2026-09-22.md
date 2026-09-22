# OpenLIT acceptance record — 2026-09-22

Status: **manual remote acceptance PASSED — traces, logs, and metrics confirmed**.
This record covers the implementation in epic [#21](https://github.com/mule/ai-hack-poc/issues/21).
It separates saved CLI evidence from manual checks in the authenticated remote
OpenLIT UI. All required signal groups below were confirmed using the exact
fresh identifiers. Automated ClickHouse persistence verification was not run
because ClickHouse credentials were not supplied; original CLI verification
flags remain false.

## Build and deployment

- Tested revision: `f59f4babc71e250c0c324f7f8fb949705e780191` (`f59f4ba`).
- Harness and game checkouts were clean (`dirty: false`).
- Service: `dungeon-director`; exported service version: `f59f4ba`;
  deployment environment: `dev`.
- Remote deployment: OpenLIT UI at `http://192.168.50.195:3000`;
  OTLP/HTTP receiver at `http://192.168.50.195:4318`.
- Implementation PRs were subsequently merged into `main` at `ec3db21`.
  The acceptance runs below retain their original tested revision.

## Director and live provider smoke

Started **2026-09-22 10:25:08.304 UTC**. Unique resource instance ID:
`d73db8518c124b778b9c15cdf37ef25a`.

| Provider | Configured model | Request ID suffix | Result |
|---|---|---|---|
| Rules baseline | `builtin-v1` | `-0` | Generation succeeded |
| TypeSafe Jev | `jev-latest` | `-1` | Generation succeeded; UI showed served model `jev-1.13.0` |
| Groq | `openai/gpt-oss-20b` | `-2` | Generation succeeded |
| Cerebras | `qwen-3.8-27b` | `-3` | Generation succeeded |

Each request ID has prefix `smoke-d73db8518c124b778b9c15cdf37ef25a`.
Cloudflare Jev was unconfigured and **not tested**.

The saved CLI result was `status: emitted_unverified`,
`ingestion_verified: false` (intentional emit-only exit code 2). These fields
remain unchanged. The CLI did not independently verify ClickHouse persistence;
manual UI checks below provide separate evidence.

The authenticated OpenLIT trace view showed **12 spans** associated with this
smoke: four `director.generate` spans, four provider spans, and four smoke spans.
The TypeSafe Jev span showed the resolved response model `jev-1.13.0`.
The UI also showed four correlated `director.smoke.generation` logs, one per
provider. Metrics were verified for baseline and separately filtered Cerebras,
Groq, and TypeSafe Jev: each had request count 1 and generation/provider duration
series; token series were present for hosted providers.

This is one generation per provider. It establishes functional smoke coverage,
**not a latency benchmark**, ranking, or statistically meaningful comparison.

## Real Godot lifecycle smoke

Started **2026-09-22 10:25:24.973 UTC**. Unique run/resource instance ID:
`game-smoke-73bf97725f91419d951b`.

The harness started its own rules-only director on loopback port **18000**.
It used actual Godot state, generation coordination, HTTP generation client,
and HTTP telemetry delivery. The player opened a door and entered the generated
room. It did not modify or stop the user's existing director server.

- Provider/model: `rules-baseline` / `builtin-v1`.
- Committed room: `rules-4da0ffb952`.
- Game lifecycle events delivered: **9**.
- Generated request: `req-game-smoke-73bf97725f91419d951b-1`.
- UI trace view: nine game lifecycle spans plus the director and provider spans.
- Confirmed room-entry trace attribute: `time_to_entry_ms: 6` for that request.
- Persisted logs: nine events — `generation.queued`, `generation.sent`,
  `generation.response_received`, `generation.accepted`, `room.committed`,
  two `frontier.discovered` events, `door.revealed`, and `room.entered`.
- Persisted metrics: `game.lifecycle.events`, `game.lifecycle.duration`
  (selected entry series: 0.006 seconds), `game.rooms.committed` (1),
  `game.room.density` (selected series: 0.023), and director request/duration
  metrics. Values refer to the selected series, not sums across unrelated dimensions.

The saved result was `status: game_delivered_unverified`,
`ingestion_verified: false` (intentional exit code 2). Local delivery and manual
remote inspection are separate checks; the artifact does not claim automated
remote verification.

## Active/shadow comparison smoke

- Run: `comparison-smoke-e979b4ef5f6e4c2caf5b`.
- Comparison: `cmp-15eff2fb6a584dab8d86f0fc9a3bbb04`.
- Local result: HTTP 200, active generation successful, comparison complete,
  two records.
- Returned trace context:
  `00-954bc3fc38e29da2d76be8ebd8bc14f5-ed96d76e81d0c8d2-03`.
- Saved `remote_ingestion_verified` remains **false**.
- Active provider: Groq; shadow provider: rules baseline. This was one additional
  Groq call, separate from the four-provider smoke above.
- Remote trace: `954bc3fc38e29da2d76be8ebd8bc14f5`.
- Confirmed UI spans: seven — two director, two provider, two
  `director.shadow.execution`, and one `director.shadow.comparison`.
- Confirmed UI logs: three — two execution logs and one comparison-completed log.
- Confirmed metrics: `director.shadow.comparisons` and
  `director.shadow.executions` each displayed 1 for the selected series,
  alongside generation metrics. These selected-series values are not claims
  about the total execution count across active/shadow dimensions.

## Manual remote verification checklist

Checks use the authenticated OpenLIT UI and exact run/request/resource instance
identifiers above. Screenshots or query results must match these fresh markers;
an OTLP HTTP success or an old dashboard row alone is insufficient.

| Check | Result |
|---|---|
| Live smoke trace rows and provider model identities | Confirmed in UI: 12 spans |
| Godot lifecycle trace rows and room-entry attributes | Confirmed in UI: 9 lifecycle spans plus director/provider |
| Corresponding persisted logs | Confirmed: four live smoke, nine game lifecycle, three comparison logs |
| Corresponding persisted request/latency/game metrics | Confirmed using exact resource IDs and provider filters |
| Persisted active/shadow comparison evidence | Confirmed: seven spans, three logs, comparison/execution metrics |

OpenLIT 2.1 presents these metric card aggregates with a gauge label. This
record does not infer native instrument types from that UI label; protobuf
tests verify the exported counter/histogram types. The remote UI showed trace
IDs and event fields but no Links tab. Exact trace/span links were verified in
the decoded OTLP integration tests, not independently inspected in the UI.

## Repository validation

- Integrated Python suite: **1231 passed, 10 skipped**.
- The actual headless Godot-to-HTTP-to-OTLP test decoded traces, logs, and metrics
  from a loopback collector. It verified exact request/run correlation and that
  both `room.committed` and `room.entered` linked to the real director span's
  trace ID **and** span ID.
- The exact-link assertion failed before propagation was integrated and passed
  afterward, demonstrating that it detects the original gap.
- A fresh game copy without `.godot` cache passed `make godot-check` and the full
  Godot test suite after explicit sink preloads fixed CI's fresh-import failure.
- Implementation CI was green before merges. Ruff lint/format and Git whitespace
  checks passed.
- All seven [model-view SQL recipes](model-views.md) were validated against an
  isolated ClickHouse 24.4.1 instance using the repository table schema and
  synthetic known results: 100 ms percentile, 0.5 success rate, and one shadow
  winner. The recipes cover all six epic research questions. This was local SQL
  validation, not execution against the remote deployment.

These checks establish local implementation correctness and regression coverage;
they complement the separately completed manual remote persistence checks.

## Evidence and reproduction

The coordinator retained the original JSON artifacts as
`/tmp/openlit-live-final.json`, `/tmp/openlit-game-final.json`, and
`/tmp/openlit-comparison-final.json`. Temporary files are not durable repository
artifacts; this document preserves their non-sensitive identities and outcomes.
Credentials, headers, raw model prompts/responses, and arbitrary environment
variables are deliberately omitted.

See the [operator runbook](openlit-runbook.md) for baseline/live smoke commands,
explicit service identity configuration, actual headless game smoke, and the
separate automated ClickHouse or manual UI verification paths.
