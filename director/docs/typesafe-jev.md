# Direct TypeSafe Jev provider (`typesafe-jev`)

The `typesafe-jev` provider calls TypeSafe's System One API directly. It uses
the same room-decision rubric and deterministic `RoomPlan` composition as the
`cloudflare-jev` provider, while using TypeSafe credentials and transport.

## API contract

Verified 2026-09-21 against TypeSafe's official
[quickstart](https://docs.typesafe.ai/introduction/quickstart) and
[HTTP API reference](https://docs.typesafe.ai/api):

```http
POST https://api.typesafe.ai/v1/systemone
Authorization: Bearer <TYPESAFE_API_KEY>
Content-Type: application/json
```

The adapter sends one request per room:

```json
{
  "state": { "...": "bounded dungeon state" },
  "model": "jev-latest",
  "questions": {
    "room_type": {
      "type": "choice",
      "instructions": "...",
      "criteria": { "room": "...", "chamber": "..." }
    }
  }
}
```

The response is a bare Jev payload with `model`, `answers`, and `usage`. The
shared Jev decoder validates every typed answer before code composes the room.
No generated prose or geometry is accepted from the provider.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TYPESAFE_API_KEY` | — | TypeSafe dashboard API key; required |
| `TYPESAFE_JEV_MODEL` | `jev-latest` | Direct TypeSafe model alias or version |
| `TYPESAFE_JEV_API_URL` | `https://api.typesafe.ai/v1/systemone` | Full endpoint; override only for a proxy or local test server |

Set the variables in the shell that starts the director. The service does not
load `.env` by itself:

```sh
export TYPESAFE_API_KEY='...'
export DIRECTOR_DEFAULT_PROVIDER=typesafe-jev
export DIRECTOR_DEFAULT_MODEL=jev-latest
make run-director
```

Alternatively, keep another provider as the default and select direct Jev from
the game:

```sh
export DUNGEON_DIRECTOR_PROVIDER=typesafe-jev
export DUNGEON_DIRECTOR_MODEL=jev-latest
```

The key is stored only in the provider configuration and is excluded from
representations, registry descriptions, API responses, logs, and errors.
Without it, `typesafe-jev` remains registered but reports `available: false`.

## Errors and lifecycle

- HTTP 401/403 becomes `provider_error` without exposing the upstream body.
- HTTP 429 becomes `rate_limited`.
- HTTP 400/422 and 529 become `provider_error`.
- Invalid JSON and malformed typed answers become `invalid_json` and
  `schema_violation` respectively.
- The adapter makes exactly one attempt. The director owns the request deadline
  and cancellation; there are no hidden retries.
- One shared async HTTP client is reused and closed during application shutdown.

## Tests

The normal suite uses an injected transport and never reaches TypeSafe. The
live smoke test is explicit and billable:

```sh
cd director
RUN_LIVE_TYPESAFE_JEV=1 TYPESAFE_API_KEY='...' \
  ../director/.venv/bin/python -m pytest \
  -m live_typesafe_jev tests/test_typesafe_jev_live.py -v
```
