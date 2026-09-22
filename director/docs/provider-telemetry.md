# Provider spans and trace propagation

Each executed adapter call produces `director.provider.invoke` beneath its own
`director.generate` span. This applies to Groq, Cerebras, TypeSafe Jev, Cloudflare
Jev and local rules; a selection failure produces no provider span because no
adapter ran. The director performs no retries and does not invent attempt counts.

The child records provider identity, requested model and reported model, operation,
execution mode, outcome, schema validity and timeout origin. The exact adapter
duration is `director.provider.call_duration_ms`; the span also includes the small
amount of local response validation needed to classify schema failures. Keep
end-to-end comparison on the parent `director.generate` duration.

`gen_ai.usage.input_tokens`, `output_tokens`, `total_tokens` and `cost` are present
only when reported. Cost is USD from canonical provider usage, without an assumed
price table. Metadata uses a bounded allowlist and field types: Jev confidence and
score summaries, safe model/request identifiers and numeric timing fields. Raw
probability maps, prompts, features, responses, headers and exception text are
excluded. Credential-shaped strings are rejected even under allowed field names.

`POST /v1/generate` accepts standard W3C `traceparent`/`tracestate` context. A valid
incoming parent is preserved; malformed context is ignored. The response includes
the generated director span's `traceparent` header when tracing is available.
The canonical JSON response remains unchanged. Provider, shadow and replay
contexts are passed explicitly using task-local state, without attaching a
generation span as ambient current state.

The app registers the bounded comparison observer whenever shadow evaluation is
configured. Active and shadow generation/provider spans carry their common
`shadow_comparison_id`; comparison spans/logs reference the active trace even when
the shadow finishes later. Replay executions use `replay` mode and inherit
evaluation/dataset/case IDs, while nested shadow executions remain `shadow`.

Telemetry calls are isolated from canonical generation. Spans close on success,
schema/provider failures, timeout and cancellation; instrumentation failures do
not change the answer. Production batch processors handle export off the request
path. See [model comparisons](../../docs/model-comparison-telemetry.md) for metrics,
replay artifact lookup and bounded retention.
