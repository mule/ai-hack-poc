SELECT SpanAttributes['director.provider'] AS provider,
       SpanAttributes['director.model'] AS model,
       SpanAttributes['director.execution_mode'] AS mode,
       count() AS requests,
       countIf(SpanAttributes['director.status'] = 'success') / count() AS success_rate,
       countIf(SpanAttributes['director.status'] = 'timeout') / count() AS timeout_rate,
       countIf(SpanAttributes['director.status'] = 'schema_error') / count() AS schema_failure_rate,
       countIf(SpanAttributes['director.status'] = 'provider_error') / count() AS provider_failure_rate
FROM otel_traces
WHERE ServiceName = {service:String}
  AND ResourceAttributes['deployment.environment'] = {environment:String}
  AND Timestamp >= parseDateTime64BestEffort({start:String})
  AND Timestamp < parseDateTime64BestEffort({end:String})
  AND SpanName = 'director.generate'
GROUP BY provider, model, mode
ORDER BY provider, model, mode
