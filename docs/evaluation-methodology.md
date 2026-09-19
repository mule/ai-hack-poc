# POC evaluation methodology

Issue [#18](https://github.com/mule/ai-hack-poc/issues/18), under epic #1.
This document defines what evidence would make the experiment succeed or fail,
and a comparison protocol anyone can repeat. The protocol is executable: every
step below is a command, and every claim it makes is backed by a file in an
evidence bundle that the tooling verifies.

The question the POC asks is *how fast can models generate game content on the
fly, and at what reliability, cost and quality?* The protocol answers it as
**measurements per provider/model**. It never produces a verdict.

- [Principles](#principles)
- [What counts as success or failure](#what-counts-as-success-or-failure)
- [Corpora: fixed replay and optional live](#corpora-fixed-replay-and-optional-live)
- [Design of a run](#design-of-a-run)
- [Warm-up and sample sizes](#warm-up-and-sample-sizes)
- [What is recorded](#what-is-recorded)
- [Metrics](#metrics)
- [Measured, derived, descriptive, subjective](#measured-derived-descriptive-subjective)
- [Running it](#running-it)
- [Reproducibility](#reproducibility)
- [Limitations](#limitations)

## Principles

1. **Same inputs, same conditions.** Every provider/model answers the *same*
   stored request, back to back, one at a time.
2. **Nothing is inferred that was not observed.** A missing token count, price
   or retry count is reported as missing (`n/a`), never as zero.
3. **Measured is kept apart from opinion.** Tables of numbers and a person's
   impression of a session live in different sections and different files.
4. **No hard-coded winner.** Reports list providers alphabetically. Latency,
   cost, reliability and behaviour trade off differently for different uses
   (say, a fast cheap model for prefetching versus a slower, more varied one for
   set-piece rooms); the protocol reports the trade-off and the reader chooses.
5. **The default path is free and offline.** The offline rules baseline
   reproduces without credentials. Anything that spends money is a separate,
   explicitly confirmed step.

## What counts as success or failure

These are *questions the evidence must be able to answer*, not thresholds baked
into code. Thresholds are a product decision; the protocol makes them checkable.

| Question | Evidence (bundle file) |
|---|---|
| Can a provider keep up with exploration? Is the tail acceptable, not just the median? | p50/p90/p95/p99 end-to-end latency (`summary.*`), with sample-size flags (`metrics.json`) |
| Is it reliable enough that the game rarely falls back? | success rate, timeout, schema-failure and other-error rates, retries (`summary.*`) |
| Does it stay affordable at play volume? | cost per 1000 decisions, from measured tokens and a cited price table (`metrics.json`) |
| Are the rooms good enough to play? | behaviour: diversity, repetition, danger progression, agreement (`behavior.json`); plus *subjective* notes (`observations.json`), kept apart |
| Can a result be trusted and repeated? | verified bundle: corpus digest, versions, config, reproduction of the offline plans |

The experiment **succeeds** if at least one provider/model shows, on the fixed
corpus, a tail latency, reliability and cost the team judges workable for
incremental generation, *and* the result reproduces. It **fails** if no
provider/model does, or if results cannot be reproduced. Either outcome is a
valid finding; the report supplies the numbers, the team supplies the judgement.

## Corpora: fixed replay and optional live

Two kinds of corpus exist. A run uses exactly one, the report says which, and
their results are never merged or compared with each other.

### Fixed replay corpus (the reproducible one)

`benchmarks/corpus/replay-v1/` is committed:

- `requests.jsonl`: 150 canonical-JSON `GenerationRequest`s, one per line, in
  recording order (5 runs of 30 frontier resolutions from the headless
  simulation, seed 100).
- `corpus.json`: identity, provenance (generator command, simulation arguments,
  Godot version, which provider answered when it was recorded), the file's
  sha256/size/record count, and the golden plan digest of the offline rules
  baseline.

Verification (`make eval-corpus-verify`) fails on a changed byte, a
non-canonical or duplicate line, an invalid request, or a wrong count.
`make eval-corpus-rebuild` regenerates the corpus with Godot 4.7.1 in a scratch
directory and compares it byte for byte with the committed file, so the inputs
are reproducible from the game code, not merely stored.

### Live corpus (optional, clearly separate)

A live corpus is recorded from real play. Enable the recorder while playing
(`DUNGEON_GENERATION_LOG_PATH=recording.jsonl`, see `benchmarks/README.md`) then:

```sh
python -m benchmarks.evaluation corpus build --from-recording recording.jsonl \
  --out benchmarks/corpus/live/<name> --id <name> --kind live \
  --description "..." --provenance player=<who> --provenance build=<version>
```

Live corpora live in the git-ignored `benchmarks/corpus/live/`, carry
`kind: "live"`, and are labelled as such in every report. They are not
reproducible from the repository and were shaped by whichever provider drove the
recorded game (see [Limitations](#limitations)).

Shadow evaluation (`director/docs/shadow-mode.md`, issue #13) is the other live
source: it sends a copy of live traffic to further providers. Its comparisons are
live evidence and belong in the live column; the protocol here does not import
them.

## Design of a run

- **Paired.** For each corpus request every selected provider/model is asked in
  immediate succession, so the comparison is between decisions made seconds
  apart under the same network and load conditions.
- **Interleaved and rotated.** Which provider goes first rotates with the
  request position, so none is always first (cold connection) or last.
- **Seeded order.** Each iteration visits the corpus in a shuffle seeded from the
  protocol (`order_seed`), so the order is reproducible but not the file order.
- **Concurrency 1.** Latency is measured one request at a time. Concurrency
  belongs to a different experiment (throughput) and would put our own queueing
  into the numbers.
- **One call per request, no retries.** The director and every adapter make
  exactly one provider call per request, with one timeout for all. A timeout is
  a failure, and it stays in the latency distribution: dropping it would flatter
  the provider. The summarizer's `retry_count` therefore stays `null` (not
  reported) rather than a guessed zero.
- **Director-native.** Requests go through `DirectorService.generate()` like the
  game's, so contract validation, timeout and error classification are identical
  for every provider.

## Warm-up and sample sizes

**Warm-up.** The first `warmup_requests` calls to every provider/model are
discarded from all statistics. They are kept in `warmup.jsonl`, and the first
warm-up call is reported separately as the **cold start** (a single sample, so
indicative only). Warm-up absorbs connection setup and first-call effects; it
cannot remove provider-side cold starts that recur after idle periods, which is
why cold start is shown rather than hidden.

**Sample sizes.** A percentile is *reliable* when at least
`min_tail_samples = 10` observations lie above it, i.e.

`n >= ceil(10 / (1 - p))`  →  p50: 20, p90: 100, p95: 200, p99: 1000.

The report flags every percentile that is not reliable for the sample it has.
Three tiers are defined in `benchmarks/evaluation/protocol.json`:

| Tier | Warm-up | Iterations | Samples (150 requests) | Reliable up to | Use |
|---|---|---|---|---|---|
| `smoke` | 3 | 1 | 150 | p90 | wiring check |
| `standard` | 10 | 3 | 450 | p95 | first live comparison |
| `full` | 20 | 7 | 1050 | p99 | the offline default; final live comparison |

Live cost scales with the tier: `full` on one hosted provider is 1070 billable
calls. Overrides (`--iterations`, `--warmup-requests`, `--timeout`) are allowed
and are recorded in the bundle as overrides.

## What is recorded

Every run writes an evidence bundle to `evaluation-output/<UTC stamp>/` (git-ignored):

| File | Content |
|---|---|
| `manifest.json` | completion marker; corpus identity; sha256 of each raw file; expected row counts |
| `protocol.json` | protocol id/version; effective parameters and overrides; selections; sample adequacy |
| `environment.json` | git commit and dirty flag; Python, OS, package versions; contract version; Godot version if present; vantage-point note; per provider/model: the non-secret configuration it ran with |
| `corpus/` | the exact corpus that was replayed (manifest + requests) |
| `results.json` | one row per measured call, in the `benchmarks.replay` result shape (plus `phase`, `position`, `sequence`) |
| `warmup.jsonl` | the discarded warm-up calls |
| `summary.txt/.json/.csv` | latency, reliability, tokens, distributions, from `benchmarks.summarize` |
| `metrics.json` | sample adequacy, cold start, derived cost, determinism, upstream identity |
| `behavior.json` | diversity, repetition, danger progression, agreement |
| `pricing.json`, `observations.json` | copies of the inputs the report used |
| `report.md` | all of it, with the sections kept apart |

**Provider, model, version and configuration.** Configuration is read from the
same `*Config.from_env` objects the providers run with, so what is recorded is
what was used: model id, API origin (scheme, host and port only), reasoning
effort, output-token cap, and so on. Credentials are never recorded: a field is
withheld when its config marks it `repr=False` or its name is a key/token/secret/
account id, and the record lists the *names* withheld plus whether a credential
was present. Hosted providers do not expose a version number, so the report also
lists the upstream identity the responses themselves carried (for example a
system fingerprint or a served-model id) and warns if it changed during the run.

## Metrics

Latency, reliability, token and distribution metrics are **not computed by this
protocol**. They come from `python -m benchmarks.summarize` (issue #15,
`make benchmark-summary`), which the report runs on the bundle's own
`results.json`. Its definitions apply unchanged; see `benchmarks/README.md`.

| Need (issue #18) | Source |
|---|---|
| p50/p95/p99 end-to-end latency (also p90, min/mean/max), tail first | summarizer, over every request including failures |
| success rate; timeout, schema-failure, other-error rates | summarizer |
| retries | summarizer `retries` (`null` unless a provider reports it; the director makes none) |
| tokens, room-type and danger distributions | summarizer |
| percentile reliability for the sample size | protocol (`metrics.json`) |
| cold start | protocol (first warm-up call) |
| normalized cost | protocol (below) |
| diversity, repetition, danger progression, agreement | protocol (below) |

The percentile convention is the summarizer's (linear interpolation between
closest ranks, rank `p * (n - 1)`).

**Normalized cost.** Adapters do not price their calls, so the cost the
summarizer shows is only what a provider reported. The protocol adds a derived
figure: measured input/output tokens times a price table you supply, expressed as
**USD per 1000 decisions** (all calls, failures included, because a bad answer
still spent tokens) and per 1000 *successful* decisions. No prices are built in:
an entry with any price must cite a `source` and `retrieved_on`, and a provider
without a sourced price reports `no_price`. Copy
`benchmarks/evaluation/pricing.template.json` to `pricing.local.json`
(git-ignored) and fill it in for your plan; free tiers, rate limits and
Cloudflare's non-token billing units are yours to state in `notes`. Prices change:
re-run `make eval-report` with an updated table; nothing needs re-measuring.

**Behaviour.** Descriptive only, computed from the returned plans:

- *diversity*: room-type entropy, distinct room types and signatures
  (`type|size|danger|exits`);
- *repetition*: consecutive same-type rate and longest streak per run, signature
  repeat rate;
- *danger progression*: mean danger in the first and last third of a run;
- *self-consistency*: same room type across iterations of one request;
- *agreement*: how often two provider/models decide alike on the same request.

Whether more variety or steeper danger is *better* is a design judgement; the
protocol does not score it. (Room-type and danger distributions themselves are in
the summary.)

## Measured, derived, descriptive, subjective

| Class | Meaning | Where |
|---|---|---|
| **measured** | read directly from a call: latency, outcome, error code, reported tokens and cost, the returned plan | report sections 1-2 |
| **derived** | computed from measured values plus an input you supplied (price table) | section 3 |
| **descriptive** | computed from the returned plans; describes, does not rank | section 4 |
| **subjective** | a person's impression of play | section 6, from `observations.json` only |

Subjective notes must say who, when and in what context, and be marked
`"kind": "subjective"`; the loader rejects anything else, so a measurement cannot
be smuggled in as an observation. The template is
`benchmarks/evaluation/observations.template.json`.

**Synthetic fixtures are not measurements.** The sample files shipped with the
summarizer (`benchmarks/fixtures/sample_replay_results.json` and `.jsonl`, issue
#15) contain **synthetic, illustrative values for tests and demos; they are not
provider measurements** and must not be quoted as benchmark results. The
evidence path cannot accidentally present them as such: `verify` requires results
to match the bundle's own corpus copy (every request id, iteration and seeded call
position exactly once), file digests, result headers, protocol selections,
environment records and the warm-up policy. A copied fixture or an edited raw file
therefore fails verification and no report is produced. This is an internal
consistency check, not cryptographic proof that a hosted call happened; establish
authenticity externally if the producer is not trusted. No `make eval-*` target
defaults to those fixtures.

## Running it

Offline, no credentials (the rules baseline only):

```sh
make setup
make eval-corpus-verify           # the fixed corpus matches its manifest
make eval-offline                 # run + verify + report; prints the bundle path
make eval-verify EVAL_BUNDLE=evaluation-output/<stamp>
make benchmark-summary BENCH_REPORT=evaluation-output/<stamp>/results.json   # issue #15
```

`make eval-offline EVAL_TIER=smoke` is a faster wiring check. Regenerate the corpus
inputs (needs Godot) with `make eval-corpus-rebuild`.

### Live comparison (optional, billable)

Separate from the offline path and refused unless confirmed:

```sh
export GROQ_API_KEY=...  CEREBRAS_API_KEY=...        # server-side credentials only
make eval-live EVAL_LIVE=1 EVAL_SELECT='groq cerebras' \
  EVAL_ARGS='--vantage-point "home fibre, Helsinki" --label first-comparison'
```

- Hosted providers need `EVAL_LIVE=1` (`--live`), and each must be configured.
- The planned call count is printed, and a cap (`--max-live-calls`, default 2000)
  is enforced before the first call.
- Pick models with `provider:model`, for example `groq:openai/gpt-oss-120b`.
  Several models of one provider are compared side by side.
- Use `--pricing benchmarks/evaluation/pricing.local.json` for a cost figure.
- Record impressions separately: `make eval-report EVAL_BUNDLE=... EVAL_ARGS='--observations notes.json'`.

To run a live corpus, pass its manifest with `EVAL_CORPUS=benchmarks/corpus/live/<name>/corpus.json`.

## Reproducibility

- **Inputs**: the corpus is stored, checksummed, and rebuildable from the game
  (`make eval-corpus-rebuild`).
- **Offline results**: the rules baseline is deterministic. The manifest holds
  the digest of its plans; `verify` recomputes it from the bundle and fails on a
  mismatch, so a change in the rules provider or the corpus is caught. Latency is
  timing, not content, and is not expected to be identical.
- **Order**: seeded, recorded in `protocol.json`.
- **Versions and configuration**: recorded in `environment.json` and repeated in
  the report.
- **Hosted providers**: reproducibility is *of the procedure*, not of the numbers.
  Latency, and sometimes output, vary with load and model changes; repeat runs at
  different times and compare bundles.

If the rules provider is changed on purpose, regenerate the golden digest with
`make eval-corpus-build EVAL_CORPUS_OUT=$PWD/benchmarks/corpus/replay-v1` and
review the diff.

## Limitations

State these next to any result.

- **Network location.** Live latency includes the path from this machine to the
  provider (`--vantage-point` records where that was). A result from a laptop on
  home Wi-Fi does not transfer to a phone on mobile data or to a server in a data
  centre, and the game will run on phones.
- **Provider load.** Latency varies with time of day and other tenants; one run is
  a sample of that moment. Repeat at different times before generalizing; free
  tiers and rate limits may distort results and are not modelled.
- **Model and version changes.** Hosted models can be updated without notice.
  Configuration and the upstream identity seen in responses are recorded, and a
  change mid-run is flagged, but a model is only pinned as far as the provider
  pins it.
- **Caching.** Iterations repeat identical requests, so provider-side caching may
  make later iterations faster than a fresh player would see. Compare iteration 1
  with the rest in `results.json` before trusting a large gap.
- **Cold starts.** Warm-up removes first-call cost from the statistics; the cold
  start is reported as one sample. Cold starts that recur after idle time (a
  player pausing) are not measured.
- **Replay is not play.** A replayed request cannot show how a provider's earlier
  answers would have changed the later game states. The fixed corpus was
  generated by the in-game rules baseline: danger is always 1 in its states,
  depth is fixed at 1, and states are shaped by that baseline's choices. Live
  corpora carry their own recording provider's influence.
- **Behaviour is described, not judged.** Diversity and agreement metrics say
  nothing about whether rooms are fun; that is what subjective observations, and
  playtesting, are for.
- **Cost is derived.** It depends on the price table you supply and on providers
  reporting tokens; without either, cost is unknown.
- **Retries.** The director never retries. A game-level fallback (rules baseline)
  after a failure is a separate mechanism, measured by the simulation harness
  (`benchmarks/simulation/README.md`), not by this protocol.
- **Scale.** 150 requests from 5 runs of one seed is a small, single-seed sample of
  dungeon states. Add corpora with other seeds before drawing conclusions about
  behaviour.
