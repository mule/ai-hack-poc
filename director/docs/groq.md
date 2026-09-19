# Groq GPT-OSS provider (`groq`)

Implementation notes and operator guide for issue #9. The adapter lives in
`dungeon_director/groq.py`; offline tests in `director/tests/test_groq.py` and
`test_groq_live_gating.py`; paid live tests (skipped by default) in
`director/tests/test_groq_live.py`.

Groq is the *generative* comparison point next to Jev
(`cloudflare-jev.md`): one strict-JSON-schema chat completion returns a whole
`RoomPlan`; the game still derives all geometry.

## Status and live-integration caveat

The adapter is implemented and contract-tested against the documented API
below. **No live call was made during development** (no Groq credentials in
this environment), so callability against the real service is not yet
verified. What *is* verified offline: the request shape, strict-schema
compatibility rules, error mapping, credential hygiene, cancellation and
timeout behaviour, and the live tests themselves (they pass against a
faithful local fake Groq, see `test_groq_live_gating.py`). Run the live tests
(below) with a key before claiming live acceptance.

Response bodies in the offline tests follow the OpenAI-compatible
chat-completion shape Groq documents; two details are *not* confirmed by the
docs and are treated as optional: `usage.completion_tokens_details.reasoning_tokens`
and the exact `finish_reason` values. Both are read defensively and simply
absent when missing.

## Verified API (official sources only)

Checked 2026-09-19 against Groq's documentation:

- Structured outputs: <https://console.groq.com/docs/structured-outputs>
  - `response_format = {"type": "json_schema", "json_schema": {"name": ...,
    "strict": true, "schema": ...}}`; strict (constrained-decoding) mode is
    supported on `openai/gpt-oss-20b`, `openai/gpt-oss-120b` and
    `qwen/qwen3.8-27b`.
  - Strict mode: every field must be `required`, every object must set
    `additionalProperties: false`, optional values are nullable unions
    (`["string", "null"]`) that stay `required`; nested `anyOf` members follow
    the same rules. Streaming and tool use are not supported with structured
    outputs.
  - The docs do **not** list unsupported JSON Schema keywords, schema size
    limits or truncation/`finish_reason` behaviour. The schema therefore uses
    no range, length, pattern, format, `$ref` or size keywords at all (a test
    enforces this); those bounds are checked by `RoomPlan` in the service.
- Reasoning: <https://console.groq.com/docs/reasoning>
  - `openai/gpt-oss-20b` / `-120b` accept `reasoning_effort` of **`low`,
    `medium` or `high` only**. `none` (Qwen) and `minimal` are not accepted for
    GPT-OSS, so reasoning cannot be disabled there, only minimized.
  - `include_reasoning: false` excludes the reasoning text from the response
    (GPT-OSS only). `reasoning_format` is unsupported on GPT-OSS and mutually
    exclusive with `include_reasoning`, so it is never sent.
- Chat completions reference: <https://console.groq.com/docs/api-reference>
  - `POST https://api.groq.com/openai/v1/chat/completions`, `Authorization:
    Bearer <key>`; `max_completion_tokens` (`max_tokens` is deprecated),
    `seed` (best-effort determinism), `temperature`, `stream`.
  - `usage`: `prompt_tokens`, `completion_tokens`, `total_tokens` and timing in
    seconds: `queue_time`, `prompt_time`, `completion_time`, `total_time`.
    `x_groq.id` is the request id (`req_...`).
- Errors: <https://console.groq.com/docs/errors> (200, 400, 401, 403, 404, 413,
  422, 424, 429, 498 flex-tier capacity exceeded, 499 cancelled, 500, 502, 503;
  body `{"error": {"message": ..., "type": ...}}`). No `error.code` or
  `Retry-After` handling is documented, and none is relied on.
- Models: <https://console.groq.com/docs/models>: `openai/gpt-oss-20b`,
  131,072-token context, 65,536 max completion tokens.

## What the adapter sends

One request per `generate`, no retries:

```json
{
  "model": "openai/gpt-oss-20b",
  "messages": [{"role": "system", "content": "<fixed rules>"},
               {"role": "user", "content": "<compact JSON state>"}],
  "temperature": 0,
  "seed": 1234567890,
  "max_completion_tokens": 2048,
  "stream": false,
  "reasoning_effort": "low",
  "include_reasoning": false,
  "response_format": {"type": "json_schema",
                      "json_schema": {"name": "room_plan", "strict": true, "schema": {...}}}
}
```

- **Determinism.** The system prompt is a constant; the user message is
  `json.dumps(state, sort_keys=True, separators=(",", ":"))` of a bounded
  slice of the request (last 6 rooms, 5 events, 12 inventory items). The seed
  and the model-visible `room_id` (`groq-<10 hex>`) derive from
  `sha256(run_id:request_id:model)`. Identical requests produce byte-identical
  bodies (tested); Groq documents `seed` as best effort, so identical *outputs*
  are likely but not guaranteed.
- **Prompt size.** About 1,100 characters of rules plus the state: the sample
  request in the contract fixtures is roughly 1,900 characters in total, and a
  maximal contract-legal request stays under 6,000 (both bounds are tested).
- **Reasoning.** `reasoning_effort: low` is the floor for GPT-OSS. The
  `include_reasoning: false` flag is sent only to the GPT-OSS family (Groq
  documents it only there). For other models the configured effort is sent
  as-is and Groq's API is the judge; only GPT-OSS is a supported target.
- **Schema.** `ROOM_PLAN_SCHEMA` mirrors every `RoomPlan` field with the
  contract enums (a test fails if the contract drifts). Optional fields
  (`has_secret`, `description`) are required nullable unions.

## What comes back, and failure semantics

The adapter returns the model's JSON text **verbatim** as the payload and never
repairs it. The service validates it against `RoomPlan`, so:

| Situation | Recorded as | HTTP |
|-----------|-------------|------|
| Truncated / non-JSON text (`finish_reason: length`) | `invalid_json` + `raw_excerpt` | 502 |
| Out-of-range value, wrong `depth`, duplicate exits, `has_secret` with probability 0, unknown field | `schema_violation` + `raw_excerpt` | 502 |
| Empty content (typically reasoning ate the whole token cap) | `empty_response` | 502 |
| `message.refusal` present | `safety_refusal` | 502 |
| HTTP 429 or 498 | `rate_limited` | 429 |
| Other non-200 (400/401/403/404/413/422/5xx, redirects) | `provider_error` | 502 |
| Transport or socket timeout | `provider_timeout` | 504 |
| Director deadline (`DIRECTOR_TIMEOUT_SECONDS`) | `provider_timeout`, `timeout_origin: director_deadline` | 504 |

For the first three rows (the model answered, but its content was unusable)
the failure envelope keeps the response's token usage and the metadata below,
so failed calls still count in cost and latency comparisons. The other rows are
raised by the adapter before any telemetry is parsed (refusals, malformed
choice shapes, HTTP errors, timeouts) and carry none. No failure carries
upstream body text, URLs or credentials.

Cancellation and timeouts: the service's deadline cancels the awaiting task,
which aborts the in-flight `httpx` request (the client has no timeout of its
own and never follows redirects). A timeout is not retried.

### Telemetry

`usage.input_tokens` / `output_tokens` come from `prompt_tokens` /
`completion_tokens`; `estimated_cost_usd` is left empty (no invented pricing).
`provider_metadata` (bounded, JSON-safe, fields absent when Groq omits them):

| Key | Meaning |
|-----|---------|
| `queue_time_s`, `prompt_time_s`, `completion_time_s`, `total_time_s` | Groq-reported timing, seconds |
| `prompt_tokens`, `completion_tokens`, `total_tokens`, `reasoning_tokens` | Groq-reported counts |
| `finish_reason`, `response_chars` | how generation ended; content length |
| `groq_request_id`, `groq_model`, `service_tier`, `system_fingerprint` | Groq identity fields |
| `reasoning_effort`, `max_completion_tokens`, `seed`, `strict_schema` | what was requested |
| `upstream_http_status`, `adapter_elapsed_ms` | adapter-side wall time for the call |

Reasoning text is never stored. The director-side `latency_ms` covers the whole
request; `adapter_elapsed_ms` isolates the Groq call.

## Configuration

Read once from the process environment (the director does not load `.env`).

| Variable | Default | Notes |
|----------|---------|-------|
| `GROQ_API_KEY` | none | Required. Printable ASCII, no spaces. Never logged, echoed or exposed by `/v1/config`. |
| `GROQ_MODEL` | `openai/gpt-oss-20b` | Any Groq model id; strict output needs one that supports it. |
| `GROQ_REASONING_EFFORT` | `low` | GPT-OSS: `low`, `medium`, `high`. Other models: any value in Groq's API reference. |
| `GROQ_MAX_COMPLETION_TOKENS` | `2048` | 64-65536. Groq's docs do not say whether reasoning tokens count against it; assume they do and leave headroom. |
| `GROQ_API_BASE_URL` | `https://api.groq.com/openai/v1` | HTTPS, or `http` only for loopback test servers; no credentials, query or whitespace. |

Blank means default. **Malformed optional configuration never stops the
service:** the registry logs one warning naming only the provider and the
exception type (never values), registers `groq` as `available: false`, and the
rules baseline (and Jev) start normally. Selecting `groq` then fails as
`provider_unavailable` (HTTP 503). Setting `DIRECTOR_DEFAULT_PROVIDER=groq`
without a valid key is an explicit choice and stops startup.

```
export GROQ_API_KEY=...            # server side only
export DIRECTOR_DEFAULT_PROVIDER=groq   # optional: otherwise select per request
curl -X POST 'http://127.0.0.1:8000/v1/generate?provider=groq' -d @request.json
```

## Live tests (paid)

`director/tests/test_groq_live.py` makes real calls and runs only when **both**
`RUN_LIVE_GROQ=1` (exactly `1`) and `GROQ_API_KEY` are set:

```
export RUN_LIVE_GROQ=1 GROQ_API_KEY=...
cd director && python -m pytest -m live tests/test_groq_live.py -v
```

Four tests: configuration detection; a schema-valid room with token and timing
telemetry; hard options honoured (`max_danger`, forbidden types,
`allow_secrets=false`); a rejected key surfacing as a sanitized
`provider_error`. Expect three short completions (roughly 1,000 prompt tokens
each on `openai/gpt-oss-20b`) plus one unbilled 401.

Ordinary runs never touch the network, even with credentials exported:
`tests/conftest.py` removes every `GROQ_*` variable and refuses non-loopback
DNS and sockets for all non-`live` tests (and fails the test if anything
tried), and `test_groq_live_gating.py` proves with a loopback tripwire that a
key alone, wrong opt-in values, or a missing key never dial out.

## Known limitations

- Strict decoding guarantees shape and enums, not values or pacing. Hard
  options (`max_danger`, `forbidden_room_types`, `allow_secrets`), the
  back-link exit and pacing gates are stated in the prompt but only the
  `RoomPlan` contract is enforced by the service; violations of the rest show
  up as benchmark quality differences (the live test asserts them), not as
  failures.
- Groq's docs do not state schema size limits or truncation behaviour; the
  default `max_completion_tokens` is sized to leave headroom for `low` reasoning (unmeasured without a live key), and a cap
  that is too small yields `invalid_json` or `empty_response`, recorded rather
  than repaired.
- No cost estimate is produced (Groq's per-token prices are not fetched).
- Models other than the GPT-OSS family are best-effort: the API, not the
  adapter, decides whether they support strict output or a given effort.
