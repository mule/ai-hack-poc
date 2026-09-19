# Benchmarks

Status: **placeholder.** No tooling exists yet.

Purpose (epic #1): replay recorded dungeon states against multiple providers
through the director's canonical API, so providers are compared on identical
input.

Planned scope, delivered by later issues:

- Replay recorded decisions against several providers/models.
- Shadow evaluation of non-active providers without affecting gameplay.
- Reports with p50/p90/p95/p99 latency, reliability, schema-failure rate, and
  token/cost figures where providers report them.
- A simulation harness for driving many generations without a player.

Design constraints:

- Benchmarks talk to the **director**, never to providers directly, so results
  reflect the same adapters and `RoomPlan` validation that gameplay uses.
- Provider credentials come from the director's configuration (see the root
  `.env.example`); do not commit results that contain secrets.
