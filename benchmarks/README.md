# Dungeon Director Benchmarks & Replay CLI

Tools for recording gameplay generation events, replaying identical inputs
across director providers, and growing large headless dungeon datasets. The
simulation harness from issue #16 is documented in
[`simulation/README.md`](simulation/README.md) and runs with `make simulate`.

Delivered by issue #14 under epic #1.

## Architecture & Guarantees

- **Director-native evaluation:** Benchmarks evaluate requests strictly through `DirectorService.generate()`. They never call provider endpoints directly, ensuring that identical contract validation (`RoomPlan`), timeouts, error classification, and adapter logic are applied.
- **Offline safety:** Runs 100% offline out-of-the-box using the deterministic `rules-baseline` provider or mocked transports in tests. Hosted providers (`cloudflare-jev`, `groq`, `cerebras`) activate automatically when credentials exist in the environment or director settings.
- **Telemetry preservation:** Metrics capture raw latencies, percentile distributions (p50, p90, p95, p99), token usage, estimated costs, and classified failure reasons (`schema_violation`, `provider_timeout`, `rate_limited`, `provider_error`).
- **Machine-readable outputs:** Results can be saved to `.json` or `.jsonl` formats or piped directly to other data pipelines.

---

## Dataset Recording

During gameplay or headless simulation, canonical generation requests and their committed outcomes can be persisted to a JSONL dataset.

### Enabling Recording in Godot

Set either of the following environment variables:
```bash
export DUNGEON_GENERATION_LOG_PATH="recordings/gameplay_events.jsonl"
# or
export DUNGEON_RECORD_DATASET="recordings/gameplay_events.jsonl"
```

Each recorded line is a complete JSON object containing:
- `contract_version`: Contract version (e.g. `"1.0.0"`).
- `run_id`: Playthrough identifier.
- `request_id`: Request correlation identifier.
- `timestamp`: UTC ISO 8601 timestamp.
- `seed`: World generation seed used.
- `provider` / `model`: Active provider and model configured.
- `outcome`: Resolution outcome (`committed`, `fallback`, `sealed`).
- `source`: Plan authority (`director`, `fallback`, `sealed`).
- `target_exit`: The exit frontier being resolved.
- `request`: The canonical `GenerationRequest` dictionary.
- `result`: The committed room plan dictionary (when available).
- `fallback_reason`: Reason for fallback or sealing (if applicable).
- `metadata`: Placement metadata (size used, repositioned, pruned exits).
- `response_metadata`: Canonical director metadata (resolved provider/model,
  timing, usage, provider metadata, or classified error) when a response was
  received.

Sample dataset fixtures are provided under `benchmarks/fixtures/sample_run.jsonl`.

---

## Replay CLI

The replay benchmark CLI is available via python module:
```bash
python -m benchmarks.replay --input <dataset_path> [options]
```
Or using the virtual environment:
```bash
director/.venv/bin/python -m benchmarks.replay --input benchmarks/fixtures/sample_run.jsonl
```

### CLI Arguments

| Flag | Argument | Description | Default |
| --- | --- | --- | --- |
| `--input`, `-i` | `<path>` | **Required.** Path to JSON or JSONL file containing recorded generation requests or events. | |
| `--output`, `-o` | `<path>` | Path to save machine-readable report (`.json` or `.jsonl`). Prints JSON to stdout if omitted. | `None` (stdout) |
| `--providers`, `-p` | `<list>` | Providers to evaluate: `rules-baseline`, `cloudflare-jev`, `groq`, `cerebras`. | `rules-baseline` |
| `--models`, `-m` | `<list>` | Model overrides in `provider:model` format (e.g. `groq:openai/gpt-oss-120b`). | Provider defaults |
| `--iterations`, `-n` | `<int>` | Number of iterations per request per provider. | `1` |
| `--concurrency`, `-c` | `<int>` | Maximum concurrent in-flight requests. | `2` |
| `--timeout`, `-t` | `<float>` | Per-generation timeout in seconds. | `10.0` |
| `--quiet`, `-q` | Flag | Suppress human-readable summary table on stderr. | `False` |

### Examples

#### 1. Baseline Replay (Offline)
```bash
python -m benchmarks.replay \
  --input benchmarks/fixtures/sample_run.jsonl \
  --providers rules-baseline
```

#### 2. Multi-Provider Comparison with JSON Output
```bash
python -m benchmarks.replay \
  --input benchmarks/fixtures/sample_run.jsonl \
  --providers rules-baseline groq cerebras \
  --concurrency 4 \
  --output benchmarks/reports/run_comparison.json
```

#### 3. High-Iteration Benchmark with Model Overrides to JSONL
```bash
python -m benchmarks.replay \
  --input benchmarks/fixtures/sample_run.jsonl \
  --providers rules-baseline groq \
  --models groq:openai/gpt-oss-120b \
  --iterations 5 \
  --output benchmarks/reports/benchmark.jsonl
```

---

## Make Targets

- `make replay-benchmark`: Runs replay benchmark against `rules-baseline` using the sample fixture.
- `make simulate`: Grows deterministic headless dungeons and writes validated
  datasets under the git-ignored `simulation-output/` directory.
- `make check`: Runs Python ruff linting, formatting check, and pytest test suite (including replay tests).
- `make godot-test`: Runs headlessly all Godot test suites (including `GenerationRecorder` tests).
