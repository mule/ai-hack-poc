# Shadow evaluation (A/B comparison mode)

Implementation notes and operator guide for issue #13. Code:
`dungeon_director/shadow.py` (fanout, records, store, observer interface),
`settings.py` (`ShadowSettings`), `service.py` (the launch point) and `app.py`
(lifecycle, header, config view). Tests: `tests/test_shadow.py`,
`test_shadow_settings.py`, `test_shadow_api.py`.

## What it does

One **active** provider answers the game, exactly as before. Zero or more
configured **shadow** targets receive the same validated `GenerationRequest` at
the same moment, run under the same service policy (timeout, validation,
failure conversion, no retries), and have their raw canonical outcome recorded
for later comparison. Nothing a shadow does can reach the game.

```
POST /v1/generate ──▶ select active ──┬──▶ active provider ──▶ response to the game
                                      │        (records its outcome, linked by comparison_id)
                                      ├──▶ shadow A  (background task, own copy of the request)
                                      └──▶ shadow B  (background task, own copy of the request)
                                               └─▶ ExecutionRecord ─▶ observers / ShadowStore
```

Shadow mode is **off** unless `DIRECTOR_SHADOW_TARGETS` names at least one
valid target. Off means no comparison ids, no tasks and no header: the service
behaves exactly as it did before shadow mode existed.

## Configuration

Read from the process environment (the director does not load `.env` itself).
All optional; blank counts as unset.

| Variable | Meaning | Default |
|----------|---------|---------|
| `DIRECTOR_SHADOW_TARGETS` | Comma-separated `provider` or `provider:model` entries, at most 4. A bare provider uses that provider's own default model (not `DIRECTOR_DEFAULT_MODEL`, which only applies to the active provider). Only the *first* colon separates provider from model, so `groq:openai/gpt-oss-20b` and `local:llama3:8b` work. | off |
| `DIRECTOR_SHADOW_TIMEOUT_SECONDS` | Per-shadow-call deadline, > 0 and <= 300. Independent of the active deadline. | `DIRECTOR_TIMEOUT_SECONDS` |
| `DIRECTOR_SHADOW_MAX_IN_FLIGHT` | Shadow calls allowed to run at once (1-64). Beyond it new shadow runs are **skipped**, not queued. | `8` |
| `DIRECTOR_SHADOW_STORE_SIZE` | Comparisons kept in the in-memory store (1-1024). Oldest are evicted first. | `128` |
| `DIRECTOR_SHADOW_DRAIN_SECONDS` | On shutdown, how long running shadow calls may finish before they are cancelled (0-60). | `5` |

Example: `DIRECTOR_DEFAULT_PROVIDER=groq` with
`DIRECTOR_SHADOW_TARGETS=cerebras,cloudflare-jev` compares Groq (active) with
Cerebras and Jev on every request the game makes. Shadow targets cost real
upstream calls, one per target per request.

A shadow target may equal the active target (useful to measure a model's own
run-to-run variance). Shadow targets are fixed by configuration; they apply
whichever provider the active side resolves to, including one chosen per
request with `?provider=`.

### Malformed or unavailable configuration degrades, never stops startup

Unlike the core settings, shadow settings never raise:

- a malformed, duplicate or over-limit target entry is dropped and only
  *counted* (`rejected_config_entries`); a warning says how many, never which;
- a bad number falls back to its default with a warning that names the variable
  but not the value;
- a target naming an unregistered provider or model, or a provider that is
  registered but unavailable (for example no API key), stays configured and is
  recorded as `skipped` with a `skip_reason` on every request. It is warned
  about once. The active path is unaffected.

The values of dropped or unregistered entries are never logged or returned:
an operator who pasted a credential into the wrong variable would otherwise
see it echoed. `GET /v1/config` lists only *registered* targets.

## Behavioural guarantees

- **Identical input.** Each shadow gets its own deep copy of the request, made
  synchronously *before* the active provider is awaited. A shadow that mutates
  its input cannot change what the active provider or another shadow sees.
- **Active-only effect.** Only the active outcome is returned. The response body
  is identical to a run without shadows.
- **Concurrent, never blocking.** Shadows are separate background tasks started
  before the active call is awaited. The active path pays only for a
  synchronous launch step; a slow shadow cannot delay the answer.
- **Shadows start only for a real active call.** If the active selection fails
  (unknown provider/model, unavailable) no shadow runs, so typos in a query
  string cannot spend money.
- **Failure isolation.** A shadow that times out, fails, returns garbage,
  raises, crashes or is cancelled becomes a record; nothing propagates. Launch
  errors, observer errors and store errors are caught and logged by exception
  *type* only. Task exceptions are always retrieved, so asyncio never reports
  an unretrieved one.
- **Client cancellation.** If the active request is cancelled (client
  disconnect, server cancel) the `CancelledError` propagates unchanged and
  immediately, and that request's shadow calls are cancelled with it (recorded
  as `cancelled`). A stubborn shadow cannot delay the cancellation.
- **Deterministic shutdown.** The app's lifespan first drains shadow calls (up
  to `DIRECTOR_SHADOW_DRAIN_SECONDS`), then cancels the rest and waits a short
  bounded grace (1 s), and only then closes the providers those calls used. A
  shadow that ignores cancellation is logged and abandoned rather than hanging
  shutdown. No shadow work starts after shutdown began. If the drain itself is
  cancelled, the shadows are cancelled first and the providers are still closed.
- **Bounded everything.** At most 4 targets, `MAX_IN_FLIGHT` concurrent shadow
  calls, `STORE_SIZE` comparisons, and one record per execution. Each stored
  response is itself bounded by the contract (room size, 8 KiB provider
  metadata, 4 KiB excerpt).
- **Secret hygiene.** Shadows run through the same untrusted-adapter boundary as
  the active provider: adapter messages and excerpts are replaced by stable
  public wording before they become a record, and logs carry provider, model and
  exception type only. Shadow log lines start with `shadow provider ...`.

## Records

Every execution, active or shadow, yields exactly one immutable
`ExecutionRecord`:

| Field | Notes |
|-------|-------|
| `comparison_id` | `cmp-<32 hex>`, 36 chars, fresh per `generate` call (not derived from `request_id`, so a retried request id gets a new comparison). Also a valid contract `BoundedId`. |
| `role` | `active` or `shadow` |
| `provider`, `model` | As executed; `model` is `None` only for a skipped target with no model |
| `status` | `success`, `failure` (a canonical failure response), `cancelled`, `skipped`, `error` (the director failed around the call) |
| `skip_reason` | `unknown_provider`, `unknown_model`, `provider_unavailable`, `overloaded`, `shutting_down`, `launch_failed` |
| `outcome` | The raw canonical `GenerationOutcome`: the `GenerationResponse` (room, `metadata.latency_ms`, `started_at`/`completed_at`, `usage`, `error`, `provider_metadata`, `provider`, `model`, `request_id`, `run_id`) plus the HTTP `status_code`. `None` for cancelled/skipped/error. |
| `request_id`, `run_id` | Copied from the request, also present inside `outcome.response` |
| `started_at`, `completed_at`, `duration_ms` | Wall clock from launch to the record, measured identically for active and shadow |

The active record stores a private deep copy of the response, so a caller that
mutates the returned object cannot corrupt the stored one.

### Correlating from the game or from logs

When shadow mode is on and an active provider was selected, the
`/v1/generate` answer carries an additive response header
`X-Shadow-Comparison-Id: cmp-...` (any status). It is not in the body, so the
contract and the Godot client are unchanged. `GET /v1/config` additionally
gains a `shadow` object (`targets`, `rejected_config_entries`) only while shadow
mode is enabled.

## Consuming records (for the observability issue)

Implement `ShadowObserver` (`shadow.py`) and pass it to
`DirectorService(..., shadow_observers=[...])`:

```python
class ShadowObserver(Protocol):
    def comparison_started(self, comparison: ComparisonMeta) -> None: ...
    def execution_finished(self, record: ExecutionRecord) -> None: ...
```

Calls are synchronous on the event loop: keep them fast and non-blocking (bump a
counter, enqueue for an exporter). An observer that raises is logged by type
and skipped; other observers and the request are unaffected. A comparison is
complete when it has `meta.expected` records (`ComparisonSnapshot.complete`).

The built-in `ShadowStore` (`service.shadow.store`) is an observer too: it keeps
the newest `STORE_SIZE` comparisons, evicting the oldest, drops and counts late
records for evicted comparisons (`dropped_records`) and exposes `get(id)`,
`recent(limit)`, `evicted`. It is deliberately in-process only: there is no HTTP
endpoint that returns stored rooms.

### Metric labels: keep identifiers out

`comparison_id`, `request_id` and `run_id` are unbounded. Use them on records,
logs and trace/span attributes, **never as metric labels**. For metrics use
`ExecutionRecord.metric_labels()`, which returns exactly `METRIC_LABEL_NAMES`:

| Label | Values |
|-------|--------|
| `role` | `active`, `shadow` |
| `provider` | a registered provider id, otherwise `other` |
| `model` | a registered model id (or `none`), otherwise `other` |
| `status` | `success`, `failure`, `cancelled`, `skipped`, `error` |
| `reason` | the canonical `ErrorKind` for failures, the `skip_reason` for skipped, else `none` |

Because provider and model only appear when the registry vouches for them, a
client cannot mint new label values by sending `?provider=random` and a stale
config entry cannot either. The full set has a small fixed upper bound.

## Not in this issue

- Persistence beyond the in-memory store, OTLP export and dashboards: the
  observability issue implements a `ShadowObserver`.
- A comparison/scoring UI or replay tooling (`benchmarks/`).
- Sampling a fraction of requests: every request is shadowed while targets are
  configured.
