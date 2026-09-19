# ai-hack-poc

How fast can AI models generate game content on the fly? Let's find out.

A NetHack-like roguelike POC where the dungeon is generated **incrementally** as
the player explores, and AI model providers are interchangeable runtime
"dungeon directors". The same dungeon state can be shown to several
providers/models, so the project doubles as a latency, reliability, cost and
output-quality benchmark harness. Tracking: epic
[#1](https://github.com/mule/ai-hack-poc/issues/1), this bootstrap is [#2](https://github.com/mule/ai-hack-poc/issues/2).

## Status

Early bootstrap. What exists today and what does not:

| Component | Directory | State |
|-----------|-----------|-------|
| Godot 2D client (desktop + Android) | `game/` | Shell delivered by issue #4, developed alongside this one. Not part of the bootstrap files. |
| Director service (FastAPI) | `director/` | Issue #5: `POST /v1/generate`, `GET /v1/config`, `GET /health`; provider registry; offline rules baseline as default. No external providers yet. |
| Shared plan contract (`RoomPlan`) | `director/dungeon_director/contracts.py` | Owned by issue #3. |
| Benchmarks / replay | `benchmarks/` | Placeholder README only. |
| Observability | `observability/` | Placeholder README only. |
| Docker Compose | n/a | None yet. Added only if it helps local observability/service startup; the Godot client is never containerized. |

## Architecture

```
┌──────────────┐  canonical dungeon-decision API   ┌─────────────────────────┐
│  Godot game  │ ────────────────────────────────▶ │  Director (FastAPI)     │
│  (/game)     │ ◀──────── validated RoomPlan ──── │  (/director)            │
│              │                                   │                         │
│ no provider  │                                   │  provider adapters      │
│ code, no     │                                   │   ├─ rules baseline     │
│ credentials  │                                   │   ├─ Cloudflare / Jev   │
└──────────────┘                                   │   ├─ Groq / GPT-OSS 20B │
                                                   │   └─ Cerebras / Qwen    │
                                                   └───────────┬─────────────┘
                                                               │ spans/metrics
                        ┌──────────────────┐          ┌────────▼────────────┐
                        │ Benchmarks       │  replay  │ Observability       │
                        │ (/benchmarks)    │ ───────▶ │ (/observability)    │
                        └──────────────────┘ director │ OpenTelemetry/OpenLIT│
                                                      └─────────────────────┘
```

(The providers shown are the planned initial set from the epic; only the rules
baseline is implemented. The others plug in through the same provider interface.)

### Provider-independent, semantic plans

The central design rule: **models decide what a room is, never what the map
tiles are, and never touch committed state.**

1. The game sends the director a description of the dungeon state and the
   unexplored exit that needs content. The game holds no provider-specific API
   logic and no provider credentials.
2. The director asks the active provider adapter for a decision. Each adapter
   translates its provider's request/response format into the one shared
   `RoomPlan` contract. Adding or swapping a provider (or model) is a director
   configuration change, not a game change or a rebuild.
3. The director validates the result against the contract. A malformed or failed
   response is a recorded failure, not something the game has to interpret.
4. The game turns the validated semantic plan (room kind, theme, exits, contents,
   pacing hints, ... as defined by the contract) into concrete geometry
   **deterministically and locally**, then commits it. Committed dungeon state is
   permanent; unexplored exits stay undefined until generation is requested.

Why this shape: identical state can be replayed to many providers and the plans
compared apples-to-apples; provider failures cannot corrupt the dungeon; and the
model output is small and cheap to validate, which keeps latency measurable and
gameplay-viable.

The exact `RoomPlan` fields and the request/response schema live in the
contracts module (issue #3) and are the source of truth; this README
deliberately does not duplicate them.

## Prerequisites

- Python 3.11 or newer, with the `venv` module (on Debian/Ubuntu:
  `sudo apt install python3-venv`)
- `make`
- [Godot](https://godotengine.org/) 4.x (`4.7.1.stable`) to open, run, or export the game (not needed to run the director)
- Linux desktop export: Godot 4.7.1 matching export templates (`linux_debug.x86_64`)
- Android export:
  - Godot 4.7.1 matching export templates (`android_debug.apk`)
  - OpenJDK 17 (`/usr/lib/jvm/java-17-openjdk-amd64` or `$JAVA_HOME`)
  - Android SDK with platform-tools, build-tools 35+ (e.g. 35.0.0 / 36.0.0), and platform android-35+
  - Local debug keystore (`debug.keystore`) generated via `keytool` and configured in Godot editor settings

## Platform exports (Linux Desktop & Android Debug)

Preset definitions live in [`game/export_presets.cfg`](game/export_presets.cfg). Presets are credential-free and contain no machine-local paths.

### Export commands

Using `make`:
```sh
make export-linux GODOT=/path/to/godot
make export-android GODOT=/path/to/godot
```

Or using Godot directly:
```sh
mkdir -p game/builds/linux game/builds/android
godot --headless --path game --export-debug "Linux Desktop" builds/linux/ai-hack-poc.x86_64
godot --headless --path game --export-debug "Android Debug" builds/android/ai-hack-poc-debug.apk
```

### Generated artifacts

- **Linux Desktop**: `game/builds/linux/ai-hack-poc.x86_64`, `ai-hack-poc.pck`, and launcher script `ai-hack-poc.sh`. Packaged by `make export-linux` into `ai-hack-poc-linux-x86_64.tar.gz` (preserving executable bits).
- **Android Debug**: `game/builds/android/ai-hack-poc-debug.apk` (and `.apk.idsig`).

Generated build directories (`game/builds/`) are git-ignored and never committed.

### Local Android debug keystore setup

Godot expects the debug keystore and SDK locations in its editor settings (`~/.config/godot/editor_settings-4.7.tres`). To generate a local debug keystore:

```sh
mkdir -p ~/.local/share/godot/keystores
keytool -genkeypair -v -keystore ~/.local/share/godot/keystores/debug.keystore \
  -alias androiddebugkey -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=Android Debug,O=Android,C=US" -storepass android -keypass android
```

Configure Godot editor settings (`Editor -> Editor Settings -> Export -> Android` in the GUI, or in `editor_settings-4.7.tres`):
- **Debug Keystore**: `~/.local/share/godot/keystores/debug.keystore`
- **Debug Keystore User**: `androiddebugkey`
- **Debug Keystore Pass**: `android`
- **Java SDK Path**: `/usr/lib/jvm/java-17-openjdk-amd64` (or `$JAVA_HOME`)
- **Android SDK Path**: `/home/<user>/Android/Sdk` (or `$ANDROID_HOME`)

Alternatively, export signing credentials can be supplied via environment variables without editing settings:
- `GODOT_ANDROID_KEYSTORE_DEBUG_PATH`
- `GODOT_ANDROID_KEYSTORE_DEBUG_USER`
- `GODOT_ANDROID_KEYSTORE_DEBUG_PASSWORD`

Signing keys and credentials are never stored in `game/export_presets.cfg` or committed to git.

## Continuous Integration & Build Artifacts

GitHub Actions workflow [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs automated checks and exports:

| Job | Trigger | What it does |
|---|---|---|
| `backend-checks` | Push, Pull Request, `workflow_dispatch` | Sets up Python 3.11 with cached pip dependencies and runs `make setup && make check`. |
| `godot-checks` | Push, Pull Request, `workflow_dispatch` | Downloads and verifies Godot 4.7.1-stable against official SHA-512 sums, running headless load check (`make godot-check`) and test suites (`make godot-test`). |
| `build-artifacts` | Push, Pull Request, `workflow_dispatch` | Sets up Java 17 and Android SDK 35, verifies Godot engine & export templates against official SHA-512 checksums, and compiles Linux and Android debug targets. |

### Downloadable Artifacts

When runs succeed on `main` or via manual `workflow_dispatch`, downloadable build artifacts are uploaded:
- **`linux-desktop-build`**: Contains `ai-hack-poc-linux-x86_64.tar.gz`. Unpack with `tar -xzf ai-hack-poc-linux-x86_64.tar.gz` and run `./ai-hack-poc.sh` or `./ai-hack-poc.x86_64`.
- **`android-debug-apk`**: Contains `ai-hack-poc-debug.apk`.

> [!WARNING]
> **Ephemeral CI Debug Keystore Caveat**: CI builds generate an ephemeral debug key for signing each run. Android OS verifies signature consistency during package upgrades. If you have previously installed an APK from a different CI run or a local build on your test device, Android will reject the installation with `INSTALL_FAILED_UPDATE_INCOMPATIBLE`. You **must uninstall the previous version from the device** before installing an APK from another CI run (`adb uninstall com.mule.aihackpoc`).

## Setup and run

```sh
git clone <repo-url> && cd ai-hack-poc
make setup           # creates director/.venv and installs the director + dev tools
make run-director    # http://127.0.0.1:8000, auto-reload
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"dungeon-director"}
curl http://127.0.0.1:8000/v1/generate -H 'content-type: application/json' \
     -d @contracts/fixtures/generation_request.json      # a rules-baseline RoomPlan
```

Equivalent without `make` (from the repo root, with the venv active):

```sh
python -m uvicorn dungeon_director.app:app --app-dir director
```

Override host/port with make variables: `make run-director DIRECTOR_PORT=9000`.
Interactive API docs are served at `/docs` while the director runs.

### Director API

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Liveness: `{"status":"ok","service":"dungeon-director"}` |
| `GET /v1/config` | Default provider/model and every registered provider with its models and an `available` flag. Identifiers only, never credentials |
| `POST /v1/generate` | Body: the canonical `GenerationRequest`. Optional `?provider=<id>&model=<id>` (defaults come from configuration). Returns a canonical `GenerationResponse` |

Provider/model selection is a query parameter so the shared contract is
untouched. **Every** `/v1/generate` answer, including failures, is a
`GenerationResponse`; the game checks `success`, and the HTTP status tells you why:

| Status | Meaning | `metadata.error.code` |
|--------|---------|-----------------------|
| 200 | Valid `RoomPlan` in `room` | none |
| 404 | Unknown provider or model | `provider_error` (`provider_metadata.selection_error` names the reason) |
| 422 | Request failed contract validation | `schema_violation`, `invalid_json`, `unsupported_contract_version` |
| 429 | Provider rate limited or over budget | `rate_limited`, `budget_exceeded` |
| 500 | Director-side failure while processing a provider result, or a provider-reported internal error | `internal_error` |
| 502 | Provider raised, or returned empty/invalid/wrong-depth output | `provider_error`, `schema_violation`, `invalid_json`, `empty_response`, `safety_refusal` |
| 503 | Provider registered but unavailable | `provider_error` (`selection_error: provider_unavailable`) |
| 504 | Provider exceeded the timeout, or reported a timeout of its own | `provider_timeout` (`provider_metadata.timeout_origin`: `director_deadline` or `provider`) |

Behaviour that applies to every provider:

- **No retries.** One request is one provider call, so latencies are honest.
- **Timeout.** One configurable deadline. A provider that overruns it is
  cancelled and reported as `provider_timeout`. A provider that returns *after*
  the deadline (it swallowed the cancellation, or blocked the event loop) is
  also a timeout, never a 200. A `TimeoutError` raised by the provider itself is
  a `provider_timeout` too, labelled `timeout_origin: provider`.
- **Cancellation.** If the task running the request is cancelled (for example
  the ASGI server cancels it), the cancellation propagates into the provider
  call. The director does not itself monitor for client disconnects.
- **Providers must be truly async.** A provider must not block the event loop
  (`time.sleep`, a synchronous HTTP client) and must not swallow
  `CancelledError`. A blocking call cannot be interrupted here: it stalls every
  other request while it runs, and its late answer is only discarded afterwards.
- **Results are validated, not trusted.** The result object must be a
  `ProviderResult`, and the room is validated against the contract even when
  the provider returns a ready `RoomPlan` instance (which could have been built
  unchecked or mutated). Anything else is a canonical 502.
- **Untrusted text.** For failures a provider reports itself, the game gets only
  a stable generic message for the error code, never the provider's own message
  or excerpt (these can echo credentials). Validation excerpts of malformed room
  output, which the director generates, are still returned.
- **Logs.** Unexpected provider exceptions are logged as provider, model and
  exception *type* only: no exception text, no traceback, no adapter-supplied
  message.
- **Availability.** A provider whose availability check throws is treated as
  unavailable (`available: false`, 503 on selection).
- **Selectors.** Blank or whitespace-only `provider`/`model` query values count
  as omitted (defaults apply); surrounding whitespace is trimmed.
- **Isolation.** One provider failing never affects the next request.

The rules baseline (`rules-baseline` / `builtin-v1`) is offline and
deterministic: the same state and frontier always yield the same room, its
depth equals the request depth, and it always has an exit back to the frontier
it was generated from.

Adding a provider means subclassing `DungeonDirectorProvider`
(`director/dungeon_director/providers.py`) and registering it in
`default_registry()`; nothing in the game changes.

### Running the game

The Godot project lives in `game/` (issue #4). Once it is present in your
checkout, open `game/project.godot` in the Godot editor and run it, or from a
shell:

```sh
godot --path game
```

The game requests rooms from the director lazily as the player approaches
unexplored exits (`POST /v1/generate`), and falls back to a local rules baseline
if the director is unreachable, slow or returns something unusable. Point it at
a director with `DUNGEON_DIRECTOR_URL` (default `http://127.0.0.1:8000`, or
`offline` to use local rules only); optional `DUNGEON_DIRECTOR_PROVIDER` and
`DUNGEON_DIRECTOR_MODEL` select a provider/model by id. See
[game/README.md](game/README.md#deferred-dungeon-generation).

## Development commands

| Command | What it does |
|---------|--------------|
| `make setup` | Create `director/.venv` and install the director with dev tools |
| `make run-director` | Start the director with auto-reload |
| `make test` | `pytest` for the director |
| `make lint` | `ruff check` + `ruff format --check` |
| `make format` | `ruff check --fix` + `ruff format` |
| `make check` | `lint` + `test` (Python only, no Godot needed) |
| `make godot-check` | Headless-load the Godot project for 10 frames; log at `/tmp/godot-load.log`. Fails with a message if `game/project.godot` is absent |
| `make godot-test` | Headless Godot tests: `game/tests/test_mechanics.gd` (required), then the contract, room-generator, deferred-generation (`test_deferred_world.gd`, `test_deferred_generation.gd`, `test_deferred_simulation.gd`) and scene-smoke suites if they exist. Requires `game/project.godot`. Fails on `SCRIPT ERROR:`/`Parse Error:`/`Failed to load script` in a log, or a missing success sentinel. Logs: `/tmp/godot-*.log` |
| `make godot-lint` | `gdlint game` (install with `pip install gdtoolkit`) |
| `make clean` | Remove the virtualenv, Python caches and `director/build`, `director/dist` |

`godot-check`, `godot-test` and `godot-lint` are opt-in and are not part of `make check`,
because they need Godot / `gdtoolkit` installed and the game shell present.

**Godot log scanning.** Godot exits 0 even when a script fails to parse or hits
a runtime error, so `godot-check` and `godot-test` write each run to an explicit
log under `$(GODOT_LOG_DIR)` (default `/tmp`; override with
`make godot-test GODOT_LOG_DIR=...`) and fail if it contains `SCRIPT ERROR:` or
`Parse Error:`, whatever Godot's exit code. A generic `ERROR:` line does not
fail the run: the contract test deliberately feeds invalid JSON.

**Scene smoke test.** If `game/tests/test_scene_smoke.gd` exists, `godot-test`
runs it after the other tests with its own log and applies the same scan. It is
optional, so `godot-test` still passes without it (a skip message is printed).
The intent is to exercise the main scene, which the mechanics test does not
load; its contents belong to the game shell.

## Configuration and secrets

- Copy `.env.example` to `.env` and fill in values. `.env` and other `.env.*`
  files are git-ignored; only `.env.example` is tracked.
- Provider credentials belong to the **director only**. Never put them in
  `game/`, in exported builds, or in committed files.
- The director reads `DIRECTOR_DEFAULT_PROVIDER` (default `rules-baseline`),
  `DIRECTOR_DEFAULT_MODEL` (default: the provider's own default) and
  `DIRECTOR_TIMEOUT_SECONDS` (default `10`, must be > 0 and <= 300) from the
  **process environment**. It does not load `.env` itself: export the variables
  or use your shell or a tool such as `direnv`. Invalid values, or a default
  provider that is unknown or unavailable, stop the service at startup with a
  clear message.
- The provider credential and OTLP variables in `.env.example` are placeholders
  for the external-provider and telemetry issues; nothing reads them yet. When
  they do, credentials stay inside the provider adapter and never appear in
  `/v1/config`, responses or error messages.
- Provider and model selection is configuration-driven: the game names a
  provider/model by stable id in the query string, never provider-specific logic.

## Repository layout

```
game/           Godot 2D client (issue #4)
director/       FastAPI director service
  dungeon_director/   Python package: app.py (HTTP + app factory), service.py
                      (timeout/validation/failure policy), providers.py,
                      registry.py, rules.py (baseline), settings.py, contracts.py
  tests/
benchmarks/     Replay/benchmark tooling (placeholder)
observability/  OpenTelemetry/OpenLIT config (placeholder)
Makefile        Local dev commands
.env.example    Configuration template (no secrets)
```

## Non-goals (first POC)

Full NetHack parity, an art pipeline, a large content catalog, production-grade
backend security, multiplayer, AI-generated tile-by-tile geometry, and letting
models mutate committed dungeon state directly.

## License

MIT, see [LICENSE](LICENSE).
