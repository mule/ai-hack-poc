# OpenLIT telemetry schema and correlation contract

Status: **v1 (schema_version `"1"`)** — the versioned contract for issue #22.
Implements the epic in #21 on top of the director instrumentation from #11.

This document is the checked-in reference table for every span, event/log,
metric, attribute, allowed value, and cardinality class that this system
emits or will emit as #23–#28 land. Where a name is already implemented
(director-side, issue #11) this document describes it as-is without
changing it. Where a name is reserved for a later issue, it says so
explicitly — implementing it is that issue's job, not this one's; this
document exists so those implementations agree on names before they're
written.

The only code this issue ships is
[`director/dungeon_director/telemetry_schema.py`](../director/dungeon_director/telemetry_schema.py):
the game-side `GameEvent`/`GameEventBatch` wire contract, the shared
attribute registry, and the metric-dimension guard. It does not modify
[`director/dungeon_director/telemetry.py`](../director/dungeon_director/telemetry.py)
(director-side spans/metrics, owned by #11/#23) and does not implement the
`POST /v1/telemetry/game` bridge endpoint itself (a later task).

## 1. Versioning

* `SCHEMA_VERSION` (`telemetry_schema.py`) = `"1"`. Every `GameEventBatch`
  must carry a matching `schema_version`; a mismatch fails validation so a
  producer/consumer skew is a loud error, not a silently misinterpreted
  field.
* `CONTRACT_VERSION` (`dungeon_director/contracts.py`) = `"1.0.0"` — the
  existing Godot⟷director `GenerationRequest`/`GenerationResponse` contract,
  unrelated to `SCHEMA_VERSION` but sharing the same "reject on major
  mismatch" philosophy.
* **Three independent version axes, not one.** `service.version` (§4) is
  the deployable component's own build/release version — prefer an actual
  build identifier (a package version, a git SHA) when the component has
  one; `CONTRACT_VERSION` is only a fallback for a component with no build
  version of its own. `telemetry.schema.version` (§4,
  `TELEMETRY_SCHEMA_VERSION_ATTRIBUTE`) is `SCHEMA_VERSION` — *this*
  module's event/attribute contract. `CONTRACT_VERSION` on its own is the
  room-plan contract. None of the three imply each other: a build can ship
  with an unchanged room-plan contract but a new telemetry schema, or vice
  versa. Conflating `service.version` with the telemetry schema version
  (an earlier draft of this contract did) means a schema change is
  invisible to a dashboard watching `service.version` alone — hence the
  separate attribute.
* Bumping `SCHEMA_VERSION` (game event names/required fields/attribute
  registry) or `CONTRACT_VERSION` (the room-plan contract) is a breaking
  change: document the migration path in this file's changelog (§10) before
  landing it.

## 2. Span tree

One generation decision, `frontier discovered` through `player enters the
room`, is one correlated journey. Spans below the top line are children;
`⇢` marks a span link (used across an async boundary that cannot be a single
continuous span — see §5).

```
game.frontier_discovered (event, not a span — see §3)
 └─ director.generate                                    [implemented, #11]
     ├─ gen_ai.<operation_name>                           [reserved, #24]
     │   one per physical provider attempt, including retries if added
     └─ director.response.validate                        [reserved, #24]
game.generation.accept | .normalize | .reject | .fallback (events — see §3)
game.room.commit / game.room.enter                        (events — see §3)
```

* **`director.generate`** *(implemented)* — server span, one per
  `POST /v1/generate`. Kind `SERVER`. Attributes in §6. Owns provider
  selection and (today) the provider call as attributes on the same span;
  #24 may split the provider call into a child span (`gen_ai.<operation_name>`
  per [OTel GenAI semantic conventions][genai-semconv], e.g. `gen_ai.chat`)
  without changing `director.generate`'s own attributes or the metrics in
  §4.1.
* **`gen_ai.<operation_name>`** *(reserved for #24)* — one child span per
  physical provider attempt. Must carry `gen_ai.system` (provider id),
  `gen_ai.request.model` / `gen_ai.response.model`, and token/usage
  attributes per the GenAI conventions; must not carry raw prompts or raw
  response bodies (§7).
* **`director.response.validate`** *(reserved for #24)* — schema validation
  of the provider's response against the canonical `RoomPlan` contract.
  Outcome feeds `director.generate`'s `director.schema_valid` /
  `director.status` attributes (already implemented; see §6).
* Godot-side spans for frontier discovery, queueing, acceptance/
  normalization/rejection/fallback, and room commit/entry are **not**
  separate OTel spans from the game process; the game reports them as
  `GameEvent`s (§3) and the bridge (#23, or a dedicated exporter) turns each
  into a span event or log record correlated to the same `run_id`/
  `request_id`/`traceparent`. This keeps the game client free of an
  OpenTelemetry SDK dependency while still producing one correlated journey
  in OpenLIT.

Async boundary rule: whenever the game and the director are not inside one
HTTP request/response (e.g. the game accepts a room some time after the
director responded, or a shadow/replay run happens out of band), use a span
link keyed on `request_id` (or `traceparent` if the game captured one),
never a synthetic parent span.

## 3. Game events (`GameEvent` / `GameEventBatch`)

Wire contract for the future `POST /v1/telemetry/game` bridge, implemented
by [`telemetry_schema.py`](../director/dungeon_director/telemetry_schema.py).

```jsonc
POST /v1/telemetry/game
{
  "schema_version": "1",
  "events": [
    {
      "event_name": "room.committed",
      "run_id": "run-abc123",
      "request_id": "req-def456",
      "timestamp": "2026-09-22T12:00:00Z",
      "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
      "attributes": {"room_id": "room-1", "room_type": "vault", "danger": 3}
    }
  ]
}
```

* `schema_version`: must equal `"1"` (§1).
* `events`: 1–100 per batch (`MAX_EVENTS_PER_BATCH`).
* `event_name`: one of `GameEventName` (below). Unknown values are rejected.
* `run_id` / `request_id`: `BoundedId` (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`,
  same pattern as `dungeon_director.contracts.BoundedId`). `request_id` is
  required except on `frontier.discovered` and `generation.queued`, which
  can happen before a director request exists.
* `timestamp`: timezone-aware ISO 8601 (RFC 3339). Naive timestamps are
  rejected, matching the existing contract's rule for `GenerationRequest`/
  `GenerationResponse`.
* `traceparent`: optional [W3C Trace Context][trace-context] string.
  Attach it whenever the game has an active trace context for the
  generation (e.g. one propagated from the director's response), so the
  bridge can link the game event to `director.generate` instead of relying
  on `run_id`/`request_id` correlation alone. Validated against the full
  spec, not just the `version-traceid-spanid-flags` shape: the reserved
  version `ff` and an all-zero trace-id or parent-id are rejected, matching
  the reference W3C parser (a real tracer never emits any of the three).
* `attributes`: at most 16 keys, allowlisted per `event_name` (§3.2). Any
  other key, or a value of the wrong type/shape/range, fails validation on
  `GameEvent`; `sanitize_attributes()` performs the same check but drops
  instead of raising, for use on untrusted input before constructing the
  model (see the module docstring for why both exist).

### 3.1 Event names

In lifecycle order:

| `event_name` | Meaning | `request_id` |
|---|---|---|
| `frontier.discovered` | Game finds an unresolved exit worth generating | not required |
| `generation.queued` | Game enqueues a generation request | not required |
| `generation.sent` | Game sends the request to the director | required |
| `generation.response_received` | Game receives the director's response | required |
| `generation.accepted` | Game accepts the director's room as-is | required |
| `generation.normalized` | Game accepted the room after adjusting it (e.g. clamped a value) | required |
| `generation.rejected` | Game rejected the room client-side (schema/policy failure) | required |
| `generation.fallback_applied` | Game used its local rules fallback instead of the provider's room | required |
| `room.committed` | The room was written into dungeon state | required |
| `door.revealed` | The room's entrance became visible to the player | required |
| `room.entered` | The player entered the committed room | required |

A rejection is always followed by a `generation.fallback_applied` (the game
still needs a room to commit); a clean accept or normalize goes straight to
`room.committed`.

### 3.2 Attribute registry

Every attribute key this schema knows, its type, and its cardinality class
(§7.2): `low` = safe metric dimension (a small closed set of values).
`correlation` = span/log only, id-shaped, never a metric dimension.
`measurement` = a continuous numeric observation (a duration or a density)
— a histogram *value*, never a dimension either, since a continuous float
cannot be a bounded label.

Every `id` value is additionally checked against the secret/URL guard
below (an id's character class alone doesn't rule out a leaked token —
`sk-live-...` matches `BoundedId` character-for-character); every `string`
value is checked against both the guard and the director's own
`dungeon_director.providers.MODEL_ID_RE` provider/model identifier
grammar (reused rather than redefined so the two never drift apart);
every `float` value must be finite (`math.isfinite`) and is converted
inside a guard that catches `OverflowError` (an out-of-range Python int,
e.g. `10**1000`, cannot become a `float` at all) so a pathological input
is dropped, never raised.

| Key | Type | Cardinality | Allowed values / range |
|---|---|---|---|
| `frontier_id` | id | correlation | `^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$` — game-composed as `<room_id>:<direction>` (e.g. `r-000:east`), so unlike every other id here it allows a colon and runs to 128 chars, not 64 |
| `room_id` | id | correlation | `BoundedId` (`^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$`, no colon) |
| `shadow_comparison_id` | id | correlation | `BoundedId` |
| `replay_id` | id | correlation | `BoundedId` |
| `depth` | int | low | 1–128 |
| `danger` | int | low | 1–5 (matches `RoomPlan.danger`) |
| `exit_direction` | enum | low | `north\|south\|east\|west\|up\|down` (`ExitDirection`) |
| `room_type` | enum | low | `RoomType` values (`entrance`, `room`, `corridor`, ...) |
| `room_size` | enum | low | `RoomSize` values (`tiny`…`huge`) |
| `provider` | string | low | `dungeon_director.providers.MODEL_ID_RE`-shaped (≤128 chars; allows `/`, `:`, `@` — e.g. `typesafe/jev`, `@cf/meta/llama-3.1-8b-instruct`), no secret-shaped text, no URL scheme (`scheme://…`, which is how a credential-bearing URL like `https://user:pass@host` was slipping through an earlier draft) |
| `model` | string | low | same grammar as `provider` |
| `normalize_reason` | enum | low | `danger_clamped\|room_type_forbidden\|exit_conflict\|exit_direction_reassigned\|secret_probability_clamped\|exit_pruned` |
| `reject_reason` | enum | low | `schema_invalid\|policy_violation\|empty_room\|duplicate_room_id\|placement_failure` |
| `fallback_reason` | enum | low | `provider_error\|provider_timeout\|schema_error\|selection_error\|rejected_by_game\|transport_failure` |
| `execution_mode` | enum | low | `active\|shadow\|replay` |
| `exit_count` | int | low | 0–8 (matches `RoomPlan.exits` max length) |
| `has_secret` | bool | low | — |
| `enemy_density` | float | measurement | 0.0–1.0, finite (`UnitFloat`, matches `RoomPlan.enemy_density`) |
| `loot_density` | float | measurement | 0.0–1.0, finite (`UnitFloat`, matches `RoomPlan.loot_density`) |
| `queue_ms` | float | measurement | 0–3,600,000, finite — time the request sat queued before being sent |
| `network_ms` | float | measurement | 0–3,600,000, finite — round trip observed by the game |
| `materialization_ms` | float | measurement | 0–3,600,000, finite — time to write the room into dungeon state |
| `time_to_visible_ms` | float | measurement | 0–3,600,000, finite — commit to reveal |
| `time_to_entry_ms` | float | measurement | 0–3,600,000, finite — reveal (or commit) to player entry |

`duplicate_room_id` and `placement_failure` are blocking failures the game
cannot adjust its way out of, so they reject the room outright (followed by
a fallback); `exit_pruned` is a minor adjustment the game can make while
still accepting the room, so it normalizes instead.

Which keys each `event_name` may carry (`shadow_comparison_id`,
`replay_id`, `execution_mode` are allowed on every event):

| `event_name` | Additional allowed keys |
|---|---|
| `frontier.discovered`, `generation.queued` | `frontier_id`, `depth`, `exit_direction` |
| `generation.sent` | `frontier_id`, `depth`, `exit_direction`, `queue_ms` |
| `generation.response_received` | `provider`, `model`, `network_ms` |
| `generation.accepted` | `provider`, `model`, `room_type`, `room_size`, `danger` |
| `generation.normalized` | `provider`, `model`, `room_type`, `room_size`, `danger`, `normalize_reason` |
| `generation.rejected` | `reject_reason`, `provider`, `model` |
| `generation.fallback_applied` | `fallback_reason`, `provider`, `model` |
| `room.committed` | `room_id`, `frontier_id`, `room_type`, `room_size`, `danger`, `exit_count`, `has_secret`, `enemy_density`, `loot_density`, `materialization_ms`, `provider`, `model` |
| `door.revealed` | `room_id`, `time_to_visible_ms` |
| `room.entered` | `room_id`, `time_to_entry_ms`, `provider`, `model` |

`room.committed` and `room.entered` both carry `provider`/`model` so the
full frontier→entry funnel can be attributed to a model without joining
back to `generation.accepted`.

## 4. Resource attributes

Set once per process/component, not per span:

| Attribute | Value | Notes |
|---|---|---|
| `service.name` | `dungeon-director` (director, unchanged default), `dungeon-director-game-bridge` (the future `/v1/telemetry/game` bridge), `dungeon-director-benchmark` (benchmark/replay tooling) | Existing director default is unchanged; new components get their own name rather than a shared `director.component` key. |
| `service.namespace` | `dungeon-director` | Groups all three under one system in OpenLIT's service map. |
| `service.version` | The component's own build/release version (a package version, a git SHA — operator/CI-set) when it has one; `dungeon_director.contracts.CONTRACT_VERSION` (currently `1.0.0`) only as a fallback for a component with no build version | Standard OTel semantics: the *deployable's* version, not this telemetry contract's. Do not use it as the mechanism for detecting a telemetry schema change — see `telemetry.schema.version` below and §1. |
| `telemetry.schema.version` | `telemetry_schema.SCHEMA_VERSION` (`TELEMETRY_SCHEMA_VERSION_ATTRIBUTE`, currently `"1"`) | Same value on every component that emits `GameEvent`s or director spans under this contract. This is the mechanism behind "dashboards can detect incompatible telemetry" (issue #22 acceptance criteria) — not `service.version`, which tracks something else entirely (§1). |
| `deployment.environment` | operator-set string (e.g. `dev`/`staging`/`prod`) | Not currently sourced from anywhere; a new setting either way (#23). |

## 5. Correlation rules

| ID | Scope | Where it lives | Metric dimension? |
|---|---|---|---|
| `run_id` | One player run/session | `director.run_id` span attr (#11), `GameEvent.run_id` | Never |
| `request_id` | One generation decision | `director.request_id` span attr (#11), `GameEvent.request_id` | Never |
| `traceparent` | W3C trace context for an async boundary | `GameEvent.traceparent` | N/A (not an attribute) |
| `shadow_comparison_id` | Ties an active decision to its shadow counterpart on the same state (#13, #26) | span/log attribute, `GameEvent.attributes.shadow_comparison_id` | Never |
| `replay_id` | Ties a replay/evaluation run back to the original decision (#26) | span/log attribute, `GameEvent.attributes.replay_id` | Never |
| `frontier_id` | The unresolved exit being generated for | `GameEvent.attributes.frontier_id` | Never |
| `room_id` | The committed room | `GameEvent.attributes.room_id` | Never |

Rule: correlation IDs are exactly the set above, and they are the *only*
attributes exempt from cardinality bounds — they are deliberately
high-cardinality, and belong on spans and logs, never on a metric.
`telemetry_schema.assert_safe_metric_dimensions()` enforces this
mechanically (§7.2).

## 6. Director attributes and metrics (issue #11, implemented, unchanged)

Documented here for completeness and for the #11 compatibility statement in
§9; the source of truth remains
[`telemetry.py`](../director/dungeon_director/telemetry.py).

**`director.generate` span attributes:** `director.request_id`,
`director.run_id`, `director.depth`, `director.is_shadow`,
`director.execution_mode`, `director.retry_count`, `director.status`,
`director.provider`, `director.model`, `director.latency_ms`,
`director.provider_latency_ms`, `director.error_code`,
`director.http_status`, `director.schema_valid`, `director.timeout_origin`,
`director.selection_error`, `director.room.*` (type/size/danger/exit_count/
has_secret/secret_probability/enemy_density/loot_density),
`gen_ai.system`, `gen_ai.request.model`, `gen_ai.response.model`,
`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`,
`gen_ai.usage.total_tokens`, `gen_ai.usage.cost`.

**Metrics** (`director.generation.requests` counter,
`director.generation.duration` / `director.provider.duration` histograms,
`director.generation.tokens` / `director.generation.cost` counters):
dimensions are exactly `METRIC_DIMENSIONS` = `{provider, model, status,
error_code, execution_mode, token_type}` — `token_type` only on the token
counter. This frozenset is imported (read-only) by
`telemetry_schema.assert_safe_metric_dimensions()` so the game- and
director-side dimension allowlists are checked for drift by one test
(`tests/test_telemetry_schema.py::TestMetricDimensionGuard`) instead of two
independently-maintained lists.

**`Outcome`** (the `status` dimension): `success`, `selection_error`,
`provider_error`, `timeout`, `schema_error`, `invalid_request`, `cancelled`,
`internal_error`.

### 6.1 Reserved game-side metrics (#27)

Not implemented by this issue; names reserved so #27's dashboards and
whatever aggregates `GameEvent`s into metrics agree on them:

* `game.room.commits` (counter; dimensions ⊆ `GAME_METRIC_DIMENSIONS`)
* `game.room.time_to_entry` (histogram, ms; same dimension rule)
* `game.generation.fallback_rate` (derived from `generation.fallback_applied`
  events; dimension `fallback_reason` plus `provider`/`model`)

## 7. Metadata-only vs. structured capture, and metric safety

### 7.1 Capture modes

* **Metadata-only (default).** Only the fields in §3.2/§6 are ever
  recorded: bounded scalars, enums, and ids. No canonical `DungeonState` or
  `RoomPlan` content is captured.
* **Structured (explicit opt-in, not implemented by this issue).** A future
  flag (proposed: `DIRECTOR_OTEL_CAPTURE_ROOM_PLAN=1`) may allow recording a
  redacted, size-bounded `RoomPlan` (room-level fields already in §6's
  `director.room.*` list — never `description` free text, never the
  player's `DungeonState`) as a span event, capped at the same
  `MAX_PROVIDER_METADATA_JSON_BYTES` (8192 bytes) the contract already uses
  for `provider_metadata`. Implementing this capture path is a later
  task; this section fixes the bound and the opt-in-only default so that
  implementation has no room to make structured capture the default.

### 7.2 Metric dimension safety

Three disjoint sets, enforced by `telemetry_schema.assert_safe_metric_dimensions()`:

* Safe (`GAME_METRIC_DIMENSIONS` ∪ director's `METRIC_DIMENSIONS`):
  `provider`, `model`, `status`, `error_code`, `execution_mode`,
  `token_type`, `depth`, `danger`, `exit_direction`, `room_type`,
  `room_size`, `exit_count`, `has_secret`, `normalize_reason`,
  `reject_reason`, `fallback_reason`.
* Banned, correlation-only (`CORRELATION_ONLY_KEYS`): `run_id`,
  `request_id`, `frontier_id`, `room_id`, `shadow_comparison_id`,
  `replay_id` — id-shaped, high-cardinality by construction.
* Banned, measurement-only (`MEASUREMENT_ONLY_KEYS`): `queue_ms`,
  `network_ms`, `materialization_ms`, `time_to_visible_ms`,
  `time_to_entry_ms`, `enemy_density`, `loot_density` — continuous numeric
  observations. These are what a histogram *records*, never a label a
  metric is grouped by; a duration or a density used as a dimension key
  would mint an unbounded number of time series, one per distinct value.
  Record them as histogram observations (`game.room.time_to_entry`, etc.,
  §6.1) instead.

As a blanket rule beyond the three sets above: exception text, URLs (which
can embed credentials), raw prompts, and raw provider payloads must never
be a metric dimension *or* a span/log attribute, named key or not.
`telemetry.py` already enforces this for the director side (`_never_raises`,
machine-readable `error_code` only, never a message); `telemetry_schema.py`'s
`_SECRET_LIKE_RE` guard and the label/length bounds on string attributes
enforce the same for the game side.

## 8. Trace/link shape by outcome

All eight rows correlate on `run_id` + `request_id` (+ `traceparent` where
present). "Game events" lists the `GameEvent`s a well-behaved game client
emits for that outcome, in order.

Game event sequences below omit `door.revealed` → `room.entered`, which
follows `room.committed` identically in every row (see §3.1).

| Outcome | `director.generate` | Game events |
|---|---|---|
| Success | `status=success`, `director.schema_valid=true` | `generation.queued` → `generation.sent` → `generation.response_received` → `generation.accepted` → `room.committed` → ... |
| Provider error | `status=provider_error`, `error_code=<ErrorKind>` | `generation.queued` → `generation.sent` → `generation.response_received` → `generation.fallback_applied{fallback_reason=provider_error}` → `room.committed` → ... |
| Timeout | `status=timeout`, `director.timeout_origin` set | `generation.queued` → `generation.sent` → `generation.fallback_applied{fallback_reason=provider_timeout}` → `room.committed` → ... (no `generation.response_received`: nothing came back) |
| Schema failure | `status=schema_error`, `director.schema_valid=false` | `generation.queued` → `generation.sent` → `generation.response_received` → `generation.fallback_applied{fallback_reason=schema_error}` → `room.committed` → ... |
| Game rejection | `status=success` (director's view; the room was schema-valid) | `generation.queued` → `generation.sent` → `generation.response_received` → `generation.accepted` → `generation.rejected{reject_reason=policy_violation}` → `generation.fallback_applied{fallback_reason=rejected_by_game}` → `room.committed` → ... |
| Transport failure (request never reached the director, e.g. connection refused) | no `director.generate` span for this decision | `generation.queued` → `generation.sent` → `generation.fallback_applied{fallback_reason=transport_failure}` → `room.committed` → ... |
| Local fallback (game chose not to call the director, e.g. offline) | no `director.generate` span for this decision | `generation.queued` → `generation.fallback_applied{fallback_reason=selection_error}` → `room.committed` → ... |
| Active/shadow comparison | two `director.generate` spans, `execution_mode=active` and `execution_mode=shadow`, same `request_id`, distinguished by `director.is_shadow` | active side only emits game events (§3); the shadow decision never reaches the game (shadow isolation, #13) |
| Replay | new `director.generate` span with a fresh `request_id`, correlated to the original via `replay_id` in span/log attributes | none — replay is offline evaluation, not gameplay |

## 9. Compatibility with issue #11

Every `director.*` and `gen_ai.*` attribute and every metric name/dimension
from #11 is unchanged by this document (§6) — this issue only *adds* the
game-side `GameEvent` contract and the shared dimension guard. No migration
is required for existing #11 telemetry; it remains queryable exactly as
before. `service.version` (§4) is new but additive (an unset resource
attribute defaults to absent in OpenLIT, not an error).

## 10. Worked examples

Provider ids from `dungeon_director/{rules,groq,cerebras,typesafe_jev,cloudflare_jev}.py`:
`rules-baseline`, `groq` (the "Grok" comparison target named in #21 is Groq),
`cerebras`, `typesafe-jev` (direct), `cloudflare-jev` (Cloudflare-hosted).

**Rules baseline, success:**
`director.generate{status=success, provider=rules-baseline, model=<rules model id>, gen_ai.usage.*=<absent, rules reports no tokens>}`
→ `generation.accepted{provider=rules-baseline}` → `room.committed` → `room.entered`.

**Groq, schema failure then fallback:**
`director.generate{status=schema_error, provider=groq, error_code=schema_violation, director.schema_valid=false}`
→ `generation.fallback_applied{fallback_reason=schema_error, provider=groq}` → `room.committed{room_type=<rules-chosen>}` → `room.entered`.

**Cerebras, success with usage:**
`director.generate{status=success, provider=cerebras, gen_ai.usage.input_tokens=…, gen_ai.usage.output_tokens=…, gen_ai.usage.cost=…}`
→ `generation.accepted{provider=cerebras, room_type=vault, danger=3}` → `room.committed` → `room.entered{time_to_entry_ms=4200}`.

**TypeSafe Jev, provider timeout:**
`director.generate{status=timeout, provider=typesafe-jev, director.timeout_origin=provider}`
→ `generation.fallback_applied{fallback_reason=provider_timeout, provider=typesafe-jev}` → `room.committed` → `room.entered`.

## 11. Changelog

* **v1 (`schema_version="1"`, this document)** — initial versioned contract:
  `GameEvent`/`GameEventBatch`, the shared attribute registry, resource
  attribute names, and the metric-dimension guard. Includes a review pass
  that added `generation.sent`, `generation.response_received`, and
  `door.revealed`; introduced the `measurement` cardinality class so
  durations and densities can never become metric dimensions;
  special-cased `frontier_id`'s colon-bearing shape; added
  `duplicate_room_id`/`placement_failure`/`exit_pruned`/`transport_failure`
  reason codes; and added `provider`/`model` to `room.committed` and
  `room.entered` for funnel attribution. No `schema_version` bump was
  needed — v1 had not shipped to a producer yet.
* **v1, security fixes (same `schema_version="1"`)** — a code review of PR
  #29 found four defects, all fixed in the same PR before merge, no
  `SCHEMA_VERSION` bump needed (attribute shapes got stricter, not
  incompatible): (1) `provider`/`model` used a permissive label pattern
  that a full URL with embedded credentials
  (`https://user:secret@example.com/path`) matched character-for-character
  — fixed by switching to the director's `MODEL_ID_RE` grammar plus an
  explicit ban on a URL scheme (`scheme://`), without banning the bare
  `/`/`:`/`@` real model ids use; (2) a secret-shaped `room_id` (e.g.
  `sk-live-...`) passed because an id's character class alone doesn't rule
  out a leaked token — fixed by running the same secret-shape guard on
  every `id`-typed value, not just `string`; (3) `enemy_density=NaN`
  passed because every comparison against `NaN` is `False`, so the old
  range check silently accepted it — fixed with an explicit
  `math.isfinite` check before the range check; (4)
  `sanitize_attributes(..., {"enemy_density": 10**1000})` raised
  `OverflowError` from `float()`, breaking the documented never-raises
  contract — fixed by catching it and dropping the value like any other
  invalid input. Also fixed: the `traceparent` regex accepted an all-zero
  trace-id, an all-zero parent-id, and the reserved version `ff`, all
  invalid per the W3C Trace Context spec — `_valid_traceparent()` now
  checks the three semantic bans the reference parser enforces, not just
  the shape. And `service.version` was corrected to mean the deployable's
  own build/release version (§1/§4), with a new, separate
  `telemetry.schema.version` resource attribute
  (`TELEMETRY_SCHEMA_VERSION_ATTRIBUTE`) carrying `SCHEMA_VERSION` —
  `service.version` alone cannot detect a telemetry schema change if the
  build itself didn't change.

[genai-semconv]: https://opentelemetry.io/docs/specs/semconv/gen-ai/
[trace-context]: https://www.w3.org/TR/trace-context/
