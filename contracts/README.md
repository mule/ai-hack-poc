# Dungeon Director Contracts

Canonical, versioned, provider-independent data contracts shared by the Godot
game (`game/`), the director service (`director/`), and benchmark tooling.
Defined in issue #3 under epic #1.

- **Single source of truth:** `director/dungeon_director/contracts.py`
  (Pydantic v2, Python >= 3.11).
- **Machine-readable schemas:** `contracts/schemas/*.schema.json`
  (JSON Schema draft 2020-12, regenerated from the models).
- **Examples:** `contracts/fixtures/*.json`.
- **Godot reader:** `game/contracts/dungeon_contracts.gd` (standalone, no
  project settings, no provider logic).

## Contract version

Current version: **1.0.0** (`CONTRACT_VERSION` in the Python module and
`DungeonContracts.CONTRACT_VERSION` in GDScript; also stamped on every schema
as `x-contract-version`).

Policy:

- Every request/response envelope **must** carry `contract_version`
  (`MAJOR.MINOR.PATCH`); it is a required field on the wire.
- Consumers accept any version with the **same major** and reject others.
  A wrong or malformed version fails validation with the stable Pydantic
  error type `unsupported_contract_version` (a normal `ValidationError`,
  never an escaping exception).
- Additive optional fields bump PATCH/MINOR only; breaking changes bump MAJOR.

## Envelopes

### `GenerationRequest` (game -> director)

| Field | Type | Notes |
| --- | --- | --- |
| `contract_version` | string | **required**, major must match |
| `request_id` | id | unique per request |
| `run_id` | id | identifies the playthrough |
| `state` | `DungeonState` | committed, player-visible state |
| `target_exit` | `UnresolvedExit` | **required**; must **exactly** match (room_id, direction, since_turn) one entry of `state.unresolved_exits` — identifies the frontier being generated |
| `prompt_hint` | string <= 500 | optional free-text hint |
| `options` | `GenerationOptions` | optional hints (max_danger, forbidden_room_types, allow_secrets, target densities) |

### `GenerationResponse` (director -> game)

| Field | Type | Notes |
| --- | --- | --- |
| `contract_version` | string | **required**, same policy as request |
| `request_id` / `run_id` | id | echo of the request |
| `success` | bool | see failure path below |
| `room` | `RoomPlan` \| null | present iff `success == true` |
| `metadata` | `ResponseMetadata` | telemetry envelope |

`ResponseMetadata` carries `provider`, `model`, `started_at`, `completed_at`
(timezone-aware ISO 8601 with an explicit offset — `Z`, `+02:00`, ...; naive
timestamps fail validation normally on both Python and Godot), optional
`latency_ms`, optional `usage` (`input_tokens` / `output_tokens` /
`estimated_cost_usd`), optional `error`, and `provider_metadata`: the
**only** place provider-specific payloads (finish reasons, raw response ids,
...) may appear — bounded to 32 keys and **8192 serialized UTF-8 bytes**
(compact JSON). All models use `extra="forbid"`, so provider fields leaking
to the top level or into `RoomPlan` fail validation immediately instead of
reaching game logic.

## Core payloads

### `DungeonState` (semantic snapshot)

`depth` (1..128), `turn`, `player` (`hp <= max_hp`, `level`, optional
`hunger`, bounded `conditions`), bounded `recent_rooms` (id + type + danger),
`recent_events`, `inventory`, `unresolved_exits` (committed exits whose
destination is not generated yet), optional `pacing` aggregate
(rooms/secrets/encounters on depth, `average_recent_danger`).

### `RoomPlan` (semantic room decision — never tile geometry)

`room_id`, `depth`, `room_type`, `size` (`tiny`..`huge` relative class),
`danger` (1..5), bounded `exits` (`direction` + `kind` + `locked`),
`enemy_density`, `loot_density`, `secret_probability` (each 0..1), optional
`has_secret` flag, bounded `environmental_tags`, optional flavor
`description`. Exact tile layout is derived deterministically game-side.
Model-enforced invariants: exit directions are unique within a room;
`has_secret == true` requires `secret_probability > 0`; and
`GenerationResponse.success_from_request` rejects a room whose `depth`
differs from `request.state.depth`.

## Failure path (defined and tested)

Invalid or partial provider output never reaches the game as a room:

1. The provider adapter parses raw provider output and validates it against
   `RoomPlan`. Any `ValidationError` (unknown enum, out-of-range density,
   leaked geometry fields, oversized lists, non-JSON garbage, ...) means the
   decision failed.
2. The director answers with `GenerationResponse.failure(...)`
   (`success == false`, `room == null`, `metadata.error` populated) instead of
   a room. The response invariant is enforced by the model itself: a success
   envelope must carry a room and no error; a failure envelope must carry an
   error and no room.
3. `metadata.error.code` is one of the `ErrorKind` machine codes
   (`schema_violation`, `invalid_json`, `unsupported_contract_version`,
   `empty_response`, `provider_error`, `provider_timeout`, `rate_limited`,
   `safety_refusal`, `budget_exceeded`, `internal_error`).
4. The game sees `success == false`, keeps dungeon state unchanged, and falls
   back to the rules baseline provider for that room.

`GenerationResponse.failure(...)` is hardened for arbitrary provider errors
and never fails validation itself; every field is sanitized first:

- `request_id` / `run_id`: invalid values become the sentinels
  `unknown-request-id` / `unknown-run-id`.
- `provider` / `model`: empty or non-string values become `unknown`;
  longer values are truncated to 128 characters.
- `message`: empty becomes `Generation failed (<code>).`; values longer
  than 500 characters are truncated.
- `raw_excerpt`: coerced to string and truncated to 4096 characters
  (`None` stays `None`).
- `code`: unknown values fall back to `provider_error`.
- `provider_metadata`: malformed, >32 keys, or over the 8192-byte
  serialized budget becomes `{}`.
- timestamps: naive values are normalized to UTC, and `completed_at` is
  clamped up to `started_at` when it would precede it.

Failure examples: `fixtures/generation_response_failure.json` and
`fixtures/malformed_provider_response.json` (a deliberately broken raw
provider payload: unknown room type, out-of-range danger, wrong-typed
density, and leaked tile geometry).

## Fixtures

| File | Purpose |
| --- | --- |
| `fixtures/generation_request.json` | well-formed generation request |
| `fixtures/generation_response.json` | successful response (rules baseline) |
| `fixtures/generation_response_failure.json` | canonical failure envelope |
| `fixtures/malformed_provider_response.json` | raw provider output that must be rejected |

Both sides validate the same fixtures: `director/tests/test_contracts.py`
(Pydantic) and `game/tests/test_contracts.gd` (Godot deserializer). Override
the fixtures directory with `DUNGEON_CONTRACTS_DIR` if needed (the Godot test
also probes common relative locations).

## Regenerating schemas

```
python contracts/export_schemas.py        # or pass an output dir
```

`director/tests/test_contracts.py::TestSchemaExport` fails if the committed
schemas drift from the Pydantic models.

## Running the tests

Python (pydantic >= 2 and pytest are enough):

```
python -m pytest director/tests/test_contracts.py
```

Godot (standalone script, no `project.godot` needed):

```
godot --headless --script game/tests/test_contracts.gd
```
