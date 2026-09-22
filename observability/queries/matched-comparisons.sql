SELECT SpanAttributes['active_provider'] AS active_provider,
       SpanAttributes['active_model'] AS active_model,
       SpanAttributes['shadow_provider'] AS shadow_provider,
       SpanAttributes['shadow_model'] AS shadow_model,
       count() AS pairs,
       countIf(SpanAttributes['success_mismatch'] = 'true') AS success_disagreements,
       countIf(SpanAttributes['room_type_mismatch'] = 'true') AS room_type_disagreements,
       countIf(SpanAttributes['schema_mismatch'] = 'true') AS schema_disagreements,
       countIf(SpanAttributes['latency_winner'] = 'active') AS active_faster,
       countIf(SpanAttributes['latency_winner'] = 'shadow') AS shadow_faster,
       countIf(SpanAttributes['cost_winner'] = 'unknown') AS missing_cost_pairs,
       countIf(SpanAttributes['latency_winner'] IN ('unknown', '')) AS missing_latency_pairs,
       countIf(SpanAttributes['success_mismatch'] IN ('unknown', '')) AS unknown_success_pairs,
       countIf(SpanAttributes['schema_mismatch'] IN ('unknown', '')) AS unknown_schema_pairs,
       countIf(SpanAttributes['room_type_mismatch'] IN ('unknown', '')) AS unknown_room_pairs
FROM otel_traces
WHERE ServiceName = {service:String}
  AND ResourceAttributes['deployment.environment'] = {environment:String}
  AND Timestamp >= parseDateTime64BestEffort({start:String})
  AND Timestamp < parseDateTime64BestEffort({end:String})
  AND SpanName = 'director.shadow.comparison'
GROUP BY active_provider, active_model, shadow_provider, shadow_model
ORDER BY active_provider, active_model, shadow_provider, shadow_model
