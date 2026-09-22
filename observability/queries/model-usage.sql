SELECT SpanAttributes['director.provider'] AS provider,
       SpanAttributes['director.model'] AS model,
       SpanAttributes['director.execution_mode'] AS mode,
       count() AS requests,
       countIf(mapContains(SpanAttributes, 'gen_ai.usage.input_tokens')) AS input_coverage,
       countIf(mapContains(SpanAttributes, 'gen_ai.usage.output_tokens')) AS output_coverage,
       countIf(mapContains(SpanAttributes, 'gen_ai.usage.cost')) AS cost_coverage,
       sum(toUInt64OrNull(SpanAttributes['gen_ai.usage.input_tokens'])) AS input_tokens,
       sum(toUInt64OrNull(SpanAttributes['gen_ai.usage.output_tokens'])) AS output_tokens,
       sum(toFloat64OrNull(SpanAttributes['gen_ai.usage.cost'])) AS reported_cost_usd,
       avgIf(toFloat64OrNull(SpanAttributes['gen_ai.usage.cost']),
             SpanAttributes['director.status'] = 'success') AS mean_reported_cost_per_success_usd
FROM otel_traces
WHERE ServiceName = {service:String}
  AND ResourceAttributes['deployment.environment'] = {environment:String}
  AND Timestamp >= parseDateTime64BestEffort({start:String})
  AND Timestamp < parseDateTime64BestEffort({end:String})
  AND SpanName = 'director.generate'
GROUP BY provider, model, mode
ORDER BY provider, model, mode
