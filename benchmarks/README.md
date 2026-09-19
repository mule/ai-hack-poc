# Dungeon Director Benchmarks & Replay CLI

Tools for recording gameplay generation events, replaying identical inputs
across director providers, and growing large headless dungeon datasets. The
simulation harness from issue #16 is documented in
[`simulation/README.md`](simulation/README.md) and runs with `make simulate`.

Delivered by issues #14 (replay) and #15 (provider/model summaries) under epic #1.

The comparison protocol built on these tools (a fixed replay corpus, a paired run
design, version/config capture, cost normalization, and a report that keeps
measured results apart from subjective notes) is defined in
[`../docs/evaluation-methodology.md`](../docs/evaluation-methodology.md) and runs
with `make eval-offline`. Its evidence bundles hold a `results.json` in the raw
replay shape, so `benchmarks.summarize` reads them directly.

## Architecture & Guarantees

- **Director-native evaluation:** Benchmarks evaluate requests strictly through `DirectorService.generate()`. They never call provider endpoints directly, ensuring that identical contract validation (`RoomPlan`), timeouts, error classification, and adapter logic are applied.
- **Offline safety:** Runs 100% offline out-of-the-box using the deterministic `rules-baseline` provider or mocked transports in tests. Hosted providers (`cloudflare-jev`, `groq`, `cerebras`) activate automatically when credentials exist in the environment or director settings.
- **Telemetry preservation:** Metrics capture raw latencies, percentile distributions (p50, p90, p95, p99), token usage, estimated costs, and classified failure reasons (`schema_violation`, `provider_timeout`, `rate_limited`, `provider_error`).
- **Machine-readable outputs:** Results can be saved to `.json` or `.jsonl` formats or piped directly to other data pipelines.
- **Summaries:** `python -m benchmarks.summarize` (`make benchmark-summary`) turns replay results into per-provider, per-model comparisons. See [Summaries](#summaries-provider-and-model-comparison).

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
| `--models`, `-m` | `<list>` | Model overrides in `provider:model` format (e.g. `groq:openai/gpt-oss-120b`). Repeat a provider to replay several of its models; each is reported separately. | Provider defaults |
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

#### 4. Several Models of One Provider
```bash
python -m benchmarks.replay \
  --input benchmarks/fixtures/sample_run.jsonl \
  --providers groq \
  --models groq:openai/gpt-oss-120b groq:llama-3.3-70b-versatile \
  --output benchmarks/reports/groq_models.json
```

### Raw Output Shape

`--output` writes one record per request: `.json` is one report object with a
`results` list; `.jsonl` is a `benchmark_summary` header line followed by one
`benchmark_result` line per request. Each result carries what the summarizer
needs: `provider`, `model`, `request_id`, `iteration`, `success`, `latency_ms`,
`error_code`, `usage` (`input_tokens`, `output_tokens`, `estimated_cost_usd`,
present only when the provider reported them), `room` (`room_type`, `danger`),
`provider_metadata` and `retry_count` (`null` unless the provider reports it; the
director itself never retries).

`summary_by_provider` is kept for backwards compatibility but is deprecated: it is
keyed by provider alone, so a provider replayed with several models is left out
instead of having one model overwrite another. Use `providers` (one entry per
provider/model) or the summarizer below.

---

## Summaries: Provider and Model Comparison

```bash
make benchmark-summary                                    # bundled sample, text
make benchmark-summary BENCH_REPORT=reports/run.json BENCH_FORMAT=json
make benchmark-summary BENCH_REPORT='reports/a.json reports/b.jsonl' BENCH_FORMAT=csv BENCH_OUT=summary.csv
python -m benchmarks.summarize --input reports/run.json --format text
```

| Flag | Argument | Description | Default |
| --- | --- | --- | --- |
| `--input`, `-i` | `<path> [<path> ...]` | **Required.** Replay outputs (`.json` report or `.jsonl` stream, also a bare array or bare JSONL of results). Several files are merged. | |
| `--format`, `-f` | `text`, `json`, `csv` | Output format. | `text` |
| `--output`, `-o` | `<path>` | Write to a file instead of stdout. | stdout |

Exit code `0` on success, `2` for unreadable, malformed or incompatible input (the
message names the file and the line or result number, and the field).
`make benchmark-summary` takes `BENCH_REPORT`, `BENCH_FORMAT` and `BENCH_OUT`.

### Grouping

One group per `(provider, model)`. Two models of one provider are two groups;
the same model name under two providers is two groups. Groups are sorted by
provider, then model, so output is deterministic. If groups did not run over the
same set of `request_id`s the report carries a warning, because their latencies
and rates are then not directly comparable.

### Metrics

| Metric | Definition |
| --- | --- |
| Latency `p99`, `p95`, `p90`, `p50`, `min`, `mean`, `max` (ms) | End-to-end latency of **every** request, failures and timeouts included. Text output lists the tail (`p99`) first. |
| `success_rate` | Successful requests / all requests. |
| `timeouts` / `timeout_rate` | `error_code = provider_timeout`. |
| `schema_failures` / `schema_failure_rate` | `schema_violation`, `invalid_json` or `empty_response`. |
| `other_errors` / `other_error_rate` | Every other failure (`provider_error`, `rate_limited`, ..., or a failure without an error code). |
| `retries` | `count` = total retries; `requests_retried`; `rate` = retried requests / requests that reported a retry count. |
| `tokens` (`input`, `output`, `total`) | `min`, `avg`, `max` over requests that reported them. `total` is the reported `total_tokens`, else input + output when both were reported. |
| `cost.per_1000_decisions_usd` | Mean `estimated_cost_usd` over the requests that reported a cost, times 1000. `total_usd` is the sum. |
| `room_types`, `danger` | Count and share of each room type / danger level over successful rooms. |

All rates use every request in the group as denominator (retry and distribution
rates use the reported/recorded subset, stated by their `reported` count).

### Percentile convention

Linear interpolation between closest ranks: sort the `n` samples, take
`rank = p * (n - 1)` (0-indexed) and interpolate between the samples at
`floor(rank)` and `ceil(rank)`. This is Hyndman-Fan type 7, the numpy default.
With few samples the upper percentiles interpolate toward the maximum; with 10
samples `p99` is `0.91 * max + 0.09 * second-largest`. The convention is printed
in the text output and stored in the JSON as `percentile_convention`.

### Missing data is never zero

A metric that no request reported is `null` in JSON, an **empty cell** in CSV and
`n/a` in text. Every metric has a `reported` count (text shows `k/n`), so a
figure built from 5 of 10 requests says so. A provider that reports a genuine
`0` (the offline `rules-baseline` reports 0 tokens and `$0`) stays `0`.

### Output formats

* **text**: latency (tail first), reliability, tokens and cost, then room decisions.
* **json**: the full report: `groups[]` with nested `latency_ms`, `outcomes`,
  `retries`, `tokens`, `cost`, `room_types`, `danger`, plus `warnings`, `sources`
  and `percentile_convention`.
* **csv**: one row per provider/model with flat columns (`latency_p99_ms`,
  `timeout_rate`, `input_tokens_min`, `cost_per_1000_decisions_usd`,
  `room_type_<type>_count`, `danger_<level>_rate`, ...). Room-type and danger
  columns always include every contract value, plus any extra value seen, so
  files from different runs line up.

Sample inputs: `benchmarks/fixtures/sample_replay_results.json` and
`.jsonl` (the same 50 results in both shapes: five provider/model groups, two
Groq models, timeouts, a schema failure, and partial or missing usage).

---

## Make Targets

- `make replay-benchmark`: Runs replay benchmark against `rules-baseline` using the sample fixture.
- `make simulate`: Grows deterministic headless dungeons and writes validated
  datasets under the git-ignored `simulation-output/` directory.
- `make benchmark-summary`: Summarizes replay results per provider/model as text, JSON or CSV (`BENCH_REPORT`, `BENCH_FORMAT`, `BENCH_OUT`).
- `make check`: Runs ruff linting and the format check over `director/` and `benchmarks/`, plus the pytest suite (replay and summary tests included).
- `make format`: Auto-fixes and formats `director/` and `benchmarks/`.
- `make godot-test`: Runs headlessly all Godot test suites (including `GenerationRecorder` tests).
- `make eval-corpus-verify`, `make eval-corpus-rebuild`: check the fixed replay corpus against its manifest, or regenerate it with Godot and compare bytes (issue #18).
- `make eval-offline`: run the evaluation protocol on the offline rules baseline and write a verified evidence bundle under the git-ignored `evaluation-output/`.
- `make eval-live` (billable, needs `EVAL_LIVE=1`), `make eval-report`, `make eval-verify`: see [`../docs/evaluation-methodology.md`](../docs/evaluation-methodology.md).
