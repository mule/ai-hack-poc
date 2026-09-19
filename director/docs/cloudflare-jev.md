# Cloudflare TypeSafe Jev provider (`cloudflare-jev`)

Implementation notes for issue #8. The adapter lives in
`dungeon_director/cloudflare_jev.py`; offline tests in
`director/tests/test_cloudflare_jev.py`; live tests (skipped without
credentials) in `director/tests/test_cloudflare_jev_live.py`; sanitized
samples in `director/tests/fixtures/jev/`.

## Status and live-integration caveat

The adapter is implemented and contract-tested against the documented API
below. **No live call was made during development** (no Cloudflare
credentials in this environment), so end-to-end callability against a real
account is not yet verified: run the live tests (see below) with credentials
to confirm. Everything else — request shape, answer composition, error
classification, credential hygiene — is covered by offline tests. Do not
claim live acceptance until those tests pass against a real account.

## Verified API (official sources only)

Checked 2026-09-19 against:

- Cloudflare model catalog entry for Jev (unified AI catalog, third-party):
  <https://developers.cloudflare.com/ai/models/typesafe/jev/>
  - model id `typesafe/jev`; "Jev is TypeSafe's structured evaluation model.
    It evaluates one state against typed Noul, Choice, and Score questions
    and returns calibrated answers with probabilities and confidence."
  - its machine-readable input/output JSON Schemas:
    `.../typesafe/jev/schema-input.json`, `.../typesafe/jev/schema-output.json`
- Cloudflare Workers AI REST API (endpoint shape and response envelope):
  <https://developers.cloudflare.com/workers-ai/get-started/rest-api/> and
  <https://developers.cloudflare.com/api/resources/ai/methods/run/>
  - `POST /client/v4/accounts/{account_id}/ai/run`, `Authorization: Bearer
    <token>` (token needs Workers AI Read + Edit), responses wrapped in
    `{"result": ..., "success": true, "errors": [], "messages": []}`
- TypeSafe Jev semantics (question types, answer fields, fan-out, confidence):
  <https://docs.typesafe.ai/primitives.md>,
  <https://docs.typesafe.ai/api.md>,
  <https://docs.typesafe.ai/models.md>
  - Jev is non-generative: typed answers only, no prose, no refusals.
  - Direct TypeSafe API exists too (`POST api.typesafe.ai/v1/systemone`,
    models `jev-latest` / `jev-1.13.0`); this adapter deliberately targets
    the Cloudflare endpoint per issue #8.

### Request (verified assumption)

`POST {CLOUDFLARE_JEV_API_BASE_URL}/accounts/{CLOUDFLARE_ACCOUNT_ID}/ai/run`
with `Authorization: Bearer {CLOUDFLARE_API_TOKEN}` and body:

```json
{
  "model": "typesafe/jev",
  "input": {
    "state": { "...": "string | object | array of JSON values" },
    "questions": {
      "q_id": {
        "type": "noul | choice | score",
        "instructions": "...",
        "criteria": { "opt": "desc" }
      }
    }
  }
}
```

Per the catalog page's curl and schemas: `state` is free-form JSON; each
question needs `type` + `instructions`; `criteria` is required for
`choice` (option → description map) and `score` (ordered level list, ≥ 2
levels) and optional for `noul` (`{"true": ..., "false": ...}`). Many
questions per call is the documented pattern (evaluated in parallel).

### Response (verified assumption)

The catalog page shows the bare Jev payload as the response; the REST API
docs show model output inside the standard v4 envelope. The adapter accepts
both (envelope when present, bare payload otherwise):

```json
{
  "result": {
    "model": "jev-1.13.0",
    "answers": {
      "q1": { "type": "noul", "noul": 0.95 },
      "q2": { "type": "choice", "choice": "x", "confidence": 0.8,
              "probabilities": { "x": 0.87, "y": 0.13 } },
      "q3": { "type": "score", "score": 1.04, "confidence": 0.94,
              "legend": { "0": "...", "1": "..." },
              "probabilities": { "0": 0.0, "1": 0.96 } }
    },
    "usage": { "input_tokens": 426, "output_tokens": 73 }
  },
  "success": true,
  "errors": [],
  "messages": []
}
```

Noul answers carry no `confidence` (the probability is the signal); score
`score` is a number that may fall between levels. The adapter accepts only
`[0, levels-1]` (apart from 1e-9 boundary noise) and rejects other values.

## Decision rubric (one Jev call per room)

| Question id | Type | Decides |
|---|---|---|
| `room_type` | choice | `RoomPlan.room_type` among code-gated eligible types |
| `size` | choice | `RoomPlan.size` |
| `danger` | score (5 levels) | `RoomPlan.danger` (round + 1, clamped 1..5, capped by `options.max_danger`) |
| `enemy_density` | score (5 levels) | `RoomPlan.enemy_density` (score/4, unless `options.target_enemy_density`) |
| `loot_density` | score (5 levels) | `RoomPlan.loot_density` (score/4, unless `options.target_loot_density`) |
| `has_secret` | noul | `secret_probability` = noul; `has_secret` = noul ≥ 0.5 (0 when `options.allow_secrets=false`) |
| `exit_count` | choice 0–3 | number of extra exits; directions assigned deterministically in code |
| `tag_<name>` × 10 | noul | environmental tags with noul ≥ 0.5, contradictions resolved, capped at 8 |

Code-owned gates (mirroring the rules baseline so providers stay
comparable): `entrance`/`stairs_up` never offered; `stairs_down` is preferred
only after 6 rooms on the depth; `vault`/`treasure` are preferred from depth 2;
`forbidden_room_types` are never offered. If the forbidden list leaves only a
normally gated type, pacing is relaxed so the hard caller constraint still
wins. The back-link exit (opposite of `target_exit.direction`,
stairs for vertical frontiers) is always present; extra exits take free
cardinal directions in fixed order; the last extra becomes `secret` kind when
`has_secret`. Non-Jev string fields (`room_id`, `description`) are derived
deterministically in code: Jev returns no prose.

## Error classification

| Upstream event | `ErrorKind` | Notes |
|---|---|---|
| transport timeout (`httpx.TimeoutException`) | `provider_timeout` | service labels `timeout_origin: provider` |
| transport/network failure | `provider_error` | exception type only, no text |
| HTTP 401/403 | `provider_error` | "rejected the credentials" |
| HTTP 429 | `rate_limited` | |
| HTTP 404 | `provider_error` | unknown account or model |
| HTTP 400/422 | `provider_error` | request body rejected |
| HTTP ≥ 500 | `provider_error` | upstream error |
| non-JSON body | `invalid_json` | |
| envelope `success: false` | `provider_error` | error count only, never body text |
| missing/empty answers, wrong answer type, missing answer field | `schema_violation` | |
| choice/exit_count value outside the offered options | `schema_violation` | invalid decision output, never coerced |
| boolean masquerading as a number, NaN/Infinity | `schema_violation` | Python bools are ints; `json.loads` accepts `NaN`/`Infinity` literals — both rejected explicitly |
| score/probability/confidence outside its range | `schema_violation` | only float noise within 1e-9 of a rubric boundary is tolerated |
| probabilities naming options that were never offered | `schema_violation` | |

No upstream body text, error strings, URLs or credentials appear in any
`ProviderError` message; the service additionally discards adapter text
before it reaches the game or logs.

## Metadata

`ProviderResult.provider_metadata` (bounded, JSON-safe) preserves **every**
Jev decision's calibrated signal — confidence, probability distribution, or
noul value — not just the room-level ones:

- `room_type_*`, `size_*`, `exit_count_*` (choices): `*_confidence` +
  `*_probabilities` (the chosen option itself is the corresponding
  `RoomPlan` field)
- `danger_*`, `enemy_density_*`, `loot_density_*` (scores): `*_score` (the
  raw upstream number, since `RoomPlan` holds the transformed value),
  `*_confidence`, `*_probabilities`
- `has_secret_probability` (the raw Jev noul even when secrets are disabled),
  `secret_allowed`, and `tag_probabilities` (all ten tag nouls, selected or not)
- request telemetry: `jev_model` (versioned id from the response),
  `upstream_http_status`, `cf_ray` (response header, when present),
  `adapter_elapsed_ms`, `exit_count` (realized extra exits)

Probabilities/confidences are retained at six decimal places, with values
within 1e-9 of rubric boundaries normalized to the boundary. Keys and option
names are fixed by the rubric, so the blob is structurally inside the
contract's 32-key / 8192-byte budget.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CLOUDFLARE_ACCOUNT_ID` | — | account for `/ai/run` (required; charset-validated before URL interpolation) |
| `CLOUDFLARE_API_TOKEN` | — | Bearer token, Workers AI Read+Edit (required) |
| `CLOUDFLARE_JEV_MODEL` | `typesafe/jev` | model id (must exist in the catalog) |
| `CLOUDFLARE_JEV_API_BASE_URL` | `https://api.cloudflare.com/client/v4` | API root; remote roots require HTTPS, while HTTP is allowed only for loopback tests |

Without both credentials, or when optional Jev configuration is invalid, the
provider registers but reports `available: false` (`/v1/config` shows it;
selection returns 503), and the offline `rules-baseline` default is untouched.
Choosing unavailable Jev as the service default still fails startup. Credentials
are read once at construction, live only in `JevConfig` (`repr`-redacted), and
never reach the registry, `/v1/config`, responses, errors or logs.

## Runtime behaviour

- **One shared HTTP client**: the provider lazily creates a single
  `HttpxJevTransport` (one `httpx.AsyncClient`, connection pooling) and
  reuses it for every call — never a client per request. `provider.aclose()`
  closes an owned client; injected transports (tests) are left to their
  owner.
- **No adapter timeout, no retries**: exactly one HTTP attempt per
  `generate`; the service's deadline cancels the call, and `CancelledError`
  always propagates.

## Running the live tests (requires credentials, makes paid calls)

```sh
export RUN_LIVE_JEV=1 CLOUDFLARE_ACCOUNT_ID=... CLOUDFLARE_API_TOKEN=...
cd director && ../director/.venv/bin/python -m pytest -m live tests/test_cloudflare_jev_live.py -v
```

They skip cleanly (with an explicit reason) unless the opt-in and both
credentials are present, and print no credentials or upstream bodies. The
valid-plan test makes one paid Cloudflare call; the configuration test makes
none.
