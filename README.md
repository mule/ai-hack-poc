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
| Director service (FastAPI) | `director/` | Runs, exposes `GET /health` only. No dungeon endpoint, no providers yet. |
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

(The providers shown are the planned initial set from the epic; none are
implemented yet.)

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
- [Godot](https://godotengine.org/) 4.x to open and run the game (the shell
  project pins the exact version; not needed to run the director)
- Android export (later): Android SDK and Godot export templates, covered by the
  platform-build issue

## Setup and run

```sh
git clone <repo-url> && cd ai-hack-poc
make setup           # creates director/.venv and installs the director + dev tools
make run-director    # http://127.0.0.1:8000, auto-reload
curl http://127.0.0.1:8000/health
# {"status":"ok","service":"dungeon-director"}
```

Equivalent without `make` (from the repo root, with the venv active):

```sh
python -m uvicorn dungeon_director.app:app --app-dir director
```

Override host/port with make variables: `make run-director DIRECTOR_PORT=9000`.
Interactive API docs are served at `/docs` while the director runs.

### Running the game

The Godot project lives in `game/` (issue #4). Once it is present in your
checkout, open `game/project.godot` in the Godot editor and run it, or from a
shell:

```sh
godot --path game
```

How the client locates the director is defined by the game shell.

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
| `make godot-test` | Headless Godot tests: `game/tests/test_mechanics.gd` (required), then `test_contracts.gd` and `test_scene_smoke.gd` if they exist. Requires `game/project.godot`. Logs: `/tmp/godot-mechanics.log`, `/tmp/godot-contracts.log`, `/tmp/godot-scene-smoke.log` |
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
- `.env.example` currently lists placeholder names for anticipated settings
  (active provider, Cloudflare/Groq/Cerebras credentials, OTLP endpoint). The
  director does not read any of them yet; config loading arrives with the
  provider work, and names may change then.
- Provider and model selection is meant to be configuration-driven, not
  hard-coded in game code.

## Repository layout

```
game/           Godot 2D client (issue #4)
director/       FastAPI director service
  dungeon_director/   Python package (app.py, contracts.py, ...)
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
