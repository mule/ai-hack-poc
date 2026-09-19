# Cerebras Qwen provider (`cerebras`)

Implementation notes for issue #10. The adapter lives in
`dungeon_director/cerebras.py`; offline tests in
`director/tests/test_cerebras.py`; live tests (skipped without credentials) in
`director/tests/test_cerebras_live.py`; sanitized samples in
`director/tests/fixtures/cerebras/`.

## Status and live-integration caveat

The adapter is implemented and contract-tested against the documented API
below. **No live call was made during development** (no Cerebras credentials in
this environment), so end-to-end callability against a real account is verified
via live tests: run the live tests (see below) with credentials to confirm.
Everything else — request shape, structured output schema, error classification,
credential hygiene — is covered by offline tests.

## Verified API (official sources)

Cerebras Inference offers an OpenAI-compatible Chat Completions endpoint with
support for strict Structured Outputs via JSON Schema.

- **Endpoint**: `POST {CEREBRAS_API_BASE_URL}/chat/completions` (default `https://api.cerebras.ai/v1/chat/completions`)
- **Headers**: `Authorization: Bearer <CEREBRAS_API_KEY>`, `Content-Type: application/json`
- **Model**: Explicitly configured via `CEREBRAS_MODEL` (default: `qwen-3.8-27b`) so the deployment can follow Cerebras model availability without code changes.
- **Structured Output**: `response_format` is passed as:
  ```json
  {
    "type": "json_schema",
    "json_schema": {
      "name": "room_plan",
      "strict": true,
      "schema": { ... }
    }
  }
  ```
- **Reasoning control**: To minimize latency and token usage on models with reasoning enabled, `reasoning_effort: "none"` is passed.

### Request Body

```json
{
  "model": "qwen-3.8-27b",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "response_format": {
    "type": "json_schema",
    "json_schema": {
      "name": "room_plan",
      "strict": true,
      "schema": { ... }
    }
  },
  "reasoning_effort": "none",
  "max_completion_tokens": 512,
  "temperature": 0,
  "seed": 1
}
```

### Response Envelope

Standard OpenAI-compatible chat completion payload:

```json
{
  "id": "chatcmpl-...",
  "choices": [
    {
      "finish_reason": "stop",
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "{\"room_id\": \"cerebras-...\", ...}"
      }
    }
  ],
  "usage": {
    "prompt_tokens": 412,
    "completion_tokens": 85,
    "total_tokens": 497
  },
  "time_info": {
    "queue_time": 0.0012,
    "prompt_time": 0.0045,
    "completion_time": 0.0421,
    "total_time": 0.0478
  }
}
```

## Error classification

| Upstream event | `ErrorKind` | Notes |
|---|---|---|
| transport timeout (`httpx.TimeoutException`) | `provider_timeout` | service labels `timeout_origin: provider` |
| transport/network failure | `provider_error` | exception type only, no text |
| HTTP 401/403 | `provider_error` | "rejected the credentials" |
| HTTP 429 | `rate_limited` | rate limited |
| HTTP 404 | `provider_error` | unknown endpoint or model |
| HTTP 400/422 | `provider_error` | request body rejected |
| HTTP ≥ 500 | `provider_error` | upstream error |
| non-JSON body | `invalid_json` | |
| empty choices or content | `empty_response` | |
| response content fails `RoomPlan` validation | `schema_violation` | |

No upstream body text, error strings, URLs or credentials appear in any
`ProviderError` message; the service additionally discards adapter text before
it reaches the game or logs.

## Metadata

`ProviderResult.provider_metadata` captures:
- `cerebras_id` (completion ID)
- `cerebras_model` (model string from response)
- `finish_reason` (e.g. `stop`)
- `prompt_tokens`, `completion_tokens`
- `upstream_http_status` (200)
- `adapter_elapsed_ms`
- `time_*` keys from `time_info` (e.g. `time_queue_time`, `time_completion_time`)

Keys and byte budget stay well within the contract's 32-key / 8192-byte limit.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CEREBRAS_API_KEY` | — | Cerebras API key (Bearer token, required) |
| `CEREBRAS_MODEL` | `qwen-3.8-27b` | Explicit model ID; configurable as Cerebras offerings change |
| `CEREBRAS_API_BASE_URL` | `https://api.cerebras.ai/v1` | Base URL; HTTPS required except for loopback tests |

Without `CEREBRAS_API_KEY`, the provider is registered but reports
`available: false`. Missing credentials never take down the default offline
service. Credentials stay inside the adapter: never in `/v1/config`, responses,
errors, logs or test output.

## Running the live tests (requires credentials, makes paid calls)

```sh
export RUN_LIVE_CEREBRAS=1 CEREBRAS_API_KEY=...
cd director && ../director/.venv/bin/python -m pytest -m live_cerebras tests/test_cerebras_live.py -v
```
